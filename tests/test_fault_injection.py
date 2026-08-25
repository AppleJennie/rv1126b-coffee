#!/usr/bin/env python3
# tests/test_fault_injection.py —— TASK 6 故障注入自动测试 + TASK 7 安全联锁测试
#
# 纯标准库 unittest（子进程跑 projects/coffee_fsm/cafe_fsm.py，不依赖 pytest）：
#   python3 tests/test_fault_injection.py        # 直接运行，结尾打印 总计 N PASS / M FAIL
#
# 覆盖：
#   - 正常路径：默认单 / hot_water 跳磨豆 / --recipe 覆盖 / --order-id 透传 / 事件序列完整性
#   - 11 种故障注入各一案（config/sim_scenario.yaml 的全部键；终态 + 退出码 + result 事件）
#   - 安全联锁专项（TASK 7）：无杯不出液 / 设备离线不开工 / 出餐位占用不出餐 /
#     异常路径臂卸力日志 / 冲泡期间禁止臂动作
#   - 场景文件用 tempfile 现写现删，不留垃圾
#   - 设备层直接调用（不跑子进程）：继电器粘连 wait_done 抛错且如实上报；急停拒绝动作
#
# 关键语义以 TASK 3 已定行为为准（见各断言注释），本文件只断言、不修改被测代码。

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAFE_DIR = os.path.join(ROOT, "projects", "coffee_fsm")
CAFE_FSM = os.path.join(CAFE_DIR, "cafe_fsm.py")
for _p in (ROOT, CAFE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cafe_fsm import MAIN_FLOW, CafeFSM                      # noqa: E402
from hardware import DeviceError                              # noqa: E402
from hardware.factory import connect_all, default_fsm_config, make_devices  # noqa: E402

SUBPROCESS_TIMEOUT = 60     # 每个子进程用例的死等兜底（秒；正常单约 5s，余量充足）


def parse_events(stdout):
    """从子进程 stdout 提取 [EVENT] {json} 结构化事件。"""
    events = []
    for line in stdout.splitlines():
        if line.startswith("[EVENT] "):
            events.append(json.loads(line[len("[EVENT] "):]))
    return events


def states_of(events):
    """状态转换事件序列（type=state 的 state 字段，按出现顺序）。"""
    return [e["state"] for e in events if e.get("type") == "state"]


def results_of(events):
    return [e for e in events if e.get("type") == "result"]


class _FSMCase(unittest.TestCase):
    """公共工具：真实子进程跑 cafe_fsm make + 通用断言。"""

    def run_fsm(self, extra=(), faults=None):
        """子进程执行一单咖啡流程。

        faults 非空时用 tempfile 现写场景 yaml（键同 config/sim_scenario.yaml），
        跑完即删，不留垃圾。返回 (exit_code, stdout, events, scenario_path)。
        死等（>60s 未退出）或无 Traceback 检查失败直接判 FAIL。
        """
        scenario = None
        if faults:
            fd, scenario = tempfile.mkstemp(prefix="fi_scenario_", suffix=".yaml")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for k, v in faults.items():
                    f.write(f"{k}: {str(v).lower()}\n")
        cmd = [sys.executable, CAFE_FSM, "make", "--drink", "1", *extra]
        if scenario:
            cmd += ["--scenario", scenario]
        try:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace",
                                      timeout=SUBPROCESS_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.fail(f"子进程死等（>{SUBPROCESS_TIMEOUT}s 未退出）: {' '.join(cmd)}")
        finally:
            if scenario:                    # 场景文件现写现删
                os.unlink(scenario)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr,
                         f"子进程输出含 Traceback:\n{proc.stdout[-1500:]}")
        return proc.returncode, proc.stdout, parse_events(proc.stdout), scenario

    def assert_fault(self, faults, expect_recovery, note_kw,
                     terminal="ERROR", exit_code=1, result="failed"):
        """故障注入用例的通用断言：终态 + 退出码 + result 事件 + RECOVERY 路由。"""
        rc, out, events, _ = self.run_fsm(faults=faults)
        seq, res = states_of(events), results_of(events)
        self.assertEqual(rc, exit_code, f"退出码应为 {exit_code}:\n{out[-800:]}")
        self.assertEqual(seq[0], "ORDER_RECEIVED")          # 首事件必为接单
        self.assertEqual(seq[-1], terminal)                 # 终态
        self.assertEqual(len(res), 1, "必须恰好一个 result 事件")
        self.assertEqual(res[0]["result"], result)
        self.assertEqual(res[0]["state"], terminal)
        if note_kw:
            self.assertIn(note_kw, res[0]["note"])
        if expect_recovery:                                 # 可重试故障必经 RECOVERY
            self.assertIn("RECOVERY", seq)
        else:                                               # 不可重试故障直达终态
            self.assertNotIn("RECOVERY", seq)
        return seq, out, events


