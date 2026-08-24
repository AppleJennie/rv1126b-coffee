# hardware/arm.py —— 机械臂统一接口
# 业务层只允许语义动作（TASK 20）：禁止出现舵机角度/ID。
# 角度映射只在具体驱动（StsRobotArm）内部，配置见 config/poses.yaml。

from .base import Device


class RobotArm(Device):
    """机械臂抽象：语义动作 + 运动状态查询。"""

    # 语义位姿名（对应 config/poses.yaml 的键）
    POSES = ("HOME", "CUP", "BREWER", "WATER", "SERVE")

    def home(self):
        """回待机位。"""
        raise NotImplementedError

    def move_to(self, pose):
        """移动到语义位姿（HOME/CUP/BREWER/WATER/SERVE）。"""
        raise NotImplementedError

    def pick_cup(self, correction_mm=None):
        """取杯：下抓 + 夹爪闭合。correction_mm=(dx,dy) 视觉纠偏量。"""
        raise NotImplementedError

    def place_cup(self):
        """把杯放到冲泡位并松开夹爪。"""
        raise NotImplementedError

    def pour_grounds(self, steps=3):
        """倒粉：分 steps 步慢倒。"""
        raise NotImplementedError

    def pick_finished_drink(self):
        """取成品杯（冲泡完成后取回）。"""
        raise NotImplementedError

    def serve(self):
        """递杯到出餐位并松开。"""
        raise NotImplementedError

    def release(self):
        """松开夹爪（当前位置不动臂）。"""
        raise NotImplementedError

    def emergency_stop(self):
        """急停：立即停一切动作并卸力。必须幂等、永不抛异常。"""
        raise NotImplementedError

    def is_moving(self):
        """是否运动中（供 Watchdog 检测长期 BUSY）。"""
        return False
