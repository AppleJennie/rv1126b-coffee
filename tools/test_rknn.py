#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加载 .rknn 推理一张图，输出 detections（JSON）。

运行平台要求：
  - --backend simulator：rknn-toolkit2（x86_64 PC 完整支持；aarch64 实测可装，
    需 opencv-python==4.10.0.84 规避 SVE SIGILL，见 reports/scrfd_rknn_validation.md）。
    ⚠ 实测限制：模拟器**不能**直接推理 load_rknn 的成品 .rknn（报
    "not support inference on the simulator, please set 'target' first"）；
    PC 侧验证转换数值请用 tools/compare_onnx_rknn.py --from-source，
    成品 .rknn 的最终验证在板端用 --backend lite。
  - --backend lite：rknn-toolkit-lite2，仅 RV1126B 板端（真 NPU 推理）。
  - --help 在任何平台可跑（numpy/cv2/rknn 全部延迟导入）。

x86_64 PC 上的完整命令示例（SCRFD 模型，PC 模拟器）：
    pip install rknn-toolkit2==2.3.2 "numpy<2" opencv-python-headless
    python3 tools/test_rknn.py --model models/scrfd_rv1126b.rknn \
        --image rknn/dataset/test_face.jpg --task scrfd --backend simulator

板端（RV1126B + rknn-toolkit-lite2 + OpenCV 4.9）：
    python3 tools/test_rknn.py --model models/scrfd.rknn \
        --image /tmp/face.jpg --task scrfd --backend lite

所需 pip 包及版本：
    PC 模拟器：rknn-toolkit2==2.3.2, numpy<2, opencv-python-headless>=4.5
    板端推理：rknn-toolkit-lite2（版本与固件 librknnrt 一致，2.3.x），numpy, opencv