class NormalPathTest(_FSMCase):
    """正常路径：无故障注入。"""

    def test_default_order_completes(self):
        """默认单（drink 1 -> americano）：COMPLETE exit=0，状态严格沿 MAIN_FLOW 推进。"""
        rc, out, events, _ = self.run_fsm()
        self.assertEqual(rc, 0, out[-800:])
        seq = states_of(events)
        self.assertEqual(seq, MAIN_FLOW + ["IDLE"])         # 单调推进并回 IDLE
        res = results_of(events)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["result"], "completed")
        self.assertEqual(res[0]["state"], "COMPLETE")
        totals = [e["total_sec"] for e in events if e.get("type") == "brew_tick"]
        self.assertTrue(totals and max(totals) == 180)      # americano brew_sec=180

    def test_hot_water_recipe_skips_grind(self):
        """hot_water 配方（dose_g=0）：GRIND 跳过，磨豆机从不通电。"""
        rc, out, events, _ = self.run_fsm(extra=["--recipe", "hot_water"])
        self.assertEqual(rc, 0, out[-800:])
        self.assertIn("无需磨豆", out)                      # GRIND 态进入但跳过
        self.assertNotIn("grinder 通电运行", out)           # 磨豆机未通电
        totals = [e["total_sec"] for e in events if e.get("type") == "brew_tick"]
        self.assertTrue(totals and max(totals) == 30)       # hot_water brew_sec=30

    def test_recipe_override_double_americano(self):
        """--recipe 覆盖缺省配方：按 double_americano 执行（brew_sec=200）。"""
        rc, out, events, _ = self.run_fsm(extra=["--recipe", "double_americano"])
        self.assertEqual(rc, 0, out[-800:])
        self.assertIn("双份美式", out)
        totals = [e["total_sec"] for e in events if e.get("type") == "brew_tick"]
        self.assertTrue(totals and max(totals) == 200)

    def test_order_id_passthrough(self):
        """--order-id 透传：全部事件（state/brew_tick/result）携带同一订单号。"""
        rc, out, events, _ = self.run_fsm(extra=["--order-id", "424242"])
        self.assertEqual(rc, 0, out[-800:])
        self.assertTrue(events, "应产生事件")
        for e in events:
            self.assertEqual(e.get("order_id"), 424242,
                             f"事件未透传 order_id: {e}")

    def test_event_sequence_integrity(self):
        """事件序列完整性：首事件 ORDER_RECEIVED(prev=IDLE)，唯一 result 为最后业务事件
        （其后仅允许回 IDLE 的转换），prev 链严格衔接。"""
        rc, out, events, _ = self.run_fsm(extra=["--order-id", "7"])
        self.assertEqual(rc, 0, out[-800:])
        first = events[0]
        self.assertEqual(first["type"], "state")
        self.assertEqual(first["state"], "ORDER_RECEIVED")
        self.assertEqual(first["prev"], "IDLE")
        res = results_of(events)
        self.assertEqual(len(res), 1)
        idx = events.index(res[0])
        for e in events[idx + 1:]:                          # result 之后只允许回 IDLE
            self.assertEqual(e["type"], "state")
            self.assertEqual(e["state"], "IDLE")
        prev = "IDLE"                                       # prev 链校验
        for e in events:
            if e.get("type") == "state":
                self.assertEqual(e["prev"], prev)
                prev = e["state"]


