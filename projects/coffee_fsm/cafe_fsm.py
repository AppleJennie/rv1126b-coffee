#!/usr/bin/env python3
# cafe_fsm.py —— 完整咖啡制作状态机（TASK 3）
# 基于 hardware/ 适配层 + recipe.py 配方引擎的表驱动 17 状态 FSM。
# 当前无真硬件，全部走 Simulation Adapter（--mode SIM，默认）。
#
# 子命令：
#   make --drink N [--order-id N] [--mode SIM|REAL|HYBRID] [--scenario yaml] [--recipe slug]
#         执行一单咖啡流程。退出码沿用旧约定：0 成功 / 1 流程失败(含急停) / 2 初始化失败
#   states
#         打印状态定义表（进入/完成条件、超时、失败处理），由 STATES 自动生成
#
# 与旧版 fsm.py 的关系：fsm.py 保留不动（其 simulate 行为不变）。本模块的改进：
#   - 结构化事件：每次状态转换/进度向 stdout 打一行 [EVENT] {json}，
#     替代旧版"日志文本即接口"（kiosk 正则解析日志的脆弱性，见 docs/ARCHITECTURE.md）
#   - 每个状态动作由 daemon 线程 + join(timeout) 包裹（禁止 signal.alarm，
#     本模块以后会被嵌入非主线程），超时按 DeviceTimeout 处理
#   - 失败路由：DeviceError(retryable=True) → RECOVERY 一次并重试该状态一次；
#     retryable=False 或重试仍失败 → ERROR（卸力停止，订单 failed）；
#     EstopError → EMERGENCY_STOP（全部停止，等待外部 reset）；
#     未知异常兜底 → ERROR。任何路径都不允许异常逃逸出 run()
#   - 安全联锁（TASK 7 预热，内置不可关）：无杯禁止 GRIND/BREW；
#     place_cup 未确认前禁止 GRIND；GRIND/BREW/WAIT_BREW 期间禁止任何臂动作；
#     设备未 connect 禁止开始（CHECK_SYSTEM 裁决）

import argparse
import json
import os
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hardware import DeviceError, DeviceTimeout, EstopError, log            # noqa: E402
from hardware.factory import (SIM_TIME_SCALE, connect_all, default_fsm_config,  # noqa: E402
                              load_scenario, make_devices)
from recipe import RecipeEngine                                              # noqa: E402


