#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TASK 32 自测：audio_manager 音频事件管理 + host_fsm 播报挂钩。

纯标准库 unittest，直接运行：
    python3 projects/ai_host/test_audio_manager.py

覆盖：
  - AudioManager.play：九个语义事件 + 13 条 manifest key 全调一遍不抛异常
  - 未知事件：返回 False、记日志、不抛异常
  - 事件映射完整性：EVENT_MAP 的值全部落在 voice_manifest.json 现有键里
  - CmdAudio 降级：无播放器整机降级；wav 缺失单条降级（用 /bin/true 做
    确定性假播放器，不依赖本机是否装 aplay，也不真发声）
  - host_fsm 挂钩：audio=AudioManager 时 voice_key 事件同步触发播报；
    默认 audio=None 行为不变（由 test_host_fsm.py 全套回归保证）
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import audio_manager as amod            # noqa: E402
import host_fsm                         # noqa: E402
from audio_manager import (             # noqa: E402
    ALL_EVENTS, EVENT_MAP, AudioManager, CmdAudio, MockAudio, load_manifest)


class TestAudioManager(unittest.TestCase):
    """AudioManager 统一入口与后端降级。"""

    def test_event_map_values_in_manifest(self):
        """映射完整性：EVENT_MAP 九个值都是 voice_manifest.json 现有键。"""
        texts = load_manifest()
        self.assertTrue(texts, 'voice_manifest.json 应可读')
        for ev, key in EVENT_MAP.items():
            self.assertIn(key, texts, msg='%s -> %s 不在 manifest 里' % (ev, key))
        self.assertEqual(len(ALL_EVENTS), 9)

    def test_play_all_events_and_keys_no_raise(self):
        """九个语义事件 + 全部 manifest key 各播一遍，不抛异常。"""
        buf = io.StringIO()
        am = AudioManager(backend='mock')
        with contextlib.redirect_stdout(buf):
            for ev in ALL_EVENTS:
                am.play(ev)
            for key in am.texts:
                am.play(key)
        out = buf.getvalue()
        # mock 每条都有日志；九个事件 + 13 条 key = 22 行
        self.assertEqual(out.count('[audio:mock]'), 9 + len(am.texts))
        self.assertEqual(len(am.backend.played), 9 + len(am.texts))

    def test_unknown_event_graceful(self):
        """未知事件：返回 False、stderr 记一行、不抛异常。"""
        err = io.StringIO()
        am = AudioManager(backend='mock')
        with contextlib.redirect_stderr(err):
            self.assertFalse(am.play('NO_SUCH_EVENT'))
        self.assertIn('未知音频事件', err.getvalue())

    def test_cmd_backend_degrades_without_player(self):
        """找不到播放器 → 构造即整机降级，play 走 mock 日志不抛异常。"""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            be = CmdAudio(player=None)
            # 强行模拟无播放器环境（即便本机有 aplay）
            be.player = None
            be.degraded = True
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertFalse(be.play('greet', '您好'))
        self.assertIn('[audio:mock]', out.getvalue())

    def test_cmd_backend_missing_wav_falls_back(self):
        """wav 缺失 → 单条降级 Mock 并记日志；wav 就位 → 真调播放器。

        用 /bin/true 当假播放器：秒回、不发声、跨平台确定性。
        """
        tmp = tempfile.mkdtemp(prefix='audio_test_')
        err = io.StringIO()
        be = CmdAudio(audio_dir=tmp, player='/bin/true')
        self.assertFalse(be.degraded)
        # 缺文件：返回 False，stderr 记降级，mock 日志里有该 key
        with contextlib.redirect_stderr(err):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertFalse(be.play('greet', '您好'))
        self.assertIn('降级', err.getvalue())
        self.assertIn('greet', be._mock.played)
        # 放个假 wav：返回 True（真调了 /bin/true）
        with open(os.path.join(tmp, 'greet.wav'), 'wb') as f:
            f.write(b'RIFF')
        self.assertTrue(be.play('greet', '您好'))

    def test_bad_backend_name_raises(self):
        """工厂对未知后端名抛 ValueError（构造期显式失败，非运行期）。"""
        with self.assertRaises(ValueError):
            amod.make_backend('nope')


class TestHostFsmAudioHook(unittest.TestCase):
    """host_fsm 经 voice_key 挂钩 AudioManager（默认关闭，行为不变）。"""

    def setUp(self):
        self.events = []
        self._orig_emit = host_fsm.emit
        host_fsm.emit = self.events.append   # 与 test_host_fsm.py 同款拦截

    def tearDown(self):
        host_fsm.emit = self._orig_emit

    def _drive_greet_and_farewell(self, fsm):
        """两帧确认进场（触发 greet）→ 人离开超时（触发 goodbye）。"""
        fsm.step({'present': True, 'face_ratio': 0.05, 'smile': 0.3,
                  'fatigue': None, 'person_id': 'p1'})
        fsm.step({'present': True, 'face_ratio': 0.09, 'smile': 0.3,
                  'fatigue': None, 'person_id': 'p1'})
        fsm.step({'present': False, 'face_ratio': 0.0, 'smile': 0.0,
                  'fatigue': None})

    def test_hook_plays_voice_keys(self):
        """挂 AudioManager(mock)：问候/告别各触发一次对应 key 的播报。"""
        am = AudioManager(backend='mock')
        fsm = host_fsm.HostFSM(use_weather=False, absent_timeout_s=0.0,
                               confirm_polls=2, audio=am)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self._drive_greet_and_farewell(fsm)
        self.assertEqual(am.backend.played, ['greet', 'goodbye'])

    def test_default_no_audio(self):
        """默认 audio=None：只发事件不播报，事件流里仍带 voice_key 字段。"""
        fsm = host_fsm.HostFSM(use_weather=False, absent_timeout_s=0.0,
                               confirm_polls=2)
        self.assertIsNone(fsm._audio)
        self._drive_greet_and_farewell(fsm)
        keys = [e.get('voice_key') for e in self.events if e.get('voice_key')]
        self.assertEqual(keys, ['greet', 'goodbye'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
