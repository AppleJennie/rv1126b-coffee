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
#   python3 kiosk_server.py --simulate          # 无硬件仿真（开发调试用）
#   python3 kiosk_server.py                     # 真机：调用 ../coffee_fsm/fsm.py run
#   python3 kiosk_server.py --port 8080 --host 0.0.0.0 --timeout 600
#
# API 一览（详见 docs/04-WiFi与网页通讯设计.md）：
#   GET  /                 -> 点单屏页面
#   GET  /api/menu         -> {categories, menu, machine}
#   POST /api/order        -> 请求 {drink_id, opts:{cup,temp,sugar,extras}, qty}
#                             响应 {ok, order_id, pickup_no, total} 或 {ok:false, reason}
#                             （队列满返回 409 {ok:false, reason:"queue_full"}）
#   POST /api/order/cancel -> 请求 {order_id}，仅排队中(queued)可取消；
#                             制作中拒绝 409 {ok:false, reason:"already_preparing"}
#   GET  /api/status       -> {machine, queue_len, current, queue[]} 队列快照（含 eta_sec）
#   GET  /api/events       -> SSE 事件流（hello / machine / progress / done / error / order_state）
#   POST /api/machine      -> {state:"ok|nowater|nobeans"} 维护模拟（将来由水位/豆位传感器驱动）
#
# 订单状态机：
#   queued(排队) -> preparing(制作中) -> ready(可取餐) -> completed(已取走/超时归档)
#   queued -> cancelled(取消) ；preparing -> failed(制作失败)

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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "..", "ui_prototype", "coffee_kiosk.html")
MENU_PATH = os.path.join(BASE_DIR, "..", "ai_host", "menu.json")
FSM_DIR = os.path.join(BASE_DIR, "..", "coffee_fsm")
FSM_PY = os.path.join(FSM_DIR, "fsm.py")

# 屏幕制作步骤与机械臂 FSM 状态的映射（真机模式解析 fsm.py 日志用）
# 屏幕步骤：取杯 -> 磨豆 -> 冲泡 -> 出品
STEP_NAMES = ["取杯", "磨豆", "冲泡", "出品"]
STATE_TO_STEP = {
    "LOCATE_CUP": 0, "PICK_CUP": 0, "PLACE_CUP": 0,
    "PRESS_GRINDER": 1, "POUR_GROUNDS": 1,
    "PRESS_BREWER": 2, "WAIT_BREW": 2,
    "SERVE": 3,
}
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
    def __init__(self):
        self._subs = []          # list[queue.Queue]
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
        """event: dict，自动补 ts。订阅者队列满则丢弃（页面卡顿不阻塞制作）。"""
        event["ts"] = int(time.time())
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


# =====================================================================
# 订单管理 + 制作执行
# =====================================================================

