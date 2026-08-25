#!/usr/bin/env python3
# kiosk_server.py —— 点单屏后台服务（运行在 RV1126B 开发板上，纯 Python3 标准库）
#
# 职责：
#   1. 托管点单屏页面 coffee_kiosk.html（浏览器/板载屏幕通过 http 访问）
#   2. 提供 JSON API：查菜单、下单、取消订单、查状态、维护模式切换
#   3. 通过 SSE（Server-Sent Events）向页面实时推送制作进度/机器状态/订单状态
#   4. 订单排队，逐单驱动机械臂主控 fsm.py（--simulate 时用内置仿真时间线）
#
# 运行：
#   python3 kiosk_server.py --simulate          # 无硬件仿真（开发调试用，内置 19s 时间线）
#   python3 kiosk_server.py                     # 真机：调用 ../coffee_fsm/fsm.py run
#   python3 kiosk_server.py --port 8080 --host 0.0.0.0 --timeout 600
#   python3 kiosk_server.py --mode SIM                            # TASK 27：制作后端切到
#   python3 kiosk_server.py --mode HYBRID [--scenario xx.yaml]    # cafe_fsm.py 子进程，
#                                                                 # 解析 [EVENT] JSON 驱动 SSE
#
# --mode {SIM,HYBRID,REAL}（TASK 27 模式机制）：
#   指定后每单 fork ../coffee_fsm/cafe_fsm.py make --drink <id> --order-id <oid>
#   --mode <mode> [--scenario <yaml>]，逐行解析 stdout 的 [EVENT] {json}：
#   state 事件映射到屏幕 4 步进度，brew_tick 直接提供 remain_sec，
#   result 事件（completed/failed/estop）决定 done/error。HYBRID 的设备真/假
#   配置由 cafe_fsm 读 projects/coffee_fsm/config.json 的 devices 段，kiosk 只透传 --mode。
#   不填 --mode 时保持旧行为：--simulate 内置时间线 / 真机 fsm.py 日志正则解析。
#
# API 一览（详见 docs/04-WiFi与网页通讯设计.md）：
#   GET  /                 -> 点单屏页面
#   GET  /api/menu         -> {categories, menu, machine}
#   POST /api/order        -> 请求 {drink_id, opts:{cup,temp,sugar,extras}, qty}
#                             响应 {ok, order_id, pickup_no, total} 或 {ok:false, reason}
#                             （队列满返回 409 {ok:false, reason:"queue_full"}）
#   POST /api/order/cancel -> 请求 {order_id}，仅排队中(queued)可取消；
#                             制作中拒绝 409 {ok:false, reason:"already_preparing"}
#   GET  /api/status       -> {machine, queue_len, current, queue[], ready[], health, watchdog}
#                             队列快照（含 eta_sec）；ready=TASK 9 待取订单（断线恢复用）
#   GET  /api/health       -> TASK 24：8 项巡检明细 + overall(SYSTEM READY/DEGRADED/OFFLINE)
#                             （TASK 25 起追加 watchdog 段：state/reasons/last_healthy_ts）
#   GET  /api/events       -> SSE 事件流（hello / machine / progress / done / error / order_state
#                             + TASK 9 新增：snapshot 首帧快照 / ORDER_CREATED 等统一大写事件）
#                             支持 Last-Event-ID 头或 ?last_id=N 重放断线缺口（环形缓冲 300 条）
#   POST /api/machine      -> {state:"ok|nowater|nobeans"} 维护模拟（将来由水位/豆位传感器驱动）
#   GET  /admin            -> TASK 34：管理后台页（鉴权见 _admin_ok）
#   GET  /api/admin/stats  -> TASK 34/35：统计 JSON（今日订单/成功率/TOP 饮品/失败原因/近 50 单）
#   POST /api/admin/mode   -> {mode:SIM|HYBRID} 切换**下一单**制作后端（不热切换当前单）
#   POST /api/admin/reinit -> 重新初始化设备（触发 HealthManager 重检一轮）
#
# TASK 25 软件 watchdog（watchdog.py）：监控制作线程卡死/设备长期 BUSY/
# 健康巡检线程活性/HTTP 自连活性，异常记 ERROR 日志并标记 degraded，
# 不做自动重启（真机由 systemd Restart 兜底，见 deploy/cafe-backend.service）。
#
# 订单状态机：
#   queued(排队) -> preparing(制作中) -> ready(可取餐) -> completed(已取走/超时归档)
#   queued -> cancelled(取消) ；preparing -> failed(制作失败)
#   订单进 completed/failed/cancelled 时落 SQLite 统计库（TASK 35，stats.py；
#   隐私红线：不存任何人脸/视觉数据）

import argparse
import json
import os
import queue
import random
import re
import select
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# TASK 24/26：健康管理器 + 开机自检（同目录模块，无循环依赖）
from health import HealthManager, run_selfcheck
# TASK 25：软件看门狗；TASK 35：SQLite 统计；TASK 34：管理后台页模板
from watchdog import Watchdog
from stats import Stats
from admin_page import ADMIN_HTML

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "..", "ui_prototype", "coffee_kiosk.html")
MENU_PATH = os.path.join(BASE_DIR, "..", "ai_host", "menu.json")
FSM_DIR = os.path.join(BASE_DIR, "..", "coffee_fsm")
FSM_PY = os.path.join(FSM_DIR, "fsm.py")
CAFE_FSM_PY = os.path.join(FSM_DIR, "cafe_fsm.py")

