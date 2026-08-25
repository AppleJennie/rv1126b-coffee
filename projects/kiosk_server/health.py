# health.py —— 系统健康管理器（TASK 24）+ 开机自检（TASK 26）
#
# 职责：
#   1. HealthManager：后台线程每 2s 巡检 8 项
#      camera / npu / arm(机械臂) / coffee(咖啡机) / grinder(磨豆机)
#      / water(热水) / network / storage
#   2. 每项巡检结果：ok / degraded / offline / unknown + 最后检查时间 + 说明
#   3. 接单闸门：关键设备（arm/coffee/grinder/water）degraded/offline 时
#      禁止接新订单（OrderManager.place_order 调用 can_accept_order()），
#      制作中的订单不受影响
#   4. run_selfcheck()：kiosk 启动时逐项打印开机自检结果，
#      全过 -> READY；部分缺失 -> DEMO MODE（只降级，绝不退出进程）
#
# 探测方式：
#   - arm/coffee/grinder/water 走 hardware/factory.make_devices 组装的设备
#     （SIM 模式 = 模拟器，故障注入场景同样生效；HYBRID/REAL = 真实适配器），
#     巡检用 dev.health()，断线设备每轮尝试重连一次（模拟器重连便宜；
#     真机 connect 失败只记录 offline，不影响巡检线程）
#   - camera 查 /dev/video0（或任一 /dev/video*，可用 camera_dev="sim" 配置为模拟）
#   - npu 查 /sys 下 rknn/devfreq 设备节点，查不到记 unknown（非 RV1126B 环境）
#   - network 查 /proc/net/route 默认路由
#   - storage 查磁盘剩余空间阈值
#
# 注意：本模块只做探测与裁决，绝不向调用方抛异常——巡检线程任何一轮
# 出错都被吞掉记日志，不许拖垮 kiosk 主服务。

import glob
import os
import shutil
import socket
import struct
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hardware.base import log                      # noqa: E402
from hardware.factory import load_scenario, make_devices   # noqa: E402

CHECK_INTERVAL_SEC = 2.0     # 巡检周期
STORAGE_MIN_FREE_MB = 200    # 磁盘剩余低于该值记 degraded

# 状态等级
OK = "ok"
DEGRADED = "degraded"
OFFLINE = "offline"
UNKNOWN = "unknown"

# 巡检项表：(键, 展示名, 是否关键设备)
# 关键设备异常 -> 接单闸门拒单 + 整体 OFFLINE；非关键项只影响 DEGRADED。
ITEMS = [
    ("camera", "Camera", False),
    ("npu", "NPU", False),
    ("arm", "Robot Arm", True),
    ("coffee", "Coffee Machine", True),
    ("grinder", "Grinder", True),
    ("water", "Hot Water", True),
    ("network", "Network", False),
    ("storage", "Storage", False),
]

OVERALL_TEXT = {"READY": "SYSTEM READY",
                "DEGRADED": "SYSTEM DEGRADED",
                "OFFLINE": "SYSTEM OFFLINE"}


