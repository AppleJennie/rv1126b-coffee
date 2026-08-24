#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 店员互动状态机（主程序）。

状态流转：
  ABSENT（无人）
    → 连续 confirm_polls 次检测到人脸且 face_ratio 不减小（人走近）→ GREET
  GREET（问候，瞬时态）
    → 输出问候事件 + 语音文案 + 一条推荐 → ENGAGED
  ENGAGED（互动中）
    → 微笑度 ≥ 0.7 触发一次微笑彩蛋文案
    → fatigue_score ≥ 0.6 触发一次提神提示 + 提神推荐（每个在场会话一次）
    → 停留超过 hesitate_after_s 秒触发一次引导文案
    → 人脸消失超过 absent_timeout_s 秒 → 告别 → ABSENT

每次状态转换 / 触发都向 stdout 打印一行事件 JSON，方便将来对接点单屏
（mascot 状态机）和日志采集。本模块只输出事件与文案，不直接驱动屏幕。

子命令：
  simulate   mock 人脸事件，快速演示全流程（无需摄像头）
  run        真摄像头跑状态机（Ctrl+C 退出）
  recommend  命令行直接要一条推荐，可用 --temp/--smile/--hour 等模拟上下文
"""

import argparse
import json
import os
import sys
import time

import recommend as recommend_mod
import weather as weather_mod

STATE_ABSENT = 'ABSENT'
STATE_GREET = 'GREET'
STATE_ENGAGED = 'ENGAGED'

# 语音文案清单（key 与 voice_manifest.json 对齐），加载失败时用这里的兜底文案
_DEFAULT_TEXTS = {
    'greet': '您好，欢迎光临，想喝点什么呢？',
    'smile_bonus': '您的笑容真好看，这杯给您打个好心情折！',
    'hesitate_help': '拿不定主意的话，我可以为您推荐一杯。',
    'fatigue_tip': '您看起来有点累了，来杯提神的咖啡吧！',
    'goodbye': '欢迎下次再来，祝您今天愉快！',
}
_MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'voice_manifest.json')


def load_voice_texts():
    """读 voice_manifest.json 取语音文案；文件缺失/损坏时用兜底文案。"""
    try:
        with open(_MANIFEST_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        texts = dict(_DEFAULT_TEXTS)
        for k in texts:
            if k in data:
                texts[k] = data[k]
        return texts
    except Exception:
        return dict(_DEFAULT_TEXTS)


def emit(event):
    """打印一行事件 JSON（未来由点单屏 / 日志系统消费）。"""
    event = dict(event)
    event.setdefault('ts', round(time.time(), 3))
    print(json.dumps(event, ensure_ascii=False), flush=True)


class MockFaceEventSource(object):
    """simulate 用：按脚本回放人脸事件，接口与 FaceEventSource 一致。

    脚本元素为 (present, face_ratio, smile) 或 (present, face_ratio, smile, fatigue)，
    fatigue 为 FatigueMonitor 风格的 dict 或 None。
    """

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def poll(self):
        # 脚本播完后一直重复最后一帧
        item = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        fatigue = item[3] if len(item) > 3 else None
        ev = {'present': bool(item[0]), 'face_ratio': float(item[1]),
              'smile': float(item[2]), 'fatigue': fatigue, 'ts': time.time()}
        return ev

    def close(self):
        pass


class HostFSM(object):
    """互动状态机主体。参数均可调，simulate 用小数值压缩演示时间。"""

    def __init__(self, texts=None, confirm_polls=2, absent_timeout_s=15.0,
                 hesitate_after_s=10.0, smile_bonus_threshold=0.7,
                 fatigue_tip_threshold=0.6,
                 poll_interval=0.6, use_weather=True, weather_timeout=3):
        self.texts = texts if texts is not None else load_voice_texts()
        self.confirm_polls = confirm_polls
        self.absent_timeout_s = absent_timeout_s
        self.hesitate_after_s = hesitate_after_s
        self.smile_bonus_threshold = smile_bonus_threshold
        self.fatigue_tip_threshold = fatigue_tip_threshold
        self.poll_interval = poll_interval
        self.use_weather = use_weather
        self.weather_timeout = weather_timeout

        self.state = STATE_ABSENT
        self._present_streak = 0    # ABSENT 下连续「有人且未走远」计数
        self._prev_ratio = 0.0
        self._engaged_since = 0.0
        self._last_present = 0.0
        self._smile_bonus_done = False
        self._hesitate_done = False
        self._fatigue_tip_done = False

    # ---- 状态转换 ----
    def _to_greet(self):
        """ABSENT → GREET → ENGAGED：GREET 是瞬时态，进场即完成问候与推荐。"""
        emit({'event': 'state', 'from': self.state, 'to': STATE_GREET,
              'mascot': 'greet', 'voice_key': 'greet', 'text': self.texts['greet']})
        self.state = STATE_GREET

        # 组装推荐上下文：天气可空，传感器数据由部署方接入（此处占位 None）
        ctx = {'hour': time.localtime().tm_hour,
               'smile': None, 'sensor_temp': None, 'sensor_humidity': None}
        w = weather_mod.get_weather(timeout=self.weather_timeout) if self.use_weather else None
        if w:
            ctx['temp_c'] = w['temp_c']
            ctx['weather_desc'] = w['desc']
        rec = recommend_mod.recommend(ctx)
        emit({'event': 'recommend', 'mascot': 'recommend',
              'drink': rec['drink']['name'], 'price': rec['drink']['price'],
              'reason': rec['reason'], 'tags': rec['tags'],
              'weather': w})

        emit({'event': 'state', 'from': STATE_GREET, 'to': STATE_ENGAGED, 'mascot': 'wake'})
        self.state = STATE_ENGAGED
        now = time.time()
        self._engaged_since = now
        self._last_present = now
        self._smile_bonus_done = False
        self._hesitate_done = False
        self._fatigue_tip_done = False

    def _to_absent(self):
        """ENGAGED → ABSENT：告别后回到无人待机。"""
        emit({'event': 'state', 'from': self.state, 'to': STATE_ABSENT,
              'mascot': 'wave', 'voice_key': 'goodbye', 'text': self.texts['goodbye']})
        emit({'event': 'idle', 'mascot': 'sleep'})
        self.state = STATE_ABSENT
        self._present_streak = 0
        self._prev_ratio = 0.0

    # ---- 事件触发 ----
    def _emit_fatigue_tip(self, fatigue):
        """ENGAGED 下检测到明显疲劳：提示 + 一条提神推荐（每会话一次）。"""
        fs = fatigue.get('fatigue_score')
        emit({'event': 'fatigue_tip', 'mascot': 'wake',
              'voice_key': 'fatigue_tip', 'text': self.texts['fatigue_tip'],
              'fatigue_score': fs,
              'level': fatigue.get('level'),
              'events': fatigue.get('events')})
        ctx = {'hour': time.localtime().tm_hour, 'fatigue_score': fs,
               'smile': None, 'sensor_temp': None, 'sensor_humidity': None}
        rec = recommend_mod.recommend(ctx)
        emit({'event': 'recommend', 'mascot': 'recommend',
              'drink': rec['drink']['name'], 'price': rec['drink']['price'],
              'reason': rec['reason'], 'tags': rec['tags'],
              'weather': None})

    # ---- 主循环 ----
    def step(self, ev):
        """喂一帧人脸事件，驱动一次状态机。"""
        now = time.time()
        present = ev['present']
        ratio = ev['face_ratio']
        smile = ev['smile']

        if self.state == STATE_ABSENT:
            # 人在且没有走远（face_ratio 不减小）才算有效靠近，连续若干次确认
            if present and ratio >= self._prev_ratio * 0.95:
                self._present_streak += 1
            else:
                self._present_streak = 0
            if present:
                self._prev_ratio = max(self._prev_ratio, ratio)
            else:
                self._prev_ratio = 0.0
            if self._present_streak >= self.confirm_polls:
                self._to_greet()

        elif self.state == STATE_ENGAGED:
            if present:
                self._last_present = now
                if (not self._smile_bonus_done
                        and smile >= self.smile_bonus_threshold):
                    self._smile_bonus_done = True
                    emit({'event': 'smile_bonus', 'mascot': 'happy',
                          'voice_key': 'smile_bonus', 'text': self.texts['smile_bonus'],
                          'smile': smile})
                # 疲劳提示：landmark106 后端给出 fatigue dict，分数够高时每会话提示一次
                fatigue = ev.get('fatigue')
                if (not self._fatigue_tip_done
                        and fatigue is not None
                        and fatigue.get('fatigue_score') is not None
                        and fatigue['fatigue_score'] >= self.fatigue_tip_threshold):
                    self._fatigue_tip_done = True
                    self._emit_fatigue_tip(fatigue)
                if (not self._hesitate_done
                        and now - self._engaged_since >= self.hesitate_after_s):
                    self._hesitate_done = True
                    emit({'event': 'hesitate', 'mascot': 'wake',
                          'voice_key': 'hesitate_help', 'text': self.texts['hesitate_help'],
                          'dwell_s': round(now - self._engaged_since, 1)})
            else:
                if now - self._last_present >= self.absent_timeout_s:
                    self._to_absent()

    def loop(self, source, max_steps=None):
        """poll 循环；max_steps 仅 simulate 用来收敛退出。"""
        n = 0
        while True:
            ev = source.poll()
            self.step(ev)
            n += 1
            if max_steps is not None and n >= max_steps:
                break
            time.sleep(self.poll_interval)


# ---- 子命令实现 ----

def cmd_simulate(args):
    """mock 人脸事件演示全流程：无人 → 人出现 → 问候+推荐 → 顾客打哈欠
    （fatigue_score 爬升过 0.6）→ 疲劳提示+提神推荐 → 微笑彩蛋 →
    停留引导 → 人离开 → 回 ABSENT。

    脚本第 4 个元素是 fatigue dict（模拟 FatigueMonitor.update 的输出），
    代表「顾客打哈欠，疲劳分逐步爬升又回落」的过程。"""
    def fat(score, level, events=None):
        return {'present': True, 'calibrated': True, 'ear': 0.3, 'mar': 0.5,
                'head_down': 0.0, 'fatigue_score': score,
                'events': events or [], 'level': level}

    script = (
        [(False, 0.0, 0.0)] * 2            # 无人
        + [(True, 0.05, 0.3)]              # 远处出现人脸
        + [(True, 0.09, 0.3)]              # 走近（ratio 增大）→ 触发问候
        + [(True, 0.10, 0.3, fat(0.1, 'alert'))]        # 站在屏前，精神正常
        + [(True, 0.10, 0.3, fat(0.35, 'mild'))]        # 有点困意
        + [(True, 0.10, 0.3, fat(0.7, 'tired', ['yawn']))]   # 打哈欠 → 疲劳提示+提神推荐
        + [(True, 0.10, 0.3, fat(0.75, 'tired', ['yawn']))]  # 持续疲劳（不再重复提示）
        + [(True, 0.10, 0.3, fat(0.4, 'mild'))]         # 缓过来一点
        + [(True, 0.10, 0.85, fat(0.2, 'alert'))] * 2   # 笑了 → 微笑彩蛋
        + [(True, 0.10, 0.4, fat(0.1, 'alert'))] * 6    # 停留较久 → 引导文案
        + [(False, 0.0, 0.0)] * 10         # 离开 → 超时回 ABSENT
    )
    source = MockFaceEventSource(script)
    fsm = HostFSM(confirm_polls=2, absent_timeout_s=0.3, hesitate_after_s=0.5,
                  poll_interval=0.1, use_weather=not args.no_weather)
    fsm.loop(source, max_steps=len(script))
    if fsm.state != STATE_ABSENT:
        print('simulate 结束但状态未回到 ABSENT: %s' % fsm.state, file=sys.stderr)
        return 1
    return 0


def cmd_run(args):
    """真摄像头跑状态机，Ctrl+C 退出。"""
    from face_events import FaceEventSource   # 延迟 import：simulate/recommend 不需要 cv2
    try:
        source = FaceEventSource(backend=args.backend, device=args.device,
                                 retinaface_path=args.retinaface_model,
                                 landmark_path=args.landmark_model)
    except Exception as e:
        print('初始化人脸后端失败: %s' % e, file=sys.stderr)
        return 2
    fsm = HostFSM()
    print('[host_fsm] 后端=%s 设备=/dev/video%d，Ctrl+C 退出'
          % (source.backend.name, args.device), file=sys.stderr)
    try:
        fsm.loop(source)
    except RuntimeError as e:
        print('运行失败: %s' % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('\n[host_fsm] 已退出', file=sys.stderr)
    finally:
        source.close()
    return 0


def cmd_recommend(args):
    """命令行直接要一条推荐。--weather 拉真实天气，否则只用命令行给的上下文。"""
    ctx = {'hour': args.hour if args.hour is not None else time.localtime().tm_hour,
           'smile': args.smile,
           'fatigue_score': args.fatigue_score,
           'temp_c': args.temp,
           'weather_desc': None,
           'sensor_temp': args.sensor_temp,
           'sensor_humidity': args.humidity}
    if args.weather:
        w = weather_mod.get_weather()
        if w:
            ctx['temp_c'] = w['temp_c']
            ctx['weather_desc'] = w['desc']
        else:
            print('[recommend] 天气获取失败，按无天气上下文降级', file=sys.stderr)
    rec = recommend_mod.recommend(ctx)
    print(json.dumps({'ctx': ctx, 'result': rec}, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='AI 店员互动状态机')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_sim = sub.add_parser('simulate', help='mock 人脸事件演示全流程')
    p_sim.add_argument('--no-weather', action='store_true', help='不请求天气 API')
    p_sim.set_defaults(func=cmd_simulate)

    p_run = sub.add_parser('run', help='真摄像头运行')
    p_run.add_argument('--backend', default='auto',
                       choices=['auto', 'haar', 'scrfd', 'landmark106'],
                       help='landmark106 = retinaface+2d106det 疲劳检测后端')
    p_run.add_argument('--device', type=int, default=23,
                       help='摄像头设备号：板端 MIPI=23，板端 USB=52，本机一般 0')
    p_run.add_argument('--retinaface-model', default='./models/retinaface.rknn',
                       help='landmark106 后端的 RetinaFace 模型路径')
    p_run.add_argument('--landmark-model', default='./models/2d106det.rknn',
                       help='landmark106 后端的 106 关键点模型路径')
    p_run.set_defaults(func=cmd_run)

    p_rec = sub.add_parser('recommend', help='命令行直接要一条推荐')
    p_rec.add_argument('--temp', type=float, default=None, help='模拟天气温度 °C')
    p_rec.add_argument('--smile', type=float, default=None, help='模拟微笑度 0~1')
    p_rec.add_argument('--fatigue-score', type=float, default=None,
                       help='模拟疲劳度 0~1（≥0.6 触发提神推荐）')
    p_rec.add_argument('--hour', type=int, default=None, help='模拟小时 0~23')
    p_rec.add_argument('--sensor-temp', type=float, default=None, help='机身传感器温度（优先于 --temp）')
    p_rec.add_argument('--humidity', type=float, default=None, help='机身湿度 %')
    p_rec.add_argument('--weather', action='store_true', help='拉取真实天气')
    p_rec.set_defaults(func=cmd_recommend)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
