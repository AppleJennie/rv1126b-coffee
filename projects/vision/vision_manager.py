#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VisionManager —— 统一视觉管理（TASK 17，视觉层唯一入口）。

上层（kiosk_server 等）不直接碰任何视觉模型，统一走本模块：
  输入：帧源 —— MockFrameSource（合成帧，测试/演示默认）或
        Cv2FrameSource（真摄像头，仅可选演示，测试不得依赖）
  能力：face detection（Haar，级联路径发现逻辑复用 ai_host/face_events.py）、
        fatigue（TASK 15 时间窗状态机）、expression（TASK 16 工厂选后端）、
        cup detection（TASK 18 ROI + 背景差分）
  输出：统一事件 PERSON_PRESENT / PERSON_LEFT / TIRED / HAPPY /
        CUP_PRESENT / CUP_REMOVED（回调订阅 + 队列拉取两种方式）

事件去抖设计：
  - 出现类事件（PERSON_PRESENT / CUP_PRESENT）边沿触发：持续在场不重复发
  - 消失类事件（PERSON_LEFT / CUP_REMOVED）需目标持续消失超过
    person_gone_s（默认 3s）/ cup_gone_s（默认 2s）才发，防单帧漏检抖动
  - TIRED：疲劳状态机进入 possibly_tired 的边沿发一次（恢复不发事件）
  - HAPPY：表情进入 happy 的边沿发一次；人离场后各边沿状态复位
  - 每个能力可独立开关：config['capabilities']

演示（合成帧，脚本化场景打印事件流）：
  python3 projects/vision/vision_manager.py --demo-mock
真摄像头可选演示（/dev/videoN）：
  python3 projects/vision/vision_manager.py --demo-real --device 0