# 屏幕制作步骤与机械臂 FSM 状态的映射（真机模式解析 fsm.py 日志用）
# 屏幕步骤：取杯 -> 磨豆 -> 冲泡 -> 出品
STEP_NAMES = ["取杯", "磨豆", "冲泡", "出品"]
STATE_TO_STEP = {
    "LOCATE_CUP": 0, "PICK_CUP": 0, "PLACE_CUP": 0,
    "PRESS_GRINDER": 1, "POUR_GROUNDS": 1,
    "PRESS_BREWER": 2, "WAIT_BREW": 2,
    "SERVE": 3,
}
# cafe_fsm.py（--mode 模式）状态 -> 屏幕步骤的映射（TASK 27）。
# 未列出的状态不推 progress：ORDER_RECEIVED（起步阶段）、RECOVERY（保持当前步骤）、
# COMPLETE/ERROR/EMERGENCY_STOP/IDLE（由 result 事件统一收尾）。
CAFE_STATE_TO_STEP = {
    "CHECK_SYSTEM": 0, "CHECK_CUP": 0, "PICK_CUP": 0, "MOVE_TO_MACHINE": 0,
    "GRIND": 1,
    "BREW": 2, "WAIT_BREW": 2,
    "PICK_FINISHED_DRINK": 3, "MOVE_TO_SERVE": 3, "SERVE": 3,
    "WAIT_CUSTOMER_PICKUP": 3,
}

# =====================================================================
# TASK 9：统一实时事件枚举（新增 SSE 事件类型，与旧事件**并列**发送）
#   ORDER_CREATED   下单成功（进队列）          —— 对应旧 order_state(queued)
#   ORDER_STARTED   开始制作                    —— 对应旧 order_state(preparing)
#   ARM_MOVING      机械臂移动（取杯/送杯）     —— 对应旧 progress(step 0/3 臂动作段)
#   GRINDING        磨豆                        —— 对应旧 progress(step 1)
#   BREWING         冲泡                        —— 对应旧 progress(step 2)
#   SERVING         出品/等待取餐               —— 对应旧 progress(step 3 出品段)
#   READY           制作完成待取餐              —— 对应旧 done / order_state(ready)
#   ORDER_COMPLETE  订单归档（已取走/超时）     —— 对应旧 order_state(completed)
#   ERROR           制作失败                    —— 对应旧 error / order_state(failed)
# 兼容契约：旧事件（hello/machine/progress/done/error/order_state）原样照发，
# 新事件额外并列发；前端按能力消费（旧前端忽略未知类型，新前端可用统一事件）。
# =====================================================================
UNIFIED_EVENTS = ("ORDER_CREATED", "ORDER_STARTED", "ARM_MOVING", "GRINDING",
                  "BREWING", "SERVING", "READY", "ORDER_COMPLETE", "ERROR")

# cafe_fsm 状态 -> 统一事件（与 CAFE_STATE_TO_STEP 并列的事件表：
# 进度步表只回答"屏上点亮第几步"，本表回答"发生了什么机器动作"；
# ORDER_RECEIVED/CHECK_SYSTEM 属起步自检不映射，RECOVERY 保持当前不映射，
# COMPLETE/ERROR/EMERGENCY_STOP 由 result 事件统一收尾成 READY/ERROR）
CAFE_STATE_TO_UNIFIED = {
    "CHECK_CUP": "ARM_MOVING", "PICK_CUP": "ARM_MOVING", "MOVE_TO_MACHINE": "ARM_MOVING",
    "GRIND": "GRINDING",
    "BREW": "BREWING", "WAIT_BREW": "BREWING",
    "PICK_FINISHED_DRINK": "ARM_MOVING", "MOVE_TO_SERVE": "ARM_MOVING",
    "SERVE": "SERVING", "WAIT_CUSTOMER_PICKUP": "SERVING",
}
# 旧路径（--simulate 内置时间线 / fsm.py 日志解析）屏幕步骤 -> 统一事件
STEP_TO_UNIFIED = ["ARM_MOVING", "GRINDING", "BREWING", "SERVING"]
# 仿真模式每步耗时（秒）
SIM_STEP_SEC = [3, 5, 8, 3]
SIM_TOTAL_SEC = sum(SIM_STEP_SEC)   # 单杯预计制作时长 19s，估算排队等待时间用

QUEUE_MAX = 10          # 订单上限：制作中 + 排队中合计，满了拒单 queue_full
READY_TTL_SEC = 300     # 做好了长时间没人取，超时自动归档为 completed
MAKE_TIMEOUT_SEC = 600  # 真机模式单杯制作超时（--timeout 可改），超时杀子进程按失败处理


def log(tag, msg):
    print(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}", flush=True)


# =====================================================================
# 事件总线：SSE 广播（一个屏幕或多个调试端都能收到）
# =====================================================================

class EventBus:
    """SSE 广播总线。TASK 9 起增加：
    - 每条事件分配单调递增 id 并写入 SSE `id:` 行，浏览器 EventSource 自动记录
      lastEventId，断线重连时经 Last-Event-ID 头回传；
    - 最近事件环形缓冲（默认 300 条），供重连时重放缺口。"""

    def __init__(self, history=300):
        self._subs = []          # list[queue.Queue]
        self._hist = deque(maxlen=history)   # 最近事件环形缓冲
        self._seq = 0            # 事件单调递增 id
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event):
        """event: dict，自动补 id/ts。订阅者队列满则丢弃（页面卡顿不阻塞制作）。"""
        with self._lock:
            self._seq += 1
            event["id"] = self._seq          # 新增字段，旧前端忽略无影响
            event["ts"] = int(time.time())
            self._hist.append(event)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def replay_since(self, last_id):
        """重放缺口：返回缓冲中 id > last_id 的事件（按序）。last_id<=0 返回空。"""
        if last_id <= 0:
            return []
        with self._lock:
            return [e for e in self._hist if e.get("id", 0) > last_id]


