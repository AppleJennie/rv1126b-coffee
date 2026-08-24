# hardware/machines.py —— 电器统一接口
# SmartSwitch 是原始开关（on/off/press）；Grinder/CoffeeMachine/HotWater
# 是设备级封装：持有一个开关 + 运行参数 + 最大运行时间保护。
# 业务层只碰 Grinder/CoffeeMachine/HotWater，不直接操作 SmartSwitch。

import time

from .base import Device, DeviceError, log


class SmartSwitch(Device):
    """原始开关接口（WiFi 插座 / 点动继电器 / GPIO）。驱动方实现。"""

    name = "switch"
    critical = False      # 开关本身非关键，关键性由包它的电器决定

    def on(self):
        raise NotImplementedError

    def off(self):
        raise NotImplementedError

    def press(self, sec=1.0):
        """点动：吸合 sec 秒 = 按一次键。"""
        self.on()
        time.sleep(sec)
        self.off()

    def is_on(self):
        raise NotImplementedError


class Appliance(Device):
    """电器基类：电源型(power) / 点动型(press) 两种控制模式。

    power：通电 = 开工，运行 run_sec 后断电（机械锁定开关的机器）
    press：吸合 press_sec = 按一次键（电子轻触按键的机器）
    """

    MAX_RUN_SEC = 120.0   # 最大运行时间保护：超过视为异常，强制断电

    def __init__(self, switch, mode="power", run_sec=10.0, press_sec=1.0,
                 name="appliance"):
        super().__init__()
        self.switch = switch
        self.mode = mode
        self.run_sec = min(float(run_sec), self.MAX_RUN_SEC)
        self.press_sec = float(press_sec)
        self.name = name
        self._running_since = None
        self.time_scale = 1.0      # 模拟层设为 0.02 等，实际耗时 = 名义秒 × time_scale

    def _real(self, nominal_sec):
        """名义秒 -> 实际等待秒（经时间缩放）。"""
        return max(0.0, float(nominal_sec) * self.time_scale)

    @property
    def _max_real(self):
        return self.MAX_RUN_SEC * self.time_scale

    def connect(self):
        self.switch.connect()
        self._connected = True
        return True

    def run(self, seconds=None):
        """启动电器干一件事（磨一份豆 / 冲一杯）。失败抛 DeviceError。"""
        if not self._connected:
            raise DeviceError(f"{self.name}: 未 connect")
        if self.mode == "press":
            log("APPL", f"{self.name} 点动按压 {self.press_sec}s")
            self.switch.press(self.press_sec)
            return self.run_sec
        sec = min(float(seconds or self.run_sec), self.MAX_RUN_SEC)
        log("APPL", f"{self.name} 通电运行 {sec}s（上限 {self.MAX_RUN_SEC}s，"
                    f"实际等待 {self._real(sec):.1f}s）")
        self._running_since = time.time()
        self.switch.on()
        return sec

    def tick(self):
        """运行中周期检查：超过 MAX_RUN_SEC 自动断电（失控保护）。"""
        if self._running_since and time.time() - self._running_since > self._max_real:
            log("APPL", f"{self.name} 超最大运行时间 {self.MAX_RUN_SEC}s，强制断电")
            self.abort()
            raise DeviceError(f"{self.name}: 运行超时强制停止", retryable=False)

    def wait_done(self, seconds=None):
        """阻塞等运行结束并断电（power 型）。实际等待经 time_scale 缩放。
        断电后回读开关状态，断电无效（继电器粘连等）抛 DeviceError。"""
        if self.mode == "press":
            return
        sec = min(float(seconds or self.run_sec), self.MAX_RUN_SEC)
        deadline = time.time() + self._real(sec)
        while time.time() < deadline:
            self.tick()
            time.sleep(min(0.2, max(0.0, deadline - time.time())))
        self.abort()
        if self.switch.is_on():
            raise DeviceError(f"{self.name}: 断电无效（继电器粘连？），需人工检查",
                              retryable=False)

    def abort(self):
        """立即断电/停止（急停时调用，必须幂等）。"""
        try:
            self.switch.off()
        finally:
            self._running_since = None

    def health(self):
        h = self.switch.health()
        return {"ok": self._connected and h["ok"],
                "detail": f"{self.mode} via {self.switch.name}: {h['detail']}",
                "ts": time.time()}

    def status(self):
        return {"online": self.online, "state":
                "running" if self._running_since else "idle",
                "mode": self.mode}


class Grinder(Appliance):
    """磨豆机。run_sec = 磨一份粉的通电时长。"""
    MAX_RUN_SEC = 60.0

    def __init__(self, switch, **kw):
        super().__init__(switch, name="grinder", **kw)


class CoffeeMachine(Appliance):
    """滴滤/胶囊机主体。"""
    MAX_RUN_SEC = 600.0

    def __init__(self, switch, **kw):
        super().__init__(switch, name="coffee", **kw)


class HotWater(Appliance):
    """热水/泵。"""
    MAX_RUN_SEC = 120.0

    def __init__(self, switch, **kw):
        super().__init__(switch, name="hot_water", **kw)
