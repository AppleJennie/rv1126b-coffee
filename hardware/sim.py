# hardware/sim.py —— 全模拟设备（Simulation Adapter）
# 所有 Sim 设备支持故障注入（构造参数），与 config/sim_scenario.yaml 的键一一对应（TASK 6）。
# time_scale：模拟时间缩放，SIM 模式 0.02（50 倍速），真实 1.0。

import time

from .arm import RobotArm
from .base import Device, DeviceError, DeviceTimeout, log
from .cup import CupDetector
from .machines import CoffeeMachine, Grinder, HotWater, SmartSwitch


class SimRobotArm(RobotArm):
    """模拟机械臂：记录语义位姿与夹爪状态，支持故障注入。

    fail_at:  执行到该动作时抛 DeviceError（robot_arm_fail）
    hang_at:  执行到该动作时抛 DeviceTimeout（长期 BUSY 卡死）
    offline:  connect() 即失败（设备离线）
    latency:  每个动作的模拟耗时（秒，已含 time_scale）
    """

    def __init__(self, fail_at=None, hang_at=None, offline=False, latency=0.05):
        super().__init__()
        self.fail_at = fail_at
        self.hang_at = hang_at
        self.offline = offline
        self.latency = latency
        self.pose = "HOME"
        self.holding = False
        self._estopped = False

    def connect(self):
        if self.offline:
            raise DeviceError("机械臂离线（串口无应答）")
        return super().connect()

    def _act(self, name, pose=None):
        if self._estopped:
            raise DeviceError("急停中，拒绝动作", retryable=False)
        if self.fail_at == name:
            raise DeviceError(f"机械臂动作 {name} 失败（注入故障）")
        if self.hang_at == name:
            raise DeviceTimeout(f"机械臂动作 {name} 超时（长期 BUSY）")
        log("SIM-ARM", f"{name}" + (f" -> {pose}" if pose else "")
            + (f"（持杯）" if self.holding else ""))
        time.sleep(self.latency)
        if pose:
            self.pose = pose

    def home(self):
        self._act("home", "HOME")

    def move_to(self, pose):
        if pose not in self.POSES:
            raise DeviceError(f"未知语义位姿 {pose}")
        self._act("move_to", pose)

    def pick_cup(self, correction_mm=None):
        if correction_mm:
            log("SIM-ARM", f"视觉纠偏 dx={correction_mm[0]:+.1f} dy={correction_mm[1]:+.1f} mm")
        self._act("pick_cup", "CUP")
        self.holding = True

    def place_cup(self):
        self._act("place_cup", "BREWER")
        self.holding = False

    def pour_grounds(self, steps=3):
        for i in range(1, steps + 1):
            self._act(f"pour_grounds {i}/{steps}")

    def pick_finished_drink(self):
        self._act("pick_finished_drink", "BREWER")
        self.holding = True

    def serve(self):
        self._act("serve", "SERVE")
        self.holding = False

    def release(self):
        self._act("release")
        self.holding = False

    def emergency_stop(self):
        self._estopped = True
        self.holding = False
        log("SIM-ARM", "!! 急停：全部关节卸力")

    def reset(self):
        self._estopped = False
        self.pose = "HOME"
        self.holding = False
        log("SIM-ARM", "复位完成，回 HOME")

    def is_moving(self):
        return False

    def status(self):
        return {"online": self.online, "state":
                "estop" if self._estopped else ("busy" if self.is_moving() else "idle"),
                "pose": self.pose, "holding": self.holding}


class SimSmartSwitch(SmartSwitch):
    """模拟开关。

    offline:  connect() 失败（wifi_disconnect）
    hang:     on/off 抛 DeviceTimeout（设备无应答 → grinder_timeout 等）
    stuck_on: off() 无效（继电器粘连 → 触发电器最大运行时间保护）
    """

    def __init__(self, name="switch", offline=False, hang=False, stuck_on=False):
        super().__init__()
        self.name = name
        self.offline = offline
        self.hang = hang
        self.stuck_on = stuck_on
        self._on = False

    def connect(self):
        if self.offline:
            raise DeviceError(f"{self.name}: 离线（网络不可达）")
        return super().connect()

    def on(self):
        if self.hang:
            raise DeviceTimeout(f"{self.name}: 指令无应答（注入超时）")
        log("SIM-SW", f"{self.name} ON")
        self._on = True

    def off(self):
        if self.hang:
            raise DeviceTimeout(f"{self.name}: 指令无应答（注入超时）")
        if self.stuck_on:
            log("SIM-SW", f"{self.name} OFF 无效（继电器粘连，注入故障）")
            return
        log("SIM-SW", f"{self.name} OFF")
        self._on = False

    def is_on(self):
        return self._on

    def health(self):
        return {"ok": self._connected and not self.offline,
                "detail": "offline" if self.offline else "", "ts": time.time()}

    def status(self):
        return {"online": self.online, "state": "on" if self._on else "off"}


class SimGrinder(Grinder):
    def __init__(self, time_scale=0.02, **sw_kw):
        super().__init__(SimSmartSwitch("grinder_sw", **sw_kw),
                         mode="power", run_sec=15.0)
        self.time_scale = time_scale


class SimCoffeeMachine(CoffeeMachine):
    def __init__(self, time_scale=0.02, brew_sec=180.0, **sw_kw):
        super().__init__(SimSmartSwitch("coffee_sw", **sw_kw),
                         mode="press", press_sec=1.0 * time_scale)
        self.time_scale = time_scale
        self.brew_sec = brew_sec          # 冲泡完成的实际等待（真机 180s）


class SimHotWater(HotWater):
    def __init__(self, time_scale=0.02, **sw_kw):
        super().__init__(SimSmartSwitch("water_sw", **sw_kw),
                         mode="power", run_sec=20.0)
        self.time_scale = time_scale


class SimCupDetector(CupDetector):
    """模拟杯检测。

    present:           取杯位是否有杯（cup_missing 注入时 False）
    hang:              locate() 抛 DeviceTimeout（vision_timeout）
    customer_removes:  出餐后顾客是否取杯（customer_not_take_cup 注入时 False，
                       出餐位杯子永远不被取走）
    """

    def __init__(self, present=True, hang=False, customer_removes=True):
        super().__init__()
        self.present = present
        self.hang = hang
        self.customer_removes = customer_removes
        self._serve_checks = 0

    def cup_present(self, where="pickup"):
        if where == "pickup":
            log("SIM-CUP", f"取杯位检测 -> {'有杯' if self.present else '无杯'}")
            return self.present
        # 出餐位：顾客取杯则第二次检查时已取走
        self._serve_checks += 1
        taken = self.customer_removes and self._serve_checks >= 2
        log("SIM-CUP", f"出餐位检测 -> {'已取走' if taken else '仍有杯'}")
        return not taken

    def locate(self):
        if self.hang:
            raise DeviceTimeout("视觉采帧超时（注入故障）")
        if not self.present:
            log("SIM-CUP", "采帧 -> 未检出杯口圆")
            return None
        log("SIM-CUP", "采帧 -> 杯口圆 (u=960.0, v=540.0, r=85.0)")
        return {"u": 960.0, "v": 540.0, "x_mm": 152.3, "y_mm": 88.6}

    def reset(self):
        self._serve_checks = 0