# ---------- 状态定义表（表驱动：handler 经表查名派发，禁止 if state == 长链） ----------
# 每项字段：
#   handler        处理函数名（None = 无动作态，由 run() 起步/故障路径处理）
#   timeout        状态超时（真机秒；SIM 模式乘 SIM_TIME_SCALE）。
#                  可为 callable(fsm)（依赖配方时长）；None = 不执行无超时
#   timeout_margin 该态收尾动作的额外余量（真机秒，如顾客超时后的臂收回）
#   timeout_desc   超时的人类可读说明（states 子命令展示用；缺省由 timeout 生成）
#   entry / done / on_fail  进入条件 / 完成条件 / 失败处理说明
STATES = {
    "IDLE": {
        "handler": None, "timeout": None,
        "entry": "待命态：设备已连接，等待订单（CLI 单订单模式由 run() 从此起步）",
        "done": "收到新订单",
        "on_fail": "—（无动作，不会失败）",
    },
    "ORDER_RECEIVED": {
        "handler": "_h_order_received", "timeout": 10,
        "entry": "IDLE 收到订单（drink id 已解析出配方，或由 --recipe 指定）",
        "done": "订单记录完成，配方参数确认",
        "on_fail": "理论不失败；未知异常兜底 → ERROR",
    },
    "CHECK_SYSTEM": {
        "handler": "_h_check_system", "timeout": 30,
        "entry": "订单已接收",
        "done": "全部关键设备 health ok（未 connect / 底层开关离线均判不健康）",
        "on_fail": "任一关键设备不健康 → ERROR（retryable=False，不进 RECOVERY）；"
                   "联锁：设备未 connect 禁止开始",
    },
    "CHECK_CUP": {
        "handler": "_h_check_cup", "timeout": 30,
        "entry": "系统健康",
        "done": "cup.locate() 找到杯口并算出视觉纠偏量 (dx, dy)",
        "on_fail": "无杯重试 3 次仍无杯 → RECOVERY 一次后重试本态，再失败 → ERROR"
                   "（订单 failed）；locate() 抛 DeviceTimeout 按全局路由",
    },
    "PICK_CUP": {
        "handler": "_h_pick_cup", "timeout": 30,
        "entry": "已定位杯",
        "done": "arm.pick_cup(纠偏量) 完成，夹爪持杯",
        "on_fail": "臂动作失败按全局路由：retryable → RECOVERY 重试一次；否则 → ERROR",
    },
    "MOVE_TO_MACHINE": {
        "handler": "_h_move_to_machine", "timeout": 30,
        "entry": "已持杯（联锁：未取杯禁止放杯）",
        "done": "arm.place_cup() 完成，杯放冲泡位并松爪（放置确认）",
        "on_fail": "同 PICK_CUP",
    },
    "GRIND": {
        "handler": "_h_grind", "timeout": lambda fsm: fsm.recipe.grind_sec + 30,
        "timeout_desc": "recipe.grind_sec + 30",
        "entry": "杯已放置确认（联锁：无杯/未放置禁止磨豆）；配方 needs_grinder=False 时直接跳过",
        "done": "grinder.run(grind_sec) + wait_done 完成并断电回读确认",
        "on_fail": "DeviceTimeout/DeviceError 按全局路由；磨豆期间联锁禁止任何臂动作",
    },
    "BREW": {
        "handler": "_h_brew", "timeout": 15,
        "entry": "杯已放置确认（联锁：无杯禁止冲泡）",
        "done": "coffee.run() 点动启动冲泡完成",
        "on_fail": "同 GRIND；冲泡启动期间联锁禁止任何臂动作",
    },
    "WAIT_BREW": {
        "handler": "_h_wait_brew", "timeout": lambda fsm: fsm.recipe.brew_sec + 60,
        "timeout_desc": "recipe.brew_sec + 60",
        "entry": "冲泡已启动",
        "done": "等待 recipe.brew_sec（经 coffee.time_scale 缩放）结束；"
                "每秒打 brew_tick 事件倒计时",
        "on_fail": "超时 → DeviceTimeout → RECOVERY；等待期间联锁禁止任何臂动作",
    },
    "PICK_FINISHED_DRINK": {
        "handler": "_h_pick_finished_drink", "timeout": 30,
        "entry": "冲泡完成",
        "done": "arm.pick_finished_drink() 完成，持成品杯",
        "on_fail": "同 PICK_CUP",
    },
    "MOVE_TO_SERVE": {
        "handler": "_h_move_to_serve", "timeout": 40,
        "entry": "持成品杯",
        "done": "出餐位确认无杯（cup_present(serve) 为 True 则等 2s 重查，最多 3 次）"
                "且 arm.move_to(SERVE) 完成",
        "on_fail": "出餐位 3 次检查仍占用 → RECOVERY 一次后重试本态，再占用 → ERROR",
    },
    "SERVE": {
        "handler": "_h_serve", "timeout": 30,
        "entry": "臂已到出餐位",
        "done": "arm.serve() 递杯松爪完成；出餐位检测归零，转入取杯监测",
        "on_fail": "同 PICK_CUP",
    },
    "WAIT_CUSTOMER_PICKUP": {
        "handler": "_h_wait_customer_pickup", "timeout": 120, "timeout_margin": 30,
        "timeout_desc": "120s + 30s 收回动作余量",
        "entry": "成品已递到出餐位",
        "done": "每 2s 轮询 cup_present(serve)：杯被取走 → COMPLETE；超时 120s →"
                "臂收回（move_to SERVE + 取回 + home），订单仍 COMPLETE 且"
                " note=顾客未取杯已收回",
        "on_fail": "顾客未取杯不算失败（收回后照常完成）；收回动作失败按全局路由",
    },
    "COMPLETE": {
        "handler": "_h_complete", "timeout": 10,
        "entry": "交付结束（顾客已取杯，或超时由机械臂收回）",
        "done": "订单标记 completed，打 result 事件，回 IDLE",
        "on_fail": "理论不失败；未知异常兜底 → ERROR",
    },
    "RECOVERY": {
        "handler": "_h_recovery", "timeout": 60,
        "entry": "任一状态可重试失败（DeviceError retryable=True / DeviceTimeout 超时）",
        "done": "arm.reset() + 各电器 abort() 完成，随后重试原状态一次",
        "on_fail": "恢复动作本身失败或重试仍失败 → ERROR",
    },
    "ERROR": {
        "handler": None, "timeout": 15,
        "entry": "不可重试失败 / 重试仍失败 / 未知异常兜底",
        "done": "全部设备停止卸力（不走 emergency_stop 锁存；臂 stop 失败则降级 "
                "emergency_stop 保证卸力），订单标记 failed，打 result=failed",
        "on_fail": "终态，不再路由；exit 1，等待人工处理",
    },
    "EMERGENCY_STOP": {
        "handler": None, "timeout": 15,
        "entry": "EstopError（急停触发，不可自动重试）",
        "done": "arm.emergency_stop() + 全部电器 abort，订单 failed，打 result=estop",
        "on_fail": "终态；exit 1，等待外部 reset",
    },
}

