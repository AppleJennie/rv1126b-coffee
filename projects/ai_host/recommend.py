# -*- coding: utf-8 -*-
"""推荐引擎：规则表打分制（可解释、数据驱动，不训练模型），永远能给出结果。

recommend(ctx) 输入上下文（所有字段都可空）：
  hour            当前小时 0~23（用于推导时段；给了 period 就不用 hour）
  period          时段：morning / afternoon / evening / night
  temp_c          天气 API 温度（°C）
  weather_desc    天气中文描述（仅用于文案前缀）
  sensor_temp     机身温湿度传感器温度（优先于 temp_c）
  sensor_humidity 机身湿度 %
  smile           微笑度 0~1（用于推导表情；给了 expression 就不用 smile）
  expression      表情：happy / neutral / unhappy
  fatigue_score   疲劳度 0~1（来自 fatigue.FatigueMonitor；用于推导疲劳档位）
  fatigue         疲劳档位：awake / possibly_tired
  user_selected   用户已在点单屏自选的饮品（id 或 name）：有则尊重自选，不再硬推
  history         匿名历史偏好（饮品 id 或 name 列表），小权重加权

输出 {'drink': 菜单项 dict, 'reason': 中文推荐理由, 'tags': 命中的规则列表}。

设计：
  1. _normalize() 把原始上下文归一化成几个离散维度（时段/疲劳/表情/温度档/闷热），
     规则只跟这些维度打交道，不直接碰原始数值。
  2. 规则全部集中在 RULES 表：{'when': {维度: [允许值...]}, 'boost': [(饮品名, 加分)...],
     'tag': ..., 'reason': ...}。新增/调整规则只改这张表，不动匹配逻辑。
  3. 命中的规则给候选饮品加分，总分最高者胜出（平分依次看：热销标记 → 价格低 →
     菜单顺序）。一条都没命中时走兜底（热销款），保证任何上下文都有结果。
"""

import json
import os
import time

_MENU_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'menu.json')

# ---- 维度归一化阈值 ----
HOT_TEMP_C = 28.0     # ≥ 此温度视为「热」
COLD_TEMP_C = 10.0    # ≤ 此温度视为「冷」
HUMID_PCT = 80.0      # > 此湿度视为「闷热」
SMILE_HAPPY = 0.7     # 微笑度 ≥ 此值视为 happy
SMILE_UNHAPPY = 0.2   # 微笑度 < 此值视为 unhappy
FATIGUE_TIRED = 0.3   # 疲劳度 ≥ 此值视为 possibly_tired


def _period_of(hour):
    """小时 → 时段。morning 6~11 / afternoon 11~17 / evening 17~21 / night 其余。"""
    if 6 <= hour < 11:
        return 'morning'
    if 11 <= hour < 17:
        return 'afternoon'
    if 17 <= hour < 21:
        return 'evening'
    return 'night'


def _normalize(ctx):
    """原始上下文 → 归一化维度 dict（值均可为 None，表示该维度无数据）。"""
    # 时段
    hour = ctx.get('hour')
    if hour is None:
        hour = time.localtime().tm_hour
    period = ctx.get('period') or _period_of(hour)

    # 疲劳档位：显式 fatigue 优先，否则由 fatigue_score 映射
    fatigue = ctx.get('fatigue')
    fs = ctx.get('fatigue_score')
    if fatigue is None and fs is not None:
        fatigue = 'possibly_tired' if fs >= FATIGUE_TIRED else 'awake'

    # 表情：显式 expression 优先，否则由 smile 映射
    expression = ctx.get('expression')
    smile = ctx.get('smile')
    if expression is None and smile is not None:
        if smile >= SMILE_HAPPY:
            expression = 'happy'
        elif smile < SMILE_UNHAPPY:
            expression = 'unhappy'
        else:
            expression = 'neutral'

    # 温度档：机身传感器温度优先于天气 API
    temp = ctx.get('sensor_temp')
    if temp is None:
        temp = ctx.get('temp_c')
    temp_level = None
    if temp is not None:
        if temp >= HOT_TEMP_C:
            temp_level = 'hot'
        elif temp <= COLD_TEMP_C:
            temp_level = 'cold'
        else:
            temp_level = 'mild'

    humidity = ctx.get('sensor_humidity')

    return {
        'period': period,
        'fatigue': fatigue,
        'expression': expression,
        'temp_level': temp_level,
        'humid': (humidity > HUMID_PCT) if humidity is not None else None,
        # 以下不进规则匹配，仅供理由文案使用
        'temp': temp,
        'weather_desc': ctx.get('weather_desc'),
    }


