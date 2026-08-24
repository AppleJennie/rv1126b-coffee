#!/usr/bin/env python3
# kiosk_server.py —— 点单屏后台服务（运行在 RV1126B 开发板上，纯 Python3 标准库）
#
# 职责：
#   1. 托管点单屏页面 coffee_kiosk.html（浏览器/板载屏幕通过 http 访问）
#   2. 提供 JSON API：查菜单、下单、查状态、维护模式切换
#   3. 通过 SSE（Server-Sent Events）向页面实时推送制作进度/机器状态
#   4. 订单排队，逐单驱动机械臂主控 fsm.py（--simulate 时用内置仿真时间线）
#
# 运行：
#   python3 kiosk_server.py --simulate          # 无硬件仿真（开发调试用）
#   python3 kiosk_server.py                     # 真机：调用 ../coffee_fsm/fsm.py run
#   python3 kiosk_server.py --port 8080 --host 0.0.0.0
#
# API 一览（详见 docs/WiFi控制与网页通讯设计.md）：
#   GET  /                 -> 点单屏页面
#   GET  /api/menu         -> {categories, menu, machine}
#   POST /api/order        -> 请求 {drink_id, opts:{cup,temp,sugar,extras}, qty}
#                             响应 {ok, order_id, pickup_no, total} 或 {ok:false, reason}
#   GET  /api/status       -> 队列与当前订单快照（调试用）
#   GET  /api/events       -> SSE 事件流（progress / done / error / machine / hello）
#   POST /api/machine      -> {state:"ok|nowater|nobeans"} 维护模拟（将来由水位/豆位传感器驱动）

import argparse
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
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
    """单队列、逐单制作（只有一条机械臂，不能并行）。"""

    def __init__(self, bus_events, simulate=False, menu=None):
        self.events = bus_events
        self.simulate = simulate
        self.menu = menu or {}
        self.machine = "ok"                # ok | nowater | nobeans
        self._orders = queue.Queue()
        self._lock = threading.Lock()
        self._seq = 0
        self.current = None                # 正在制作的订单 dict
        self._worker = threading.Thread(target=self._work_loop, daemon=True)
        self._worker.start()

    # ---------- 下单 ----------
    def _price_of(self, drink, opts, qty):
        """服务端重算总价（不信任页面传来的金额）。"""
        total = drink["price"]
        total += 3 if opts.get("cup") == 1 else 0            # 大杯 +3
        for i in opts.get("extras", []):                     # 加料 +2/+3/+2
            total += [2, 3, 2][i] if 0 <= i <= 2 else 0
        return total * max(1, min(int(qty), 9))

    def place_order(self, body):
        if self.machine != "ok":
            return None, f"machine_{self.machine}"
        drinks = {d["id"]: d for d in self.menu.get("drinks", [])}
        drink = drinks.get(body.get("drink_id"))
        if drink is None:
            return None, "bad_drink"
        opts = body.get("opts") or {}
        qty = body.get("qty", 1)
        with self._lock:
            self._seq += 1
            order_id = self._seq
        order = {
            "order_id": order_id,
            "pickup_no": str(random.randint(100, 999)),
            "drink": drink["name"],
            "opts": opts,
            "qty": qty,
            "total": self._price_of(drink, opts, qty),
        }
        self._orders.put(order)
        log("ORDER", f"接单 #{order_id} {drink['name']} x{qty} 取餐号 {order['pickup_no']}")
        return order, None

    def set_machine(self, state):
        if state not in ("ok", "nowater", "nobeans"):
            return False
        self.machine = state
        self.events.publish({"type": "machine", "state": state})
        log("MACHINE", f"机器状态 -> {state}")
        return True

    def snapshot(self):
        return {
            "machine": self.machine,
            "queue_len": self._orders.qsize(),
            "current": self.current,
        }

    # ---------- 制作循环 ----------
    def _progress(self, order, step_index, remain_sec=None):
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
            order = self._orders.get()
            self.current = order
            log("WORK", f"开始制作 #{order['order_id']}")
            try:
                if self.simulate:
                    self._make_simulated(order)
                else:
                    self._make_real(order)
                self.events.publish({"type": "done", "order_id": order["order_id"],
                                     "pickup_no": order["pickup_no"]})
                log("WORK", f"#{order['order_id']} 制作完成")
            except Exception as e:
                log("ERROR", f"#{order['order_id']} 制作失败: {e}")
                self.events.publish({"type": "error", "order_id": order["order_id"],
                                     "message": str(e)})
            finally:
                self.current = None

    def _make_simulated(self, order):
        for i, sec in enumerate(SIM_STEP_SEC):
            self._progress(order, i, remain_sec=sec)
            time.sleep(sec)

    def _make_real(self, order):
        """真机：调 fsm.py run，解析日志行驱动屏幕进度。
        日志格式（fsm.py）：[HH:MM:SS] [FSM] 状态转换 A -> B
                           [HH:MM:SS] [BREW] 冲泡中... 剩余 Ns / 共 Ms"""
        proc = subprocess.Popen(
            [sys.executable, FSM_PY, "run"],
            cwd=FSM_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        last_step = -1
        for line in proc.stdout:
            line = line.rstrip()
            m = re.search(r"\[FSM\] 状态转换 \S+ -> (\S+)", line)
            if m:
                step = STATE_TO_STEP.get(m.group(1))
                if step is not None and step != last_step:
                    last_step = step
                    self._progress(order, step)
                continue
            m = re.search(r"\[BREW\] 冲泡中\.\.\. 剩余 (\d+)s", line)
            if m and last_step == 2:
                self._progress(order, 2, remain_sec=int(m.group(1)))
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"机械臂流程异常退出 rc={rc}（详见服务端日志）")


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
                "machine": mgr.machine,
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
            self._sse_write({"type": "hello", "machine": mgr.machine,
                             "queue_len": mgr.snapshot()["queue_len"]})
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
    args = ap.parse_args()

    with open(MENU_PATH, "r", encoding="utf-8") as f:
        menu = json.load(f)

    event_bus = EventBus()
    mgr = OrderManager(event_bus, simulate=args.simulate, menu=menu)

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
