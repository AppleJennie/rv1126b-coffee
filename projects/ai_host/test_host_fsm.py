#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TASK 10/11 自测：host_fsm 互动状态机 + recommend 规则推荐引擎。

纯标准库 unittest，不依赖 pytest，直接运行：
    python3 projects/ai_host/test_host_fsm.py

覆盖：
  状态机（TASK 10 硬性要求）
    - 同一个人短时间内两次靠近，只打一次招呼（同人判定：person_id + 冷却时间窗）
    - 制作中不推荐：WAITING 状态收到疲劳触发事件不输出推荐
    - 顾客离开（FAREWELL → NO_PERSON）后，新顾客可重新开启完整交互
    - 每次状态转换都有事件日志（state 事件必带 from/to/mascot）
  推荐引擎（TASK 11）
    - morning+tired → 美式（含加浓说明）/ afternoon+tired → 美式
    - happy → 更甜的饮品（摩卡/焦糖玛奇朵）
    - 天气热 → 冰饮；无疲劳无上下文 → 兜底热销款且不带疲劳 tag
    - 用户已自选 → 原样返回自选饮品，不再硬推
    - 匿名历史偏好小权重生效
    - 推荐结果永远落在 menu.json 真实 id/name
    - 文案红线扫描：任何上下文组合的理由都不含医疗诊断类违禁词
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import host_fsm                      # noqa: E402
import recommend as recommend_mod    # noqa: E402

# 文案红线违禁词：视觉/疲劳推断绝不写成医疗诊断（recommend.py 顶部注释同义）
FORBIDDEN_WORDS = ['睡眠不足', '诊断', '疾病', '确诊', '失眠症']


def face(present, ratio=0.1, smile=0.3, fatigue=None, person_id=None):
    """构造一帧人脸事件（与 host_fsm.MockFaceEventSource 输出同构）。"""
    ev = {'present': present, 'face_ratio': ratio, 'smile': smile,
          'fatigue': fatigue}
    if person_id is not None:
        ev['person_id'] = person_id
    return ev


def fat(score, level='tired'):
    """构造 FatigueMonitor 风格的疲劳 dict。"""
    return {'present': True, 'calibrated': True, 'ear': 0.3, 'mar': 0.5,
            'head_down': 0.0, 'fatigue_score': score,
            'events': ['yawn'] if score >= 0.6 else [], 'level': level}


def approach_frames(person_id):
    """两帧「由远走近」的人脸事件：足以让 confirm_polls=2 的状态机确认进场。"""
    return [face(True, 0.05, person_id=person_id),
            face(True, 0.09, person_id=person_id)]


class _FSMCase(unittest.TestCase):
    """公共工具：捕获 host_fsm.emit 输出的事件流 + 快速构造状态机。"""

    def setUp(self):
        self.events = []
        self._orig_emit = host_fsm.emit
        host_fsm.emit = self.events.append   # 拦截事件，不打 stdout

    def tearDown(self):
        host_fsm.emit = self._orig_emit

    def make_fsm(self, **kw):
        # 测试默认：无天气请求；absent_timeout=0 让人离开立即触发告别，
        # 免去 sleep；其余超时参数按需覆盖
        kw.setdefault('use_weather', False)
        kw.setdefault('absent_timeout_s', 0.0)
        return host_fsm.HostFSM(**kw)

    def feed(self, fsm, evs):
        for ev in evs:
            fsm.step(ev)

    def state_events(self):
        return [e for e in self.events if e.get('event') == 'state']

    def greet_count(self):
        return len([e for e in self.state_events() if e.get('to') == host_fsm.STATE_GREETING])

    def recommend_events(self):
        return [e for e in self.events if e.get('event') == 'recommend']


