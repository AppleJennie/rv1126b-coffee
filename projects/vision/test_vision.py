#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉层 mock 测试（TASK 15/16/17/18）—— 全程合成帧，不依赖真摄像头。

运行：python3 projects/vision/test_vision.py
（纯标准库 unittest + 合成 numpy/cv2 帧；不保存任何图像，不涉及真实人脸数据）

覆盖：
  - 疲劳检测（TASK 15）：blink / long_eye_close / yawn / normal 四场景，
    合成帧经 像素启发式分析器 → 时间窗状态机 全链路驱动（虚拟时间戳）
  - 表情接口（TASK 16）：Mock 脚本化 / CPU Haar 可跑 / RKNN 桩抛 NotImplementedError
  - 杯检测（TASK 18）：合成「空台面 / 有杯」两图，背景差分与 Hough 回退都断言
  - VisionManager（TASK 17）：事件去抖、gone 超时、能力开关、demo 脚本事件流
  - 隐私（TASK 12）：配置加载 / 事件 dict 纯标量 / 日志白名单子集 / 全程零落盘
"""

import os
import subprocess
import sys
import tempfile
import unittest

import cv2  # noqa: F401  （确保板端依赖在本机可用，问题在测试期暴露）

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fatigue_detector import (  # noqa: E402
    AWAKE, POSSIBLY_TIRED, FatigueDetector, FatigueWindowSM,
    HeuristicEyeMouthAnalyzer)
from expression import (  # noqa: E402
    CPUExpression, LABELS, MockExpression, RKNNExpression,
    make_expression_backend)
from cup_presence import CupPresenceDetector  # noqa: E402
from vision_manager import (  # noqa: E402
    CUP_PRESENT, CUP_REMOVED, HAPPY, MOCK_CUP_ROI, MOCK_FACE_BOX,
    PERSON_LEFT, PERSON_PRESENT, TIRED, MockFrameSource, VisionManager,
    demo_script, load_privacy_config, render_mock_frame)

FPS = 5   # 合成场景的虚拟帧率


def drive_fatigue(segments, fps=FPS, **sm_params):
    """用合成帧驱动 FatigueDetector：segments = [(持续秒, eyes, mouth), ...]。

    返回每帧结果列表（含虚拟时间戳 ts）。
    """
    det = FatigueDetector(**sm_params)
    out = []
    t = 0.0
    for dur, eyes, mouth in segments:
        n = max(1, int(round(dur * fps)))
        for _ in range(n):
            frame = render_mock_frame(person=True, eyes=eyes, mouth=mouth)
            r = det.update(frame, MOCK_FACE_BOX, ts=t)
            r['ts'] = t
            out.append(r)
            t += 1.0 / fps
    return out


def run_manager(script, config=None):
    """跑一遍 VisionManager（MockFrameSource），返回事件列表。"""
    src = MockFrameSource(script, fps=FPS)
    vm = VisionManager(src, config)
    vm.run()
    return vm.poll_events()


class TestFatigue(unittest.TestCase):
    """TASK 15：时间窗状态机两档输出（awake / possibly_tired）。"""

    def test_analyzer_signals(self):
        """像素启发式在合成帧上的基本判别：睁/闭眼、哈欠/正常。"""
        an = HeuristicEyeMouthAnalyzer()
        s = an.analyze(render_mock_frame(person=True, eyes='open'), MOCK_FACE_BOX)
        self.assertFalse(s['eye_closed'])
        s = an.analyze(render_mock_frame(person=True, eyes='closed'), MOCK_FACE_BOX)
        self.assertTrue(s['eye_closed'])
        s = an.analyze(render_mock_frame(person=True, mouth='yawn'), MOCK_FACE_BOX)
        self.assertTrue(s['mouth_open'])
        s = an.analyze(render_mock_frame(person=True, mouth='normal'), MOCK_FACE_BOX)
        self.assertFalse(s['mouth_open'])

    def test_normal_stays_awake(self):
        """normal：12s 全程睁眼 → 始终 awake。"""
        rs = drive_fatigue([(12.0, 'open', 'normal')])
        self.assertTrue(all(r['state'] == AWAKE for r in rs))
        self.assertFalse(any('tired' in r['events'] for r in rs))

    def test_blink_stays_awake(self):
        """blink：短暂闭眼 0.6s 淹没在窗口里 → 始终 awake。"""
        rs = drive_fatigue([(5.0, 'open', 'normal'),
                            (0.6, 'closed', 'normal'),
                            (6.0, 'open', 'normal')])
        self.assertTrue(all(r['state'] == AWAKE for r in rs))

    def test_long_eye_close_tired(self):
        """long_eye_close：持续闭眼，窗满（10s）后判 possibly_tired。"""
        rs = drive_fatigue([(12.0, 'closed', 'normal')])
        # 窗未满（前 10s）不得凭单帧/短窗定疲劳
        early = [r for r in rs if r['ts'] < 9.99]
        self.assertTrue(early)
        self.assertTrue(all(r['state'] == AWAKE for r in early))
        self.assertEqual(rs[-1]['state'], POSSIBLY_TIRED)
        tired = [r for r in rs if 'tired' in r['events']]
        self.assertEqual(len(tired), 1)               # 边沿触发，只报一次
        self.assertGreaterEqual(tired[0]['ts'], 9.99)  # 窗满才判

    def test_yawn_tired_then_recover(self):
        """yawn：窗口内哈欠 >= 2 次 → possibly_tired；哈欠滑出窗口后恢复 awake。"""
        rs = drive_fatigue([(1.5, 'open', 'yawn'),
                            (3.0, 'open', 'normal'),
                            (1.5, 'open', 'yawn'),
                            (13.0, 'open', 'normal')])
        tired = [r for r in rs if 'tired' in r['events']]
        self.assertEqual(len(tired), 1)
        # 第二次哈欠确认时（约 t=4.6s）判疲劳
        self.assertAlmostEqual(tired[0]['ts'], 4.6, places=1)
        # 哈欠滑出 10s 窗口后自动恢复
        self.assertTrue(any('recovered' in r['events'] for r in rs))
        self.assertEqual(rs[-1]['state'], AWAKE)

    def test_no_face_keeps_state(self):
        """无人脸帧不喂样本、不崩、不翻转状态。"""
        det = FatigueDetector()
        r = det.update(None, None, ts=0.0)
        self.assertFalse(r['present'])
        self.assertEqual(r['state'], AWAKE)


class TestExpression(unittest.TestCase):
    """TASK 16：neutral / happy 两分类接口。"""

    def test_mock_script_and_pin(self):
        m = MockExpression(script=('neutral', 'happy'))
        self.assertEqual(m.infer(None), 'neutral')
        self.assertEqual(m.infer(None), 'happy')
        self.assertEqual(m.infer(None), 'happy')   # 脚本用完保持最后一个
        m.set_label('neutral')
        self.assertEqual(m.infer(None), 'neutral')
        m.clear_label()
        self.assertEqual(m.infer(None), 'happy')   # 解除钉住回到脚本末尾
        with self.assertRaises(ValueError):
            m.set_label('angry')

    def test_factory(self):
        self.assertIsInstance(make_expression_backend(), MockExpression)  # 默认 mock
        self.assertIsInstance(make_expression_backend('cpu'), CPUExpression)
        with self.assertRaises(ValueError):
            make_expression_backend('nope')

    def test_cpu_backend_runs(self):
        """CPU Haar 后端在合成中性人脸上跑出合法标签（不声称能识别合成笑）。"""
        cpu = CPUExpression()
        label = cpu.infer(render_mock_frame(person=True), MOCK_FACE_BOX)
        self.assertIn(label, LABELS)
        self.assertEqual(label, 'neutral')

    def test_rknn_stub(self):
        """RKNN 桩：记录模型路径，infer 抛 NotImplementedError 并说明需真 NPU。"""
        rk = RKNNExpression(model_path='models/expr.rknn')
        self.assertEqual(rk.model_path, 'models/expr.rknn')
        with self.assertRaises(NotImplementedError) as cm:
            rk.infer(render_mock_frame(person=True), MOCK_FACE_BOX)
        self.assertIn('NPU', str(cm.exception))


class TestCupPresence(unittest.TestCase):
    """TASK 18：ROI + 背景差分 + 阈值 + 轮廓 → 有/无杯；Hough 回退。"""

    def setUp(self):
        self.empty = render_mock_frame()                 # 空台面
        self.with_cup = render_mock_frame(cup=True)      # 有杯（亮圆）
        self.person_no_cup = render_mock_frame(person=True)  # 有人无杯

    def test_bgdiff(self):
        det = CupPresenceDetector(roi=MOCK_CUP_ROI, min_area=800)
        det.set_background(self.empty)
        r0 = det.present_debug(self.empty)
        self.assertFalse(r0['present'])
        self.assertEqual(r0['method'], 'bgdiff')
        r1 = det.present_debug(self.with_cup)
        self.assertTrue(r1['present'])
        self.assertGreaterEqual(r1['max_area'], 800)
        # ROI 限定：人脸入镜（不在出餐位 ROI 内）不得误判有杯
        self.assertFalse(det.present(self.person_no_cup))

    def test_hough_fallback(self):
        """无背景帧时回退 HoughCircles（复用 cup_detect.detect_cup）。"""
        det = CupPresenceDetector(roi=None, hough_params=(15, 60, 100, 20))
        self.assertTrue(det.present(self.with_cup))
        self.assertFalse(det.present(self.empty))


class TestVisionManager(unittest.TestCase):
    """TASK 17：统一事件与去抖。"""

    CUP_CFG = {'cup_background': 'first_frame',
               'cup': {'roi': MOCK_CUP_ROI, 'min_area': 800}}

    def test_person_debounce(self):
        """持续在场只发一次 PERSON_PRESENT；离开 <3s 不发 PERSON_LEFT；
        离开 >3s 才发，且只发一次。"""
        script = [{'dur': 5.0, 'person': True},
                  {'dur': 2.0, 'person': False},   # 短暂离开，不超时
                  {'dur': 1.0, 'person': True},    # 回来：不重复发 PRESENT
                  {'dur': 4.0, 'person': False}]   # 离开超时 → LEFT
        evs = run_manager(script)
        types = [e['type'] for e in evs]
        self.assertEqual(types, [PERSON_PRESENT, PERSON_LEFT])
        self.assertEqual(evs[0]['ts'], 0.0)
        # 最后在场 t=7.8，超时 3s → t=11.0 发 LEFT
        self.assertAlmostEqual(evs[1]['ts'], 11.0, places=1)

    def test_cup_events(self):
        """杯出现发 CUP_PRESENT 一次；撤走超过 cup_gone_s 发 CUP_REMOVED。"""
        script = [{'dur': 1.0, 'person': False},          # 首帧作背景
                  {'dur': 2.0, 'person': False, 'cup': True},
                  {'dur': 3.0, 'person': False}]
        evs = run_manager(script, self.CUP_CFG)
        types = [e['type'] for e in evs]
        self.assertEqual(types, [CUP_PRESENT, CUP_REMOVED])
        self.assertEqual(evs[0]['ts'], 1.0)
        self.assertAlmostEqual(evs[1]['ts'], 5.0, places=1)

    def test_happy_edge(self):
        """HAPPY 边沿触发：两次进入 happy 发两次，持续 happy 不重复发。"""
        script = [{'dur': 1.0, 'person': True, 'expression': 'neutral'},
                  {'dur': 2.0, 'person': True, 'expression': 'happy'},
                  {'dur': 1.0, 'person': True, 'expression': 'neutral'},
                  {'dur': 2.0, 'person': True, 'expression': 'happy'}]
        evs = run_manager(script)
        happy = [e for e in evs if e['type'] == HAPPY]
        self.assertEqual(len(happy), 2)
        self.assertAlmostEqual(happy[0]['ts'], 1.0, places=1)
        self.assertAlmostEqual(happy[1]['ts'], 4.0, places=1)

    def test_tired_via_manager(self):
        """持续闭眼经 VisionManager → TIRED 一次（detail 带闭眼占比）。"""
        script = [{'dur': 12.0, 'person': True, 'eyes': 'closed'}]
        evs = run_manager(script)
        tired = [e for e in evs if e['type'] == TIRED]
        self.assertEqual(len(tired), 1)
        self.assertGreaterEqual(tired[0]['ts'], 9.99)
        self.assertEqual(tired[0]['detail']['yawn_count'], 0)

    def test_capabilities_off(self):
        """能力独立开关：只开 face 时，疲劳/表情/杯一律不出事件。"""
        script = [{'dur': 1.0, 'person': False, 'cup': True},
                  {'dur': 3.0, 'person': True, 'eyes': 'closed',
                   'expression': 'happy', 'cup': True},
                  {'dur': 4.0, 'person': False}]
        cfg = {'capabilities': {'face': True, 'fatigue': False,
                                'expression': False, 'cup': False}}
        evs = run_manager(script, cfg)
        types = set(e['type'] for e in evs)
        self.assertEqual(types, {PERSON_PRESENT, PERSON_LEFT})

    def test_demo_script_event_stream(self):
        """--demo-mock 同款脚本：事件流与预期逐帧一致。"""
        evs = run_manager(demo_script(), self.CUP_CFG)
        self.assertEqual([e['type'] for e in evs],
                         [PERSON_PRESENT, HAPPY, CUP_PRESENT, CUP_REMOVED,
                          PERSON_LEFT, PERSON_PRESENT, TIRED])
        want_ts = [1.0, 3.0, 5.0, 9.0, 10.0, 11.0, 21.0]
        for e, ts in zip(evs, want_ts):
            self.assertAlmostEqual(e['ts'], ts, places=1,
                                   msg='%s 时间戳不符' % e['type'])


class TestPrivacy(unittest.TestCase):
    """TASK 12：隐私配置加载、日志字段白名单、全程零图像落盘。"""

    CUP_CFG = {'cup_background': 'first_frame',
               'cup': {'roi': MOCK_CUP_ROI, 'min_area': 800}}

    def test_config_defaults_when_file_missing(self):
        """配置文件缺失 → 回退默认最严：两个 save 开关 False，4 个白名单字段。"""
        cfg = load_privacy_config('/nonexistent/privacy.yaml')
        self.assertFalse(cfg['save_face_images'])
        self.assertFalse(cfg['save_raw_video'])
        self.assertEqual(set(cfg['log_fields']),
                         {'face_present', 'fatigue_score',
                          'expression', 'timestamp'})

    def test_config_loads_repo_file(self):
        """仓库自带 config/privacy.yaml 能加载，且默认不留存任何图像。"""
        cfg = load_privacy_config()
        self.assertFalse(cfg['save_face_images'])
        self.assertFalse(cfg['save_raw_video'])
        self.assertIn('timestamp', cfg['log_fields'])

    def test_event_dict_scalar_only(self):
        """事件回调输出的 dict 只含 type/ts/detail，detail 全是标量/字符串
        （无图像、无人脸框、无 landmarks 坐标等多余信息）。"""
        evs = run_manager(demo_script(), self.CUP_CFG)
        self.assertTrue(evs)
        for e in evs:
            self.assertEqual(set(e.keys()), {'type', 'ts', 'detail'})
            self.assertIsInstance(e['type'], str)
            self.assertIsInstance(e['ts'], float)
            for v in e['detail'].values():
                self.assertIsInstance(v, (int, float, str, bool, type(None)))

    def test_privacy_log_subset_of_whitelist(self):
        """默认配置下 privacy_log 记录的键集合 ⊆ 允许字段白名单。"""
        src = MockFrameSource(demo_script(), fps=FPS)
        vm = VisionManager(src, self.CUP_CFG)
        vm.run()
        allowed = set(vm.privacy['log_fields'])
        recs = [vm.privacy_log(e) for e in vm.poll_events()]
        self.assertTrue(recs)
        for r in recs:
            self.assertLessEqual(set(r.keys()), allowed)
            self.assertIn('timestamp', r)

    def test_privacy_log_respects_custom_fields(self):
        """收窄 log_fields 后，记录只保留收窄后的字段。"""
        src = MockFrameSource([{'dur': 1.0, 'person': True, 'expression': 'happy'}],
                              fps=FPS)
        vm = VisionManager(src, privacy_config={'log_fields': ['timestamp']})
        vm.run()
        for e in vm.poll_events():
            self.assertEqual(set(vm.privacy_log(e).keys()), {'timestamp'})

    def test_demo_writes_no_image_files(self):
        """用子进程在全新临时目录里完整跑一遍 --demo-mock，
        断言临时目录没有任何新文件生成（不只图像，任何文件都不行）。"""
        tmp = tempfile.mkdtemp(prefix='vision_privacy_')
        r = subprocess.run(
            [sys.executable, os.path.join(_HERE, 'vision_manager.py'),
             '--demo-mock'],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120)
        self.assertEqual(r.returncode, 0, msg=r.stderr.decode('utf-8', 'replace'))
        self.assertEqual(os.listdir(tmp), [],
                         msg='demo 运行后临时目录出现文件: %s' % os.listdir(tmp))


if __name__ == '__main__':
    unittest.main(verbosity=2)
