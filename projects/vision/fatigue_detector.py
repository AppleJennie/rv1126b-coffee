#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疲劳检测重设计（TASK 15）—— 纯 CPU 流水线，开发 VM 上可跑可测。

流水线：
    face detect（由调用方给 face_box；本项目中由 VisionManager 的
    Haar / Mock 人脸检测提供）→ face ROI → eye state（睁/闭）
    → yawn（嘴部大张）→ 时间窗状态机（连续窗口判定，禁止单帧定疲劳）。

输出只有两档：'awake' / 'possibly_tired'。
默认规则（均可配置）：
    10s 窗口内闭眼占比 > 40%  → possibly_tired
    10s 窗口内哈欠 >= 2 次     → possibly_tired
    闭眼占比回落 < 25% 且窗口内哈欠 < 2 次 → 恢复 awake

眼/嘴状态用「亮度启发式」：睁眼时眼 ROI 内有高亮区域（眼白/高光），
均值不低于脸部均值；闭眼成暗色睑线，均值明显低于脸部均值；
哈欠时嘴 ROI 内出现大块暗区。该启发式对本项目的合成 mock 帧是确定性的，
可直接驱动单元测试；真机部署建议替换为 EAR/MAR 关键点方案
（projects/ai_host/fatigue.py 已有基于 106 关键点的实现，走 NPU 模型）。

