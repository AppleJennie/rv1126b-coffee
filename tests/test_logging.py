#!/usr/bin/env python3
# tests/test_logging.py —— TASK 28 统一结构化日志 + 每杯订单报告自动测试
#
# 纯标准库 unittest（子进程跑 projects/coffee_fsm/cafe_fsm.py，不依赖 pytest）：
#   python3 tests/test_logging.py        # 直接运行，结尾打印 总计 N PASS / M FAIL
#
# 覆盖：
#   - emit() 单元级：必填字段齐全、可选字段按需写入、level 大写归一
#   - 正常单（SIM make）：logs/cafe-*.jsonl 有本单 cafe_fsm/hardware 结构化行，
#     每行 JSON 可解析且必填字段齐全；控制台 [HH:MM:SS] [TAG] 格式未变；
#     [EVENT] 契约未破（state/brew_tick/result 齐全，result 事件带 report 路径）；
#     reports/order_<id>.json 存在，含完整状态序列（== MAIN_FLOW）与 result=completed
#   - 故障单（cup_missing 临时 yaml，用完删）：exit 1，reports/order_<id>.json
#     result=failed 且 error 非空；JSONL 有对应 ORDER_REPORT 行（level=ERROR）
#   - 测试产生的 reports/order_<测试id>.json 跑完即删，不留垃圾

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAFE_FSM = os.path.join(ROOT, "projects", "coffee_fsm", "cafe_fsm.py")
LOG_DIR = os.path.join(ROOT, "logs")
REPORTS_DIR = os.path.join(ROOT, "reports")
for _p in (ROOT, os.path.join(ROOT, "projects", "coffee_fsm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from projects.common.structured_log import OPTIONAL_FIELDS, emit  # noqa: E402
from cafe_fsm import MAIN_FLOW                                    # noqa: E402

SUBPROCESS_TIMEOUT = 90     # 子进程死等兜底（正常 SIM 单约 5~8s，余量充足）
ORDER_OK = 771001           # 测试用订单号（避开真实时间戳订单号段）
ORDER_FAIL = 771002

REQUIRED_FIELDS = ("timestamp", "module", "event", "level", "message")
CONSOLE_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] \[[A-Z0-9_\-]+\] .+")


def run_cafe(order_id, extra=()):
    """子进程执行一单 cafe_fsm make（SIM），返回 (exit_code, stdout, t0)。
    t0 为发起时刻，用于在 JSONL 里按 timestamp 过滤本单日志行。"""
    cmd = [sys.executable, CAFE_FSM, "make", "--drink", "1",
           "--order-id", str(order_id), "--mode", "SIM", *extra]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=SUBPROCESS_TIMEOUT)
    return proc.returncode, proc.stdout, t0