# 主线流程（任一态失败按上面的全局规则路由 RECOVERY/ERROR，不在表内重复）
MAIN_FLOW = ["ORDER_RECEIVED", "CHECK_SYSTEM", "CHECK_CUP", "PICK_CUP",
             "MOVE_TO_MACHINE", "GRIND", "BREW", "WAIT_BREW",
             "PICK_FINISHED_DRINK", "MOVE_TO_SERVE", "SERVE",
             "WAIT_CUSTOMER_PICKUP", "COMPLETE"]

# 取杯/出餐位检查的轮询间隔（真机秒，实际等待经 time_scale 缩放）
CHECK_INTERVAL_SEC = 2.0


class _FatalFlow(Exception):
    """流程致命错误内部信号：进 ERROR 态（携带原因，不再重试）。"""


class CafeFSM:
    """表驱动咖啡制作状态机。设备只经 hardware/ 抽象接口访问。"""

    def __init__(self, devices, cfg, time_scale=1.0):
        self.dev = devices            # {"arm","cup","grinder","coffee","water"}
        self.cfg = cfg
        self.time_scale = time_scale  # 状态超时的缩放（SIM=0.02，真机 1.0）
        self.state = "IDLE"
        self.order_id = None
        self.drink_id = None
        self.recipe = None
        self.note = ""                # 订单备注（如 顾客未取杯已收回）
        # 杯状态跟踪（安全联锁的数据源：动作成功返回才算确认）
        self._cup_picked = False      # pick_cup 成功
        self._cup_placed = False      # place_cup 成功
        self._cup_holding = False     # 当前持杯
        self._correction = None       # 视觉纠偏量 (dx, dy)
        self._in_brew_phase = False   # GRIND/BREW/WAIT_BREW 期间置位，联锁禁止臂动作

    # ---------- 结构化事件 ----------

    def _event(self, **fields):
        """向 stdout 打一行 [EVENT] {json}（结构化事件总线，取代日志解析）。"""
        print("[EVENT] " + json.dumps(fields, ensure_ascii=False), flush=True)

    def _transition(self, next_state):
        prev = self.state
        log("FSM", f"状态转换 {prev} -> {next_state}")
        self.state = next_state
        self._event(type="state", state=next_state, prev=prev,
                    order_id=self.order_id, ts=round(time.time(), 3))

    def _emit_result(self, result, state, note=""):
        self._event(type="result", order_id=self.order_id, result=result,
                    state=state, note=note)

    # ---------- 超时包装与安全工具 ----------

    def _run_with_timeout(self, label, fn, timeout_sec):
        """在 daemon 线程中执行 fn 并 join(timeout_sec)。
        禁止 signal.alarm：本模块以后会被嵌入非主线程。
        超时抛 DeviceTimeout（retryable=True，走 RECOVERY 路由）；超时后工作线程
        仍为 daemon 留在后台，不阻塞进程退出——真机驱动必须自带底层超时，本层是兜底。
        子线程异常回收后由主线程原样重抛，类型不变。"""
        box = {}

        def target():
            try:
                box["value"] = fn()
            except BaseException as e:      # 子线程异常回收，绝不逃逸
                box["error"] = e

        t = threading.Thread(target=target, name=f"fsm-{label}", daemon=True)
        t.start()
        t.join(timeout_sec)
        if t.is_alive():
            raise DeviceTimeout(f"状态 {label} 超时（>{timeout_sec:.2f}s 未完成）")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _dev_scale(self, dev):
        """设备时间缩放：模拟设备自带 time_scale；真实/无属性设备回退到 FSM 级缩放。"""
        return float(getattr(dev, "time_scale", self.time_scale))

    def _arm(self, action, *args, **kwargs):
        """机械臂动作统一入口：冲泡阶段（GRIND/BREW/WAIT_BREW）联锁禁止臂动作。"""
        if self._in_brew_phase:
            raise DeviceError(f"安全联锁：冲泡阶段禁止臂动作 {action}", retryable=False)
        return getattr(self.dev["arm"], action)(*args, **kwargs)

    def _safe_shutdown(self, estop=False):
        """ERROR/EMERGENCY_STOP 收尾：全部设备停止/卸力。单步失败不影响后续步骤。"""
        try:
            if estop:
                self.dev["arm"].emergency_stop()   # 急停：立即卸力（接口要求幂等不抛）
            else:
                self.dev["arm"].stop()             # ERROR：停止卸力，不触发急停锁存
        except Exception as e:
            log("ERROR", f"机械臂停止异常（{e}），降级 emergency_stop 保证卸力")
            try:
                self.dev["arm"].emergency_stop()
            except Exception:
                pass
        for name in ("grinder", "coffee", "water"):
            try:
                self.dev[name].abort()
            except Exception as e:
                log("ERROR", f"{name} abort 异常（忽略，继续收尾）: {e}")
        try:
            self.dev["cup"].stop()
        except Exception:
            pass
        log("ESTOP" if estop else "ERROR", "全部设备已停止/卸力")

    def external_estop(self):
        """外部触发急停（如 CLI 收到 KeyboardInterrupt）：尽力卸力，不抛异常。"""
        try:
            self._safe_shutdown(estop=True)
        except Exception:
            pass

    # ---------- 状态执行框架 ----------

    def _state_timeout(self, name):
        spec = STATES[name]
        t = spec["timeout"]
        if callable(t):
            t = t(self)
        t = (t or 0) + spec.get("timeout_margin", 0)
        return t * self.time_scale

    def _call_state(self, name):
        handler = getattr(self, STATES[name]["handler"])
        return self._run_with_timeout(name, handler, self._state_timeout(name))

    def _exec(self, name):
        """执行已进入的状态 name，含失败路由：
        EstopError 直抛（→ EMERGENCY_STOP）；可重试失败 → RECOVERY 一次并重试一次；
        不可重试/重试仍失败/未知异常 → _FatalFlow（→ ERROR）。"""
        try:
            self._call_state(name)
            return
        except EstopError:
            raise                                   # 直达 run() → EMERGENCY_STOP
        except DeviceError as e:
            if not e.retryable:
                raise _FatalFlow(f"状态 {name} 不可重试失败: {e}") from e
            first_err = e
        except Exception as e:                      # 未知异常兜底（机械臂在 ERROR 中卸力）
            raise _FatalFlow(f"状态 {name} 未预期异常: {e!r}") from e
        # 可重试失败（含超时）→ RECOVERY 一次
        log("FSM", f"状态 {name} 可重试失败（{first_err}），进入 RECOVERY")
        self._recover_target = name
        self._transition("RECOVERY")
        try:
            self._call_state("RECOVERY")
        except EstopError:
            raise
        except Exception as e:
            raise _FatalFlow(f"RECOVERY 失败: {e}") from e
        log("FSM", f"恢复完成，重试状态 {name}")
        self._transition(name)
        try:
            self._call_state(name)
        except EstopError:
            raise
        except DeviceError as e:
            raise _FatalFlow(f"状态 {name} 重试仍失败: {e}") from e
        except Exception as e:
            raise _FatalFlow(f"状态 {name} 重试未预期异常: {e!r}") from e

    def run(self, drink_id, order_id, recipe):
        """执行一单咖啡流程，返回退出码（0 成功 / 1 失败含急停）。
        任何路径都不允许异常逃逸：全部归并到 ERROR/EMERGENCY_STOP 收尾。"""
        self.drink_id = drink_id
        self.order_id = order_id
        self.recipe = recipe
        self.note = ""
        self._correction = None
        self._cup_picked = self._cup_placed = self._cup_holding = False
        self._in_brew_phase = False
        self.state = "IDLE"
        log("FSM", f"===== 咖啡流程开始（订单 #{order_id}）=====")
        try:
            for name in MAIN_FLOW:
                self._transition(name)
                self._exec(name)
            self._emit_result("completed", "COMPLETE", self.note)
            self._transition("IDLE")
            log("FSM", "===== 咖啡流程完成 =====")
            return 0
        except EstopError as e:
            return self._enter_emergency_stop(str(e))
        except _FatalFlow as e:
            return self._enter_error(str(e))
        except Exception as e:          # 兜底：未知异常也走 ERROR，机械臂必须卸力
            return self._enter_error(f"未预期异常兜底: {e!r}")

    # ---------- 终态处理（收尾阶段绝不抛出） ----------

    def _enter_error(self, cause):
        try:
            log("ERROR", f"流程中止：{cause}")
            self._transition("ERROR")
            try:
                self._run_with_timeout("ERROR", lambda: self._safe_shutdown(estop=False),
                                       self._state_timeout("ERROR"))
            except Exception as e:
                log("ERROR", f"收尾动作异常（忽略，继续收尾）: {e}")
            self._emit_result("failed", "ERROR", cause)
        except Exception:
            pass
        return 1

    def _enter_emergency_stop(self, cause):
        try:
            log("ESTOP", f"急停：{cause}")
            self._transition("EMERGENCY_STOP")
            try:
                self._run_with_timeout("EMERGENCY_STOP",
                                       lambda: self._safe_shutdown(estop=True),
                                       self._state_timeout("EMERGENCY_STOP"))
            except Exception as e:
                log("ESTOP", f"急停收尾异常（忽略）: {e}")
            self._emit_result("estop", "EMERGENCY_STOP", cause)
            log("ESTOP", "等待外部 reset，本单终止")
        except Exception:
            pass
        return 1

    # ---------- 各状态 handler ----------

    def _h_order_received(self):
        log("ORDER", f"订单 #{self.order_id}: drink={self.drink_id} "
                     f"-> {self.recipe.summary()}")

    def _h_check_system(self):
        bad = []
        for name, dev in self.dev.items():
            if not getattr(dev, "critical", True):
                continue
            try:
                h = dev.health()
                ok = bool(h.get("ok"))
                detail = h.get("detail", "")
            except Exception as e:
                ok, detail = False, f"health() 异常: {e}"
            log("CHECK", f"{name}: {'OK' if ok else 'NG'}"
                         + (f"（{detail}）" if detail else ""))
            if not ok:
                bad.append(name)
        if bad:
            raise DeviceError(f"关键设备不健康: {', '.join(bad)}（禁止开始）",
                              retryable=False)

    def _h_check_cup(self):
        cup = self.dev["cup"]
        scale = self._dev_scale(cup)
        spot = None
        for attempt in range(1, 5):                     # 首次 + 重试 3 次
            spot = cup.locate()
            if spot:
                break
            log("CUP", f"取杯位未找到杯（第 {attempt} 次定位，最多重试 3 次）")
            if attempt < 4:
                time.sleep(CHECK_INTERVAL_SEC * scale)
        if not spot:
            raise DeviceError("取杯位无杯：重试 3 次仍未找到", retryable=True)
        log("CUP", f"杯口定位 像素({spot['u']:.1f}, {spot['v']:.1f})")
        if spot.get("x_mm") is None:
            log("CUP", "警告：无台面坐标（未标定），跳过视觉纠偏")
            self._correction = None
        else:
            dx = spot["x_mm"] - float(self.cfg.get("cup_ref_x_mm", 150.0))
            dy = spot["y_mm"] - float(self.cfg.get("cup_ref_y_mm", 90.0))
            self._correction = (dx, dy)
            log("CUP", f"台面坐标 ({spot['x_mm']:.1f}, {spot['y_mm']:.1f}) mm，"
                       f"纠偏量 dx={dx:+.1f} dy={dy:+.1f} mm")

    def _h_pick_cup(self):
        self._arm("pick_cup", self._correction)
        self._cup_picked = True
        self._cup_holding = True
        log("ARM", "取杯完成，夹爪持杯")

    def _h_move_to_machine(self):
        if not self._cup_picked:
            raise DeviceError("安全联锁：未取杯禁止放杯到冲泡位", retryable=False)
        self._arm("place_cup")
        self._cup_placed = True
        self._cup_holding = False
        log("ARM", "杯已放置到冲泡位（放置确认）")

    def _h_grind(self):
        if not self.recipe.needs_grinder:
            log("GRIND", f"配方 {self.recipe.slug} 无需磨豆（dose_g=0），跳过")
            return
        if not (self._cup_picked and self._cup_placed):
            raise DeviceError("安全联锁：杯未放置确认，禁止磨豆", retryable=False)
        grinder = self.dev["grinder"]
        self._in_brew_phase = True                      # 联锁：磨豆期间禁止臂动作
        try:
            grinder.run(self.recipe.grind_sec)
            grinder.wait_done(self.recipe.grind_sec)
        finally:
            self._in_brew_phase = False
        log("GRIND", f"磨豆完成（{self.recipe.dose_g:.0f}g / "
                     f"{self.recipe.grind_sec:.0f}s）")

    def _h_brew(self):
        if not self._cup_placed:
            raise DeviceError("安全联锁：杯未放置确认，禁止冲泡", retryable=False)
        self._in_brew_phase = True                      # 联锁：冲泡启动期间禁止臂动作
        try:
            self.dev["coffee"].run()                    # 点动：按一次启动键
        finally:
            self._in_brew_phase = False
        log("BREW", "滴滤机已启动冲泡")

    def _h_wait_brew(self):
        coffee = self.dev["coffee"]
        scale = self._dev_scale(coffee)                 # 经设备 time_scale 缩放
        total = int(round(self.recipe.brew_sec))
        log("BREW", f"冲泡等待 {total}s（实际 {total * scale:.2f}s）")
        self._in_brew_phase = True                      # 联锁：等待期间禁止臂动作
        try:
            for remain in range(total, 0, -1):
                self._event(type="brew_tick", remain_sec=remain,
                            total_sec=total, order_id=self.order_id)
                if remain % 30 == 0 or remain <= 3:
                    log("BREW", f"冲泡中... 剩余 {remain}s / 共 {total}s")
                coffee.tick()                           # 失控保护：超最大运行时间断电
                time.sleep(scale)
        finally:
            self._in_brew_phase = False
        log("BREW", "冲泡完成")

    def _h_pick_finished_drink(self):
        self._arm("pick_finished_drink")
        self._cup_holding = True
        log("ARM", "已取回成品杯")

    def _h_move_to_serve(self):
        if not self._cup_holding:
            raise DeviceError("安全联锁：未持成品杯禁止去出餐位", retryable=False)
        cup = self.dev["cup"]
        scale = self._dev_scale(cup)
        for attempt in range(1, 4):                     # 出餐位占用检查，最多 3 次
            if not cup.cup_present("serve"):
                break
            log("SERVE", f"出餐位被占用（第 {attempt}/3 次检查），等待后重查")
            if attempt < 3:
                time.sleep(CHECK_INTERVAL_SEC * scale)
        else:
            raise DeviceError("出餐位持续被占用，无法安全出餐", retryable=True)
        self._arm("move_to", "SERVE")

    def _h_serve(self):
        self._arm("serve")
        self._cup_holding = False
        # 出餐位检测归零：WAIT_CUSTOMER_PICKUP 从干净状态监测本单杯子
        #（SimCupDetector 按检查次数模拟"顾客稍后取走"；真实检测器 reset 为无操作）
        try:
            self.dev["cup"].reset()
        except Exception:
            pass
        log("SERVE", "成品已递到出餐位")

    def _h_wait_customer_pickup(self):
        cup = self.dev["cup"]
        scale = self._dev_scale(cup)
        deadline = time.time() + STATES["WAIT_CUSTOMER_PICKUP"]["timeout"] * scale
        while time.time() < deadline:
            if not cup.cup_present("serve"):
                log("SERVE", "顾客已取杯")
                return
            time.sleep(min(CHECK_INTERVAL_SEC * scale,
                           max(0.0, deadline - time.time())))
        # 超时：顾客未取杯。机械臂收回，订单仍 COMPLETE（差异记在 note）
        log("SERVE", "顾客超时未取杯，机械臂收回")
        self._arm("move_to", "SERVE")
        self._arm("pick_finished_drink")
        self._arm("home")
        self._cup_holding = False
        self.note = "顾客未取杯已收回"

    def _h_complete(self):
        log("FSM", f"订单 #{self.order_id} 完成"
                   + (f"（{self.note}）" if self.note else ""))

    def _h_recovery(self):
        target = getattr(self, "_recover_target", "?")
        log("RECOVERY", f"恢复：arm.reset() + 各电器 abort()，随后重试状态 {target}")
        self.dev["arm"].reset()
        for name in ("grinder", "coffee", "water"):
            try:
                self.dev[name].abort()
            except Exception as e:
                log("RECOVERY", f"{name} abort 异常（忽略，继续恢复）: {e}")
        self._in_brew_phase = False


