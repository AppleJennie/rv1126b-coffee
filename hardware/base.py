# hardware/base.py —— 设备抽象基类与统一异常
# 所有硬件适配器（真实/模拟）都实现同一套接口，业务层只依赖这里的抽象。

import os
import sys
import time

# TASK 28：log() 转发统一结构化日志（projects/common）；导入失败回退原 print，
# 任何日志层问题都不许影响硬件层可用性。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from projects.common.structured_log import make_logger as _make_logger
    _slog = _make_logger("hardware")
except Exception:
    _slog = None


class DeviceError(Exception):
    """设备操作失败。retryable=True 表示可安全重试（如暂时无应答）。"""

    def __init__(self, msg, retryable=False):
        super().__init__(msg)
        self.retryable = retryable


class DeviceTimeout(DeviceError):
    """设备超时：无应答、长期 BUSY、动作未在限定时间内完成。"""

    def __init__(self, msg):
        super().__init__(msg, retryable=True)


class EstopError(DeviceError):
    """急停触发：所有动作必须立即停止，不可自动重试。"""

    def __init__(self, msg="急停触发"):
        super().__init__(msg, retryable=False)


class Device:
    """统一设备接口。子类按需覆写；默认实现保证 Sim 设备最小可用。"""

    name = "device"
    critical = True       # 关键设备：offline 时 Health Manager 禁止接新订单

    def __init__(self):
        self._connected = False

    # ---- 生命周期 ----
    def connect(self):
        """建立连接，成功返回 True，失败抛 DeviceError。"""
        self._connected = True
        return True

    def start(self):
        """上电/使能（默认无操作）。"""

    def stop(self):
        """停止当前动作（默认无操作；机械臂等必须覆写）。"""

    def reset(self):
        """故障后恢复到可用状态（默认无操作）。"""

    # ---- 状态 ----
    def health(self):
        """健康检查：{ok, detail, ts}。"""
        return {"ok": self._connected, "detail": "", "ts": time.time()}

    def status(self):
        """状态快照：{online, state}，子类可扩展字段。"""
        return {"online": self._connected, "state": "idle"}

    @property
    def online(self):
        return self._connected


def log(tag, msg):
    """硬件层统一日志格式（与 fsm.py 风格一致，保持日志解析兼容）。
    TASK 28：内部转发统一结构化日志——控制台行逐字不变，同时写 logs/*.jsonl。"""
    if _slog is not None:
        _slog(tag, msg)
    else:
        print(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}", flush=True)
