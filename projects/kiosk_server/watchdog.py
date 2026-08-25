# watchdog.py —— 软件看门狗（TASK 25）
#
# 监控项（每 interval 秒巡检一轮，阈值均可经构造参数配置）：
#   1. 主制作线程卡死：当前订单在某步骤停留 > order_stuck_sec（默认 120s）
#      且无任何事件推进（以订单 _step_ts 为准——progress/brew_tick 都会刷新它）
#   2. 设备长期 BUSY：当前订单制作总时长 > busy_sec（默认 300s，
#      远大于 SIM 单杯 19s；真机长流程可调大）
#   3. 健康巡检线程活性：HealthManager 各项最后巡检时间戳距今超
#      health_stale_sec（默认 10s，正常 2s 一轮）视为巡检线程异常
#   4. HTTP 服务活性：本机自连 http://127.0.0.1:<port>/api/status 超时
#      （http_timeout 默认 3s）视为 HTTP 异常；port 为 None 时跳过
#
# 检测到异常的动作：记 ERROR 日志 + 自有状态标记 degraded（经 section()
# 暴露给 /api/health 与 /api/status 的 watchdog 段）。**不做自动重启进程**——
# 进程级自愈在真机上由 systemd Restart=always 兜底（见 deploy/cafe-backend.service），
# 应用内 watchdog 只负责发现与暴露，避免应用层自杀掩盖根因。
#
# 本模块只做探测与记录，绝不向调用方抛异常（任何一轮出错吞掉记日志）。

import threading
import time
import urllib.request

CHECK_INTERVAL_SEC = 5.0     # 巡检周期
ORDER_STUCK_SEC = 120.0      # 订单步骤停留阈值（无事件推进视为卡死）
BUSY_SEC = 300.0             # 单杯制作总时长阈值（长期 BUSY）
HEALTH_STALE_SEC = 10.0      # 健康巡检线程活性阈值（正常 2s 一轮）
HTTP_TIMEOUT_SEC = 3.0       # HTTP 自连超时


def log(tag, msg):
    print(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}", flush=True)


class Watchdog:
    """软件看门狗。线程安全：状态读写都在 self._lock 内。"""

    def __init__(self, mgr=None, health=None, port=None,
                 interval=CHECK_INTERVAL_SEC,
                 order_stuck_sec=ORDER_STUCK_SEC,
                 busy_sec=BUSY_SEC,
                 health_stale_sec=HEALTH_STALE_SEC,
                 http_timeout=HTTP_TIMEOUT_SEC):
        self.mgr = mgr            # OrderManager（None=不监控制作线程）
        self.health = health      # HealthManager（None=不监控健康巡检活性）
        self.port = port          # HTTP 自连端口（None=不监控 HTTP）
        self.interval = interval
        self.order_stuck_sec = order_stuck_sec
        self.busy_sec = busy_sec
        self.health_stale_sec = health_stale_sec
        self.http_timeout = http_timeout
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        now = time.time()
        self._state = {
            "state": "ok",                 # ok | degraded
            "reasons": [],                 # 当前异常原因列表（人类可读）
            "last_healthy_ts": now,        # 最近一次全项正常的时刻
            "checked_ts": now,             # 最近一次巡检时刻
        }

    # ---------- 生命周期 ----------
    def start(self):
        """启动后台巡检线程（幂等）。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="watchdog")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as e:      # 双保险：看门狗自身绝不崩服务
                log("WATCHDOG", f"巡检轮异常（已吞没）: {e}")
            self._stop.wait(self.interval)

    # ---------- 巡检 ----------
    def check_once(self):
        """同步巡检一轮（测试可直接调用）。返回当前异常原因列表。"""
        now = time.time()
        reasons = []

        # 1/2. 制作线程卡死 / 设备长期 BUSY
        if self.mgr is not None:
            probe = self.mgr.activity_probe()
            if probe["busy"]:
                if probe["step_age"] > self.order_stuck_sec:
                    reasons.append(
                        f"制作卡死：订单 #{probe['order_id']} 在当前步骤已停留 "
                        f"{probe['step_age']:.0f}s（阈值 {self.order_stuck_sec:.0f}s，"
                        "无任何事件推进）")
                if probe["make_age"] > self.busy_sec:
                    reasons.append(
                        f"设备长期 BUSY：订单 #{probe['order_id']} 制作总时长 "
                        f"{probe['make_age']:.0f}s（阈值 {self.busy_sec:.0f}s）")

        # 3. 健康巡检线程活性：取各项最后巡检时间戳的最大值
        if self.health is not None:
            try:
                snap = self.health.snapshot()
                stamps = [it.get("ts") or 0 for it in snap.get("items", {}).values()]
                last = max(stamps) if stamps else 0
                if now - last > self.health_stale_sec:
                    reasons.append(
                        f"健康巡检线程无响应：最后巡检距今 {now - last:.0f}s"
                        f"（阈值 {self.health_stale_sec:.0f}s）")
            except Exception as e:
                reasons.append(f"健康巡检快照异常: {e}")

        # 4. HTTP 服务活性（本机自连 /api/status）
        if self.port is not None:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/api/status",
                        timeout=self.http_timeout) as resp:
                    if resp.status != 200:
                        reasons.append(f"HTTP 自连返回 {resp.status}")
            except Exception as e:
                reasons.append(f"HTTP 服务无响应: {e}")

        with self._lock:
            prev = self._state["state"]
            self._state["reasons"] = reasons
            self._state["checked_ts"] = now
            if reasons:
                self._state["state"] = "degraded"
            else:
                self._state["state"] = "ok"
                self._state["last_healthy_ts"] = now
            cur = self._state["state"]
        if cur != prev:
            # 状态翻转：进入 degraded 记 ERROR，恢复记 INFO 级日志
            if cur == "degraded":
                log("ERROR", "watchdog 检出异常，系统标记 degraded: " + "；".join(reasons))
            else:
                log("WATCHDOG", "全部监控项恢复正常")
        elif cur == "degraded":
            log("ERROR", "watchdog 异常持续中: " + "；".join(reasons))
        return list(reasons)

    # ---------- 对外状态 ----------
    def section(self):
        """/api/health 与 /api/status 的 watchdog 段。"""
        with self._lock:
            return dict(self._state, reasons=list(self._state["reasons"]))