本模块不碰摄像头：输入是 (帧, 人脸框, 时间戳)，方便用合成帧 + 虚拟时间测试。
"""

import time
from collections import deque

import cv2
import numpy as np

# 两档输出常量
AWAKE = 'awake'
POSSIBLY_TIRED = 'possibly_tired'


class HeuristicEyeMouthAnalyzer(object):
    """从人脸 ROI 估计「眼是否闭、嘴是否大张（哈欠）」的亮度启发式分析器。

    相对人脸框的经验子区域：
      双眼：y 25%~45%，左眼 x 15%~45% / 右眼 x 55%~85%
      嘴：  y 70%~92%，x 30%~70%
    """

    def __init__(self, eye_dark_ratio=0.97, mouth_dark_ratio=0.30,
                 mouth_dark_level=0.6):
        # 眼 ROI 灰度均值 < 脸均值 * eye_dark_ratio → 判闭眼
        self.eye_dark_ratio = eye_dark_ratio
        # 嘴 ROI 暗像素占比 > mouth_dark_ratio → 判哈欠
        self.mouth_dark_ratio = mouth_dark_ratio
        # 暗像素定义：灰度 < 脸均值 * mouth_dark_level
        self.mouth_dark_level = mouth_dark_level

    @staticmethod
    def _sub(face, rx0, ry0, rx1, ry1):
        """按相对坐标取人脸子区域，过小返回 None。"""
        h, w = face.shape[:2]
        sub = face[int(ry0 * h):int(ry1 * h), int(rx0 * w):int(rx1 * w)]
        if sub.size == 0:
            return None
        return sub

    def analyze(self, frame, face_box):
        """返回 {'eye_closed': bool, 'mouth_open': bool, 调试值...}。

        任何 ROI 退化（人脸框出界等）都按「眼睁、嘴正常」处理，不抛异常。
        """
        x, y, w, h = [int(v) for v in face_box]
        fh, fw = frame.shape[:2]
        # 人脸框裁剪到图内，防越界切片
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(fw, x + w), min(fh, y + h)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return {'eye_closed': False, 'mouth_open': False,
                    'eye_mean': None, 'mouth_dark': None}
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        face = gray[y0:y1, x0:x1]
        face_mean = float(face.mean())

        eye_l = self._sub(face, 0.15, 0.25, 0.45, 0.45)
        eye_r = self._sub(face, 0.55, 0.25, 0.85, 0.45)
        mouth = self._sub(face, 0.30, 0.70, 0.70, 0.92)

        eye_closed = False
        eye_mean = None
        if eye_l is not None and eye_r is not None and face_mean > 1.0:
            eye_mean = (float(eye_l.mean()) + float(eye_r.mean())) * 0.5
            eye_closed = eye_mean < face_mean * self.eye_dark_ratio

        mouth_open = False
        mouth_dark = None
        if mouth is not None and face_mean > 1.0:
            dark = np.count_nonzero(mouth < face_mean * self.mouth_dark_level)
            mouth_dark = float(dark) / float(mouth.size)
            mouth_open = mouth_dark > self.mouth_dark_ratio

        return {'eye_closed': bool(eye_closed), 'mouth_open': bool(mouth_open),
                'eye_mean': None if eye_mean is None else round(eye_mean, 1),
                'mouth_dark': None if mouth_dark is None else round(mouth_dark, 3)}


class FatigueWindowSM(object):
    """疲劳时间窗状态机：连续窗口判定，禁单帧定疲劳。

    每帧喂 (eye_closed, mouth_open, ts)，内部维护 window_s 滑窗：
      - 闭眼占比 = 窗内闭眼样本数 / 窗内总样本数
      - 哈欠计数 = 窗内哈欠次数（mouth_open 上升沿计 1 次，不应期内不重复计）
    进入 possibly_tired：哈欠数达标（立即），或窗满且闭眼占比超阈；
    恢复 awake：闭眼占比回落到恢复阈以下且窗内哈欠数不达标。
    事件：状态翻转时在返回的 events 里给出 'tired' / 'recovered'（边沿触发）。
    """

    def __init__(self, window_s=10.0, closed_ratio_tired=0.40,
                 closed_ratio_recover=0.25, yawn_count_tired=2,
                 yawn_refractory_s=3.0):
        self.window_s = window_s                  # 判定窗口长度（秒）
        self.closed_ratio_tired = closed_ratio_tired    # 闭眼占比疲劳阈
        self.closed_ratio_recover = closed_ratio_recover  # 闭眼占比恢复阈（滞回防抖）
        self.yawn_count_tired = yawn_count_tired        # 窗内哈欠疲劳次数
        self.yawn_refractory_s = yawn_refractory_s      # 哈欠计数不应期（防抖动重复计）
        self.reset()

    def reset(self):
        """清空窗口与状态（换人/重新进场时调用）。"""
        self.state = AWAKE
        self._samples = deque()   # (ts, eye_closed)
        self._yawns = deque()     # 哈欠确认时间戳
        self._mouth_was_open = False
        self._last_yawn_ts = None

    def update(self, eye_closed, mouth_open, ts=None):
        """喂一帧的眼/嘴状态，返回 {'state', 'closed_ratio', 'yawn_count',
        'window_full', 'events'}。ts 可显式传入（测试用虚拟时间）。"""
        if ts is None:
            ts = time.time()

        self._samples.append((ts, bool(eye_closed)))
        # 哈欠：mouth_open 上升沿确认 1 次；不应期内不重复计
        if mouth_open and not self._mouth_was_open:
            if (self._last_yawn_ts is None
                    or ts - self._last_yawn_ts >= self.yawn_refractory_s):
                self._yawns.append(ts)
                self._last_yawn_ts = ts
        self._mouth_was_open = bool(mouth_open)

        # 滑窗裁剪：只保留 window_s 内的样本与哈欠
        while self._samples and ts - self._samples[0][0] > self.window_s:
            self._samples.popleft()
        while self._yawns and ts - self._yawns[0] > self.window_s:
            self._yawns.popleft()

        n = len(self._samples)
        closed_n = sum(1 for _, c in self._samples if c)
        ratio = float(closed_n) / float(n) if n else 0.0
        span = ts - self._samples[0][0] if n else 0.0
        # 窗未满不做闭眼占比判定（1e-9 吸收虚拟时间戳累加的浮点误差）
        window_full = span >= self.window_s - 1e-9
        yawn_n = len(self._yawns)

        events = []
        if self.state == AWAKE:
            if (yawn_n >= self.yawn_count_tired
                    or (window_full and ratio > self.closed_ratio_tired)):
                self.state = POSSIBLY_TIRED
                events.append('tired')
        else:
            if ratio < self.closed_ratio_recover and yawn_n < self.yawn_count_tired:
                self.state = AWAKE
                events.append('recovered')

        return {'state': self.state,
                'closed_ratio': round(ratio, 3),
                'yawn_count': yawn_n,
                'window_full': window_full,
                'events': events}


class FatigueDetector(object):
    """疲劳检测器：启发式分析器 + 时间窗状态机的组合封装。

    用法：
        det = FatigueDetector()
        r = det.update(frame, face_box, ts)   # frame/face_box 为 None 表示本帧无人脸
        # r = {present, state: 'awake'|'possibly_tired', eye_closed, mouth_open,
        #      closed_ratio, yawn_count, events}
    """

    def __init__(self, analyzer=None, **sm_params):
        self.analyzer = analyzer if analyzer is not None else HeuristicEyeMouthAnalyzer()
        self.sm = FatigueWindowSM(**sm_params)

    def reset(self):
        self.sm.reset()

    def update(self, frame, face_box, ts=None):
        if frame is None or face_box is None:
            # 无人脸不喂样本（窗口自然滑空），只回当前状态
            return {'present': False, 'state': self.sm.state, 'events': [],
                    'eye_closed': None, 'mouth_open': None}
        sig = self.analyzer.analyze(frame, face_box)
        r = self.sm.update(sig['eye_closed'], sig['mouth_open'], ts)
        r['present'] = True
        r['eye_closed'] = sig['eye_closed']
        r['mouth_open'] = sig['mouth_open']
        return r
