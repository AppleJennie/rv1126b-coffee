# hardware/vision_cup.py —— 真实杯检测适配器（OpenCV Hough 圆，TASK 18 传统视觉优先）
# 薄封装 projects/vision/cup_detect.py + hand_eye_calib.py（移植自 fsm.py 的 RealVision）。
# 无摄像头环境只保证可导入；connect() 才碰设备。

import os
import sys

from .base import DeviceError, log
from .cup import CupDetector

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "projects", "vision"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class VisionCupDetector(CupDetector):
    """cfg: projects/coffee_fsm/config.json 内容（camera_device/hough_*/calib_file 等）。"""

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.H = None
        self._grab = None
        self._detect = None

    def connect(self):
        try:
            from cup_detect import detect_cup, grab_frame
            from hand_eye_calib import apply_homography, load_calib
        except Exception as e:
            raise DeviceError(f"视觉模块导入失败（板端需 cv2）: {e}")
        self._grab = grab_frame
        self._detect = detect_cup
        self._homography = apply_homography
        calib = self.cfg["calib_file"]
        if not os.path.isabs(calib):
            calib = os.path.join(_ROOT, "projects", "coffee_fsm", calib)
        if os.path.exists(calib):
            self.H = load_calib(calib)
            log("CUP", f"已加载标定 {calib}")
        else:
            log("CUP", f"警告：无标定文件 {calib}，只输出像素坐标")
        frame = self._grab(self.cfg["camera_device"])
        if frame is None:
            raise DeviceError(f"摄像头 {self.cfg['camera_device']} 采帧失败")
        self._connected = True
        return True

    def _frame_detect(self):
        frame = self._grab(self.cfg["camera_device"])
        if frame is None:
            raise DeviceError("采帧失败（设备断开？）", retryable=True)
        hough = (self.cfg["hough_min_r"], self.cfg["hough_max_r"],
                 self.cfg["hough_param1"], self.cfg["hough_param2"])
        _circles, best = self._detect(frame, *hough)
        return best

    def cup_present(self, where="pickup"):
        return self._frame_detect() is not None

    def locate(self):
        best = self._frame_detect()
        if best is None:
            return None
        u, v, _r = best
        x_mm = y_mm = None
        if self.H is not None:
            x_mm, y_mm = self._homography(self.H, u, v)
        return {"u": u, "v": v, "x_mm": x_mm, "y_mm": y_mm}