class OrderManager:
    """订单状态机 + 单队列逐单制作（只有一条机械臂，不能并行）。
    只管订单与调度，不碰硬件；machine / current / 队列的读写全部在 self._lock 内进行。"""

    def __init__(self, bus_events, simulate=False, menu=None, make_timeout=MAKE_TIMEOUT_SEC):
        self.events = bus_events
        self.simulate = simulate
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
                "_step_index": 0,      # 制作进度（ETA 折算用，内部字段不对外）
                "_step_ts": None,      # 进入当前步骤的时刻
                "_ready_ts": None,     # 进入 ready 的时刻（超时归档用）
            }
            self._queue.append(order)
            self._by_id[order["order_id"]] = order
            # queued 事件必须在唤醒 worker 前发，否则可能晚于 preparing 事件
            self._publish_state(order)
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
        log("ORDER", f"取消 #{order['order_id']} 取餐号 {order['pickup_no']}")
        return order, None

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
            return {
                "machine": self.machine,
                "queue_len": len(self._queue),
                "current": current,
                "queue": queue_list,
            }

    # ---------- 制作循环 ----------
    def _publish_state(self, order):
        """订单状态机变化事件（新增 SSE 类型，旧前端忽略未知事件不受影响）。"""
        self.events.publish({"type": "order_state", "order_id": order["order_id"],
                             "pickup_no": order["pickup_no"], "state": order["state"]})

    def _progress(self, order, step_index, remain_sec=None):
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

    def _work_loop(self):
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                order = self._queue.popleft()
                order["state"] = "preparing"
                order["_step_index"] = 0
                order["_step_ts"] = time.time()
                self.current = order
            self._publish_state(order)
            log("WORK", f"开始制作 #{order['order_id']}")
            try:
                if self.simulate:
                    self._make_simulated(order)
                else:
                    self._make_real(order)
                with self._lock:
                    order["state"] = "ready"
                    order["_ready_ts"] = time.time()
                self.events.publish({"type": "done", "order_id": order["order_id"],
                                     "pickup_no": order["pickup_no"]})
                self._publish_state(order)
                log("WORK", f"#{order['order_id']} 制作完成，等待取餐")
            except Exception as e:
                with self._lock:
                    order["state"] = "failed"
                    self._by_id.pop(order["order_id"], None)
                log("ERROR", f"#{order['order_id']} 制作失败: {e}")
                self.events.publish({"type": "error", "order_id": order["order_id"],
                                     "message": str(e)})
                self._publish_state(order)
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

    # ---- GET ----
    def do_GET(self):
        mgr = self.server.order_mgr
        if self.path in ("/", "/index.html"):
            with open(HTML_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/menu":
            drinks = [dict(d, cn=d["name"], cat=d["category"], iced=d["ice"])
                      for d in mgr.menu.get("drinks", [])]
            self._send_json({
                "categories": mgr.menu.get("categories", []),
                "menu": drinks,
                "machine": mgr.machine_state(),
            })
        elif self.path == "/api/status":
            self._send_json(mgr.snapshot())
        elif self.path == "/api/events":
            self._serve_sse(mgr)
        else:
            self._send_json({"ok": False, "reason": "not_found"}, 404)

    def _serve_sse(self, mgr):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.server.event_bus.subscribe()
        try:
            # 连接即下发当前机器状态，页面据此决定是否可点单
            snap = mgr.snapshot()
            self._sse_write({"type": "hello", "machine": snap["machine"],
                             "queue_len": snap["queue_len"]})
            last_ping = time.time()
            while True:
                try:
                    ev = q.get(timeout=5)
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
            self.server.event_bus.unsubscribe(q)

    def _sse_write(self, obj):
        data = json.dumps(obj, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # ---- POST ----
    def do_POST(self):
        mgr = self.server.order_mgr
        body = self._read_body()
        if self.path == "/api/order":
            order, err = mgr.place_order(body)
            if err:
                self._send_json({"ok": False, "reason": err}, 409)
            else:
                self._send_json({"ok": True, "order_id": order["order_id"],
                                 "pickup_no": order["pickup_no"],
                                 "total": order["total"]})
        elif self.path == "/api/order/cancel":
            order, err = mgr.cancel_order(body.get("order_id"))
            if err is None:
                self._send_json({"ok": True})
            elif err == "already_preparing":
                self._send_json({"ok": False, "reason": err}, 409)
            else:
                self._send_json({"ok": False, "reason": err}, 404)
        elif self.path == "/api/machine":
            ok = mgr.set_machine(body.get("state", ""))
            self._send_json({"ok": ok}, 200 if ok else 400)
        else:
            self._send_json({"ok": False, "reason": "not_found"}, 404)


# =====================================================================

def main():
    ap = argparse.ArgumentParser(prog="kiosk_server.py", description="点单屏后台服务")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--simulate", action="store_true", help="仿真制作流程（无硬件）")
    ap.add_argument("--timeout", type=int, default=MAKE_TIMEOUT_SEC,
                    help="真机模式单杯制作超时秒数，超时杀子进程按失败处理（默认 600）")
    args = ap.parse_args()

    with open(MENU_PATH, "r", encoding="utf-8") as f:
        menu = json.load(f)

    event_bus = EventBus()
    mgr = OrderManager(event_bus, simulate=args.simulate, menu=menu,
                       make_timeout=args.timeout)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.event_bus = event_bus
    srv.order_mgr = mgr

    mode = "仿真" if args.simulate else "真机(调 fsm.py run)"
    log("HTTP", f"点单屏服务已启动: http://{args.host}:{args.port}/  模式={mode}")
    log("HTTP", f"页面入口 http://<板子IP>:{args.port}/  API: /api/menu /api/order /api/events")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
