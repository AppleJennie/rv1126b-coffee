# hardware —— 硬件适配层（TASK 2）
# 业务代码只依赖这里的接口，不直接依赖某款机械臂/某厂商插座。

from .base import Device, DeviceError, DeviceTimeout, EstopError, log
from .arm import RobotArm
from .cup import CupDetector
from .machines import Appliance, CoffeeMachine, Grinder, HotWater, SmartSwitch
from .sim import (SimCoffeeMachine, SimCupDetector, SimGrinder, SimHotWater,
                  SimRobotArm, SimSmartSwitch)
from .factory import connect_all, load_scenario, make_devices

__all__ = [
    "Device", "DeviceError", "DeviceTimeout", "EstopError", "log",
    "RobotArm", "CupDetector",
    "SmartSwitch", "Appliance", "Grinder", "CoffeeMachine", "HotWater",
    "SimRobotArm", "SimCupDetector", "SimSmartSwitch",
    "SimGrinder", "SimCoffeeMachine", "SimHotWater",
    "make_devices", "connect_all", "load_scenario",
]