class HealthManager:
    """系统健康巡检器。线程安全：结果读写都在 self._lock 内。"""

    def __init__(self, mode="SIM", scenario=None, interval=CHECK_INTERVAL_SEC,
                 camera_dev=None, storage_path="/",
                 storage_min_mb=STORAGE_MIN_FREE_MB):
        self.mode = (mode or "SIM").upper()
        # 全模拟模式下，本机外设（camera 等）探测不到记 unknown 而非 offline：
        # SIM 本来就允许没有真硬件，整体状态不应被一个可有可无的外设拖黄。
        self.sim_like = self.mode == "SIM"
        self.interval = interval
        self.camera_dev = camera_dev          # None=自动探测 /dev/video*；"sim"=按模拟
        self.storage_path = storage_path
        self.storage_min_mb = storage_min_mb
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._items = {
            name: {"label": label, "critical": crit,
                   "status": UNKNOWN, "detail": "尚未检查", "ts": None}
            for name, label, crit in ITEMS
        }
        # 健康巡检专用设备组（与每单 cafe_fsm 子进程自建的设备互不相干）
        faults = load_scenario(scenario) if scenario else {}
        self.devices = make_devices(self.mode, faults=faults)
        self._probes = {
            "camera": self._check_camera,
            "npu": self._check_npu,
            "arm": lambda: self._check_device("arm"),
            "coffee": lambda: self._check_device("coffee"),
            "grinder": lambda: self._check_device("grinder"),
            "water": lambda: self._check_device("water"),
            "network": self._check_network,
            "storage": self._check_storage,
        }

    # ---------- 生命周期 ----------
    def start(self):
        """启动后台巡检线程（幂等）。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="health-check")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as e:      # 双保险：单项已各自 try，整轮异常也吞掉
                log("HEALTH", f"巡检轮异常（已吞没，不影响服务）: {e}")
            self._stop.wait(self.interval)

    # ---------- 巡检 ----------
    def check_once(self):
        """同步巡检一轮（开机自检复用本轮作为首轮数据）。"""
        for name, _, _ in ITEMS:
            try:
                status, detail = self._probes[name]()
            except Exception as e:
                status, detail = DEGRADED, f"巡检异常: {e}"
            with self._lock:
                it = self._items[name]
                it["status"] = status
                it["detail"] = detail
                it["ts"] = time.time()

    def _check_device(self, name):
        """hardware 设备健康检查：断线先尝试重连，再读 health()。"""
        dev = self.devices.get(name)
        if dev is None:
            return UNKNOWN, "无此设备"
        if not dev.online:
            try:
                dev.connect()
            except Exception as e:
                return OFFLINE, f"连接失败: {e}"
        h = dev.health()
        tag = "（模拟）" if getattr(dev, "mode_tag", "") == "sim" else ""
        # 有些 health() detail 以 ": " 结尾（开关明细为空时），先修边再拼模拟标记
        detail = (h.get("detail") or "").strip().rstrip(":：").strip()
        if h.get("ok"):
            return OK, (detail or "正常") + tag
        return OFFLINE, (detail or "health() 未通过") + tag

    def _check_camera(self):
        if self.camera_dev == "sim":
            return OK, "模拟摄像头（camera_dev 配置为 sim）"
        cands = [self.camera_dev] if self.camera_dev else ["/dev/video0"]
        cands += sorted(glob.glob("/dev/video*"))
        for p in cands:
            if p and os.path.exists(p):
                return OK, f"检测到设备节点 {p}"
        if self.sim_like:
            return UNKNOWN, "未探测到摄像头（SIM 模式按模拟继续）"
        return OFFLINE, "未找到 /dev/video* 设备节点"

    def _check_npu(self):
        """RV1126B 的 RK NPU 没有统一设备节点，综合几处 /sys 痕迹判断。"""
        cands = glob.glob("/sys/class/devfreq/*npu*")
        cands += glob.glob("/sys/bus/platform/devices/*npu*")
        cands += [p for p in ("/dev/rknpu", "/sys/kernel/debug/rknpu")
                  if os.path.exists(p)]
        if cands:
            return OK, f"检测到 NPU 节点 {cands[0]}"
        return UNKNOWN, "未探测到 NPU 节点（非 RV1126B 环境或按模拟运行）"

    def _check_network(self):
        """查 /proc/net/route 是否存在默认路由（不 ping，避免巡检线程阻塞）。"""
        try:
            with open("/proc/net/route", "r", encoding="utf-8") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    # Destination==00000000 即默认路由，Gateway 是小端 hex
                    if len(parts) > 2 and parts[1] == "00000000":
                        gw = socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
                        return OK, f"默认路由 via {gw} dev {parts[0]}"
        except OSError as e:
            return UNKNOWN, f"无法读取 /proc/net/route: {e}"
        return OFFLINE, "无默认路由（网络未连接）"

    def _check_storage(self):
        usage = shutil.disk_usage(self.storage_path)
        free_mb = usage.free / 1048576.0
        if free_mb < self.storage_min_mb:
            return DEGRADED, (f"磁盘剩余 {free_mb:.0f} MB "
                              f"低于阈值 {self.storage_min_mb} MB")
        return OK, f"磁盘剩余 {free_mb / 1024.0:.1f} GB"

    # ---------- 对外快照 ----------
    def _overall_locked(self):
        """整体裁决（须持锁）。unknown 视为信息项，不参与降级。"""
        crit_bad = False
        crit_degraded = False
        other_bad = False
        for name, _, crit in ITEMS:
            st = self._items[name]["status"]
            if crit and st == OFFLINE:
                crit_bad = True
            elif crit and st == DEGRADED:
                crit_degraded = True
            elif not crit and st in (OFFLINE, DEGRADED):
                other_bad = True
        if crit_bad:
            return "OFFLINE"
        if crit_degraded or other_bad:
            return "DEGRADED"
        return "READY"

    def _headline_locked(self):
        """角标文案：READY -> SYSTEM READY；有离线项 -> 'X Offline'；其余 DEGRADED。"""
        overall = self._overall_locked()
        if overall == "READY":
            return OVERALL_TEXT["READY"]
        for name, label, crit in ITEMS:
            if self._items[name]["status"] == OFFLINE:
                return f"{label} Offline"
        return OVERALL_TEXT[overall]

    def snapshot(self):
        """/api/health 完整快照。"""
        with self._lock:
            overall = self._overall_locked()
            return {
                "overall": overall,
                "overall_text": OVERALL_TEXT[overall],
                "headline": self._headline_locked(),
                "blocking": self._blocking_locked() is not None,
                "mode": self.mode,
                "items": {name: dict(it) for name, it in self._items.items()},
                "ts": int(time.time()),
            }

    def summary(self):
        """/api/status 内嵌的健康摘要（小字段，不动原有结构）。"""
        with self._lock:
            overall = self._overall_locked()
            return {"overall": overall,
                    "overall_text": OVERALL_TEXT[overall],
                    "blocking": self._blocking_locked() is not None}

    # ---------- 接单闸门 ----------
    def _blocking_locked(self):
        """返回首个异常关键设备 (name, item)；全部正常返回 None。须持锁。"""
        for name, _, crit in ITEMS:
            if not crit:
                continue
            it = self._items[name]
            if it["status"] in (DEGRADED, OFFLINE):
                return name, it
        return None

    def can_accept_order(self):
        """接单闸门：任一关键设备 degraded/offline 即拒绝新订单。
        返回 (可否接单, reason, detail)；reason 形如 health_arm_offline。"""
        with self._lock:
            hit = self._blocking_locked()
            if hit is None:
                return True, None, None
            name, it = hit
            reason = f"health_{name}_{it['status']}"
            detail = f"{it['label']} {it['status']}: {it['detail']}"
            return False, reason, detail

    def blocking_detail(self):
        """拒单时给前端的明细文案（全部异常关键设备）。"""
        with self._lock:
            bad = []
            for name, _, crit in ITEMS:
                if not crit:
                    continue
                it = self._items[name]
                if it["status"] in (DEGRADED, OFFLINE):
                    bad.append(f"{it['label']} {it['status']}（{it['detail']}）")
            return "; ".join(bad)


# =====================================================================
# TASK 26：开机自检
# =====================================================================

# 自检打印序列：(打印名, 巡检项键)；AUDIO 单独探测，不在巡检 8 项内
_SELFCHECK_STEPS = [
    ("CHECK CAMERA", "camera"),
    ("CHECK NPU", "npu"),
    ("CHECK ROBOT", "arm"),
    ("CHECK COFFEE MACHINE", "coffee"),
    ("CHECK GRINDER", "grinder"),
    ("CHECK HOT WATER", "water"),
    ("CHECK AUDIO", None),
    ("CHECK NETWORK", "network"),
    ("CHECK STORAGE", "storage"),
]


def _probe_audio():
    """声卡探测：/dev/snd 或 /proc/asound/cards 有真实声卡。"""
    if os.path.isdir("/dev/snd"):
        return True, "/dev/snd"
    try:
        with open("/proc/asound/cards", "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content and "no soundcards" not in content.lower():
            return True, "/proc/asound/cards"
    except OSError:
        pass
    return False, "未探测到声卡"


def run_selfcheck(hm):
    """开机自检：逐项打印结果，返回 "READY" 或 "DEMO MODE"。
    复用 HealthManager 首轮巡检数据（/api/health 看到的就是这轮结果）。
    任何缺失都只降级为 DEMO MODE，绝不让 kiosk 启动失败退出。"""
    log("SELFCHECK", "开机自检开始 " + "=" * 30)
    hm.check_once()                       # 首轮同步巡检
    snap = hm.snapshot()
    problems = []                          # 导致 DEMO MODE 的原因列表
    for title, key in _SELFCHECK_STEPS:
        if key is None:                    # AUDIO：本机探测，不在巡检项内
            ok, detail = _probe_audio()
            word = "OK" if ok else ("SIM" if hm.sim_like else "MISSING")
            if not ok:
                problems.append(f"AUDIO {detail}" + ("（按模拟继续）" if hm.sim_like else ""))
        else:
            it = snap["items"][key]
            st = it["status"]
            word = {"ok": "OK", "degraded": "WARN",
                    "offline": "FAIL", "unknown": "SIM"}[st]
            if st == "ok":
                detail = it["detail"]
            elif st == "unknown":
                detail = it["detail"]      # SIM 模式允许的缺省项
                problems.append(f"{title} {detail}")
            else:
                detail = it["detail"]
                problems.append(f"{title} {st}: {detail}")
        log("SELFCHECK", f"{title:<22} {word:<4} {detail}")
    if problems:
        verdict = "DEMO MODE"
        log("SELFCHECK", f"自检完成：DEMO MODE（{'；'.join(problems)}），服务继续启动")
    else:
        verdict = "READY"
        log("SELFCHECK", "自检完成：READY（全部检查通过）")
    log("SELFCHECK", "=" * 46)
    return verdict
