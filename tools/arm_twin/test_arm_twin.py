#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TASK 21 自测：机械臂数字孪生。

纯标准库 unittest（yaml 已在项目依赖内），直接运行：
    python3 tools/arm_twin/test_arm_twin.py

覆盖任务书三个验证点：
  - 动作顺序合法：未 PICK_CUP 不能 PLACE_CUP / SERVE；持杯不能再 PICK
  - 每动作有模拟耗时与超时检查（注入超小 timeout 必报 TwinTimeoutError）
  - 急停插入后轨迹立即中止（aborted、急停后零新帧；RESET 后可恢复）
另固化：全流程 demo 合法可跑、SVG 落盘格式正确、占位位姿回退生效。
"""

import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from arm_twin import (  # noqa: E402
    DEMO_POSES_DEG, DEMO_SCRIPT, TwinArm, TwinEstopError, TwinStateError,
    TwinTimeoutError, forward_kinematics, load_poses, parse_script,
    render_ascii, render_svg, run_script)

POSES = {k: dict(v) for k, v in DEMO_POSES_DEG.items()}   # 测试用演示角度


class TestLegality(unittest.TestCase):
    """动作顺序合法性（holding 语义对齐 hardware/sim.py）。"""

    def test_full_demo_script_legal(self):
        """内置咖啡全流程跑通：无错误、不中止、末端位置随位姿变化。"""
        r = run_script(DEMO_SCRIPT, POSES)
        self.assertEqual(r['errors'], [])
        self.assertFalse(r['aborted'])
        self.assertEqual(r['final_pose'], 'HOME')
        self.assertFalse(r['holding'])
        # 末端轨迹行程足够大（HOME 竖直收起 ↔ CUP/SERVE 前伸，动作清晰可见）
        xs = [f['points'][3][0] for f in r['frames']]
        ys = [f['points'][3][1] for f in r['frames']]
        self.assertGreater(max(xs) - min(xs), 100.0)
        self.assertGreater(max(ys) - min(ys), 100.0)

    def test_place_without_pick_illegal(self):
        """未 PICK_CUP 不能 PLACE_CUP（TwinStateError）。"""
        r = run_script('PLACE_CUP\n', POSES)
        self.assertTrue(r['aborted'])
        self.assertIsInstance(r['errors'][0], TwinStateError)

    def test_serve_without_holding_illegal(self):
        """空手不能 SERVE。"""
        r = run_script('SERVE\n', POSES)
        self.assertIsInstance(r['errors'][0], TwinStateError)

    def test_pick_twice_illegal(self):
        """持杯状态再次 PICK_CUP 违法。"""
        r = run_script('PICK_CUP\nPICK_CUP\n', POSES)
        self.assertIsInstance(r['errors'][0], TwinStateError)
        self.assertEqual(len(r['actions']), 1)     # 第一次成功，第二次被拒

    def test_bad_verb_and_missing_arg(self):
        """脚本语法：未知动作 / MOVE_TO 缺参数都报 TwinStateError。"""
        with self.assertRaises(TwinStateError):
            parse_script('DANCE\n')
        with self.assertRaises(TwinStateError):
            parse_script('MOVE_TO\n')


class TestTimeout(unittest.TestCase):
    """每动作有模拟耗时与超时检查。"""

    def test_durations_recorded(self):
        """全流程每个动作都有正的模拟耗时，总时长=各动作之和。"""
        r = run_script(DEMO_SCRIPT, POSES)
        self.assertTrue(all(d > 0 for _, _, d in r['actions']))
        self.assertAlmostEqual(sum(d for _, _, d in r['actions']),
                               r['total_s'], places=2)

    def test_timeout_raises(self):
        """注入超小 timeout：大行程 MOVE_TO 必报 TwinTimeoutError。"""
        arm = TwinArm(POSES, timeouts={'MOVE_TO': 0.05})
        with self.assertRaises(TwinTimeoutError):
            arm.act('MOVE_TO', 'CUP')
        # 超时失败不更新位姿（状态保持 HOME，可重试）
        self.assertEqual(arm.pose, 'HOME')


class TestEstop(unittest.TestCase):
    """急停立即中止轨迹。"""

    def test_estop_aborts_trajectory(self):
        """脚本中段插入 EMERGENCY_STOP：后续动作零执行、零新帧。"""
        script = 'HOME\nMOVE_TO CUP\nEMERGENCY_STOP\nMOVE_TO SERVE\nSERVE\n'
        r = run_script(script, POSES)
        self.assertTrue(r['aborted'])
        self.assertIsInstance(r['errors'][0], TwinEstopError)
        done = [v for v, _, _ in r['actions']]
        self.assertEqual(done, ['HOME', 'MOVE_TO', 'EMERGENCY_STOP'])
        # 轨迹最后一帧就是急停帧，SERVE 位姿的帧不存在
        self.assertEqual(r['frames'][-1]['action'], 'EMERGENCY_STOP')
        self.assertNotIn('SERVE', {f['action'] for f in r['frames']})

    def test_estop_idempotent_and_reset_recovers(self):
        """急停幂等（重复 ESTOP 不抛）；RESET 后动作恢复。"""
        arm = TwinArm(POSES)
        arm.act('EMERGENCY_STOP')
        arm.act('EMERGENCY_STOP')          # 幂等
        with self.assertRaises(TwinEstopError):
            arm.act('HOME')
        arm.act('RESET')
        self.assertFalse(arm.estopped)
        arm.act('HOME')                    # 恢复后可动


class TestRenderAndPoses(unittest.TestCase):
    """可视化产物与位姿加载。"""

    def test_svg_written(self):
        """demo 轨迹落 SVG：存在、结构完整、有实质内容。"""
        r = run_script(DEMO_SCRIPT, POSES)
        tmp = tempfile.mkdtemp(prefix='arm_twin_test_')
        path = render_svg(r, os.path.join(tmp, 't.svg'))
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding='utf-8') as f:
            svg = f.read()
        self.assertTrue(svg.startswith('<svg'))
        self.assertTrue(svg.rstrip().endswith('</svg>'))
        self.assertGreater(len(svg), 500)
        self.assertIn('SERVE', svg)          # 关键姿态标签画进去了

    def test_ascii_shape(self):
        """ASCII 帧：含台面线、底座 B、末端标记。"""
        pts = forward_kinematics(POSES['CUP'])
        art = render_ascii(pts, 't', holding=True)
        self.assertIn('B', art)
        self.assertIn('C', art)              # 持杯时末端画 C
        self.assertIn('-' * 20, art)         # 台面线

    def test_placeholder_fallback(self):
        """仓库当前 poses.yaml 全是占位中位值 → 回退演示角度且姿态可分。"""
        poses, used_fallback = load_poses()
        self.assertTrue(used_fallback)
        self.assertNotEqual(poses['HOME'], poses['CUP'])

    def test_forward_kinematics_reach(self):
        """HOME（全 0°）竖直向上：末端 y ≈ 连杆总长，x ≈ 0。"""
        pts = forward_kinematics(POSES['HOME'])
        self.assertAlmostEqual(pts[3][0], 0.0, places=6)
        self.assertAlmostEqual(pts[3][1], 270.0, places=6)


if __name__ == '__main__':
    unittest.main(verbosity=2)