class FaultInjectionTest(_FSMCase):
    """11 种故障注入各一案（键同 config/sim_scenario.yaml）。"""

    def test_fault_robot_arm_fail(self):
        """robot_arm_fail：PICK_CUP 处 DeviceError 不可重试 -> ERROR exit=1（无 RECOVERY）。"""
        seq, _, _ = self.assert_fault({"robot_arm_fail": True},
                                      expect_recovery=False, note_kw="PICK_CUP")
        self.assertIn("PICK_CUP", seq)
        self.assertNotIn("MOVE_TO_MACHINE", seq)            # 失败点之后不再推进

    def test_fault_robot_arm_hang(self):
        """robot_arm_hang：动作超时 DeviceTimeout -> RECOVERY -> 重试仍超时 -> ERROR exit=1。"""
        seq, _, _ = self.assert_fault({"robot_arm_hang": True},
                                      expect_recovery=True, note_kw="超时（长期 BUSY）")
        self.assertEqual(seq.count("PICK_CUP"), 2)          # 原态 + 重试各一次

    def test_fault_robot_arm_offline(self):
        """robot_arm_offline：connect 失败 -> CHECK_SYSTEM 检出臂不健康 -> ERROR exit=1。"""
        seq, _, _ = self.assert_fault({"robot_arm_offline": True},
                                      expect_recovery=False, note_kw="禁止开始")
        self.assertEqual(seq[-2], "CHECK_SYSTEM")           # 裁决于系统检查
        self.assertNotIn("PICK_CUP", seq)

    def test_fault_cup_missing(self):
        """cup_missing：CHECK_CUP 3 次重试仍无杯 -> RECOVERY -> 仍无杯 -> ERROR exit=1。"""
        seq, out, _ = self.assert_fault({"cup_missing": True},
                                        expect_recovery=True, note_kw="无杯")
        self.assertEqual(seq.count("CHECK_CUP"), 2)
        self.assertIn("重试 3 次仍未找到", out)

    def test_fault_vision_timeout(self):
        """vision_timeout：locate 超时 -> RECOVERY -> 仍超时 -> ERROR exit=1。"""
        seq, _, _ = self.assert_fault({"vision_timeout": True},
                                      expect_recovery=True, note_kw="视觉采帧超时")
        self.assertEqual(seq.count("CHECK_CUP"), 2)

    def test_fault_grinder_timeout(self):
        """grinder_timeout：GRIND 通电指令无应答 -> RECOVERY -> 重试仍败 -> ERROR exit=1。"""
        seq, _, _ = self.assert_fault({"grinder_timeout": True},
                                      expect_recovery=True, note_kw="指令无应答")
        self.assertEqual(seq.count("GRIND"), 2)

    def test_fault_grinder_stuck_on(self):
        """grinder_stuck_on：GRIND 结束断电回读发现仍通电（继电器粘连）-> ERROR exit=1，
        不可重试（最大运行时间/断电校验生效，需人工检查）。"""
        seq, _, _ = self.assert_fault({"grinder_stuck_on": True},
                                      expect_recovery=False, note_kw="断电无效")
        self.assertEqual(seq[-2], "GRIND")

    def test_fault_coffee_machine_timeout(self):
        """coffee_machine_timeout：BREW 点动无应答 -> RECOVERY -> 重试仍败 -> ERROR exit=1。"""
        seq, _, _ = self.assert_fault({"coffee_machine_timeout": True},
                                      expect_recovery=True, note_kw="指令无应答")
        self.assertEqual(seq.count("BREW"), 2)

    def test_fault_hot_water_timeout_unused_in_flow(self):
        """hot_water_timeout：热水设备当前流程未使用（仅 RECOVERY/ERROR 收尾会 abort），
        health 不受 hang 影响 -> 本单正常 COMPLETE exit=0。"""
        rc, out, events, _ = self.run_fsm(faults={"hot_water_timeout": True})
        self.assertEqual(rc, 0, out[-800:])
        res = results_of(events)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["result"], "completed")

    def test_fault_wifi_disconnect(self):
        """wifi_disconnect：全部 WiFi 开关离线 -> CHECK_SYSTEM 检出电器不健康
        -> ERROR exit=1（设备离线禁止开始，不进 RECOVERY）。"""
        seq, _, _ = self.assert_fault({"wifi_disconnect": True},
                                      expect_recovery=False, note_kw="禁止开始")
        self.assertEqual(seq[-2], "CHECK_SYSTEM")

    def test_fault_customer_not_take_cup(self):
        """customer_not_take_cup：出餐位永久占用 -> MOVE_TO_SERVE 3 次检查失败
        -> RECOVERY -> 仍占用 -> ERROR exit=1（安全联锁优先，规格内正确行为）。"""
        seq, _, _ = self.assert_fault({"customer_not_take_cup": True},
                                      expect_recovery=True, note_kw="出餐位")
        self.assertEqual(seq.count("MOVE_TO_SERVE"), 2)


