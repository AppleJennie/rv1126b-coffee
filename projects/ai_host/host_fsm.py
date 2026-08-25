#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 店员互动状态机（主程序，TASK 10 升级版）。

状态集：
  NO_PERSON        无人待机
  PERSON_APPROACH  检测到有人靠近（未确认，连续 confirm_polls 次有效才确认）
  GREETING         问候（瞬时态：进场即播问候文案）
  RECOMMEND        进场推荐（瞬时态：紧随 GREETING 输出一条推荐）
  OBSERVE          在场观察：微笑彩蛋 / 疲劳提示（含一次提神推荐）/ 犹豫引导
  ORDERING         顾客已在屏上自选饮品，等待下单确认
  WAITING          制作中（本状态与 SERVING 一律不输出推荐）
  SERVING          出餐完成，等待顾客取走
  FAREWELL         告别（瞬时态），回到 NO_PERSON 后才允许开启新一轮交互

硬性约束：
  - 同人短时间内不重复打招呼：优先比对视觉 track / mock 的 person_id，
    无 id 时退化为「告别后 regreet_cooldown_s 内又有人靠近」的时间窗启发式；
    命中则跳过 GREETING/RECOMMEND 直接进 OBSERVE。
  - WAITING / SERVING（制作/出餐中）不处理任何推荐触发，不输出推荐事件。
  - 只有走完 FAREWELL → NO_PERSON 才会响应新的靠近，在场会话不被打断重开。
  - 每次状态转换都打印一行事件 JSON；所有可停留状态都有超时出口：
    PERSON_APPROACH approach_timeout_s → NO_PERSON
    OBSERVE        max_observe_s       → FAREWELL（人离开走 absent_timeout_s）
    ORDERING       ordering_timeout_s  → FAREWELL（播 timeout_cancel）
    WAITING        making_timeout_s    → FAREWELL
    SERVING        serving_timeout_s   → FAREWELL（人离开走 absent_timeout_s）

事件输入（step 接受两类）：
  人脸事件  {'present', 'face_ratio', 'smile', 'fatigue', 'ts', 'person_id'?}
            person_id 由视觉 track / 模拟层提供，可空（空则走时间窗启发式）
  控制事件  {'type': 'user_select'|'order_confirmed'|'making_done'|'served', ...}
            由点单屏 / 制作流程（coffee_fsm 侧）注入

每次状态转换 / 触发都向 stdout 打印一行事件 JSON，方便将来对接点单屏
（mascot 状态机）和日志采集。本模块只输出事件与文案，不直接驱动屏幕。

子命令：
  simulate   mock 人脸事件，快速演示全流程（无需摄像头，一次性回放完退出）
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

STATE_NO_PERSON = 'NO_PERSON'
STATE_PERSON_APPROACH = 'PERSON_APPROACH'
STATE_GREETING = 'GREETING'
STATE_OBSERVE = 'OBSERVE'
STATE_RECOMMEND = 'RECOMMEND'
STATE_ORDERING = 'ORDERING'
STATE_WAITING = 'WAITING'
STATE_SERVING = 'SERVING'
STATE_FAREWELL = 'FAREWELL'

# 状态 → mascot 展示态（对接约定见 docs/modules/ai_host.md）
_STATE_MASCOT = {
    STATE_NO_PERSON: 'sleep',
    STATE_PERSON_APPROACH: 'wake',
    STATE_GREETING: 'greet',
    STATE_OBSERVE: 'wake',
    STATE_RECOMMEND: 'recommend',
    STATE_ORDERING: 'wake',
    STATE_WAITING: 'brewing',
    STATE_SERVING: 'wake',
    STATE_FAREWELL: 'wave',
}

# 制作/出餐状态：这些状态下一律不输出推荐
_MAKING_STATES = (STATE_WAITING, STATE_SERVING)

