# hardware/wifi_plug.py —— 真实 WiFi 开关适配器
# 薄封装 projects/coffee_fsm/wifi_switch.py（Tasmota / Sonoff DIY / 自定义 URL），
# 厂商细节不进入业务层（TASK 22）。

import os
import sys

from .base import DeviceError, log
from .machines import SmartSwitch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "projects", "coffee_fsm"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class WifiSmartSwitch(SmartSwitch):
    """actcfg: config.json 中 actuators 的一项
    （{type: wifi, driver: tasmota|sonoff|custom, host, ...}）。"""

    def __init__(self, name, actcfg):
        super().__init__()
        self.name = name
        self.actcfg = actcfg
        self._sw = None

    def connect(self):
        from wifi_switch import make_switch
        try:
            self._sw = make_switch(self.name, self.actcfg, mock=False)
        except Exception as e:
            raise DeviceError(f"{self.name}: 开关初始化失败: {e}")
        self._connected = True
        return True

    def _need(self):
        if self._sw is None:
            raise DeviceError(f"{self.name}: 未 connect")

    def on(self):
        self._need()
        log("WIFI-SW", f"{self.name} ON -> {self.actcfg.get('host')}")
        self._sw.set_power(True)

    def off(self):
        self._need()
        log("WIFI-SW", f"{self.name} OFF -> {self.actcfg.get('host')}")
        self._sw.set_power(False)

    def press(self, sec=1.0):
        self._need()
        log("WIFI-SW", f"{self.name} 点动 {sec}s -> {self.actcfg.get('host')}")
        self._sw.press(sec)

    def is_on(self):
        return None     # 现有驱动无回读，真机联调时按固件能力补
