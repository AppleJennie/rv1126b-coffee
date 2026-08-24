# hardware/cup.py —— 杯子视觉检测统一接口
# 传统 OpenCV 方案优先（TASK 18），神经网络不是必需的。

from .base import Device


class CupDetector(Device):
    """杯检测抽象：取杯位有杯判断 + 杯口定位（像素 + 台面坐标）。"""

    name = "cup_detector"

    def cup_present(self, where="pickup"):
        """指定位置（pickup 取杯位 / serve 出餐位）是否有杯。True/False。"""
        raise NotImplementedError

    def locate(self):
        """定位杯口：返回 {"u","v","x_mm","y_mm"}；找不到返回 None。
        x_mm/y_mm 为台面坐标（mm），无标定时可为 None。"""
        raise NotImplementedError