def jsonl_lines_since(t0):
    """读取 logs/cafe-*.jsonl 中 timestamp >= t0（留 1s 余量）的结构化行。"""
    lines = []
    if not os.path.isdir(LOG_DIR):
        return lines
    for name in sorted(os.listdir(LOG_DIR)):
        if not (name.startswith("cafe-") and name.endswith(".jsonl")):
            continue
        with open(os.path.join(LOG_DIR, name), encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                rec = json.loads(raw)          # 不可解析直接抛错 = 测试失败
                if rec.get("timestamp", 0) >= t0 - 1.0:
                    lines.append(rec)
    return lines


def parse_events(stdout):
    """从 stdout 提取 [EVENT] {json} 行（与 kiosk _parse_cafe_event 同契约）。"""
    events = []
    for line in stdout.splitlines():
        if line.startswith("[EVENT] "):
            events.append(json.loads(line[len("[EVENT] "):]))
    return events


class TestEmitSchema(unittest.TestCase):
    """emit() 单元级：字段契约（必填齐全 / 可选按需 / level 归一）。"""

    def test_emit_fields(self):
        rec = emit("test_logging", "SELFTEST", "字段自测",
                   order_id=0, result="ok", 无关字段="应被忽略")
        for k in REQUIRED_FIELDS:
            self.assertIn(k, rec)
        # 白名单之外无字段漂移：除必填外只允许 OPTIONAL_FIELDS
        extra = set(rec) - set(REQUIRED_FIELDS)
        self.assertTrue(extra.issubset(set(OPTIONAL_FIELDS)), extra)
        self.assertEqual(rec["module"], "test_logging")
        self.assertEqual(rec["event"], "SELFTEST")
        self.assertEqual(rec["level"], "INFO")
        self.assertEqual(rec["order_id"], 0)
        self.assertEqual(rec["result"], "ok")
        self.assertNotIn("无关字段", rec)            # 白名单外字段不写入
        self.assertNotIn("error", rec)               # None 可选字段不写入
        rec2 = emit("test_logging", "SELFTEST", "级别归一", level="error")
        self.assertEqual(rec2["level"], "ERROR")


class TestNormalOrder(unittest.TestCase):
    """正常单：JSONL 结构化行 + 订单报告 + 控制台/[EVENT] 双契约。"""

    @classmethod
    def setUpClass(cls):
        cls.report_path = os.path.join(REPORTS_DIR, f"order_{ORDER_OK}.json")
        if os.path.exists(cls.report_path):
            os.remove(cls.report_path)
        cls.rc, cls.stdout, cls.t0 = run_cafe(ORDER_OK)
        cls.lines = jsonl_lines_since(cls.t0)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.report_path):
            os.remove(cls.report_path)

    def test_exit_ok(self):
        self.assertEqual(self.rc, 0, f"正常单应 exit 0\n{self.stdout[-2000:]}")
        self.assertNotIn("Traceback", self.stdout)

    def test_console_format_kept(self):
        """控制台人类可读行格式未变：[HH:MM:SS] [TAG] 消息。"""
        human = [l for l in self.stdout.splitlines() if CONSOLE_RE.match(l)]
        self.assertTrue(human, "控制台缺少人类可读日志行")
        self.assertTrue(any("[FSM] 状态转换" in l for l in human),
                        "FSM 状态转换日志格式变了（kiosk 正则契约）")
        self.assertTrue(any("[BREW]" in l for l in human))

    def test_event_contract_kept(self):
        """[EVENT] 契约：state 序列完整、brew_tick 存在、result 带 report 路径。"""
        events = parse_events(self.stdout)
        states = [e["state"] for e in events if e.get("type") == "state"]
        self.assertEqual(states[: len(MAIN_FLOW)], MAIN_FLOW)
        self.assertTrue(any(e.get("type") == "brew_tick" for e in events))
        results = [e for e in events if e.get("type") == "result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["result"], "completed")
        self.assertEqual(results[0].get("report"),
                         os.path.join(REPORTS_DIR, f"order_{ORDER_OK}.json"))

    def test_jsonl_structured_lines(self):
        """logs/cafe-*.jsonl 有本单 cafe_fsm/hardware 行，逐行字段齐全。"""
        self.assertTrue(self.lines, "JSONL 无本运行日志行")
        for rec in self.lines:
            for k in REQUIRED_FIELDS:
                self.assertIn(k, rec, f"JSONL 行缺字段 {k}: {rec}")
        modules = {rec["module"] for rec in self.lines}
        self.assertIn("cafe_fsm", modules)
        self.assertIn("hardware", modules)
        events = {rec["event"] for rec in self.lines}
        self.assertIn("FSM", events)                # 旧 TAG 进入 event 字段
        # 订单报告结构化行：order_id/result/duration_sec 可选字段在位
        orp = [rec for rec in self.lines
               if rec["event"] == "ORDER_REPORT" and rec.get("order_id") == ORDER_OK]
        self.assertEqual(len(orp), 1)
        self.assertEqual(orp[0]["result"], "completed")
        self.assertGreater(orp[0]["duration_sec"], 0)
        self.assertEqual(orp[0]["level"], "INFO")

    def test_order_report_json(self):
        """reports/order_<id>.json：状态序列 == MAIN_FLOW，result=completed。"""
        self.assertTrue(os.path.exists(self.report_path), "订单报告未生成")
        with open(self.report_path, encoding="utf-8") as f:
            rep = json.load(f)
        self.assertEqual(rep["order_id"], ORDER_OK)
        self.assertEqual(rep["drink_id"], 1)
        self.assertTrue(rep["recipe"])
        self.assertEqual(rep["result"], "completed")
        self.assertEqual(rep["error"], "")
        names = [s["state"] for s in rep["states"]]
        self.assertEqual(names, MAIN_FLOW)
        for s in rep["states"]:
            self.assertIn("enter_ts", s)
            self.assertIn("exit_ts", s)
            self.assertGreaterEqual(s["duration_sec"], 0)
        self.assertGreater(rep["total_duration_sec"], 0)
        self.assertGreaterEqual(rep["finished_at"], rep["started_at"])


class TestFaultOrder(unittest.TestCase):
    """故障单（cup_missing）：报告 result=failed 且 error 非空。"""

    @classmethod
    def setUpClass(cls):
        cls.report_path = os.path.join(REPORTS_DIR, f"order_{ORDER_FAIL}.json")
        if os.path.exists(cls.report_path):
            os.remove(cls.report_path)
        # 临时故障场景 yaml（用完即删）：取杯位无杯 -> RECOVERY 后仍失败 -> ERROR
        fd, cls.scenario = tempfile.mkstemp(prefix="tl_scenario_", suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("cup_missing: true\n")
        cls.rc, cls.stdout, cls.t0 = run_cafe(
            ORDER_FAIL, extra=("--scenario", cls.scenario))
        cls.lines = jsonl_lines_since(cls.t0)

    @classmethod
    def tearDownClass(cls):
        for p in (cls.report_path, cls.scenario):
            if p and os.path.exists(p):
                os.remove(p)

    def test_fault_exit_1(self):
        self.assertEqual(self.rc, 1, f"故障单应 exit 1\n{self.stdout[-2000:]}")
        self.assertNotIn("Traceback", self.stdout)

    def test_fault_report(self):
        self.assertTrue(os.path.exists(self.report_path), "故障单订单报告未生成")
        with open(self.report_path, encoding="utf-8") as f:
            rep = json.load(f)
        self.assertEqual(rep["order_id"], ORDER_FAIL)
        self.assertEqual(rep["result"], "failed")
        self.assertTrue(rep["error"], "故障单 error 字段不能为空")
        names = [s["state"] for s in rep["states"]]
        self.assertIn("RECOVERY", names)
        self.assertEqual(names[-1], "ERROR")
        self.assertGreater(rep["total_duration_sec"], 0)

    def test_fault_jsonl_order_report(self):
        orp = [rec for rec in self.lines
               if rec["event"] == "ORDER_REPORT" and rec.get("order_id") == ORDER_FAIL]
        self.assertEqual(len(orp), 1)
        self.assertEqual(orp[0]["result"], "failed")
        self.assertTrue(orp[0].get("error"), "JSONL ORDER_REPORT 行 error 不能为空")
        self.assertEqual(orp[0]["level"], "ERROR")
        # ERROR/ESTOP 旧 TAG 自动归 ERROR level
        self.assertTrue(any(rec["level"] == "ERROR" and rec["module"] == "cafe_fsm"
                            for rec in self.lines))


def main():
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    n_fail = len(result.failures) + len(result.errors)
    print(f"\n===== 总计 {result.testsRun - n_fail} PASS / {n_fail} FAIL =====")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