"""

import argparse
import json
import platform
import sys


def parse_args():
    p = argparse.ArgumentParser(
        description='加载 .rknn 推理一张图，输出 detections（JSON）。',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--model', required=True, help='.rknn 模型路径')
    p.add_argument('--image', required=True, help='输入图片路径')
    p.add_argument('--backend', choices=['auto', 'simulator', 'lite'], default='auto',
                   help='simulator=PC 端 rknn-toolkit2 模拟器；lite=板端 rknn-toolkit-lite2')
    p.add_argument('--task', choices=['scrfd', 'raw'], default='scrfd',
                   help='scrfd=按 SCRFD 9 输出解码 bbox/kps；raw=只打印输出 tensor 元信息')
    p.add_argument('--input-size', type=int, default=640, help='模型输入边长（正方形）')
    p.add_argument('--conf', type=float, default=0.5, help='置信度阈值')
    p.add_argument('--nms', type=float, default=0.5, help='NMS IoU 阈值')
    p.add_argument('--target', default=None,
                   help='simulator 连板推理时的目标（如 rv1126b），不填=本机模拟')
    return p.parse_args()


# ---------------- 后端加载 ----------------

def _load_simulator(model_path, target):
    """rknn-toolkit2 模拟器（x86_64 PC 主用路径）。"""
    from rknn.api import RKNN  # noqa: N813
    net = RKNN(verbose=False)
    if net.load_rknn(model_path) != 0:
        raise RuntimeError('load_rknn 失败: %s' % model_path)
    # target=None → 本机模拟器推理；指定 target 则走连板（adb）模式
    if net.init_runtime(target=target) != 0:
        raise RuntimeError('init_runtime 失败（target=%s）' % target)
    return net


def _load_lite(model_path):
    """rknn-toolkit-lite2（仅板端 RV1126B）。"""
    from rknnlite.api import RKNNLite
    net = RKNNLite()
    if net.load_rknn(model_path) != 0:
        raise RuntimeError('load_rknn 失败: %s' % model_path)
    if net.init_runtime() != 0:
        raise RuntimeError('rknnlite init_runtime 失败')
    return net


def load_backend(model_path, backend, target):
    if backend in ('auto', 'simulator'):
        try:
            return _load_simulator(model_path, target), 'simulator'
        except ImportError:
            if backend == 'simulator':
                raise
        except RuntimeError:
            if backend == 'simulator':
                raise
    if backend in ('auto', 'lite'):
        return _load_lite(model_path), 'lite'
    raise RuntimeError('无可用后端（%s）' % backend)


# ---------------- SCRFD 后处理（与 reference/ai_facedet/scrfd/main.py 一致） ----------------

def scrfd_decode(outs, np, input_size, conf_th, nms_th, orig_shape,
                 pad=(0, 0), new_shape=None):
    """SCRFD 9 输出 → [{score, bbox[x1,y1,x2,y2], kps[5][2]}, ...]（原图坐标）。

    outs: rknn 推理返回的 9 个 tensor（顺序任意，先按 [::3]/[1::3]/[2::3] 重排）。
    """
    outs = outs[::3] + outs[1::3] + outs[2::3]
    fmc, strides, num_anchors = 3, [8, 16, 32], 2
    scores_list, bboxes_list, kpss_list = [], [], []
    for idx, stride in enumerate(strides):
        # 兼容两种 batch 约定：rknn 输出 [1,N,C]，onnxruntime 输出 [N,C]
        def _nb(t):
            return t[0] if t.ndim == 3 else t
        scores = _nb(outs[idx * fmc])
        bbox_preds = _nb(outs[idx * fmc + 1]) * stride
        kps_preds = _nb(outs[idx * fmc + 2]) * stride
        size = input_size // stride
        anchors = np.stack(np.mgrid[:size, :size][::-1], axis=-1).astype(np.float32)
        anchors = (anchors * stride).reshape((-1, 2))
        if num_anchors > 1:
            anchors = np.stack([anchors] * num_anchors, axis=1).reshape((-1, 2))
        pos = np.where(scores >= conf_th)[0]
        if len(pos) == 0:
            continue
        b = bbox_preds[pos]
        a = anchors[pos]
        # distance2bbox：锚点中心 ± 四边距离
        x1, y1 = a[:, 0] - b[:, 0], a[:, 1] - b[:, 1]
        x2, y2 = a[:, 0] + b[:, 2], a[:, 1] + b[:, 3]
        bboxes_list.append(np.stack([x1, y1, x2, y2], axis=-1))
        scores_list.append(scores[pos])
        # distance2kps：锚点中心 + 偏移
        kp = kps_preds[pos]
        pts = []
        for i in range(0, kp.shape[1], 2):
            pts.append(a[:, 0] + kp[:, i])
            pts.append(a[:, 1] + kp[:, i + 1])
        kpss_list.append(np.stack(pts, axis=-1).reshape(-1, 5, 2))
    if not scores_list:
        return []
    scores = np.hstack(scores_list).ravel()
    bboxes = np.vstack(bboxes_list)
    kpss = np.vstack(kpss_list)
    # letterbox 坐标映射回原图
    if new_shape is not None:
        newh, neww = new_shape
        padh, padw = pad
        ratioh, ratiow = orig_shape[0] / newh, orig_shape[1] / neww
        bboxes[:, [0, 2]] = (bboxes[:, [0, 2]] - padw) * ratiow
        bboxes[:, [1, 3]] = (bboxes[:, [1, 3]] - padh) * ratioh
        kpss[:, :, 0] = (kpss[:, :, 0] - padw) * ratiow
        kpss[:, :, 1] = (kpss[:, :, 1] - padh) * ratioh
    # NMS（xyxy → xywh）
    boxes = bboxes.copy()
    boxes[:, 2] -= boxes[:, 0]
    boxes[:, 3] -= boxes[:, 1]
    keep = _nms(boxes, scores, nms_th, np)
    return [{'score': round(float(scores[i]), 4),
             'bbox': [round(float(v), 1) for v in bboxes[i]],
             'kps': [[round(float(x), 1), round(float(y), 1)] for x, y in kpss[i]]}
            for i in keep]


def _nms(boxes_xywh, scores, nms_th, np):
    """纯 numpy NMS（优先用 cv2.dnn.NMSBoxes，无 cv2 时兜底）。"""
    try:
        import cv2
        idx = cv2.dnn.NMSBoxes(boxes_xywh.tolist(), scores.tolist(), 0.0, nms_th)
        return np.array(idx).ravel() if len(idx) else []
    except ImportError:
        pass
    x1, y1 = boxes_xywh[:, 0], boxes_xywh[:, 1]
    x2, y2 = x1 + boxes_xywh[:, 2], y1 + boxes_xywh[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        ix1 = np.maximum(x1[i], x1[order[1:]])
        iy1 = np.maximum(y1[i], y1[order[1:]])
        ix2 = np.minimum(x2[i], x2[order[1:]])
        iy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, ix2 - ix1) * np.maximum(0, iy2 - iy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= nms_th]
    return keep


def _preprocess(image_path, input_size):
    """letterbox 到 input_size×input_size，返回 (blob, 原图shape, pad, new_shape)。"""
    import cv2
    import numpy as np
    src = cv2.imread(image_path)
    if src is None:
        raise RuntimeError('读图失败: %s' % image_path)
    h, w = src.shape[:2]
    padh = padw = 0
    newh, neww = input_size, input_size
    if h != w:  # 等比缩放补黑边（同 scrfd/main.py keep_ratio 逻辑）
        scale = h / w
        if scale > 1:
            newh, neww = input_size, int(input_size / scale)
            img = cv2.resize(src, (neww, newh), interpolation=cv2.INTER_AREA)
            padw = int((input_size - neww) * 0.5)
            img = cv2.copyMakeBorder(img, 0, 0, padw, input_size - neww - padw,
                                     cv2.BORDER_CONSTANT, value=0)
        else:
            newh, neww = int(input_size * scale) + 1, input_size
            img = cv2.resize(src, (neww, newh), interpolation=cv2.INTER_AREA)
            padh = int((input_size - newh) * 0.5)
            img = cv2.copyMakeBorder(img, padh, input_size - newh - padh, 0, 0,
                                     cv2.BORDER_CONSTANT, value=0)
    else:
        img = cv2.resize(src, (input_size, input_size), interpolation=cv2.INTER_AREA)
    return np.expand_dims(img, 0), (h, w), (padh, padw), (newh, neww)


def main():
    args = parse_args()
    print('[test_rknn] 平台: %s, python %s' % (platform.machine(), platform.python_version()))
    try:
        import numpy as np  # noqa: F401
    except ImportError:
        print('[test_rknn] ✗ 缺 numpy：pip install "numpy<2"')
        return 2
    try:
        net, used = load_backend(args.model, args.backend, args.target)
    except ImportError as e:
        print('[test_rknn] ✗ 后端库不可用: %s' % e)
        print('  PC 模拟器: pip install rknn-toolkit2==2.3.2（x86_64 完整支持）')
        print('  板端: pip install rknn-toolkit-lite2（仅 RV1126B）')
        print('  aarch64 实测结论见 reports/scrfd_rknn_validation.md')
        return 2
    except RuntimeError as e:
        print('[test_rknn] ✗ %s' % e)
        return 1
    print('[test_rknn] 后端: %s' % used)

    blob, orig_shape, pad, new_shape = _preprocess(args.image, args.input_size)
    outs = net.inference(inputs=[blob])
    net.release()

    import numpy as np
    if args.task == 'raw':
        print(json.dumps([{'idx': i, 'shape': list(o.shape), 'dtype': str(o.dtype),
                           'min': float(np.min(o)), 'max': float(np.max(o))}
                          for i, o in enumerate(outs)], indent=2, ensure_ascii=False))
        return 0
    dets = scrfd_decode(outs, np, args.input_size, args.conf, args.nms,
                        orig_shape, pad, new_shape)
    print(json.dumps({'model': args.model, 'image': args.image, 'backend': used,
                      'num_faces': len(dets), 'detections': dets},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