# ---- 规则表 ----
# 每条规则：
#   when       {维度: [允许值...]}，全部维度命中才算命中；缺省的维度不关心
#   boost      [(饮品名, 加分)...]，饮品名必须落在 menu.json（找不到自动跳过）
#   boost_iced 可选：给全部「可做冰饮」的饮品统一加分（menu.json 里 ice=true）
#   tag        命中记录（输出 tags 用）
#   reason     推荐理由；支持 {temp} 占位符（有温度数据时替换）
#
# ⚠ 文案红线：视觉/疲劳推断绝不能写成医疗诊断。只能说「看起来有点疲惫」
#   这类观察性描述，禁止「检测出你睡眠不足 / 诊断 / 疾病」等表述。
#   新增规则时请自觉遵守，test_host_fsm.py 里有违禁词扫描兜底。
RULES = [
    # --- 时段 × 疲劳组合（优先级最高，浓咖提神）---
    {'tag': 'morning_tired',
     'when': {'period': ['morning'], 'fatigue': ['possibly_tired']},
     'boost': [('美式', 6)],
     # menu 里没有 double americano：映射到美式，文案说明可加浓
     'reason': '早上看着有点疲惫，来杯美式提提神，想更浓可以备注双份浓缩'},
    {'tag': 'afternoon_tired',
     'when': {'period': ['afternoon'], 'fatigue': ['possibly_tired']},
     'boost': [('美式', 6)],
     'reason': '午后容易犯困，来杯美式提提神'},
    {'tag': 'tired',
     'when': {'fatigue': ['possibly_tired']},
     'boost': [('美式', 4), ('Dirty', 3)],
     'reason': '看起来有点疲惫，咖啡提提神'},

    # --- 时段 ---
    {'tag': 'morning',
     'when': {'period': ['morning']},
     'boost': [('美式', 3)],
     'reason': '早上来杯美式提提神'},

    # --- 表情 ---
    {'tag': 'happy_sweet',
     'when': {'expression': ['happy']},
     'boost': [('摩卡', 3), ('焦糖玛奇朵', 3)],
     'reason': '看你心情不错，来点甜的更开心'},
    {'tag': 'comfort',
     'when': {'expression': ['unhappy']},
     'boost': [('焦糖玛奇朵', 3), ('摩卡', 3)],
     'reason': '看你心情一般，来点甜的会好一些'},

    # --- 天气 / 环境 ---
    {'tag': 'hot_ice',
     'when': {'temp_level': ['hot']},
     'boost': [('生椰拿铁', 3), ('Dirty', 3), ('蜂蜜柠檬水', 3)],
     'boost_iced': 1,
     'reason': '今天 {temp:.0f}°C 挺热的，来杯冰的解暑'},
    {'tag': 'cold_hot',
     'when': {'temp_level': ['cold']},
     'boost': [('热巧克力', 3), ('拿铁', 3)],
     'reason': '今天 {temp:.0f}°C 有点冷，喝杯热的暖暖身子'},
    {'tag': 'humid',
     'when': {'humid': [True]},
     'boost_iced': 2,
     'reason': '湿度大有点闷，冰饮更爽口'},
]

# 匿名历史偏好的加分权重（小权重，不压过天气/疲劳等强规则）
HISTORY_BOOST = 2


def load_menu(path=None):
    """加载菜单，返回饮品 dict 列表。"""
    with open(path or _MENU_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)['drinks']


def _match(when, env):
    """规则的 when 条件是否全部命中（只比较列出的维度）。"""
    for dim, allowed in when.items():
        if env.get(dim) not in allowed:
            return False
    return True