class TestHostFSM(_FSMCase):
    """TASK 10：状态机行为。"""

    def test_same_person_not_greeted_twice(self):
        """同人（同 person_id）冷却期内折返：只打一次招呼，直接进 OBSERVE。"""
        fsm = self.make_fsm(regreet_cooldown_s=9999.0)
        self.feed(fsm, approach_frames('p1'))
        self.assertEqual(self.greet_count(), 1)
        self.assertEqual(fsm.state, host_fsm.STATE_OBSERVE)
        # p1 离开 → FAREWELL → NO_PERSON
        self.feed(fsm, [face(False)])
        self.assertEqual(fsm.state, host_fsm.STATE_NO_PERSON)
        # p1 冷却期内折返：不再打招呼
        self.feed(fsm, approach_frames('p1'))
        self.assertEqual(self.greet_count(), 1)
        self.assertEqual(fsm.state, host_fsm.STATE_OBSERVE)
        # 有明确的 skip_greet 事件，且折返后没有再推推荐
        self.assertTrue(any(e.get('event') == 'skip_greet' for e in self.events))
        self.assertEqual(len(self.recommend_events()), 1)   # 只有首次进场那一条

    def test_new_customer_after_person_left(self):
        """顾客离开后，新顾客（不同 person_id）可重新开启完整交互。"""
        fsm = self.make_fsm(regreet_cooldown_s=9999.0)
        self.feed(fsm, approach_frames('p1'))
        self.feed(fsm, [face(False)])   # p1 离开
        self.assertEqual(fsm.state, host_fsm.STATE_NO_PERSON)
        # 新顾客 p2：即使在同人冷却期内，id 不同 → 正常打招呼 + 推荐
        self.feed(fsm, approach_frames('p2'))
        self.assertEqual(self.greet_count(), 2)
        self.assertEqual(fsm.state, host_fsm.STATE_OBSERVE)
        self.assertEqual(len(self.recommend_events()), 2)

    def test_same_person_after_cooldown_regreeted(self):
        """时间窗启发式：无 person_id 时，冷却期过后的靠近视为新顾客。"""
        fsm = self.make_fsm(regreet_cooldown_s=-1.0)   # 冷却立即过期
        self.feed(fsm, approach_frames(None))
        self.feed(fsm, [face(False)])
        self.feed(fsm, approach_frames(None))
        self.assertEqual(self.greet_count(), 2)

    def test_no_recommend_while_waiting(self):
        """制作中（WAITING）收到疲劳/微笑触发事件：不推荐、状态不被打乱。"""
        fsm = self.make_fsm()
        self.feed(fsm, approach_frames('p1'))
        # 屏上自选 → ORDERING → 下单确认 → WAITING
        fsm.step({'type': 'user_select', 'drink_id': 7, 'drink_name': '焦糖玛奇朵'})
        self.assertEqual(fsm.state, host_fsm.STATE_ORDERING)
        fsm.step({'type': 'order_confirmed', 'drink_id': 7})
        self.assertEqual(fsm.state, host_fsm.STATE_WAITING)
        n_rec_before = len(self.recommend_events())
        # 制作中：高疲劳 + 高微笑连续多帧，不应有任何推荐/彩蛋输出
        self.feed(fsm, [face(True, 0.1, 0.9, fat(0.9), 'p1') for _ in range(4)])
        self.assertEqual(len(self.recommend_events()), n_rec_before)
        self.assertFalse(any(e.get('event') in ('smile_bonus', 'fatigue_tip')
                             for e in self.events[n_rec_before:]))
        self.assertEqual(fsm.state, host_fsm.STATE_WAITING)
        # SERVING 同样不推荐
        fsm.step({'type': 'making_done'})
        self.assertEqual(fsm.state, host_fsm.STATE_SERVING)
        self.feed(fsm, [face(True, 0.1, 0.9, fat(0.9), 'p1')])
        self.assertEqual(len(self.recommend_events()), n_rec_before)

    def test_full_order_flow_and_log_fields(self):
        """完整链路：进场→自选→下单→制作→出餐→取走→告别→回 NO_PERSON；
        每次状态转换事件都带 from/to/mascot 日志字段。"""
        fsm = self.make_fsm()
        self.feed(fsm, approach_frames('p1'))
        fsm.step({'type': 'user_select', 'drink_id': 1, 'drink_name': '美式'})
        fsm.step({'type': 'order_confirmed', 'drink_id': 1})
        fsm.step({'type': 'making_done'})
        fsm.step({'type': 'served'})
        self.assertEqual(fsm.state, host_fsm.STATE_NO_PERSON)
        path = [e['to'] for e in self.state_events()]
        # 关键状态依次出现（GREETING/RECOMMEND/FAREWELL 为瞬时态也在日志里）
        expect_seq = [host_fsm.STATE_PERSON_APPROACH, host_fsm.STATE_GREETING,
                      host_fsm.STATE_RECOMMEND, host_fsm.STATE_OBSERVE,
                      host_fsm.STATE_ORDERING, host_fsm.STATE_WAITING,
                      host_fsm.STATE_SERVING, host_fsm.STATE_FAREWELL,
                      host_fsm.STATE_NO_PERSON]
        self.assertEqual(path, expect_seq)
        for e in self.state_events():
            self.assertIn('from', e)
            self.assertIn('to', e)
            self.assertIn('mascot', e)

    def test_state_timeouts(self):
        """可停留状态都有超时出口，不会无限等待。"""
        # ORDERING 超时 → 告别
        fsm = self.make_fsm(ordering_timeout_s=0.0)
        self.feed(fsm, approach_frames('p1'))
        fsm.step({'type': 'user_select', 'drink_id': 1})
        self.assertEqual(fsm.state, host_fsm.STATE_ORDERING)
        self.feed(fsm, [face(True, 0.1, person_id='p1')])
        self.assertEqual(fsm.state, host_fsm.STATE_NO_PERSON)
        self.assertTrue(any(e.get('event') == 'order_timeout' for e in self.events))

        # WAITING 超时 → making_timeout → 告别
        self.events.clear()
        fsm = self.make_fsm(making_timeout_s=0.0)
        self.feed(fsm, approach_frames('p1'))
        fsm.step({'type': 'user_select', 'drink_id': 1})
        fsm.step({'type': 'order_confirmed', 'drink_id': 1})
        self.feed(fsm, [face(True, 0.1, person_id='p1')])
        self.assertEqual(fsm.state, host_fsm.STATE_NO_PERSON)
        self.assertTrue(any(e.get('event') == 'making_timeout' for e in self.events))

        # OBSERVE 人在但迟迟不互动 → 主动告别
        self.events.clear()
        fsm = self.make_fsm(max_observe_s=0.0, hesitate_after_s=9999.0)
        self.feed(fsm, approach_frames('p1'))
        self.feed(fsm, [face(True, 0.1, person_id='p1')])
        self.assertEqual(fsm.state, host_fsm.STATE_NO_PERSON)
        reasons = [e.get('reason') for e in self.state_events()]
        self.assertIn('observe_timeout', reasons)

        # PERSON_APPROACH 迟迟不确认 → 回 NO_PERSON
        self.events.clear()
        fsm = self.make_fsm(approach_timeout_s=0.0)
        self.feed(fsm, [face(True, 0.05, person_id='p1')])   # 只一帧，不够 confirm
        self.assertEqual(fsm.state, host_fsm.STATE_PERSON_APPROACH)
        self.feed(fsm, [face(True, 0.04, person_id='p1')])   # ratio 变小，streak 重置
        self.assertEqual(fsm.state, host_fsm.STATE_NO_PERSON)