class SafetyInterlockTest(_FSMCase):
    """TASK 7 安全联锁专项（清单见 docs/SAFETY_INTERLOCK.md）。"""

    def test_interlock_no_cup_no_dispense(self):
        """联锁：无杯禁止出液——cup_missing 时事件流里 GRIND/BREW 永不出现。"""
        rc, _, events, _ = self.run_fsm(faults={"cup_missing": True})
        self.assertEqual(rc, 1)
        seq = states_of(events)
        self.assertNotIn("GRIND", seq)
        self.assertNotIn("BREW", seq)
        self.assertNotIn("WAIT_BREW", seq)

    def test_interlock_offline_no_start(self):
        """联锁：设备离线禁止开工——wifi_disconnect 时 PICK_CUP 永不出现。"""
        rc, _, events, _ = self.run_fsm(faults={"wifi_disconnect": True})
        self.assertEqual(rc, 1)
        self.assertNotIn("PICK_CUP", states_of(events))

    def test_interlock_serve_occupied_no_serve(self):
        """联锁：出餐位有杯禁止出餐——customer_not_take_cup 时 SERVE 永不出现。"""
        rc, _, events, _ = self.run_fsm(faults={"customer_not_take_cup": True})
        self.assertEqual(rc, 1)
        self.assertNotIn("SERVE", states_of(events))

    def test_interlock_error_path_disarm_log(self):
        """联锁：异常路径臂必须卸力——ERROR 收尾日志含停止/卸力字样。"""
        rc, out, _, _ = self.run_fsm(faults={"robot_arm_fail": True})
        self.assertEqual(rc, 1)
        self.assertIn("卸力", out)
        self.assertIn("全部设备已停止", out)

    def test_interlock_brew_phase_blocks_arm(self):
        """联锁（直接调用层，不跑子进程）：冲泡阶段（GRIND/BREW/WAIT_BREW）禁止任何臂动作。"""
        devices = make_devices("SIM")
        fsm = CafeFSM(devices, default_fsm_config(), time_scale=0.02)
        fsm._in_brew_phase = True                           # 模拟处于冲泡阶段
        with self.assertRaises(DeviceError) as ctx:
            fsm._arm("home")
        self.assertFalse(ctx.exception.retryable)           # 联锁违规不可重试
        self.assertIn("冲泡阶段禁止臂动作", str(ctx.exception))


class ScenarioFileTest(_FSMCase):
    """故障场景文件：tempfile 现写现删；文件不存在走初始化失败 exit=2。"""

    def test_scenario_tempfile_write_run_cleanup(self):
        """现写的场景文件真实生效（cup_missing -> exit 1），跑完即删不留垃圾。"""
        rc, _, _, scenario = self.run_fsm(faults={"cup_missing": True})
        self.assertEqual(rc, 1)                             # 故障生效
        self.assertTrue(scenario and scenario.startswith(tempfile.gettempdir()))
        self.assertFalse(os.path.exists(scenario))          # 已删除

    def test_scenario_missing_file_exit2(self):
        """场景文件不存在：初始化失败 exit=2，无 result 事件，无 Traceback。"""
        rc, out, events, _ = self.run_fsm(
            extra=["--scenario", "/nonexistent/fi_scenario.yaml"])
        self.assertEqual(rc, 2)
        self.assertIn("故障场景文件不存在", out)
        self.assertEqual(results_of(events), [])


class DeviceLayerTest(unittest.TestCase):
    """设备层直接调用（make_devices，不跑子进程）。"""

    def test_stuck_on_wait_done_raises_and_reports(self):
        """继电器粘连（grinder_stuck_on）：wait_done 断电回读发现仍通电，抛 DeviceError
        （不可重试），且开关状态如实上报（is_on()=True / status=on）。"""
        devices = make_devices("SIM", faults={"grinder_stuck_on": True})
        grinder = devices["grinder"]
        connect_all(devices, strict=False)
        grinder.run(2)                                      # 通电运行（短时即可）
        with self.assertRaises(DeviceError) as ctx:
            grinder.wait_done(2)
        self.assertFalse(ctx.exception.retryable)           # 粘连属硬件故障，不可自动重试
        self.assertIn("断电无效", str(ctx.exception))
        self.assertTrue(grinder.switch.is_on())             # 如实上报：仍通电
        self.assertEqual(grinder.switch.status()["state"], "on")

    def test_estop_blocks_actions_until_reset(self):
        """急停：emergency_stop 后全部臂动作拒绝（DeviceError 不可重试），
        状态如实上报 estop；reset 后恢复可用。"""
        devices = make_devices("SIM")
        arm = devices["arm"]
        connect_all(devices, strict=False)
        arm.emergency_stop()
        self.assertEqual(arm.status()["state"], "estop")
        with self.assertRaises(DeviceError) as ctx:
            arm.home()
        self.assertFalse(ctx.exception.retryable)
        arm.reset()
        arm.home()                                          # 复位后不再抛
        self.assertEqual(arm.status()["pose"], "HOME")


def main():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    n_fail = len(result.failures) + len(result.errors)
    print(f"\n===== 总计 {result.testsRun - n_fail} PASS / {n_fail} FAIL =====")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