# =====================================================================
# 订单管理 + 制作执行
# =====================================================================

class OrderManager:
    """订单状态机 + 单队列逐单制作（只有一条机械臂，不能并行）。
    只管订单与调度，不碰硬件；machine / current / 队列的读写全部在 self._lock 内进行。"""

    def __init__(self, bus_events, simulate=False, menu=None, make_timeout=MAKE_TIMEOUT_SEC,
                 cafe_mode=None, scenario=None, health=None, stats=None):
        self.events = bus_events
        self.simulate = simulate
        self.cafe_mode = cafe_mode    # TASK 27：SIM/HYBRID/REAL；None=旧行为（simulate/fsm.py）
        self.scenario = scenario      # 故障注入场景 yaml，仅 cafe_mode 下透传给 cafe_fsm.py
        self.health = health          # TASK 24：HealthManager，关键设备异常时拒绝接新订单
        self.stats = stats            # TASK 35：Stats，订单终结落库（None=不统计）
        self.pending_mode = None      # TASK 34：管理后台设置的下一单制作后端（不热切换当前单）
        self.menu = menu or {}
        self.make_timeout = make_timeout
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)  # 有新订单时唤醒 worker
        self.machine = "ok"                # ok | nowater | nobeans
        self._queue = deque()              # 排队中的订单（state=queued）
        self._by_id = {}                   # 活跃订单索引：queued/preparing/ready -> order
        self._seq = 0
        self.current = None                # 正在制作的订单 dict（state=preparing）
        self._worker = threading.Thread(target=self._work_loop, daemon=True)
        self._worker.start()
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self._reaper.start()

    # ---------- 下单 ----------
    def _price_of(self, drink, opts, qty):
        """服务端重算总价（不信任页面传来的金额）。"""
        total = drink["price"]
        total += 3 if opts.get("cup") == 1 else 0            # 大杯 +3
        for i in opts.get("extras", []):                     # 加料 +2/+3/+2
            total += [2, 3, 2][i] if 0 <= i <= 2 else 0
        return total * max(1, min(int(qty), 9))

    def place_order(self, body):
        drinks = {d["id"]: d for d in self.menu.get("drinks", [])}
        opts = body.get("opts") or {}
        qty = body.get("qty", 1)
        with self._cond:
            if self.machine != "ok":
                return None, f"machine_{self.machine}"
            # TASK 24 接单闸门：关键设备（臂/咖啡机/磨豆机/热水）异常禁止接新订单；
            # 制作中的订单不受影响（闸门只在下单入口）
            if self.health is not None:
                ok, reason, detail = self.health.can_accept_order()
                if not ok:
                    log("ORDER", f"健康闸门拒单: {detail}")
                    return None, reason
            active = len(self._queue) + (1 if self.current else 0)
            if active >= QUEUE_MAX:
                return None, "queue_full"
            drink = drinks.get(body.get("drink_id"))
            if drink is None:
                return None, "bad_drink"
            self._seq += 1
            order = {
                "order_id": self._seq,
                "pickup_no": str(random.randint(100, 999)),
                "drink": drink["name"],
                "opts": opts,
                "qty": qty,
                "total": self._price_of(drink, opts, qty),
                "state": "queued",
                "_drink_id": drink["id"],  # cafe_fsm.py --drink 用（内部字段不对外）
                "_step_index": 0,      # 制作进度（ETA 折算用，内部字段不对外）
                "_step_ts": None,      # 进入当前步骤的时刻
                "_start_ts": None,     # 开始制作的时刻（TASK 25 监控 / TASK 35 时长统计）
                "_ready_ts": None,     # 进入 ready 的时刻（超时归档用）
                # TASK 34：本单制作后端在下单瞬间定死（管理后台只切下一单，不热切换当前单）
                "_cafe_mode": self.pending_mode or self.cafe_mode,
            }
            self._queue.append(order)
            self._by_id[order["order_id"]] = order
            # queued 事件必须在唤醒 worker 前发，否则可能晚于 preparing 事件
            self._publish_state(order)
            # TASK 9：统一事件并列发（旧 order_state 已先发，前端按能力消费）
            self._publish_unified("ORDER_CREATED", order,
                                  drink=order["drink"], qty=qty, total=order["total"])
            self._cond.notify()
        log("ORDER", f"接单 #{order['order_id']} {drink['name']} x{qty} 取餐号 {order['pickup_no']}")
        return order, None

    def cancel_order(self, order_id):
        """仅 queued 可取消；preparing 拒绝；其余（不存在/已 ready/已终结）按 not_found。"""
        with self._cond:
            order = self._by_id.get(order_id)
            if order is None or order["state"] not in ("queued", "preparing"):
                return None, "not_found"
            if order["state"] == "preparing":
                return None, "already_preparing"
            self._queue.remove(order)
            order["state"] = "cancelled"
            self._by_id.pop(order["order_id"], None)
        self._publish_state(order)
        self._record_terminal(order, "cancelled")     # TASK 35：取消也落库
        log("ORDER", f"取消 #{order['order_id']} 取餐号 {order['pickup_no']}")
        return order, None

    # ---------- TASK 34：管理后台模式切换 ----------
    def set_pending_mode(self, mode):
        """标记下一单的制作后端（SIM/HYBRID/REAL）。只影响之后下的单，
        排队中/制作中的订单维持下单时的后端不变（不硬做热切换）。"""
        with self._lock:
            self.pending_mode = mode

    # ---------- TASK 25：watchdog 探测 ----------
    def activity_probe(self):
        """当前制作活性快照：步骤停留/制作总时长（秒），watchdog 卡死判定用。"""
        with self._lock:
            if self.current is None:
                return {"busy": False, "order_id": None, "step_age": 0.0, "make_age": 0.0}
            now = time.time()
            o = self.current
            return {
                "busy": True,
                "order_id": o["order_id"],
                "step_age": now - (o["_step_ts"] or now),
                "make_age": now - (o["_start_ts"] or now),
            }

    def set_machine(self, state):
        if state not in ("ok", "nowater", "nobeans"):
            return False
        with self._lock:
            self.machine = state
        self.events.publish({"type": "machine", "state": state})
        log("MACHINE", f"机器状态 -> {state}")
        return True

    def machine_state(self):
        with self._lock:
            return self.machine

    # ---------- 状态快照 ----------
    def _remain_sec_locked(self, order):
        """估算制作中订单的剩余秒数：按当前步骤折算 SIM_STEP_SEC 剩余，减去本步已耗时。"""
        remain = sum(SIM_STEP_SEC[order["_step_index"]:])
        if order["_step_ts"] is not None:
            remain -= time.time() - order["_step_ts"]
        return max(0, int(round(remain)))

    @staticmethod
    def _public(order):
        """对外快照：剥掉 _ 前缀的内部字段。"""
        return {k: v for k, v in order.items() if not k.startswith("_")}

    def snapshot(self):
        with self._lock:
            current = None
            ahead = 0
            if self.current is not None:
                current = self._public(self.current)
                ahead = self._remain_sec_locked(self.current)
            queue_list = []
            for o in self._queue:
                # eta_sec = 排在它前面所有订单的剩余时间之和
                queue_list.append({
                    "order_id": o["order_id"],
                    "pickup_no": o["pickup_no"],
                    "state": o["state"],
                    "eta_sec": ahead,
                })
                ahead += SIM_TOTAL_SEC
            # TASK 9：ready（做好待取）订单也纳入快照——断线期间做好的订单，
            # 重连后页面据此直接恢复取餐提示（新增字段，旧前端忽略）
            ready_list = [{"order_id": o["order_id"], "pickup_no": o["pickup_no"],
                           "state": o["state"], "drink": o["drink"]}
                          for o in self._by_id.values() if o["state"] == "ready"]
            return {
                "machine": self.machine,
                "queue_len": len(self._queue),
                "current": current,
                "queue": queue_list,
                "ready": ready_list,
            }

    # ---------- 制作循环 ----------
    def _publish_state(self, order):
        """订单状态机变化事件（新增 SSE 类型，旧前端忽略未知事件不受影响）。"""
        self.events.publish({"type": "order_state", "order_id": order["order_id"],
                             "pickup_no": order["pickup_no"], "state": order["state"]})

    def _publish_unified(self, etype, order=None, **kw):
        """TASK 9：统一大写事件（与旧事件并列，见 UNIFIED_EVENTS 注释）。"""
        ev = {"type": etype}
        if order is not None:
            ev["order_id"] = order["order_id"]
            ev["pickup_no"] = order["pickup_no"]
        ev.update(kw)
        self.events.publish(ev)

    def _record_terminal(self, order, result, fail_reason=None):
        """TASK 35：订单进终结态落 SQLite。统计是附属能力，任何异常都不许影响制作。"""
        if self.stats is None:
            return
        # 制作时长 = 终结时刻 - 开始制作时刻；取消单没开始制作，时长记 NULL
        dur = None
        if result != "cancelled" and order.get("_start_ts"):
            dur = round(time.time() - order["_start_ts"], 1)
        mode = order.get("_cafe_mode") or ("SIM" if self.simulate else "REAL")
        try:
            self.stats.record(order, result, fail_reason=fail_reason,
                              duration_sec=dur, mode=mode)
        except Exception as e:
            log("STATS", f"落库异常（已吞没，不影响制作）: {e}")

    def _progress(self, order, step_index, remain_sec=None, unified=None):
        with self._lock:
            order["_step_index"] = step_index
            order["_step_ts"] = time.time()
        self.events.publish({
            "type": "progress",
            "order_id": order["order_id"],
            "pickup_no": order["pickup_no"],
            "steps": STEP_NAMES,
            "step_index": step_index,
            "step_name": STEP_NAMES[step_index],
            "remain_sec": remain_sec,
        })
        # TASK 9：统一事件并列发。unified=None 按屏幕步骤查默认表 STEP_TO_UNIFIED
        # （旧 --simulate/fsm.py 路径）；cafe_fsm 路径由 CAFE_STATE_TO_UNIFIED 表显式
        # 传入；unified=False 表示该状态不发统一事件（如 CHECK_SYSTEM 起步自检态）。
        # brew_tick 每秒刷 progress，统一事件同样逐秒发，消费方按需经事件 id 去重。
        if unified is None:
            unified = STEP_TO_UNIFIED[step_index]
        if unified:
            self._publish_unified(unified, order,
                                  step_index=step_index, step_name=STEP_NAMES[step_index],
                                  remain_sec=remain_sec)

    def _work_loop(self):
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                order = self._queue.popleft()
                order["state"] = "preparing"
                order["_step_index"] = 0
                order["_step_ts"] = time.time()
                order["_start_ts"] = order["_step_ts"]   # TASK 25/35：制作起点
                self.current = order
            self._publish_state(order)
            self._publish_unified("ORDER_STARTED", order)   # TASK 9
            log("WORK", f"开始制作 #{order['order_id']}")
            try:
                mode = order.get("_cafe_mode")          # 下单时定死的制作后端（TASK 34）
                if mode:
                    self._make_cafe_fsm(order, mode)
                elif self.simulate:
                    self._make_simulated(order)
                else:
                    self._make_real(order)
                with self._lock:
                    order["state"] = "ready"
                    order["_ready_ts"] = time.time()
                self.events.publish({"type": "done", "order_id": order["order_id"],
                                     "pickup_no": order["pickup_no"]})
                self._publish_state(order)
                self._publish_unified("READY", order)   # TASK 9
                log("WORK", f"#{order['order_id']} 制作完成，等待取餐")
            except Exception as e:
                with self._lock:
                    order["state"] = "failed"
                    self._by_id.pop(order["order_id"], None)
                log("ERROR", f"#{order['order_id']} 制作失败: {e}")
                self.events.publish({"type": "error", "order_id": order["order_id"],
                                     "message": str(e)})
                self._publish_state(order)
                self._publish_unified("ERROR", order, message=str(e))   # TASK 9
                self._record_terminal(order, "failed", fail_reason=str(e))  # TASK 35
            finally:
                with self._lock:
                    self.current = None

    def _reap_loop(self):
        """ready 订单超时没人取 -> completed 归档（屏幕没有取餐回执，只能靠超时清理）。"""
        while True:
            time.sleep(5)
            now = time.time()
            done = []
            with self._lock:
                for oid, o in list(self._by_id.items()):
                    if o["state"] == "ready" and now - o["_ready_ts"] >= READY_TTL_SEC:
                        o["state"] = "completed"
                        self._by_id.pop(oid, None)
                        done.append(o)
            for o in done:
                self._publish_state(o)
                self._publish_unified("ORDER_COMPLETE", o)          # TASK 9
                self._record_terminal(o, "success")                 # TASK 35
                log("WORK", f"#{o['order_id']} 取餐完成/超时归档")

    def _make_simulated(self, order):
        for i, sec in enumerate(SIM_STEP_SEC):
            self._progress(order, i, remain_sec=sec)
            time.sleep(sec)

    def _make_real(self, order):
        """真机：调 fsm.py run，解析日志行驱动屏幕进度。
        日志格式（fsm.py）：[HH:MM:SS] [FSM] 状态转换 A -> B
                           [HH:MM:SS] [BREW] 冲泡中... 剩余 Ns / 共 Ms
        整体超时保护：制作超过 make_timeout 秒则杀掉子进程按失败处理（推 error 事件），
        try/finally 保证任何路径下子进程都被回收、不变僵尸。"""
        proc = subprocess.Popen(
            [sys.executable, FSM_PY, "run"],
            cwd=FSM_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        deadline = time.monotonic() + self.make_timeout
        last_step = -1
        try:
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    raise TimeoutError(f"制作超过 {self.make_timeout}s 超时，已强制终止")
                r, _, _ = select.select([proc.stdout], [], [], min(1.0, remain))
                if r:
                    line = proc.stdout.readline()
                    if not line:               # EOF：子进程关闭了输出
                        break
                    last_step = self._parse_fsm_log(order, line, last_step)
                elif proc.poll() is not None:  # 进程已退出，读干残留输出后收尾
                    for line in proc.stdout:
                        last_step = self._parse_fsm_log(order, line, last_step)
                    break
            rc = proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        finally:
            if proc.poll() is None:            # 超时/异常路径：杀掉并回收子进程
                proc.kill()
                proc.wait()
        if rc != 0:
            raise RuntimeError(f"机械臂流程异常退出 rc={rc}（详见服务端日志）")

    def _parse_fsm_log(self, order, line, last_step):
        """解析一行 fsm.py 日志并驱动进度，返回新的 last_step。"""
        line = line.rstrip()
        m = re.search(r"\[FSM\] 状态转换 \S+ -> (\S+)", line)
        if m:
            step = STATE_TO_STEP.get(m.group(1))
            if step is not None and step != last_step:
                self._progress(order, step)
                return step
            return last_step
        m = re.search(r"\[BREW\] 冲泡中\.\.\. 剩余 (\d+)s", line)
        if m and last_step == 2:
            self._progress(order, 2, remain_sec=int(m.group(1)))
        return last_step

    # ---------- TASK 27：cafe_fsm.py 子进程后端（--mode 指定时启用） ----------
    def _make_cafe_fsm(self, order, mode=None):
        """每单 fork cafe_fsm.py make，解析 stdout 的 [EVENT] {json} 驱动进度。
        mode 缺省用全局 self.cafe_mode；TASK 34 起逐单后端由 order['_cafe_mode'] 传入。
        事件契约（见 cafe_fsm.py 头注释）：
          {"type":"state",...}     状态转换，CAFE_STATE_TO_STEP 映射到屏幕步骤，
                                   CAFE_STATE_TO_UNIFIED 并列映射到 TASK 9 统一事件
          {"type":"brew_tick",...} 冲泡倒计时，remain_sec 直接用作屏幕剩余秒数
          {"type":"result",...}    completed/failed/estop，决定本单成败
        整体超时骨架复用 _make_real：超过 make_timeout 秒杀子进程按失败处理，
        try/finally 保证任何路径下子进程都被回收、不变僵尸。"""
        mode = mode or self.cafe_mode
        cmd = [sys.executable, CAFE_FSM_PY, "make",
               "--drink", str(order["_drink_id"]),
               "--order-id", str(order["order_id"]),
               "--mode", mode]
        if self.scenario:                        # 仅用户显式给了 --scenario 才传
            cmd += ["--scenario", self.scenario]
        log("CAFE", f"#{order['order_id']} 启动: " + " ".join(cmd))
        proc = subprocess.Popen(
            cmd, cwd=FSM_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        deadline = time.monotonic() + self.make_timeout
        ctx = {"last_step": -1, "result": None}  # result=收到的 result 事件 dict
        try:
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    raise TimeoutError(f"制作超过 {self.make_timeout}s 超时，已强制终止")
                r, _, _ = select.select([proc.stdout], [], [], min(1.0, remain))
                if r:
                    line = proc.stdout.readline()
                    if not line:               # EOF：子进程关闭了输出
                        break
                    self._parse_cafe_event(order, line, ctx)
                elif proc.poll() is not None:  # 进程已退出，读干残留输出后收尾
                    for line in proc.stdout:
                        self._parse_cafe_event(order, line, ctx)
                    break
            rc = proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        finally:
            if proc.poll() is None:            # 超时/异常路径：杀掉并回收子进程
                proc.kill()
                proc.wait()
        result = ctx["result"]
        if result and result.get("result") != "completed":
            # failed/estop：message 用 note（失败原因），缺省退化为 state
            detail = result.get("note") or result.get("state") or "制作失败"
            raise RuntimeError(f"{detail}（{result.get('result')}@{result.get('state', '?')}）")
        if rc != 0:
            raise RuntimeError(f"制作流程异常退出 rc={rc}（mode={mode}，详见服务端日志）")

    def _parse_cafe_event(self, order, line, ctx):
        """解析一行 cafe_fsm.py 输出并驱动进度，状态记在 ctx（last_step/result）。
        非 [EVENT] 行是 cafe_fsm 的人类可读日志，原样转发到 kiosk 日志便于排查。"""
        line = line.rstrip()
        if not line.startswith("[EVENT] "):
            if line:
                log("CAFE", line)
            return
        try:
            ev = json.loads(line[len("[EVENT] "):])
        except ValueError:
            log("CAFE", f"无法解析的事件行（忽略）: {line}")
            return
        etype = ev.get("type")
        if etype == "state":
            step = CAFE_STATE_TO_STEP.get(ev.get("state"))
            # TASK 9：统一事件按状态转换独立发布，不走步骤去重——同一步骤内的
            # 不同动作都要可区分（如取杯段 CHECK_CUP/PICK_CUP/MOVE_TO_MACHINE
            # 同属屏幕步骤 0，但每次都是一次 ARM_MOVING 事件）
            unified = CAFE_STATE_TO_UNIFIED.get(ev.get("state"))
            if unified and ev.get("state") != ctx.get("last_unified_state"):
                ctx["last_unified_state"] = ev.get("state")
                self._publish_unified(unified, order, state=ev.get("state"))
            if step is not None and step != ctx["last_step"]:
                ctx["last_step"] = step
                self._progress(order, step, unified=False)  # 统一事件已发，这里只发 progress
        elif etype == "brew_tick":
            ctx["last_step"] = 2
            # brew_tick 逐秒带 remain_sec，BREWING 统一事件随之逐秒发（含倒计时，
            # 消费方按需经事件 id 去重）
            self._progress(order, 2, remain_sec=ev.get("remain_sec"), unified="BREWING")
        elif etype == "result":
            ctx["result"] = ev
            log("CAFE", f"#{order['order_id']} 结果 {ev.get('result')}"
                        f"@{ev.get('state')} {ev.get('note', '')}".rstrip())


# =====================================================================
# HTTP 处理
# =====================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "KioskSrv/1.0"

    # ---- 工具 ----
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt, *args):     # 静默默认访问日志
        pass

    # ---- TASK 34：管理后台鉴权 ----
    def _admin_ok(self):
        """管理后台(/admin 与 /api/admin/*)访问控制——**演示级弱安全，非强安全**：
        - 设置了环境变量 CAFE_ADMIN_TOKEN：必须 X-Admin-Token 头或 ?token= 参数匹配；
        - 未设置：仅允许本机回环（127.0.0.1/::1）访问。
        注意：只认 TCP 对端地址，不信 X-Forwarded-For 等可伪造头；
        危险操作（模式切换/设备重检）不许匿名公网裸奔——对外部署务必设置 token。"""
        token = os.environ.get("CAFE_ADMIN_TOKEN")
        if token:
            provided = self.headers.get("X-Admin-Token")
            if not provided:      # 浏览器页面fetch用查询参数带 token
                provided = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
            return provided == token
        return self.client_address[0] in ("127.0.0.1", "::1")

    # ---- GET ----
    def do_GET(self):
        mgr = self.server.order_mgr
        parts = urlsplit(self.path)          # 支持 ?last_id= / ?token= 等查询参数
        path, qs = parts.path, parse_qs(parts.query)
        if path in ("/", "/index.html"):
            with open(HTML_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/menu":
            drinks = [dict(d, cn=d["name"], cat=d["category"], iced=d["ice"])
                      for d in mgr.menu.get("drinks", [])]
            self._send_json({
                "categories": mgr.menu.get("categories", []),
                "menu": drinks,
                "machine": mgr.machine_state(),
            })
        elif path == "/api/status":
            snap = mgr.snapshot()
            hm = getattr(self.server, "health_mgr", None)
            if hm is not None:            # TASK 24：追加健康摘要，不动旧字段
                snap["health"] = hm.summary()
            wd = getattr(self.server, "watchdog", None)
            if wd is not None:            # TASK 25：追加 watchdog 段，不动旧字段
                snap["watchdog"] = wd.section()
            self._send_json(snap)
        elif path == "/api/health":
            # TASK 24：8 项巡检明细 + 整体 SYSTEM READY/DEGRADED/OFFLINE
            hm = getattr(self.server, "health_mgr", None)
            if hm is None:
                self._send_json({"overall": "UNKNOWN", "overall_text": "SYSTEM UNKNOWN",
                                 "headline": "SYSTEM UNKNOWN", "blocking": False,
                                 "items": {}, "ts": int(time.time())})
            else:
                data = hm.snapshot()
                wd = getattr(self.server, "watchdog", None)
                if wd is not None:        # TASK 25：watchdog 段并入健康快照
                    data["watchdog"] = wd.section()
                self._send_json(data)
        elif path == "/api/events":
            self._serve_sse(mgr, qs)
        elif path == "/admin":
            # TASK 34：管理后台页（内嵌模板，无前端框架）
            if not self._admin_ok():
                self._send_json({"ok": False, "reason": "forbidden"}, 403)
                return
            body = ADMIN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/admin/stats":
            # TASK 34/35：统计 JSON（订单历史/成功率/平均时长/失败原因/设备状态）
            if not self._admin_ok():
                self._send_json({"ok": False, "reason": "forbidden"}, 403)
                return
            st = getattr(self.server, "stats", None)
            if st is not None:
                data = st.summary()
            else:
                data = {"disabled": True, "today_count": 0, "today_success": 0,
                        "success_rate": None, "avg_duration_sec": None,
                        "top_drinks": [], "fail_reasons": {}, "recent": []}
            hm = getattr(self.server, "health_mgr", None)
            if hm is not None:
                data["health"] = hm.snapshot()
            wd = getattr(self.server, "watchdog", None)
            if wd is not None:
                data["watchdog"] = wd.section()
            # AI 推理速度占位：ai_host 尚未与 kiosk 联通，没有真实数据就给 null
            # （页面显示 n/a），绝不编造
            data["ai_infer_ms"] = None
            data["backend"] = {
                "mode": mgr.cafe_mode or ("SIM" if mgr.simulate else "REAL"),
                "pending_mode": mgr.pending_mode,
            }
            self._send_json(data)
        else:
            self._send_json({"ok": False, "reason": "not_found"}, 404)

    def _serve_sse(self, mgr, qs):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        bus = self.server.event_bus
        q = bus.subscribe()          # 先订阅：快照/重放期间的新事件进队列不丢
        try:
            snap = mgr.snapshot()
            # 1) hello：旧契约首帧（机器状态/队列长度），保持原语义不变
            self._sse_write({"type": "hello", "machine": snap["machine"],
                             "queue_len": snap["queue_len"]})
            # 2) snapshot：TASK 9 新增——断线重连后页面据此恢复一致状态
            #    （当前订单+排队列表+待取订单+每单状态+机器状态+健康摘要）
            snap_ev = {"type": "snapshot", "machine": snap["machine"],
                       "queue_len": snap["queue_len"], "current": snap["current"],
                       "queue": snap["queue"], "ready": snap["ready"]}
            hm = getattr(self.server, "health_mgr", None)
            if hm is not None:
                snap_ev["health"] = hm.summary()
            self._sse_write(snap_ev)
            # 3) 重放缺口：优先 Last-Event-ID 头（浏览器 EventSource 重连自动带），
            #    其次 ?last_id= 参数（手动重连/调试用）。环形缓冲只留最近几百条，
            #    缺口超出缓冲时由上面的快照兜底——取舍：快照保证状态一致，
            #    重放只是尽量补齐事件流（如断线期间错过的 done/error）。
            last_id = self._last_event_id(qs)
            watermark = last_id
            for ev in bus.replay_since(last_id):
                self._sse_write(ev)
                watermark = max(watermark, ev.get("id", 0))
            # 4) 实时流：订阅时刻起的事件可能已在重放里发过，按 id 水位去重
            last_ping = time.time()
            while True:
                try:
                    ev = q.get(timeout=5)
                    if ev.get("id", 0) > watermark:
                        watermark = ev.get("id", 0)
                        self._sse_write(ev)
                except queue.Empty:
                    pass
                if time.time() - last_ping >= 15:      # 保活注释，防代理断连
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            bus.unsubscribe(q)

    def _last_event_id(self, qs):
        """Last-Event-ID 头优先，?last_id= 兜底；非法值按 0（只收快照+新事件流）。"""
        raw = self.headers.get("Last-Event-ID") or qs.get("last_id", [""])[0]
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def _sse_write(self, obj):
        # 带 id 的事件同时写 SSE `id:` 行，浏览器 EventSource 自动记录 lastEventId，
        # 断线重连时经 Last-Event-ID 头回传（TASK 9 重放缺口的依据）
        eid = obj.get("id")
        data = json.dumps(obj, ensure_ascii=False)
        head = f"id: {eid}\n" if eid is not None else ""
        self.wfile.write(f"{head}data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # ---- POST ----
    def do_POST(self):
        mgr = self.server.order_mgr
        path = urlsplit(self.path).path
        body = self._read_body()
        if path == "/api/order":
            order, err = mgr.place_order(body)
            if err:
                resp = {"ok": False, "reason": err}
                # TASK 24：健康闸门拒单时附明细（旧 reason 契约不变，新字段可选读）
                if err.startswith("health_"):
                    hm = getattr(self.server, "health_mgr", None)
                    if hm is not None:
                        resp["detail"] = hm.blocking_detail()
                self._send_json(resp, 409)
            else:
                self._send_json({"ok": True, "order_id": order["order_id"],
                                 "pickup_no": order["pickup_no"],
                                 "total": order["total"]})
        elif path == "/api/order/cancel":
            order, err = mgr.cancel_order(body.get("order_id"))
            if err is None:
                self._send_json({"ok": True})
            elif err == "already_preparing":
                self._send_json({"ok": False, "reason": err}, 409)
            else:
                self._send_json({"ok": False, "reason": err}, 404)
        elif path == "/api/machine":
            ok = mgr.set_machine(body.get("state", ""))
            self._send_json({"ok": ok}, 200 if ok else 400)
        elif path == "/api/admin/mode":
            # TASK 34：切换演示模式。只标记"下一单生效"（下单时定死在订单上），
            # 不热切换当前制作中的订单——避免子进程后端中途换人出不可预期状态。
            if not self._admin_ok():
                self._send_json({"ok": False, "reason": "forbidden"}, 403)
                return
            mode = str(body.get("mode", "")).upper()
            if mode not in ("SIM", "HYBRID"):
                self._send_json({"ok": False, "reason": "bad_mode"}, 400)
                return
            mgr.set_pending_mode(mode)
            log("ADMIN", f"下一单制作后端标记为 {mode}（当前单不受影响）")
            self._send_json({"ok": True, "pending_mode": mode,
                             "effective": "next_order"})
        elif path == "/api/admin/reinit":
            # TASK 34：重新初始化设备 = 触发 HealthManager 同步重检一轮
            # （断线设备会尝试重连，结果直接反映到 /api/health）
            if not self._admin_ok():
                self._send_json({"ok": False, "reason": "forbidden"}, 403)
                return
            hm = getattr(self.server, "health_mgr", None)
            if hm is None:
                self._send_json({"ok": False, "reason": "no_health_mgr"}, 400)
                return
            hm.check_once()
            log("ADMIN", "管理后台触发设备重检")
            self._send_json({"ok": True, "health": hm.snapshot()})
        else:
            self._send_json({"ok": False, "reason": "not_found"}, 404)


# =====================================================================

def main():
    ap = argparse.ArgumentParser(prog="kiosk_server.py", description="点单屏后台服务")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--simulate", action="store_true", help="仿真制作流程（无硬件）")
    ap.add_argument("--mode", choices=("SIM", "HYBRID", "REAL"), default=None,
                    help="TASK 27：制作后端切换为 cafe_fsm.py 子进程（解析 [EVENT] JSON "
                         "驱动进度）；不填=旧行为（--simulate 时间线 / 真机 fsm.py 日志解析）")
    ap.add_argument("--scenario", default=None,
                    help="故障注入场景 yaml（键同 config/sim_scenario.yaml），"
                         "仅配合 --mode 使用，逐单透传给 cafe_fsm.py")
    ap.add_argument("--timeout", type=int, default=MAKE_TIMEOUT_SEC,
                    help="真机模式单杯制作超时秒数，超时杀子进程按失败处理（默认 600）")
    args = ap.parse_args()

    if args.scenario and not args.mode:
        ap.error("--scenario 需配合 --mode 使用（旧制作后端不支持故障注入）")

    with open(MENU_PATH, "r", encoding="utf-8") as f:
        menu = json.load(f)

    # TASK 24/26：健康管理器。健康巡检模式的确定：
    #   指定 --mode  -> 与制作后端同模式（SIM 时故障注入场景同样生效）
    #   --simulate   -> 旧路径无硬件，按 SIM 巡检
    #   其他          -> 旧真机路径（fsm.py），按 REAL 巡检
    health_mode = args.mode or ("SIM" if args.simulate else "REAL")
    health_mgr = HealthManager(mode=health_mode, scenario=args.scenario)
    # TASK 26 开机自检：逐项打印，首轮巡检数据同时作为 /api/health 的初始数据；
    # 无论 READY 还是 DEMO MODE 都继续启动服务，绝不因自检失败退出
    verdict = run_selfcheck(health_mgr)
    health_mgr.start()

    event_bus = EventBus()
    # TASK 35：SQLite 统计库（projects/kiosk_server/data/kiosk_stats.db；
    # 初始化失败自动降级仅日志，绝不阻止服务启动）
    stats = Stats()
    mgr = OrderManager(event_bus, simulate=args.simulate, menu=menu,
                       make_timeout=args.timeout,
                       cafe_mode=args.mode, scenario=args.scenario,
                       health=health_mgr, stats=stats)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.event_bus = event_bus
    srv.order_mgr = mgr
    srv.health_mgr = health_mgr
    srv.stats = stats

    # TASK 25：软件看门狗（制作卡死/长期 BUSY/健康巡检线程活性/HTTP 自连活性）。
    # 只发现与暴露（/api/health、/api/status 的 watchdog 段 + ERROR 日志），
    # 不做自动重启——真机进程级自愈由 systemd Restart=always 兜底
    wd = Watchdog(mgr=mgr, health=health_mgr, port=args.port)
    wd.start()
    srv.watchdog = wd

    if args.mode:
        backend = f"cafe_fsm.py 子进程（mode={args.mode}"
        backend += f"，scenario={args.scenario}）" if args.scenario else "）"
    elif args.simulate:
        backend = "内置仿真时间线（--simulate 旧路径）"
    else:
        backend = "真机(调 fsm.py run)"
    admin_hint = "已启用 CAFE_ADMIN_TOKEN" if os.environ.get("CAFE_ADMIN_TOKEN") \
        else "未设 CAFE_ADMIN_TOKEN，仅本机 127.0.0.1 可访问"
    log("HTTP", f"点单屏服务已启动: http://{args.host}:{args.port}/  制作后端={backend}  自检={verdict}")
    log("HTTP", f"页面入口 http://<板子IP>:{args.port}/  API: /api/menu /api/order /api/events")
    log("HTTP", f"管理后台 http://<板子IP>:{args.port}/admin  鉴权：{admin_hint}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