def _find_drink(drinks, key):
    """按 id（int/数字串）或 name 找饮品，找不到返回 None。"""
    for d in drinks:
        if key == d['id'] or key == d['name'] or str(key) == str(d['id']):
            return d
    return None


def _format_reason(template, env):
    """替换理由里的 {temp...} 占位符；没有温度数据时返回空串。"""
    if '{temp' not in template:
        return template
    if env.get('temp') is not None:
        return template.format(temp=env['temp'])
    return ''   # 理论上 temp_level 命中时 temp 必有值，这里只是兜底


def recommend(ctx, menu=None):
    """规则推荐，见模块 docstring。

    ⚠ 文案红线（生成推荐理由时必须遵守）：视觉/疲劳推断绝不写成医疗诊断。
    所有理由只能描述「看起来有点疲惫」这类观察，禁止出现
    「睡眠不足 / 诊断 / 疾病 / 确诊」等医疗化表述。规则表顶部的注释同样强调。
    """
    drinks = menu if menu is not None else load_menu()

    # ---- 用户已主动选择：尊重自选，不再硬推别的 ----
    selected = ctx.get('user_selected')
    if selected is not None:
        d = _find_drink(drinks, selected)
        if d is not None:
            return {'drink': d,
                    'reason': '您自己选的「%s」准没错，马上为您安排' % d['name'],
                    'tags': ['user_selected']}
        # 自选 id/name 在菜单里找不到：容错，继续走正常推荐

    env = _normalize(ctx)
    by_name = dict((d['name'], d) for d in drinks)
    iced_names = [d['name'] for d in drinks if d.get('ice')]

    scores = dict((d['name'], 0.0) for d in drinks)
    tags = []
    reasons = {}   # 饮品名 -> 推荐理由列表（只收给它加过分的规则）

    def boost(names, points, tag, reason):
        """登记一次加分：累加分数，记录 tag 与理由。"""
        if points <= 0 or not names:
            return
        if tag not in tags:
            tags.append(tag)
        if reason:
            for n in names:
                if reason not in reasons.setdefault(n, []):
                    reasons[n].append(reason)
        for n in names:
            if n in scores:
                scores[n] += points

    # ---- 规则表匹配 ----
    for rule in RULES:
        if not _match(rule['when'], env):
            continue
        if rule['tag'] not in tags:
            tags.append(rule['tag'])
        reason = _format_reason(rule['reason'], env)
        for name, pts in rule.get('boost', []):
            boost([name], pts, rule['tag'], reason)
        if rule.get('boost_iced'):
            boost(iced_names, rule['boost_iced'], rule['tag'], reason)

    # ---- 匿名历史偏好：小权重加权用户点过的饮品 ----
    history = ctx.get('history') or []
    hist_names = []
    for h in history:
        d = _find_drink(drinks, h)
        if d is not None and d['name'] not in hist_names:
            hist_names.append(d['name'])
    for n in hist_names:
        boost([n], HISTORY_BOOST, 'history', '按你之前的口味，「%s」应该合心意' % n)

    # ---- 选分最高者；平分依次看热销、价格、菜单顺序 ----
    def rank_key(d):
        return (scores[d['name']], 1 if d.get('hot') else 0, -d['price'], -d['id'])

    winner = max(drinks, key=rank_key)

    if scores[winner['name']] <= 0:
        # 兜底规则：无任何规则命中（比如温和天气 + 无表情数据），推热销款
        tags.append('fallback')
        hot_drinks = [d for d in drinks if d.get('hot')]
        winner = hot_drinks[0] if hot_drinks else drinks[0]
        reason = '不知道喝什么的话，试试我们的热销「%s」' % winner['name']
    else:
        reason = '；'.join(reasons.get(winner['name'], []))
        if env.get('weather_desc'):
            reason = '现在%s，%s' % (env['weather_desc'], reason)

    return {'drink': winner, 'reason': reason, 'tags': tags}


if __name__ == '__main__':
    # 直接运行：用当前时间做一次无上下文推荐，用于冒烟
    print(json.dumps(recommend({'hour': time.localtime().tm_hour}),
                     ensure_ascii=False, indent=2))