# 语音文案清单（key 与 voice_manifest.json 对齐），加载失败时用这里的兜底文案
_DEFAULT_TEXTS = {
    'greet': '您好，欢迎光临，想喝点什么呢？',
    'smile_bonus': '您的笑容真好看，这杯给您打个好心情折！',
    'hesitate_help': '拿不定主意的话，我可以为您推荐一杯。',
    'fatigue_tip': '您看起来有点累了，来杯提神的咖啡吧！',
    'order_confirm': '好的，已为您下单，请扫码支付。',
    'ready_take': '您的咖啡做好了，请取走，小心烫哦。',
    'timeout_cancel': '支付超时，订单已取消，欢迎下次再来。',
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
    """simulate 用：按脚本回放事件，接口与 FaceEventSource 一致。

    脚本元素两类：
      tuple  (present, face_ratio, smile[, fatigue[, person_id]])  人脸事件；
             fatigue 为 FatigueMonitor 风格的 dict 或 None；person_id 可空
      dict   {'type': 'user_select'|'order_confirmed'|'making_done'|'served', ...}
             控制事件（模拟点单屏 / 制作流程注入）
    """

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def poll(self):
        # 脚本播完后一直重复最后一帧
        item = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        if isinstance(item, dict):
            ev = dict(item)
            ev.setdefault('ts', time.time())
            return ev
        fatigue = item[3] if len(item) > 3 else None
        ev = {'present': bool(item[0]), 'face_ratio': float(item[1]),
              'smile': float(item[2]), 'fatigue': fatigue, 'ts': time.time()}
        if len(item) > 4:
            ev['person_id'] = item[4]
        return ev

    def close(self):
        pass


class HostFSM(object):
    """互动状态机主体。参数均可调，simulate 用小数值压缩演示时间。

    now_fn 仅测试用（注入假时钟），默认 time.time。
    """

    def __init__(self, texts=None, confirm_polls=2, absent_timeout_s=15.0,
                 hesitate_after_s=10.0, smile_bonus_threshold=0.7,
                 fatigue_tip_threshold=0.6,
                 poll_interval=0.6, use_weather=True, weather_timeout=3,
                 approach_timeout_s=6.0, max_observe_s=90.0,
                 ordering_timeout_s=60.0, making_timeout_s=300.0,
                 serving_timeout_s=60.0, regreet_cooldown_s=180.0,
                 now_fn=None):
        self.texts = texts if texts is not None else load_voice_texts()
        self.confirm_polls = confirm_polls
        self.absent_timeout_s = absent_timeout_s
        self.hesitate_after_s = hesitate_after_s
        self.smile_bonus_threshold = smile_bonus_threshold
        self.fatigue_tip_threshold = fatigue_tip_threshold
        self.poll_interval = poll_interval
        self.use_weather = use_weather
        self.weather_timeout = weather_timeout
        self.approach_timeout_s = approach_timeout_s
        self.max_observe_s = max_observe_s
        self.ordering_timeout_s = ordering_timeout_s
        self.making_timeout_s = making_timeout_s
        self.serving_timeout_s = serving_timeout_s
        self.regreet_cooldown_s = regreet_cooldown_s
        self._now = now_fn or time.time

        self.state = STATE_NO_PERSON
        self._state_since = self._now()   # 当前状态进入时刻（超时出口用）
        self._present_streak = 0          # PERSON_APPROACH 下连续「有人且未走远」计数
        self._prev_ratio = 0.0
        self._observe_since = 0.0
        self._last_present = 0.0
        self._smile_bonus_done = False
        self._hesitate_done = False
        self._fatigue_tip_done = False
        self._person_id = None            # 当前在场人 id（视觉 track / mock 提供，可空）
        self._last_person_id = None       # 上一场会话的人 id（同人判定用）
        self._last_farewell_ts = -1e9     # 上一次告别时刻（同人时间窗启发式用）
        self._selected = None             # ORDERING/WAITING/SERVING 期间顾客已选饮品名

    # ---- 基础：状态转换与推荐输出 ----
    def _set_state(self, new_state, **extra):
        """统一的状态转换出口：每次转换都打一行事件 JSON（含 mascot 展示态）。"""
        ev = {'event': 'state', 'from': self.state, 'to': new_state,
              'mascot': _STATE_MASCOT[new_state]}
        ev.update(extra)
        emit(ev)
        self.state = new_state
        self._state_since = self._now()

    def _emit_recommend(self, fatigue_score=None):
        """组装上下文并输出一条推荐。

        只允许在 GREETING→RECOMMEND 瞬时态和 OBSERVE 的疲劳提示里调用；
        WAITING/SERVING 永远不调用本函数（制作中不推荐的硬约束）。
        """
        ctx = {'hour': time.localtime().tm_hour,
               'smile': None, 'sensor_temp': None, 'sensor_humidity': None,
               'fatigue_score': fatigue_score}
        w = weather_mod.get_weather(timeout=self.weather_timeout) if self.use_weather else None
        if w:
            ctx['temp_c'] = w['temp_c']
            ctx['weather_desc'] = w['desc']
        rec = recommend_mod.recommend(ctx)
        emit({'event': 'recommend', 'mascot': 'recommend',
              'drink': rec['drink']['name'], 'drink_id': rec['drink']['id'],
              'price': rec['drink']['price'],
              'reason': rec['reason'], 'tags': rec['tags'],
              'weather': w})

    # ---- 同人判定 ----
    def _is_same_person_returning(self, person_id, now):
        """同人判定：告别后 regreet_cooldown_s 内又有人靠近才算「折返」。

        两边都有 person_id 时直接比对 id；否则退化为纯时间窗启发式
        （刚告别没多久又有人靠近，大概率是同一个人回来了）。
        """
        if now - self._last_farewell_ts > self.regreet_cooldown_s:
            return False
        if person_id is not None and self._last_person_id is not None:
            return person_id == self._last_person_id
        return True

    # ---- 会话级动作 ----
    def _do_greet(self):
        """PERSON_APPROACH 确认 → GREETING（问候）→ RECOMMEND（进场推荐）→ OBSERVE。

        GREETING / RECOMMEND 都是瞬时态，一次调用内走完。
        """
        self._set_state(STATE_GREETING, voice_key='greet',
                        text=self.texts['greet'], person_id=self._person_id)
        self._set_state(STATE_RECOMMEND)
        self._emit_recommend()
        self._enter_observe()

    def _enter_observe(self):
        """进入 OBSERVE：重置每会话的一次性触发标记。"""
        self._set_state(STATE_OBSERVE)
        now = self._now()
        self._observe_since = now
        self._last_present = now
        self._smile_bonus_done = False
        self._hesitate_done = False
        self._fatigue_tip_done = False

    def _do_farewell(self, reason):
        """→ FAREWELL（瞬时态：告别）→ NO_PERSON。记录同人判定信息。"""
        self._set_state(STATE_FAREWELL, voice_key='goodbye',
                        text=self.texts['goodbye'], reason=reason)
        emit({'event': 'idle', 'mascot': 'sleep'})
        self._last_person_id = self._person_id
        self._person_id = None
        self._selected = None
        self._last_farewell_ts = self._now()
        self._set_state(STATE_NO_PERSON)
        self._present_streak = 0
        self._prev_ratio = 0.0

    # ---- 事件触发（OBSERVE 内）----
    def _emit_fatigue_tip(self, fatigue):
        """OBSERVE 下检测到明显疲劳：提示 + 一条提神推荐（每会话一次）。

        文案红线：只说「看起来有点累」这类观察性描述，绝不写成医疗诊断。
        """
        fs = fatigue.get('fatigue_score')
        emit({'event': 'fatigue_tip', 'mascot': 'wake',
              'voice_key': 'fatigue_tip', 'text': self.texts['fatigue_tip'],
              'fatigue_score': fs,
              'level': fatigue.get('level'),
              'events': fatigue.get('events')})
        self._emit_recommend(fatigue_score=fs)

    # ---- 人脸事件处理 ----
    def _step_face(self, ev):
        now = self._now()
        present = ev['present']
        ratio = ev['face_ratio']
        smile = ev['smile']
        person_id = ev.get('person_id')

        if self.state == STATE_NO_PERSON:
            if present:
                self._person_id = person_id
                self._present_streak = 1
                self._prev_ratio = ratio
                self._set_state(STATE_PERSON_APPROACH, person_id=person_id)

        elif self.state == STATE_PERSON_APPROACH:
            if present:
                # 人在且没有走远（face_ratio 不减小）才算有效靠近，连续若干次确认
                if ratio >= self._prev_ratio * 0.95:
                    self._present_streak += 1
                else:
                    self._present_streak = 1
                self._prev_ratio = max(self._prev_ratio, ratio)
                if person_id is not None:
                    self._person_id = person_id
                if self._present_streak >= self.confirm_polls:
                    if self._is_same_person_returning(self._person_id, now):
                        # 同人折返：不重复打招呼，直接回到观察态
                        emit({'event': 'skip_greet', 'mascot': 'wake',
                              'person_id': self._person_id,
                              'cooldown_s': self.regreet_cooldown_s})
                        self._enter_observe()
                    else:
                        self._do_greet()
            else:
                self._present_streak = 0
            # 超时出口：迟迟不确认 → 回 NO_PERSON
            if (self.state == STATE_PERSON_APPROACH
                    and now - self._state_since >= self.approach_timeout_s):
                self._present_streak = 0
                self._prev_ratio = 0.0
                self._person_id = None
                self._set_state(STATE_NO_PERSON, reason='approach_timeout')

        elif self.state == STATE_OBSERVE:
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
                        and now - self._observe_since >= self.hesitate_after_s):
                    self._hesitate_done = True
                    emit({'event': 'hesitate', 'mascot': 'wake',
                          'voice_key': 'hesitate_help', 'text': self.texts['hesitate_help'],
                          'dwell_s': round(now - self._observe_since, 1)})
                # 超时出口：人在但迟迟不互动 → 主动告别，不无限等待
                if now - self._observe_since >= self.max_observe_s:
                    self._do_farewell('observe_timeout')
            else:
                if now - self._last_present >= self.absent_timeout_s:
                    self._do_farewell('person_left')

        elif self.state == STATE_ORDERING:
            if present:
                self._last_present = now
            elif now - self._last_present >= self.absent_timeout_s:
                self._do_farewell('person_left')
                return
            # 超时出口：迟迟未确认订单 → 播取消文案并告别
            if (self.state == STATE_ORDERING
                    and now - self._state_since >= self.ordering_timeout_s):
                emit({'event': 'order_timeout', 'mascot': 'wave',
                      'voice_key': 'timeout_cancel', 'text': self.texts['timeout_cancel']})
                self._do_farewell('ordering_timeout')

        elif self.state == STATE_WAITING:
            # 制作中：不处理微笑/疲劳触发，绝不输出推荐（硬约束）
            # 超时出口：制作流程迟迟不回报完成 → 报错并告别
            if now - self._state_since >= self.making_timeout_s:
                emit({'event': 'making_timeout', 'mascot': 'wake',
                      'wait_s': round(now - self._state_since, 1)})
                self._do_farewell('making_timeout')

        elif self.state == STATE_SERVING:
            # 出餐中：同样不输出推荐；人离开或超时未取 → 告别
            if not present and now - self._last_present >= self.absent_timeout_s:
                self._do_farewell('person_left')
            elif now - self._state_since >= self.serving_timeout_s:
                self._do_farewell('serving_timeout')

    # ---- 控制事件处理（点单屏 / 制作流程注入）----
    def _step_control(self, ev):
        etype = ev.get('type')

        if etype == 'user_select':
            # 顾客在屏上主动选了饮品 → 进 ORDERING，尊重自选、不再硬推
            if self.state in (STATE_OBSERVE, STATE_ORDERING):
                self._selected = ev.get('drink_name') or ev.get('drink_id')
                if self.state != STATE_ORDERING:
                    self._last_present = self._now()
                    self._set_state(STATE_ORDERING)
                emit({'event': 'user_select', 'mascot': 'wake',
                      'drink_id': ev.get('drink_id'),
                      'drink_name': ev.get('drink_name')})
            else:
                emit({'event': 'user_select_ignored', 'state': self.state,
                      'drink_id': ev.get('drink_id')})

        elif etype == 'order_confirmed':
            if self.state == STATE_ORDERING:
                self._set_state(STATE_WAITING, voice_key='order_confirm',
                                text=self.texts['order_confirm'],
                                drink_id=ev.get('drink_id'),
                                drink_name=ev.get('drink_name') or self._selected)
            else:
                emit({'event': 'order_confirmed_ignored', 'state': self.state})

        elif etype == 'making_done':
            if self.state == STATE_WAITING:
                self._last_present = self._now()
                self._set_state(STATE_SERVING, voice_key='ready_take',
                                text=self.texts['ready_take'],
                                drink_name=self._selected)
            else:
                emit({'event': 'making_done_ignored', 'state': self.state})

        elif etype == 'served':
            # 顾客已取走饮品 → 告别
            if self.state == STATE_SERVING:
                self._do_farewell('served')
            else:
                emit({'event': 'served_ignored', 'state': self.state})

        else:
            emit({'event': 'unknown_control', 'type': etype, 'state': self.state})

    def step(self, ev):
        """喂一个事件（人脸事件或控制事件），驱动一次状态机。"""
        if 'type' in ev and 'present' not in ev:
            self._step_control(ev)
        else:
            self._step_face(ev)

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
    """mock 事件演示全流程（一次性回放，播完退出，退出前须回到 NO_PERSON）：

    无人 → 顾客 p1 走近 → 问候+进场推荐 → 打哈欠（fatigue_score 过 0.6）
    → 疲劳提示+提神推荐 → 微笑彩蛋 → 停留引导 → 屏上自选（焦糖玛奇朵）
    → 下单确认 → 制作中（再打哈欠也不推荐）→ 制作完成 → 取走告别
    → 同一个人很快折返（跳过打招呼直接进观察态）→ 人离开 → 告别回 NO_PERSON。

    脚本元素见 MockFaceEventSource docstring；fatigue dict 模拟
    FatigueMonitor.update 的输出。"""
    def fat(score, level, events=None):
        return {'present': True, 'calibrated': True, 'ear': 0.3, 'mar': 0.5,
                'head_down': 0.0, 'fatigue_score': score,
                'events': events or [], 'level': level}

    script = (
        [(False, 0.0, 0.0)] * 2            # 无人
        + [(True, 0.05, 0.3, None, 'p1')]  # 远处出现人脸（p1）
        + [(True, 0.09, 0.3, None, 'p1')]  # 走近（ratio 增大）→ 确认 → 问候+推荐
        + [(True, 0.10, 0.3, fat(0.1, 'alert'), 'p1')]        # 站在屏前，精神正常
        + [(True, 0.10, 0.3, fat(0.7, 'tired', ['yawn']), 'p1')]   # 打哈欠 → 疲劳提示+提神推荐
        + [(True, 0.10, 0.3, fat(0.75, 'tired', ['yawn']), 'p1')]  # 持续疲劳（不再重复提示）
        + [(True, 0.10, 0.85, fat(0.2, 'alert'), 'p1')] * 2   # 笑了 → 微笑彩蛋
        + [(True, 0.10, 0.4, fat(0.1, 'alert'), 'p1')] * 6    # 停留较久 → 引导文案
        + [{'type': 'user_select', 'drink_id': 7, 'drink_name': '焦糖玛奇朵'}]  # 屏上自选 → ORDERING
        + [(True, 0.10, 0.4, None, 'p1')] * 2                 # 支付中
        + [{'type': 'order_confirmed', 'drink_id': 7}]        # 下单确认 → WAITING
        + [(True, 0.10, 0.3, fat(0.8, 'tired', ['yawn']), 'p1')] * 3  # 制作中打哈欠：不推荐
        + [{'type': 'making_done'}]                           # 制作完成 → SERVING
        + [(True, 0.10, 0.4, None, 'p1')]
        + [{'type': 'served'}]                                # 取走 → 告别 → NO_PERSON
        + [(False, 0.0, 0.0)] * 2                             # 无人
        + [(True, 0.05, 0.3, None, 'p1')]                     # p1 很快折返
        + [(True, 0.09, 0.3, None, 'p1')]                     # → 同人判定：跳过打招呼
        + [(False, 0.0, 0.0)] * 5                             # 人离开 → 告别 → NO_PERSON
    )
    source = MockFaceEventSource(script)
    fsm = HostFSM(confirm_polls=2, absent_timeout_s=0.3, hesitate_after_s=0.5,
                  poll_interval=0.1, use_weather=not args.no_weather)
    fsm.loop(source, max_steps=len(script))
    if fsm.state != STATE_NO_PERSON:
        print('simulate 结束但状态未回到 NO_PERSON: %s' % fsm.state, file=sys.stderr)
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
           'humidity': args.humidity}
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
