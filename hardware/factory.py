# hardware/factory.py —— 设备组装工厂（TASK 27 模式机制的核心）
#
# mode:
#   SIM    全模拟（无硬件开发/演示/回归测试）
#   REAL   全真实（板端真机）
#   HYBRID 逐项真/假混合：cfg["devices"] = {"arm": "real", "cup": "sim", ...}，
#          未列出的设备默认 sim。例：真实摄像头 + 模拟机械臂也能完整演示。
#
# faults: config/sim_scenario.yaml 的故障注入键（TASK 6），仅作用于 Sim 设备。

import json
import os

import yaml

from .base import log
from .machines import CoffeeMachine, Grinder, HotWater
from . import sim as _sim

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SIM_TIME_SCALE = 0.02        # 模拟 50 倍速：磨豆 15s -> 0.3s，冲泡 180s -> 3.6s

# sim_scenario.yaml 键 -> Sim 设备构造参数
#   robot_arm_fail: true 或动作名      机械臂动作失败
#   robot_arm_hang: true 或动作名      机械臂长期 BUSY 卡死
#   cup_missing: true                  取杯位无杯
#   vision_timeout: true               视觉采帧超时
#   grinder_timeout / coffee_machine_timeout / hot_water_timeout: true
#   grinder_stuck_on: true             继电器粘连（触发最大运行时间保护）
#   wifi_disconnect: true              全部 WiFi 开关离线
#   customer_not_take_cup: true        出餐后顾客不取杯


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def default_fsm_config():
    return _load_json(os.path.join(ROOT, "projects", "coffee_fsm", "config.json"))


def default_poses():
    return _load_yaml(os.path.join(ROOT, "config", "poses.yaml"))


def load_scenario(path=None):
    """加载故障注入场景；path 缺省 config/sim_scenario.yaml，不存在返回 {}。"""
    path = path or os.path.join(ROOT, "config", "sim_scenario.yaml")
    if not os.path.exists(path):
        return {}
    data = _load_yaml(path) or {}
    return {k: v for k, v in data.items() if v}     # 只保留启用的故障项


def _sim_kwargs(faults):
    wifi_off = bool(faults.get("wifi_disconnect"))
    arm_fail = faults.get("robot_arm_fail")
    arm_hang = faults.get("robot_arm_hang")
    return {
        "arm": dict(fail_at=("pick_cup" if arm_fail is True else arm_fail or None),
                    hang_at=("pick_cup" if arm_hang is True else arm_hang or None),
                    offline=bool(faults.get("robot_arm_offline"))),
        "cup": dict(present=not faults.get("cup_missing"),
                    hang=bool(faults.get("vision_timeout")),
                    customer_removes=not faults.get("customer_not_take_cup")),
        "grinder": dict(offline=wifi_off, hang=bool(faults.get("grinder_timeout")),
                        stuck_on=bool(faults.get("grinder_stuck_on"))),
        "coffee": dict(offline=wifi_off, hang=bool(faults.get("coffee_machine_timeout"))),
        "water": dict(offline=wifi_off, hang=bool(faults.get("hot_water_timeout"))),
    }


def _make_sim(name, faults, time_scale=SIM_TIME_SCALE):
    kw = _sim_kwargs(faults)
    if name == "arm":
        return _sim.SimRobotArm(**kw["arm"], latency=max(0.01, 2.0 * time_scale))
    if name == "cup":
        return _sim.SimCupDetector(**kw["cup"])
    if name == "grinder":
        return _sim.SimGrinder(time_scale=time_scale, **kw["grinder"])
    if name == "coffee":
        return _sim.SimCoffeeMachine(time_scale=time_scale, **kw["coffee"])
    if name == "water":
        return _sim.SimHotWater(time_scale=time_scale, **kw["water"])
    raise ValueError(f"未知设备 {name}")


def _make_real(name, cfg):
    """真实适配器。真机未验证的只保证可构造，connect() 才碰硬件。"""
    if name == "arm":
        from .sts_arm import StsRobotArm
        return StsRobotArm(cfg, default_poses())
    if name == "cup":
        from .vision_cup import VisionCupDetector
        return VisionCupDetector(cfg)
    if name in ("grinder", "coffee", "water"):
        from .wifi_plug import WifiSmartSwitch
        key = {"grinder": "grinder", "coffee": "brewer", "water": "water"}[name]
        actcfg = dict(cfg.get("actuators", {}).get(key, {}))
        actcfg.setdefault("driver", "tasmota")
        cls = {"grinder": Grinder, "coffee": CoffeeMachine, "water": HotWater}[name]
        mode = actcfg.pop("mode", "power")
        run_sec = actcfg.pop("run_sec", 10.0)
        press_sec = actcfg.pop("press_sec", 1.0)
        return cls(WifiSmartSwitch(key, actcfg),
                   mode=mode, run_sec=run_sec, press_sec=press_sec)
    raise ValueError(f"未知设备 {name}")


def make_devices(mode="SIM", cfg=None, faults=None, time_scale=SIM_TIME_SCALE):
    """组装全部设备并返回 dict：{"arm","cup","grinder","coffee","water"}。
    只构造不 connect——connect 由使用方（FSM/自检）显式调用。"""
    mode = (mode or "SIM").upper()
    if mode not in ("SIM", "REAL", "HYBRID"):
        raise ValueError(f"未知模式 {mode}（应为 SIM/REAL/HYBRID）")
    cfg = cfg or default_fsm_config()
    faults = faults or {}
    per = cfg.get("devices", {}) if mode == "HYBRID" else {}
    devices = {}
    for name in ("arm", "cup", "grinder", "coffee", "water"):
        use_real = (mode == "REAL") or (mode == "HYBRID" and per.get(name) == "real")
        devices[name] = _make_real(name, cfg) if use_real else _make_sim(name, faults, time_scale)
        devices[name].mode_tag = "real" if use_real else "sim"
    log("FACTORY", f"模式 {mode}: "
                   + ", ".join(f"{n}={'真' if d.mode_tag == 'real' else '模拟'}"
                               for n, d in devices.items()))
    if faults:
        log("FACTORY", f"故障注入: {', '.join(faults.keys())}")
    return devices


def connect_all(devices, strict=True):
    """批量 connect。strict=True 任一失败即抛；False 则记录并返回 {name: bool}。"""
    result = {}
    for name, dev in devices.items():
        try:
            dev.connect()
            result[name] = True
        except Exception as e:
            log("FACTORY", f"{name} 连接失败: {e}")
            result[name] = False
            if strict:
                raise
    return result