# ---------- 子命令 ----------

def cmd_make(args):
    # ---- 初始化阶段（失败 exit 2）----
    if args.scenario and not os.path.exists(args.scenario):
        log("ERROR", f"故障场景文件不存在: {args.scenario}")
        return 2
    try:
        faults = load_scenario(args.scenario)
    except Exception as e:
        log("ERROR", f"故障场景加载失败: {e}")
        return 2
    try:
        cfg = default_fsm_config()
    except Exception as e:
        log("ERROR", f"加载 config.json 失败: {e}")
        return 2
    try:
        eng = RecipeEngine()
    except Exception as e:
        log("ERROR", f"配方引擎初始化失败: {e}")
        return 2
    if args.recipe:
        recipe = eng.recipes.get(args.recipe)
        if recipe is None:
            log("ERROR", f"未知配方 {args.recipe!r}，可选: {', '.join(eng.recipes)}")
            return 2
    else:
        recipe = eng.for_drink(args.drink)
    try:
        devices = make_devices(args.mode, cfg=cfg, faults=faults)
    except Exception as e:
        log("ERROR", f"设备组装失败: {e}")
        return 2

    # ---- 连接与执行 ----
    # connect 失败不在此致命：CHECK_SYSTEM 统一裁决（故障注入语义 exit 1）
    connect_all(devices, strict=False)
    time_scale = SIM_TIME_SCALE if args.mode == "SIM" else 1.0
    fsm = CafeFSM(devices, cfg, time_scale=time_scale)
    order_id = args.order_id or int(time.time())
    try:
        return fsm.run(args.drink, order_id, recipe)
    except KeyboardInterrupt:
        log("ESTOP", "键盘中断：按外部急停处理")
        fsm.external_estop()
        return 1
    except Exception as e:      # run() 设计上不抛；双保险兜底
        log("ERROR", f"run() 异常逃逸（设计外）: {e!r}")
        fsm.external_estop()
        return 1


