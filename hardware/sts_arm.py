# hardware/sts_arm.py —— 真实机械臂适配器（STS 总线舵机）
# 薄封装 projects/coffee_fsm/sts.py 的 BusServo，把语义动作映射到
# config/poses.yaml 的关节角度。真机验证前本模块只保证可导入、可安全实例化
# （不 connect 不碰串口）。

import os
import sys

from .arm import RobotArm
from .base import DeviceError, DeviceTimeout, log

# projects/coffee_fsm 的 sts.py 与项目内视觉模块按路径引入
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "projects", "coffee_fsm"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class StsRobotArm(RobotArm):
    """STS 总线舵机机械臂。cfg: projects/coffee_fsm/config.json 内容；
    poses: config/poses.yaml 内容（语义位姿 -> 关节角度）。"""

    def __init__(self, cfg, poses):
        super().__init__()
        self.cfg = cfg
        self.poses = poses
        self.bus = None
        self._estopped = False
        self._holding = False

    def connect(self):
        from sts import BusServo
        try:
            self.bus = BusServo(self.cfg["serial_port"], self.cfg["baud_rate"])
        except Exception as e:
            raise DeviceError(f"机械臂串口打开失败 {self.cfg['serial_port']}: {e}")
        ids = self.cfg["joint_ids"]
        missing = [j for j, sid in ids.items() if not self.bus.ping(sid)]
        if missing:
            raise DeviceError(f"机械臂关节无应答: {', '.join(missing)}", retryable=True)
        self._connected = True
        log("ARM", f"总线连接成功 {self.cfg['serial_port']}，关节全部在线")
        return True

    # ---- 内部：按语义位姿执行关节运动（含回读校验）----
    def _goto(self, pose_name, deltas=None):
        if self.bus is None:
            raise DeviceError("机械臂未 connect")
        if pose_name not in self.poses:
            raise DeviceError(f"poses.yaml 缺少位姿 {pose_name}")
        pose = self.poses[pose_name]
        ids = self.cfg["joint_ids"]
        speed = pose.get("speed", self.cfg["default_speed"])
        time_ms = self.cfg["default_time_ms"]
        tol = self.cfg["position_tolerance"]
        deltas = deltas or {}
        targets = {}
        for jname, jpos in pose["joints"].items():
            if jname == "J6":
                continue                     # 夹爪由 gripper 字段控制
            sid = ids[jname]
            target = max(0, min(4095, jpos + deltas.get(jname, 0)))
            targets[jname] = (sid, target)
            if not self.bus.write_position(sid, target, speed, time_ms):
                raise DeviceError(f"{pose_name}: 舵机 {sid}({jname}) 写位置失败")
        grip = pose.get("gripper", "hold")
        if grip in ("open", "close"):
            gpos = self.cfg["gripper_open_pos"] if grip == "open" \
                else self.cfg["gripper_close_pos"]
            targets["J6"] = (ids["J6"], gpos)
            if not self.bus.write_position(ids["J6"], gpos, speed, time_ms):
                raise DeviceError(f"{pose_name}: 夹爪舵机写位置失败")
            self._holding = (grip == "close")
        import time as _t
        _t.sleep(time_ms / 1000.0)
        for jname, (sid, target) in targets.items():
            cur = self.bus.read_position(sid)
            if cur is None:
                raise DeviceTimeout(f"{pose_name}: 舵机 {sid}({jname}) 回读失败")
            if abs(cur - target) > tol:
                raise DeviceError(
                    f"{pose_name}: {jname} 位置误差 {abs(cur - target)} 超容差 {tol}")

    def home(self):
        self._goto("HOME")

    def move_to(self, pose):
        self._goto(pose)

    def pick_cup(self, correction_mm=None):
        deltas = {}
        if correction_mm:
            dx, dy = correction_mm
            deltas = {
                "J1": int(round(dx * self.cfg["correct_j1_steps_per_mm"])),
                "J2": int(round(dy * self.cfg["correct_j2_steps_per_mm"])),
            }
        self._goto("CUP", deltas)

    def place_cup(self):
        self._goto("BREWER")

    def pour_grounds(self, steps=3):
        self._goto("GROUNDS_PICK") if "GROUNDS_PICK" in self.poses else None
        self._goto("GROUNDS_POUR")

    def pick_finished_drink(self):
        self._goto("BREWER")

    def serve(self):
        self._goto("SERVE")

    def release(self):
        if self.bus is None:
            raise DeviceError("机械臂未 connect")
        sid = self.cfg["joint_ids"]["J6"]
        if not self.bus.write_position(sid, self.cfg["gripper_open_pos"],
                                       self.cfg["default_speed"],
                                       self.cfg["default_time_ms"]):
            raise DeviceError("夹爪松开失败")
        self._holding = False

    def emergency_stop(self):
        self._estopped = True
        self._holding = False
        if self.bus is not None:
            for sid in self.cfg["joint_ids"].values():
                try:
                    self.bus.torque(sid, False)
                except Exception:
                    pass
        log("ARM", "!! 急停：全部关节卸力")

    def reset(self):
        self._estopped = False
        if self.bus is not None:
            for sid in self.cfg["joint_ids"].values():
                self.bus.torque(sid, True)
        self.home()

    def stop(self):
        self.emergency_stop()

    def status(self):
        return {"online": self.online,
                "state": "estop" if self._estopped else "idle",
                "holding": self._holding}
