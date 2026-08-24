# -*- coding: utf-8 -*-
"""人脸事件源：向上层状态机输出「有人/没人、距离代理、微笑度、疲劳状态」。

三后端：
  - haar       ：cv2.CascadeClassifier 正脸级联 + 微笑级联（haarcascade_smile.xml），
                 纯 CPU，本机 Ubuntu 即可验证。疲劳字段恒为 None。
  - scrfd      ：板端 NPU 后端，rknnlite 加载 scrfd.rknn，输出人脸框 + 5 关键点；
                 微笑度用「嘴角关键点距离 / 双眼距离」归一化估算。
  - landmark106：板端 NPU 疲劳后端。retinaface.rknn 检测人脸框 → 松散裁剪 →
                 2d106det.rknn 推理 106 关键点 → fatigue.FatigueMonitor 出疲劳状态。
                 推理流程移植自参考工程 hand_capture_right 的
                 src/dms/dms_retinaface.c 与 src/dms/dms_face_landmark_106.c。

  import rknnlite 失败（本机无此库）或模型加载失败时优雅降级到 haar。

  ⚠ 重要：models/ 下的 .rknn 是参考工程按 RV1106 目标编译的，**不能直接
  在 RV1126B 上跑**。上板前必须用 rknn-toolkit2 >= 2.3 按
  target_platform='rv1126b' 重新转换，详见 docs/modules/ai_host-models.md。

poll() 返回 dict：
  {present: bool,        # 当前帧是否检测到人脸
   face_ratio: float,    # 最大人脸框面积 / 画面面积，作为距离代理（越大越近）
   smile: float,         # 微笑度 0~1（landmark106 后端暂为 0）
   fatigue: dict|None,   # FatigueMonitor.update() 的结果；非 landmark106 后端恒为 None
   ts: float}            # 采帧时间戳（time.time()）

采帧用 cv2.VideoCapture(device)，内部按 cap_interval（默认 0.5s）节流：
间隔内的重复 poll 直接返回上一次结果，避免空转吃 CPU。
设备号约定：板端 MIPI 摄像头 23，板端 USB 摄像头 52，本机笔记本摄像头一般 0。
"""

import time

import cv2

import fatigue as fatigue_mod

# haarcascade 模型目录：pip 版 opencv 在 cv2.data.haarcascades；
# Ubuntu apt 版（python3-opencv 4.2）没有 cv2.data，模型在系统目录
_CASCADE_DIR_CANDIDATES = [
    '/usr/share/opencv4/haarcascades/',
    '/usr/share/opencv/haarcascades/',
    '/usr/local/share/opencv4/haarcascades/',
]
if hasattr(cv2, 'data') and hasattr(cv2.data, 'haarcascades'):
    _CASCADE_DIR_CANDIDATES.insert(0, cv2.data.haarcascades)


def _cascade_dir():
    """找到第一个实际存在正脸级联文件的目录。"""
    import os
    for d in _CASCADE_DIR_CANDIDATES:
        if os.path.isfile(os.path.join(d, 'haarcascade_frontalface_default.xml')):
            return d
    raise RuntimeError('找不到 haarcascade 模型目录（试过: %s）' % _CASCADE_DIR_CANDIDATES)

# SCRFD 关键点顺序（InsightFace 约定）：
#   0 左眼  1 右眼  2 鼻尖  3 左嘴角  4 右嘴角
KPS_LEFT_EYE, KPS_RIGHT_EYE = 0, 1
KPS_MOUTH_L, KPS_MOUTH_R = 3, 4

# 嘴角距/眼距的经验区间：约 0.75 为中性表情，约 1.05 为明显微笑
# （板端实装后应按真实摄像头画面重新标定这两个常数）
SMILE_RATIO_LO = 0.75
SMILE_RATIO_HI = 1.05