class TestRecommend(unittest.TestCase):
    """TASK 11：规则型推荐引擎。"""

    @classmethod
    def setUpClass(cls):
        cls.menu = recommend_mod.load_menu()
        cls.by_name = dict((d['name'], d) for d in cls.menu)

    def assert_real_drink(self, rec):
        """推荐结果必须落在 menu.json 的真实 id/name。"""
        ids = [d['id'] for d in self.menu]
        names = [d['name'] for d in self.menu]
        self.assertIn(rec['drink']['id'], ids)
        self.assertIn(rec['drink']['name'], names)
        d = self.by_name[rec['drink']['name']]
        self.assertEqual(d['id'], rec['drink']['id'])

    def test_morning_tired_strong_coffee(self):
        """morning + tired → 美式（menu 无 double americano，映射美式并说明加浓）。"""
        rec = recommend_mod.recommend(
            {'period': 'morning', 'fatigue': 'possibly_tired'}, menu=self.menu)
        self.assert_real_drink(rec)
        self.assertEqual(rec['drink']['name'], '美式')
        self.assertIn('morning_tired', rec['tags'])
        self.assertIn('双份浓缩', rec['reason'])   # 加浓说明

    def test_afternoon_tired_americano(self):
        """afternoon + tired → 美式。"""
        rec = recommend_mod.recommend(
            {'period': 'afternoon', 'fatigue': 'possibly_tired'}, menu=self.menu)
        self.assert_real_drink(rec)
        self.assertEqual(rec['drink']['name'], '美式')
        self.assertIn('afternoon_tired', rec['tags'])

    def test_happy_sweeter_drink(self):
        """happy → 更甜的饮品（摩卡/焦糖玛奇朵）。"""
        rec = recommend_mod.recommend(
            {'period': 'afternoon', 'expression': 'happy'}, menu=self.menu)
        self.assert_real_drink(rec)
        self.assertIn(rec['drink']['name'], ('摩卡', '焦糖玛奇朵'))
        self.assertIn('happy_sweet', rec['tags'])

    def test_hot_weather_iced(self):
        """天气热（≥28°C）→ 冰饮建议（menu 里有冰选项的饮品）。"""
        rec = recommend_mod.recommend(
            {'period': 'afternoon', 'temp_c': 33}, menu=self.menu)
        self.assert_real_drink(rec)
        self.assertTrue(rec['drink']['ice'])
        self.assertIn('hot_ice', rec['tags'])

    def test_no_fatigue_no_context_fallback(self):
        """无疲劳、无任何上下文 → 兜底热销款，且不带疲劳类 tag。"""
        rec = recommend_mod.recommend({'hour': 14}, menu=self.menu)
        self.assert_real_drink(rec)
        self.assertEqual(rec['tags'], ['fallback'])
        self.assertTrue(rec['drink']['hot'])   # 兜底走热销款
        # awake + 温和天气也不应出现疲劳推荐
        rec2 = recommend_mod.recommend(
            {'period': 'evening', 'fatigue': 'awake', 'temp_c': 22}, menu=self.menu)
        self.assertNotIn('tired', rec2['tags'])
        self.assertNotIn('morning_tired', rec2['tags'])
        self.assertNotIn('afternoon_tired', rec2['tags'])

    def test_user_selected_not_overridden(self):
        """用户已主动选择：尊重自选，即使上下文强推别的也不硬推。"""
        rec = recommend_mod.recommend(
            {'period': 'morning', 'fatigue': 'possibly_tired',
             'temp_c': 33, 'user_selected': 6}, menu=self.menu)   # 6 = 摩卡
        self.assert_real_drink(rec)
        self.assertEqual(rec['drink']['name'], '摩卡')
        self.assertEqual(rec['tags'], ['user_selected'])
        # name 形式同样支持
        rec2 = recommend_mod.recommend(
            {'user_selected': '蜂蜜柠檬水'}, menu=self.menu)
        self.assertEqual(rec2['drink']['id'], 12)

    def test_history_preference_boost(self):
        """匿名历史偏好：温和上下文下加权用户点过的饮品。"""
        rec = recommend_mod.recommend(
            {'hour': 14, 'history': ['抹茶拿铁']}, menu=self.menu)
        self.assert_real_drink(rec)
        self.assertEqual(rec['drink']['name'], '抹茶拿铁')
        self.assertIn('history', rec['tags'])

    def test_fatigue_score_mapping(self):
        """旧接口兼容：fatigue_score/smile/hour 数值自动映射到归一化维度。"""
        rec = recommend_mod.recommend(
            {'hour': 8, 'fatigue_score': 0.8}, menu=self.menu)
        self.assertEqual(rec['drink']['name'], '美式')
        self.assertIn('morning_tired', rec['tags'])
        rec2 = recommend_mod.recommend({'hour': 14, 'smile': 0.9}, menu=self.menu)
        self.assertIn(rec2['drink']['name'], ('摩卡', '焦糖玛奇朵'))

    def test_reason_no_forbidden_words(self):
        """文案红线扫描：遍历上下文组合，理由绝不含医疗诊断类违禁词。"""
        periods = ['morning', 'afternoon', 'evening', 'night']
        fatigues = [None, 'awake', 'possibly_tired']
        expressions = [None, 'neutral', 'happy', 'unhappy']
        temps = [None, 35, 22, 5]
        n = 0
        for p in periods:
            for f in fatigues:
                for e in expressions:
                    for t in temps:
                        ctx = {'period': p, 'fatigue': f, 'expression': e,
                               'temp_c': t, 'hour': 12}
                        rec = recommend_mod.recommend(ctx, menu=self.menu)
                        self.assert_real_drink(rec)
                        for w in FORBIDDEN_WORDS:
                            self.assertNotIn(w, rec['reason'],
                                             '违禁词 %r 出现在: %s' % (w, rec['reason']))
                        n += 1
        # 扫描确实覆盖了组合（192 种），防空转
        self.assertEqual(n, 192)


if __name__ == '__main__':
    unittest.main(verbosity=2)
