#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音频事件管理（TASK 32）—— 全系统语音播报统一入口。

设计要点：
  - 数据源唯一：复用 voice_manifest.json（key -> 中文文案），不另造映射文件。
    wav 文件名约定为 audio/<key>.wav（与 gen_audio_mac.sh 的产物一致）。
  - 两层词汇：业务侧播「语义事件」（GREETING/READY/...，EVENT_MAP 映射到
    manifest key）；也可直接传 manifest key 点播（如 fault_beans /
    timeout_cancel / hesitate_help / brew_milk 这 4 条没有语义事件别名）。
  - backend 三档：
      MockAudio  打印日志（默认；开发 VM / 无音频文件时用）
      CmdAudio   调系统播放器（Linux aplay / macOS afplay）；播放器不存在
                 或单个 wav 缺失时优雅降级为 Mock 并记日志，永不抛异常
  - wav 路径只允许出现在本模块；业务代码（host_fsm.py 等）只发带
    voice_key 的事件或调 AudioManager.play(event)，不碰文件路径。

wav 文件生成（本开发 VM 无中文 TTS，不在此生成）：
  在 Mac 上运行 projects/ai_host/gen_audio_mac.sh（say + afconvert，
  按 voice_manifest.json 批量合成），把生成的 audio/*.wav 拷回
  板端 /usr/share/ai_host/audio/ 即可。缺文件不影响运行：CmdAudio
  会逐条降级为日志播报。

用法：
  from audio_manager import AudioManager
  am = AudioManager()                 # 默认 MockAudio
  am.play('READY')                    # 语义事件
  am.play('fault_beans')              # 或直接传 manifest key

  python3 projects/ai_host/audio_manager.py --demo            # 九个事件过一遍
  python3 projects/ai_host/audio_manager.py --list            # 打印映射与 wav 就位情况
  python3 projects/ai_host/audio_manager.py --demo --backend cmd   # 真放（需 wav）
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

# ---- 路径（唯一允许出现 wav 路径的地方）----
_HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(_HERE, 'voice_manifest.json')
AUDIO_DIR = os.path.join(_HERE, 'audio')

# ---- 语义事件 → manifest key ----
# 九个语义事件对齐 voice_manifest.json 现有键；ERROR 只是通用故障兜底
# （默认用缺水文案），具体故障请直接传 manifest key（fault_beans 等）。
GREETING = 'GREETING'
TIRED_RECOMMEND = 'TIRED_RECOMMEND'
HAPPY = 'HAPPY'
ORDER_CONFIRMED = 'ORDER_CONFIRMED'
GRINDING = 'GRINDING'
BREWING = 'BREWING'
READY = 'READY'
GOODBYE = 'GOODBYE'
ERROR = 'ERROR'

EVENT_MAP = {
    GREETING: 'greet',
    TIRED_RECOMMEND: 'fatigue_tip',
    HAPPY: 'smile_bonus',
    ORDER_CONFIRMED: 'order_confirm',
    GRINDING: 'brew_grind',
    BREWING: 'brew_extract',
    READY: 'ready_take',
    GOODBYE: 'goodbye',
    ERROR: 'fault_water',
}
ALL_EVENTS = tuple(EVENT_MAP.keys())


def load_manifest(path=None):
    """读 voice_manifest.json（key -> 文案）；缺失/损坏返回空 dict（不抛）。"""
    try:
        with open(path or MANIFEST_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        print('[audio] 加载 voice_manifest.json 失败（%s），文案查表不可用' % e,
              file=sys.stderr)
        return {}


class MockAudio(object):
    """Mock 后端：不发声，打印一行日志并记录播放历史（测试可断言）。"""

    name = 'mock'

    def __init__(self):
        self.played = []          # 已「播报」的 key 列表（含降级条目）

    def play(self, key, text, wav_path=None):
        print('[audio:mock] %s: %s' % (key, text))
        self.played.append(key)
        return False              # 未真正发声


class CmdAudio(object):
    """命令行播放器后端：Linux aplay / macOS afplay。

    降级策略（任何一条命中即退成 Mock 行为，记日志、不抛异常）：
      - 构造时找不到播放器 → 整体降级（self.degraded=True）
      - play 时 wav 文件缺失 / 播放器调用失败 → 单条降级
    play() 返回 True=真发声，False=走了降级日志。
    """

    name = 'cmd'

    def __init__(self, audio_dir=None, player=None):
        self.audio_dir = audio_dir or AUDIO_DIR
        if player is None:
            player = shutil.which('aplay') or shutil.which('afplay')
        self.player = player
        self._mock = MockAudio()
        self.degraded = player is None
        if self.degraded:
            print('[audio] 找不到 aplay/afplay，CmdAudio 整体降级为 Mock',
                  file=sys.stderr)

    def play(self, key, text, wav_path=None):
        if self.degraded:
            return self._mock.play(key, text)
        path = wav_path or os.path.join(self.audio_dir, key + '.wav')
        if not os.path.isfile(path):
            print('[audio] 缺少 %s，本条降级为日志播报' % path, file=sys.stderr)
            return self._mock.play(key, text, path)
        try:
            subprocess.call([self.player, path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception as e:
            print('[audio] 播放失败（%s），本条降级为日志播报' % e,
                  file=sys.stderr)
            return self._mock.play(key, text, path)


def make_backend(name='mock', **kw):
    """后端工厂：'mock'（默认）| 'cmd'。'cmd' 内部自带降级，不会再退。"""
    if name == 'mock':
        return MockAudio()
    if name == 'cmd':
        return CmdAudio(**kw)
    raise ValueError('未知音频后端: %r（可选 mock/cmd）' % (name,))


class AudioManager(object):
    """语音播报统一入口：play(语义事件或 manifest key)，永不抛异常。

    backend: 'mock'（默认，打印日志）| 'cmd'（aplay/afplay，缺文件自动降级）
    """

    def __init__(self, backend='mock', manifest_path=None, **backend_kw):
        self.texts = load_manifest(manifest_path)
        self.backend = make_backend(backend, **backend_kw)

    def resolve(self, event):
        """语义事件/原始 key → manifest key；不认识的返回 None。"""
        if event in EVENT_MAP:
            return EVENT_MAP[event]
        if event in self.texts:
            return event
        return None

    def play(self, event):
        """播一条。返回 True=真发声，False=Mock/降级/未知事件。永不抛异常。"""
        key = self.resolve(event)
        if key is None:
            print('[audio] 未知音频事件 %r，忽略' % (event,), file=sys.stderr)
            return False
        return self.backend.play(key, self.texts.get(key, ''))


# ---------------- CLI ----------------

def cmd_list(am):
    """打印映射表：语义事件 -> manifest key -> 文案与 wav 就位情况。"""
    print('语义事件        manifest key    wav 就位  文案')
    print('-' * 72)
    for ev in ALL_EVENTS:
        key = EVENT_MAP[ev]
        wav = os.path.join(am.backend.audio_dir if hasattr(am.backend, 'audio_dir')
                           else AUDIO_DIR, key + '.wav')
        ok = '有' if os.path.isfile(wav) else '缺'
        print('%-14s  %-14s  %s      %s' % (ev, key, ok, am.texts.get(key, '?')))
    print('-' * 72)
    extra = sorted(k for k in am.texts if k not in EVENT_MAP.values())
    print('无语义事件别名、可直接点播的 key: %s' % ', '.join(extra))
    if any(True for k in am.texts
           if not os.path.isfile(os.path.join(AUDIO_DIR, k + '.wav'))):
        print('提示：wav 未就位。在 Mac 上运行 gen_audio_mac.sh 生成后 '
              '拷到 audio/（板端 /usr/share/ai_host/audio/）；缺失不影响运行，'
              'CmdAudio 会逐条降级为日志播报。')


def cmd_demo(am):
    """九个语义事件按咖啡流程顺序过一遍（默认 Mock，只打印日志）。"""
    print('=== AudioManager --demo（backend=%s）===' % am.backend.name)
    for ev in (GREETING, TIRED_RECOMMEND, HAPPY, ORDER_CONFIRMED,
               GRINDING, BREWING, READY, GOODBYE, ERROR):
        am.play(ev)
    print('=== 结束 ===')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='音频事件管理（TASK 32）')
    ap.add_argument('--backend', default='mock', choices=['mock', 'cmd'],
                    help='mock=打印日志（默认）；cmd=aplay/afplay 真放')
    ap.add_argument('--demo', action='store_true', help='九个语义事件过一遍')
    ap.add_argument('--list', action='store_true', help='打印映射与 wav 就位表')
    args = ap.parse_args(argv)
    am = AudioManager(backend=args.backend)
    if args.list:
        cmd_list(am)
        return 0
    if args.demo:
        return cmd_demo(am)
    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