隐私红线：默认不保存任何人脸照片/原始视频；合成帧只在内存中生成。
"""

import argparse
import os
import sys
import time
from collections import deque

import cv2
import numpy as np

# 同目录模块导入兜底（本仓库惯例，同 hardware/vision_cup.py）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fatigue_detector import (  # noqa: E402
    FatigueDetector, FatigueWindowSM, HeuristicEyeMouthAnalyzer, POSSIBLY_TIRED)
from expression import make_expression_backend, MockExpression, find_cascade  # noqa: E402
from cup_presence import CupPresenceDetector  # noqa: E402

# ---- 统一事件类型 ----
PERSON_PRESENT = 'PERSON_PRESENT'
PERSON_LEFT = 'PERSON_LEFT'
TIRED = 'TIRED'
HAPPY = 'HAPPY'
CUP_PRESENT = 'CUP_PRESENT'
CUP_REMOVED = 'CUP_REMOVED'
ALL_EVENTS = (PERSON_PRESENT, PERSON_LEFT, TIRED, HAPPY, CUP_PRESENT, CUP_REMOVED)

# ---- 合成 mock 帧的几何常量（测试与 demo 共用，保证确定性）----
MOCK_W, MOCK_H = 320, 240
MOCK_FACE_BOX = (100, 40, 120, 140)        # 人脸矩形 (x, y, w, h)
MOCK_CUP_CENTER = (240, 190)               # 杯圆心
MOCK_CUP_R = 25                            # 杯半径
MOCK_CUP_ROI = (205, 150, 115, 90)         # 出餐位 ROI（与人脸框几乎不重叠）


def render_mock_frame(person=False, eyes='open', mouth='normal', cup=False):
    """渲染一帧合成图：深灰台面背景；人脸 = 亮灰矩形
    （睁眼 = 白眼点 / 闭眼 = 暗睑线；哈欠 = 嘴部大暗椭圆）；杯 = 亮圆。

    灰度设计与 fatigue_detector.HeuristicEyeMouthAnalyzer 的阈值配套：
    睁眼点使眼 ROI 均值高于脸均值，闭眼线使其低于脸均值的 97%；
    哈欠椭圆使嘴 ROI 暗像素占比约 0.7（阈值 0.30）。
    纯合成数据，不涉及任何真实人脸照片。
    """
    img = np.full((MOCK_H, MOCK_W, 3), 40, np.uint8)   # 深灰背景
    if person:
        x, y, w, h = MOCK_FACE_BOX
        cv2.rectangle(img, (x, y), (x + w, y + h), (180, 180, 180), -1)
        ey = y + int(0.35 * h)                          # 眼高
        elx, erx = x + int(0.30 * w), x + int(0.70 * w)
        if eyes == 'closed':
            cv2.line(img, (elx - 12, ey), (elx + 12, ey), (20, 20, 20), 5)
            cv2.line(img, (erx - 12, ey), (erx + 12, ey), (20, 20, 20), 5)
        else:
            cv2.circle(img, (elx, ey), 9, (255, 255, 255), -1)
            cv2.circle(img, (erx, ey), 9, (255, 255, 255), -1)
        mx, my = x + w // 2, y + int(0.80 * h)          # 嘴位置
        if mouth == 'yawn':
            cv2.ellipse(img, (mx, my), (16, 20), 0, 0, 360, (20, 20, 20), -1)
        else:
            cv2.line(img, (mx - 14, my), (mx + 14, my), (60, 60, 60), 2)
    if cup:
        cv2.circle(img, MOCK_CUP_CENTER, MOCK_CUP_R, (230, 230, 230), -1)
    return img


class MockFrameSource(object):
    """脚本化合成帧源（测试/演示用，不碰摄像头）。

    script: 步骤列表，每步 dict：
        dur        持续秒数（默认 1.0）
        person     是否有人（默认 False）
        eyes       'open'/'closed'（默认 'open'）
        mouth      'normal'/'yawn'（默认 'normal'）
        expression 'neutral'/'happy'（默认 'neutral'，供 Mock 表情后端）
        cup        出餐位是否有杯（默认 False）
    read() 返回 (frame, meta)；meta 带虚拟时间戳 ts（从 0 按 1/fps 递增）
    与归一化真值字段，供 mock 检测器使用。脚本播完返回 (None, None)。
    """

    name = 'mock'

    def __init__(self, script, fps=5):
        self.fps = fps
        self._frames = []
        t = 0.0
        for step in script:
            n = max(1, int(round(step.get('dur', 1.0) * fps)))
            for _ in range(n):
                meta = {'person': bool(step.get('person', False)),
                        'eyes': step.get('eyes', 'open'),
                        'mouth': step.get('mouth', 'normal'),
                        'expression': step.get('expression', 'neutral'),
                        'cup': bool(step.get('cup', False)),
                        'ts': t}
                self._frames.append(meta)
                t += 1.0 / fps
        self._i = 0

    def read(self):
        if self._i >= len(self._frames):
            return None, None
        meta = self._frames[self._i]
        self._i += 1
        frame = render_mock_frame(person=meta['person'], eyes=meta['eyes'],
                                  mouth=meta['mouth'], cup=meta['cup'])
        return frame, dict(meta)


class Cv2FrameSource(object):
    """真摄像头帧源（仅 --demo-real 可选演示；测试一律用 MockFrameSource）。"""

    name = 'cv2'

    def __init__(self, device=0, max_frames=None):
        self.device = device
        self.max_frames = max_frames
        self._n = 0
        self._cap = None

    def read(self):
        if self.max_frames is not None and self._n >= self.max_frames:
            return None, None
        if self._cap is None:
            self._cap = cv2.VideoCapture(self.device)
            if not self._cap.isOpened():
                self._cap.release()
                self._cap = None
                raise RuntimeError('无法打开摄像头 /dev/video%d' % self.device)
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None, None
        self._n += 1
        return frame, {'ts': time.time()}

    def close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class _PresenceDebouncer(object):
    """有/无去抖器：出现边沿立即报 'arrived'；
    消失需持续超过 gone_timeout 秒才报 'departed'（防单帧漏检抖动）。"""

    def __init__(self, gone_timeout):
        self.gone_timeout = gone_timeout
        self.present = False
        self._last_seen = None

    def update(self, present, ts):
        if present:
            self._last_seen = ts
            if not self.present:
                self.present = True
                return 'arrived'
            return None
        # 严格「超过」gone_timeout 才报（+1e-9 吸收时间戳浮点误差，防边界提前触发）
        if (self.present and self._last_seen is not None
                and ts - self._last_seen > self.gone_timeout + 1e-9):
            self.present = False
            return 'departed'
        return None


def _merge_cfg(default, override):
    """递归合并配置 dict（override 优先）。"""
    out = dict(default)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_cfg(out[k], v)
        else:
            out[k] = v
    return out


DEFAULT_CONFIG = {
    # 每个能力独立开关
    'capabilities': {'face': True, 'fatigue': True, 'expression': True, 'cup': True},
    'person_gone_s': 3.0,          # 人离开超过 N 秒才发 PERSON_LEFT
    'cup_gone_s': 2.0,             # 杯消失超过 N 秒才发 CUP_REMOVED
    'face_backend': 'mock',        # 'mock'（meta 真值）| 'haar'
    'expression_backend': 'mock',  # 'mock' | 'cpu' | 'rknn'（TASK 16 工厂）
    'expression_params': {},       # 透传给表情工厂（如 cpu 的 cascade_path）
    'fatigue': {},                 # 透传 FatigueWindowSM 参数（窗口/阈值）
    'cup': {},                     # 透传 CupPresenceDetector 参数（roi/min_area...）
    'cup_background': None,        # None=不自动采背景 | 'first_frame'=首帧作背景
}


class VisionManager(object):
    """统一视觉管理器：帧源进，事件出。

    事件投递两种方式可同时用：
      on_event(cb) 订阅回调；poll_events() 拉取并清空内部队列。
    事件格式：{'type': 六类之一, 'ts': 帧时间戳, 'detail': {...}}。
    """

    def __init__(self, source, config=None, expression_backend=None):
        self.cfg = _merge_cfg(DEFAULT_CONFIG, config)
        self.source = source
        self.caps = self.cfg['capabilities']

        # 人脸检测后端
        self.face_backend = self.cfg['face_backend']
        self._face_cascade = None
        if self.face_backend == 'haar':
            self._face_cascade = cv2.CascadeClassifier(
                find_cascade('haarcascade_frontalface_default.xml'))
            if self._face_cascade.empty():
                raise RuntimeError('加载正脸级联失败')
        elif self.face_backend != 'mock':
            raise ValueError('未知 face_backend: %r' % self.face_backend)

        # 疲劳：状态机 + 像素启发式分析器（mock 帧源时直接吃 meta 真值信号）
        self._fatigue_sm = FatigueWindowSM(**self.cfg['fatigue'])
        self._fatigue_analyzer = HeuristicEyeMouthAnalyzer()

        # 表情：工厂选后端（默认 mock）；外部也可直接注入实例
        if expression_backend is not None:
            self._expr = expression_backend
        else:
            self._expr = make_expression_backend(
                self.cfg['expression_backend'], **self.cfg['expression_params'])
        self._expr_happy = False    # HAPPY 边沿状态

        # 杯检测 + 有/无去抖
        self._cup = CupPresenceDetector(**self.cfg['cup'])
        self._cup_bg_pending = self.cfg['cup_background'] == 'first_frame'
        self._person_deb = _PresenceDebouncer(self.cfg['person_gone_s'])
        self._cup_deb = _PresenceDebouncer(self.cfg['cup_gone_s'])

        self._subs = []
        self._queue = deque()

    # ---- 事件 ----
    def on_event(self, cb):
        """订阅事件回调 cb(event_dict)。"""
        self._subs.append(cb)

    def poll_events(self):
        """拉取并清空内部事件队列。"""
        out = list(self._queue)
        self._queue.clear()
        return out

    def _emit(self, etype, ts, detail=None):
        e = {'type': etype, 'ts': round(float(ts), 3), 'detail': detail or {}}
        self._queue.append(e)
        for cb in self._subs:
            cb(e)

    # ---- 检测 ----
    def _detect_face(self, frame, meta):
        """返回 (person_present, face_box|None)。"""
        if self.face_backend == 'mock':
            present = bool(meta.get('person', False))
            return present, (MOCK_FACE_BOX if present else None)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._face_cascade.detectMultiScale(gray, 1.2, 5, minSize=(60, 60))
        if len(faces) == 0:
            return False, None
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        return True, (int(x), int(y), int(w), int(h))

    # ---- 主循环 ----
    def step(self):
        """处理一帧。帧源耗尽返回 False，否则 True。"""
        frame, meta = self.source.read()
        if frame is None:
            return False
        meta = meta or {}
        ts = meta.get('ts', time.time())

        # 杯检测背景帧：'first_frame' 模式用读到的第一帧（脚本需以空场景开头）
        if self._cup_bg_pending:
            self._cup.set_background(frame)
            self._cup_bg_pending = False

        # 1) 人脸有无 + 去抖
        person, face_box = (False, None)
        if self.caps.get('face'):
            person, face_box = self._detect_face(frame, meta)
        ev = self._person_deb.update(person, ts)
        if ev == 'arrived':
            self._emit(PERSON_PRESENT, ts)
        elif ev == 'departed':
            self._emit(PERSON_LEFT, ts)
            # 人离场：疲劳窗口与 HAPPY 边沿复位，下一个人重新计
            self._fatigue_sm.reset()
            self._expr_happy = False

        # 2) 疲劳（TASK 15：时间窗状态机，禁单帧定疲劳）
        if self.caps.get('fatigue') and person:
            if 'eyes' in meta or 'mouth' in meta:
                # mock 帧源：直接吃真值信号，保证 demo/测试确定性
                eye_closed = meta.get('eyes') == 'closed'
                mouth_open = meta.get('mouth') == 'yawn'
            else:
                sig = self._fatigue_analyzer.analyze(frame, face_box)
                eye_closed, mouth_open = sig['eye_closed'], sig['mouth_open']
            r = self._fatigue_sm.update(eye_closed, mouth_open, ts)
            if 'tired' in r['events']:
                self._emit(TIRED, ts, {'closed_ratio': r['closed_ratio'],
                                       'yawn_count': r['yawn_count']})

        # 3) 表情（TASK 16：工厂选后端，默认 mock）
        if self.caps.get('expression') and person:
            if isinstance(self._expr, MockExpression) and 'expression' in meta:
                self._expr.set_label(meta['expression'])
            label = self._expr.infer(frame, face_box)
            if label == 'happy' and not self._expr_happy:
                self._expr_happy = True
                self._emit(HAPPY, ts, {'backend': self._expr.name})
            elif label != 'happy':
                self._expr_happy = False

        # 4) 杯检测（TASK 18：ROI + 背景差分）+ 去抖
        if self.caps.get('cup'):
            cup = self._cup.present(frame)
            ev = self._cup_deb.update(cup, ts)
            if ev == 'arrived':
                self._emit(CUP_PRESENT, ts)
            elif ev == 'departed':
                self._emit(CUP_REMOVED, ts)

        return True

    def run(self, max_frames=None):
        """跑到帧源耗尽或达到 max_frames，返回处理帧数。"""
        n = 0
        while max_frames is None or n < max_frames:
            if not self.step():
                break
            n += 1
        return n

    @property
    def fatigue_state(self):
        """当前疲劳档位（'awake'/'possibly_tired'），供状态查询。"""
        return self._fatigue_sm.state


# ---------------- demo ----------------

def demo_script():
    """脚本化演示场景（虚拟时间，fps=5）：

    空场景 1s（兼作杯检测背景帧）→ 人到场 2s → 微笑 2s → 杯出现 2s
    → 人离场 4s（杯也撤走）→ 新人持续闭眼 12s → 睁眼恢复 3s。
    预期事件流（去抖后）：
      t=1.0  PERSON_PRESENT    t=3.0  HAPPY        t=5.0  CUP_PRESENT
      t=9.0  CUP_REMOVED       t=10.0 PERSON_LEFT  t=11.0 PERSON_PRESENT
      t=21.0 TIRED
    """
    return [
        {'dur': 1.0, 'person': False},
        {'dur': 2.0, 'person': True, 'expression': 'neutral'},
        {'dur': 2.0, 'person': True, 'expression': 'happy'},
        {'dur': 2.0, 'person': True, 'expression': 'neutral', 'cup': True},
        {'dur': 4.0, 'person': False},
        {'dur': 12.0, 'person': True, 'eyes': 'closed', 'expression': 'neutral'},
        {'dur': 3.0, 'person': True, 'eyes': 'open', 'expression': 'neutral'},
    ]


def demo_mock():
    """跑一遍脚本化场景并打印事件流（全合成帧，不碰摄像头）。"""
    print('=== VisionManager --demo-mock（合成帧，虚拟时间）===')
    print('场景：空 1s → 人到 2s → 微笑 2s → 杯现 2s → 人离 4s → '
          '新人闭眼 12s → 恢复 3s')
    print('--- 事件流 ---')
    src = MockFrameSource(demo_script(), fps=5)
    cfg = {'cup_background': 'first_frame',
           'cup': {'roi': MOCK_CUP_ROI, 'min_area': 800}}
    vm = VisionManager(src, cfg)

    def _print(e):
        detail = ' '.join('%s=%s' % (k, v) for k, v in e['detail'].items())
        print('[t=%6.1f] %-14s %s' % (e['ts'], e['type'], detail))
    vm.on_event(_print)
    n = vm.run()
    print('--- 结束：处理 %d 帧，最终疲劳状态 %s ---' % (n, vm.fatigue_state))


def demo_real(device):
    """真摄像头可选演示（Haar 人脸 + 像素启发式疲劳 + cpu 表情；杯检测需现场拍背景）。"""
    print('=== VisionManager --demo-real（/dev/video%d，Ctrl-C 退出）===' % device)
    src = Cv2FrameSource(device)
    cfg = {'face_backend': 'haar', 'expression_backend': 'cpu',
           'cup_background': 'first_frame',
           'cup': {'roi': None, 'min_area': 1500}}
    vm = VisionManager(src, cfg)
    vm.on_event(lambda e: print('[t=%12.1f] %s %s' % (e['ts'], e['type'], e['detail'])))
    try:
        vm.run()
    except KeyboardInterrupt:
        pass
    finally:
        src.close()


def main():
    ap = argparse.ArgumentParser(description='VisionManager 统一视觉管理（TASK 17）')
    ap.add_argument('--demo-mock', action='store_true',
                    help='合成帧脚本化演示（默认，不碰摄像头）')
    ap.add_argument('--demo-real', action='store_true',
                    help='真摄像头可选演示（仅演示用）')
    ap.add_argument('--device', type=int, default=0, help='摄像头设备号')
    args = ap.parse_args()
    if args.demo_real:
        demo_real(args.device)
    elif args.demo_mock:
        demo_mock()
    else:
        ap.print_help()


if __name__ == '__main__':
    main()
