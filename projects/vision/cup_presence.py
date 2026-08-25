#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出餐位杯子有无检测（TASK 18）—— 传统视觉，不上 NN。

对 cup_detect.py / cup_locate.py 的审计结论：
  已有 detect_cup() 用灰度 + 高斯 + HoughCircles 全图找杯口圆并返回圆心，
  满足「杯口定位」需求（hardware/vision_cup.py 在用，API 保持不变）；
  但它没有 ROI 限定、没有背景差分 —— 对「固定机位只关心出餐位有/无杯」
  这个判定既浪费又容易被画面其它区域干扰（如人脸入镜）。本模块按任务书
  补齐流水线：ROI 限定 → 与背景帧差分 → 阈值 → 形态学 → 轮廓面积 → 有/无杯。
  未设置背景帧时可回退到 HoughCircles 判定（复用 cup_detect.detect_cup）。

典型用法：
    det = CupPresenceDetector(roi=(205, 150, 115, 90), min_area=800)
    det.set_background(empty_frame)     # 先拍一张空台面
    det.present(frame)                  # True/False
"""

import os
import sys

import cv2

# 允许以脚本方式（python3 projects/vision/cup_presence.py）或被 import 运行，
# 同目录模块按本仓库惯例用 sys.path 兜底（同 hardware/vision_cup.py 的做法）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from cup_detect import detect_cup   # Hough 回退用；失败不致命
except Exception:
    detect_cup = None


class CupPresenceDetector(object):
    """固定机位「出餐位有/无杯」判定器。

    参数：
      roi           (x, y, w, h) 只看出餐位区域；None = 全图
      min_area      差分后最大轮廓面积 >= 此值判有杯（像素）
      diff_thresh   帧差二值化阈值（灰度级）
      morph_ksize   形态学开/闭运算核边长（去噪 + 补洞）
      hough_fallback  无背景帧时是否回退 HoughCircles
      hough_params  回退用的 (min_r, max_r, param1, param2)
    """

    def __init__(self, roi=None, min_area=800, diff_thresh=30, morph_ksize=5,
                 hough_fallback=True, hough_params=(15, 120, 100, 30)):
        self.roi = roi
        self.min_area = min_area
        self.diff_thresh = diff_thresh
        self.morph_ksize = morph_ksize
        self.hough_fallback = hough_fallback
        self.hough_params = hough_params
        self._bg = None   # 背景帧（已预处理：裁剪 + 灰度 + 模糊）

    # ---- 内部 ----
    def _crop(self, frame):
        if self.roi is None:
            return frame
        x, y, w, h = [int(v) for v in self.roi]
        fh, fw = frame.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(fw, x + w), min(fh, y + h)
        return frame[y0:y1, x0:x1]

    def _prep(self, frame):
        """裁剪 ROI → 灰度 → 高斯模糊（与差分/检测共用同一预处理）。"""
        gray = cv2.cvtColor(self._crop(frame), cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(gray, (5, 5), 0)

    # ---- 对外 ----
    def set_background(self, frame):
        """拍一张空台面作背景帧。换了机位/光照要重拍。"""
        self._bg = self._prep(frame)

    def present(self, frame):
        """出餐位是否有杯：True/False。"""
        return self.present_debug(frame)['present']

    def present_debug(self, frame):
        """同 present()，但带方法与调试量（max_area / best 圆）。"""
        if self._bg is not None:
            diff = cv2.absdiff(self._prep(frame), self._bg)
            _th_val, th = cv2.threshold(diff, self.diff_thresh, 255,
                                        cv2.THRESH_BINARY)
            k = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (self.morph_ksize, self.morph_ksize))
            th = cv2.morphologyEx(th, cv2.MORPH_OPEN, k)    # 去噪点
            th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, k)   # 补轮廓内空洞
            contours, _hier = cv2.findContours(
                th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            max_area = max((cv2.contourArea(c) for c in contours), default=0.0)
            return {'present': bool(max_area >= self.min_area),
                    'method': 'bgdiff',
                    'max_area': round(float(max_area), 1)}

        if self.hough_fallback and detect_cup is not None:
            _circles, best = detect_cup(self._crop(frame), *self.hough_params)
            return {'present': best is not None,
                    'method': 'hough',
                    'best': best}

        raise RuntimeError('未设置背景帧且 Hough 回退不可用，无法判定有/无杯')