class HaarBackend(object):
    """CPU 后端：正脸 Haar 级联 + 微笑级联。"""

    name = 'haar'

    def __init__(self):
        base = _cascade_dir()
        self.face_cascade = cv2.CascadeClassifier(base + 'haarcascade_frontalface_default.xml')
        self.smile_cascade = cv2.CascadeClassifier(base + 'haarcascade_smile.xml')
        if self.face_cascade.empty():
            raise RuntimeError('加载 haarcascade_frontalface_default.xml 失败')
        if self.smile_cascade.empty():
            raise RuntimeError('加载 haarcascade_smile.xml 失败')

    def detect(self, frame):
        """返回 (present, face_ratio, smile, fatigue)。任何异常都按「无人」处理，不抛给上层。"""
        h_frame, w_frame = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self.face_cascade.detectMultiScale(gray, 1.2, 5, minSize=(60, 60))
        if len(faces) == 0:
            return False, 0.0, 0.0, None
        # 多张脸取最大者（离镜头最近的人）
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        face_ratio = float(w * h) / float(w_frame * h_frame)
        # 微笑只可能在下半张脸，缩小搜索区域还能压低误检
        mouth_roi = gray[y + h // 2: y + h, x: x + w]
        smile = 0.0
        if mouth_roi.size > 0:
            smiles = self.smile_cascade.detectMultiScale(mouth_roi, 1.8, 20, minSize=(25, 25))
            smile = 1.0 if len(smiles) > 0 else 0.0
        return True, face_ratio, smile, None


class ScrfdBackend(object):
    """板端 NPU 后端：SCRFD 人脸检测（含 5 关键点），推理流程参考 reference/ai_facedet/scrfd/main.py。"""

    name = 'scrfd'

    def __init__(self, model_path='scrfd.rknn'):
        # 无 rknnlite 的环境（开发机）这里直接抛 ImportError，由上层降级
        import numpy as np
        from rknnlite.api import RKNNLite
        self.np = np
        self.inp_size = 640
        self.conf_threshold = 0.5
        self.nms_threshold = 0.5
        self.fmc = 3                      # 每个 stride 三组输出：score / bbox / kps
        self.feat_strides = [8, 16, 32]
        self.num_anchors = 2
        self.net = RKNNLite()
        if self.net.load_rknn(model_path) != 0:
            raise RuntimeError('加载 %s 失败' % model_path)
        if self.net.init_runtime() != 0:
            raise RuntimeError('rknnlite init_runtime 失败')

    def _resize_keep_ratio(self, src):
        """等比缩放到 640×640 并补黑边，返回 (图, 新高, 新宽, 上下补边, 左右补边)。"""
        np = self.np
        padh, padw, newh, neww = 0, 0, self.inp_size, self.inp_size
        if src.shape[0] != src.shape[1]:
            hw_scale = src.shape[0] / src.shape[1]
            if hw_scale > 1:
                newh, neww = self.inp_size, int(self.inp_size / hw_scale)
                img = cv2.resize(src, (neww, newh), interpolation=cv2.INTER_AREA)
                padw = int((self.inp_size - neww) * 0.5)
                img = cv2.copyMakeBorder(img, 0, 0, padw, self.inp_size - neww - padw,
                                         cv2.BORDER_CONSTANT, value=0)
            else:
                newh, neww = int(self.inp_size * hw_scale) + 1, self.inp_size
                img = cv2.resize(src, (neww, newh), interpolation=cv2.INTER_AREA)
                padh = int((self.inp_size - newh) * 0.5)
                img = cv2.copyMakeBorder(img, padh, self.inp_size - newh - padh, 0, 0,
                                         cv2.BORDER_CONSTANT, value=0)
        else:
            img = cv2.resize(src, (self.inp_size, self.inp_size), interpolation=cv2.INTER_AREA)
        return img, newh, neww, padh, padw

    @staticmethod
    def _distance2bbox(points, distance):
        """锚点中心 + 四边距离 → 框坐标。"""
        x1 = points[:, 0] - distance[:, 0]
        y1 = points[:, 1] - distance[:, 1]
        x2 = points[:, 0] + distance[:, 2]
        y2 = points[:, 1] + distance[:, 3]
        return x1, y1, x2, y2

    @staticmethod
    def _distance2kps(points, distance):
        """锚点中心 + 关键点偏移 → 关键点坐标。"""
        preds = []
        for i in range(0, distance.shape[1], 2):
            preds.append(points[:, 0] + distance[:, i])
            preds.append(points[:, 1] + distance[:, i + 1])
        return preds

    def detect(self, frame):
        """返回 (present, face_ratio, smile, fatigue=None)；取置信度最高的一张脸。"""
        np = self.np
        img, newh, neww, padh, padw = self._resize_keep_ratio(frame)
        blob = np.expand_dims(img, 0)
        outs = self.net.inference(inputs=[blob])
        outs = outs[::3] + outs[1::3] + outs[2::3]   # 按 stride 重排输出顺序

        scores_list, bboxes_list, kpss_list = [], [], []
        for idx, stride in enumerate(self.feat_strides):
            scores = outs[idx * self.fmc][0]
            bbox_preds = outs[idx * self.fmc + 1][0] * stride
            kps_preds = outs[idx * self.fmc + 2][0] * stride
            size = self.inp_size // stride
            anchors = np.stack(np.mgrid[:size, :size][::-1], axis=-1).astype(np.float32)
            anchors = (anchors * stride).reshape((-1, 2))
            if self.num_anchors > 1:
                anchors = np.stack([anchors] * self.num_anchors, axis=1).reshape((-1, 2))

            pos = np.where(scores >= self.conf_threshold)[0]
            if len(pos) == 0:
                continue
            x1, y1, x2, y2 = self._distance2bbox(anchors, bbox_preds)
            bboxes = np.stack([x1, y1, x2, y2], axis=-1)
            scores_list.append(scores[pos])
            bboxes_list.append(bboxes[pos])
            kpss = np.stack(self._distance2kps(anchors, kps_preds), axis=-1)
            kpss = kpss.reshape((kpss.shape[0], -1, 2))
            kpss_list.append(kpss[pos])

        if not scores_list:
            return False, 0.0, 0.0, None

        scores = np.hstack(scores_list).ravel()
        bboxes = np.vstack(bboxes_list)
        kpss = np.vstack(kpss_list)

        # 坐标从 640 输入空间映射回原图
        ratioh, ratiow = frame.shape[0] / newh, frame.shape[1] / neww
        bboxes[:, 0] = (bboxes[:, 0] - padw) * ratiow
        bboxes[:, 1] = (bboxes[:, 1] - padh) * ratioh
        bboxes[:, 2] = (bboxes[:, 2] - padw) * ratiow
        bboxes[:, 3] = (bboxes[:, 3] - padh) * ratioh
        kpss[:, :, 0] = (kpss[:, :, 0] - padw) * ratiow
        kpss[:, :, 1] = (kpss[:, :, 1] - padh) * ratioh

        # NMS 去重后取置信度最高的一张脸
        boxes_xywh = bboxes.copy()
        boxes_xywh[:, 2] -= boxes_xywh[:, 0]
        boxes_xywh[:, 3] -= boxes_xywh[:, 1]
        indices = cv2.dnn.NMSBoxes(boxes_xywh.tolist(), scores.tolist(),
                                   self.conf_threshold, self.nms_threshold)
        if len(indices) == 0:
            return False, 0.0, 0.0, None
        indices = np.array(indices).ravel()
        best = indices[int(np.argmax(scores[indices]))]

        x1, y1, x2, y2 = bboxes[best]
        face_ratio = float(max(0.0, (x2 - x1) * (y2 - y1))) / float(frame.shape[0] * frame.shape[1])

        # 微笑度估算：嘴角关键点距离 / 双眼距离，按经验区间归一化到 0~1
        kps = kpss[best]
        mouth_w = float(np.hypot(kps[KPS_MOUTH_L][0] - kps[KPS_MOUTH_R][0],
                                 kps[KPS_MOUTH_L][1] - kps[KPS_MOUTH_R][1]))
        eye_d = float(np.hypot(kps[KPS_LEFT_EYE][0] - kps[KPS_RIGHT_EYE][0],
                               kps[KPS_LEFT_EYE][1] - kps[KPS_RIGHT_EYE][1]))
        smile = 0.0
        if eye_d > 1e-6:
            r = mouth_w / eye_d
            smile = min(1.0, max(0.0, (r - SMILE_RATIO_LO) / (SMILE_RATIO_HI - SMILE_RATIO_LO)))
        return True, face_ratio, smile, None


class Landmark106Backend(object):
    """板端 NPU 疲劳后端：RetinaFace 人脸检测 + 2d106det 106 关键点 + FatigueMonitor。

    ⚠ models/ 下附带的 .rknn 是按 RV1106 编译的，不能直接在 RV1126B 上跑；
    上板前需按 RV1126B 重新转换模型（见 docs/modules/ai_host-models.md）。

    推理流程移植自参考工程 hand_capture_right：
      - RetinaFace：整图直接 stretch 缩放到模型输入（C 版未用 letterbox），
        prior 框解码 + NMS 取最高分人脸，归一化坐标按拉伸比映射回原图
        （对应 src/dms/dms_retinaface.c）。
      - 2d106det：以人脸框中心取边长 max(w,h)*1.5 的正方形松散裁剪，
        resize 到 192×192 喂模型；输出 [-1,1] → (p+1)*96 → 按裁剪框
        映射回原图坐标（对应 src/dms/dms_face_landmark_106.c 与
        tools/verify_landmark_rknn.py 的 postprocess）。
    """

    name = 'landmark106'

    # RetinaFace 超参数（同 dms_retinaface.c）
    SCORE_THRESHOLD = 0.5
    NMS_THRESHOLD = 0.2
    VARIANCES = (0.1, 0.2)
    MIN_SIZES = ((16, 32), (64, 128), (256, 512))
    STEPS = (8, 16, 32)
    # 松散裁剪倍数（同 dms_face_landmark_106.c LANDMARK_106_CROP_SCALE）
    CROP_SCALE = 1.5
    LM_INPUT = 192

    def __init__(self, retinaface_path='./models/retinaface.rknn',
                 landmark_path='./models/2d106det.rknn', det_input_size=320,
                 fatigue_monitor=None):
        # 无 rknnlite 的环境（开发机）这里直接抛 ImportError，由上层降级
        import numpy as np
        from rknnlite.api import RKNNLite
        self.np = np
        self.det_input_size = det_input_size   # RetinaFace 模型输入边长（正方形）

        self.net_det = RKNNLite()
        if self.net_det.load_rknn(retinaface_path) != 0:
            raise RuntimeError('加载 %s 失败' % retinaface_path)
        if self.net_det.init_runtime() != 0:
            raise RuntimeError('retinaface rknnlite init_runtime 失败')
        self.net_lm = RKNNLite()
        if self.net_lm.load_rknn(landmark_path) != 0:
            raise RuntimeError('加载 %s 失败' % landmark_path)
        if self.net_lm.init_runtime() != 0:
            raise RuntimeError('2d106det rknnlite init_runtime 失败')

        self.priors = self._generate_priors(det_input_size)   # (N,4) 归一化 cx,cy,w,h
        self.fatigue = fatigue_monitor if fatigue_monitor is not None \
            else fatigue_mod.FatigueMonitor()

    def _generate_priors(self, model_size):
        """生成 RetinaFace prior boxes，同 dms_retinaface.c generate_priors()。"""
        np = self.np
        priors = []
        for k in range(3):
            fm = model_size // self.STEPS[k]
            for i in range(fm):
                for j in range(fm):
                    for s in range(2):
                        priors.append(((j + 0.5) * self.STEPS[k] / model_size,
                                       (i + 0.5) * self.STEPS[k] / model_size,
                                       self.MIN_SIZES[k][s] / model_size,
                                       self.MIN_SIZES[k][s] / model_size))
        return np.array(priors, dtype=np.float32)

    def _detect_face(self, frame):
        """RetinaFace 检测，返回原图坐标 (x, y, w, h) 或 None。"""
        np = self.np
        size = self.det_input_size
        # C 版是整图 stretch 缩放（RGB->BGR）；cv2 读进来的帧本身就是 BGR，直接喂
        img = cv2.resize(frame, (size, size), interpolation=cv2.INTER_LINEAR)
        outs = self.net_det.inference(inputs=[np.expand_dims(img, 0)])
        num_priors = self.priors.shape[0]

        # 按元素数识别输出：loc=N*4，score=N*2，landms=N*10（同 C 版 identify_outputs）
        loc = score = None
        for o in outs:
            flat = np.asarray(o, dtype=np.float32).ravel()
            if flat.size == num_priors * 4 and loc is None:
                loc = flat.reshape(-1, 4)
            elif flat.size == num_priors * 2 and score is None:
                score = flat.reshape(-1, 2)
        if loc is None or score is None:
            raise RuntimeError('retinaface 输出与 det_input_size=%d 不匹配，'
                               '请按模型实际输入尺寸调整' % size)

        face_score = score[:, 1]
        pos = np.where(face_score > self.SCORE_THRESHOLD)[0]
        if len(pos) == 0:
            return None
        p = self.priors[pos]
        v0, v1 = self.VARIANCES
        cx = loc[pos, 0] * v0 * p[:, 2] + p[:, 0]
        cy = loc[pos, 1] * v0 * p[:, 3] + p[:, 1]
        w = np.exp(loc[pos, 2] * v1) * p[:, 2]
        h = np.exp(loc[pos, 3] * v1) * p[:, 3]
        x1 = np.clip(cx - w * 0.5, 0.0, 1.0)
        y1 = np.clip(cy - h * 0.5, 0.0, 1.0)
        x2 = np.clip(cx + w * 0.5, 0.0, 1.0)
        y2 = np.clip(cy + h * 0.5, 0.0, 1.0)

        boxes_xywh = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1)
        indices = cv2.dnn.NMSBoxes(boxes_xywh.tolist(), face_score[pos].tolist(),
                                   self.SCORE_THRESHOLD, self.NMS_THRESHOLD)
        if len(indices) == 0:
            return None
        indices = np.array(indices).ravel()
        best = indices[int(np.argmax(face_score[pos][indices]))]

        # 归一化坐标按拉伸比映射回原图（同 C 版 map_to_original）
        fh, fw = frame.shape[:2]
        bx1, by1 = x1[best] * fw, y1[best] * fh
        bx2, by2 = x2[best] * fw, y2[best] * fh
        return float(bx1), float(by1), float(bx2 - bx1), float(by2 - by1)

    def _landmark106(self, frame, box):
        """对检测到的人脸做 106 关键点推理，返回 [(x,y), ...]（原图坐标）。"""
        np = self.np
        fh, fw = frame.shape[:2]
        x, y, w, h = box
        # 正方形松散裁剪：边长 max(w,h)*CROP_SCALE，中心对齐，限制在图内
        cx, cy = x + w * 0.5, y + h * 0.5
        side = max(w, h) * self.CROP_SCALE
        x0 = int(min(max(round(cx - side * 0.5), 0), fw - 1))
        y0 = int(min(max(round(cy - side * 0.5), 0), fh - 1))
        x1 = int(min(max(round(cx + side * 0.5), 0), fw - 1))
        y1 = int(min(max(round(cy + side * 0.5), 0), fh - 1))
        crop_w, crop_h = x1 - x0 + 1, y1 - y0 + 1
        if crop_w < 8 or crop_h < 8:
            return None
        crop = frame[y0:y1 + 1, x0:x1 + 1]
        inp = cv2.resize(crop, (self.LM_INPUT, self.LM_INPUT),
                         interpolation=cv2.INTER_LINEAR)
        inp = cv2.cvtColor(inp, cv2.COLOR_BGR2RGB)   # 2d106det 训练用 RGB
        out = self.net_lm.inference(inputs=[np.expand_dims(inp, 0)])[0]

        # 后处理：[-1,1] → 192 crop 像素 → 原图坐标（同 verify_landmark_rknn.py）
        pred = np.asarray(out, dtype=np.float32).reshape(-1, 2)
        if pred.shape[0] != 106:
            raise RuntimeError('2d106det 输出点数异常: %s' % (pred.shape,))
        pred = (pred + 1.0) * (self.LM_INPUT // 2)
        pts = [(float(x0 + pred[i, 0] * crop_w / self.LM_INPUT),
                float(y0 + pred[i, 1] * crop_h / self.LM_INPUT))
               for i in range(106)]
        return pts

    def detect(self, frame):
        """返回 (present, face_ratio, smile, fatigue)。smile 暂为 0（关键点微笑估算未做）。"""
        box = self._detect_face(frame)
        if box is None:
            self.fatigue.update(None, time.time())
            return False, 0.0, 0.0, None
        x, y, w, h = box
        face_ratio = max(0.0, w * h) / float(frame.shape[0] * frame.shape[1])
        fatigue = None
        pts = self._landmark106(frame, box)
        if pts is not None:
            fatigue = self.fatigue.update(pts, time.time())
        return True, face_ratio, 0.0, fatigue


class FaceEventSource(object):
    """人脸事件源：节流采帧 + 多后端检测。"""

    def __init__(self, backend='auto', device=23, model_path='scrfd.rknn',
                 cap_interval=0.5, retinaface_path='./models/retinaface.rknn',
                 landmark_path='./models/2d106det.rknn'):
        self.device = device
        self.cap_interval = cap_interval
        self.backend = self._init_backend(backend, model_path,
                                          retinaface_path, landmark_path)
        self.cap = None                 # 摄像头懒打开，第一次 poll 时才初始化
        self._opened = False
        self._last_capture = 0.0
        self._last_result = {'present': False, 'face_ratio': 0.0, 'smile': 0.0,
                             'fatigue': None, 'ts': 0.0}

    def _init_backend(self, backend, model_path, retinaface_path, landmark_path):
        """auto 依次尝试 landmark106 → scrfd → haar，任何失败（含无 rknnlite）都降级。"""
        if backend in ('auto', 'landmark106'):
            try:
                return Landmark106Backend(retinaface_path, landmark_path)
            except Exception as e:
                # 任务约定：landmark106 不可用时打印一次提示并降级，不暴露异常
                print('[face_events] landmark106 后端不可用（%s），降级' % e)
                if backend == 'landmark106':
                    return HaarBackend()
        if backend in ('auto', 'scrfd'):
            try:
                return ScrfdBackend(model_path)
            except Exception as e:
                if backend == 'scrfd':
                    raise   # 显式指定 scrfd 时失败要暴露出来
                print('[face_events] scrfd 后端不可用（%s），降级到 haar' % e)
        if backend in ('auto', 'haar'):
            return HaarBackend()
        raise ValueError('未知后端: %s' % backend)

    def open(self):
        """打开摄像头，成功返回 True。"""
        self.cap = cv2.VideoCapture(self.device)
        self._opened = bool(self.cap.isOpened())
        if not self._opened:
            self.cap.release()
            self.cap = None
        return self._opened

    def poll(self):
        """采一帧并返回事件 dict；cap_interval 内的重复调用直接返回上次结果。"""
        now = time.time()
        if self._last_capture > 0 and (now - self._last_capture) < self.cap_interval:
            return dict(self._last_result)
        if self.cap is None:
            if not self.open():
                raise RuntimeError('无法打开摄像头设备 /dev/video%d' % self.device)
        ok, frame = self.cap.read()
        self._last_capture = now
        if not ok or frame is None:
            # 单帧读取失败不致命：沿用上次结果，等下一个周期再试
            result = dict(self._last_result)
            result['ts'] = now
            return result
        present, face_ratio, smile, fatigue = self.backend.detect(frame)
        self._last_result = {'present': bool(present),
                             'face_ratio': round(float(face_ratio), 4),
                             'smile': round(float(smile), 3),
                             'fatigue': fatigue,
                             'ts': now}
        return dict(self._last_result)

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None