def cmd_states(_args):
    """打印状态定义表：全部内容从 STATES 自动生成（单一数据源，不手写两份）。"""
    print(f"===== CafeFSM 状态定义表（{len(STATES)} 个状态，由 STATES 表自动生成）=====")
    print("全局失败路由：")
    print("  DeviceError(retryable=True) / DeviceTimeout(超时) → RECOVERY 一次"
          "（arm.reset()+各电器 abort()）后重试该状态一次")
    print("  retryable=False 或重试仍失败 → ERROR（除 emergency_stop 锁存外全部卸力停止，"
          "订单 failed，exit 1）")
    print("  EstopError → EMERGENCY_STOP（所有设备 stop/abort，订单 failed，"
          "等待外部 reset，exit 1）")
    print("  未知异常兜底 → ERROR（机械臂必须卸力）；任何路径异常不逃逸出 run()")
    print("安全联锁（内置不可关）：无杯禁止 GRIND/BREW；place_cup 未确认前禁止 GRIND；")
    print("  GRIND/BREW/WAIT_BREW 期间禁止任何臂动作；设备未 connect 禁止开始")
    print("主线流程: IDLE -> " + " -> ".join(MAIN_FLOW) + " -> IDLE")
    print(f"超时单位：真机秒；SIM 模式乘 {SIM_TIME_SCALE}\n")
    for name, spec in STATES.items():
        timeout = spec.get("timeout_desc")
        if not timeout:
            t = spec["timeout"]
            timeout = "—（无动作不执行）" if t is None else f"{t}s"
        print(f"[{name}]")
        print(f"    进入条件: {spec['entry']}")
        print(f"    完成条件: {spec['done']}")
        print(f"    超时:     {timeout}")
        print(f"    失败处理: {spec['on_fail']}")
        print(f"    handler:  {spec['handler'] or '—（起步/故障路径处理）'}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="cafe_fsm.py",
                                 description="完整咖啡制作状态机（TASK 3，表驱动 17 状态）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_make = sub.add_parser("make", help="执行一单咖啡制作流程")
    p_make.add_argument("--drink", type=int, required=True,
                        help="menu.json 饮品 id（无匹配走 default 配方）")
    p_make.add_argument("--order-id", type=int, default=None,
                        help="订单号（缺省取当前时间戳）")
    p_make.add_argument("--mode", default="SIM", choices=("SIM", "REAL", "HYBRID"),
                        help="设备模式（默认 SIM 全模拟）")
    p_make.add_argument("--scenario", default=None,
                        help="故障注入场景 yaml（键同 config/sim_scenario.yaml）")
    p_make.add_argument("--recipe", default=None,
                        help="配方 slug 覆盖（如 hot_water），缺省按 --drink 查 recipes.yaml")
    sub.add_parser("states", help="打印状态定义表（由 STATES 自动生成）")
    args = ap.parse_args()

    handlers = {"make": cmd_make, "states": cmd_states}
    sys.exit(handlers[args.cmd](args))


if __name__ == "__main__":
    main()
