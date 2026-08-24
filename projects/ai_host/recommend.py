# -*- coding: utf-8 -*-
"""推荐引擎：规则法（可解释），永远能给出结果。

recommend(ctx) 输入上下文：
  temp_c          天气 API 温度（可空）
  weather_desc    天气中文描述（可空，目前仅用于文案）
  smile           微笑度 0~1（可空）
  fatigue_score   疲劳度 0~1（可空，来自 fatigue.FatigueMonitor；landmark106 后端提供）
  hour            当前小时 0~23
  sensor_temp     机身温湿度传感器温度（可空，优先于 temp_c）
  sensor_humidity 机身湿度 %（可空）

输出 {'drink': 菜单项 dict, 'reason': 中文推荐理由, 'tags': 命中的规则列表}。

规则之间用打分制叠加：每条命中的规则给候选饮品加分，最终取总分最高者
（平分依次看：热销标记 → 价格低 → 菜单顺序）。所有规则都没命中时走兜底
（热销款），保证任何上下文都有结果。
"""

import json
import os
import time

_MENU_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'menu.json')


def load_menu(path=None):
    """加载菜单，返回饮品 dict 列表。"""
    with open(path or _MENU_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)['drinks']


def recommend(ctx, menu=None):
    """规则推荐，见模块 docstring。"""
    drinks = menu if menu is not None else load_menu()
    by_name = dict((d['name'], d) for d in drinks)

    # ---- 上下文整理：机身传感器温度优先于天气 API ----
    temp = ctx.get('sensor_temp')
    if temp is None:
        temp = ctx.get('temp_c')
    weather_desc = ctx.get('weather_desc')
    smile = ctx.get('smile')
    fatigue_score = ctx.get('fatigue_score')
    hour = ctx.get('hour')
    if hour is None:
        hour = time.localtime().tm_hour
    humidity = ctx.get('sensor_humidity')

    scores = dict((d['name'], 0.0) for d in drinks)
    tags = []
    reasons = {}   # 饮品名 -> 推荐理由列表（只收给它加过分的规则）

    def boost(names, points, tag, reason):
        """一条规则：给候选加分，登记 tag 与理由。"""
        if tag not in tags:
            tags.append(tag)
        for n in names:
            if n in scores:
                scores[n] += points
                reasons.setdefault(n, []).append(reason)

    iced_names = [d['name'] for d in drinks if d.get('ice')]
    hot_names = [d['name'] for d in drinks if d.get('hot')]
    special_names = [d['name'] for d in drinks if d['category'] == '特调']

    # 规则 1：高温（≥28°C）→ 冰饮优先，明星冰饮加重
    if temp is not None and temp >= 28:
        picks = [n for n in ('生椰拿铁', 'Dirty', '蜂蜜柠檬水') if n in by_name]
        boost(picks, 3, 'hot_ice', '今天 %.0f°C 挺热的，来杯冰的解暑' % temp)
        boost(iced_names, 1, 'hot_ice', '天热适合冰饮')

    # 规则 2：低温（≤10°C）→ 热饮暖胃
    if temp is not None and temp <= 10:
        picks = [n for n in ('热巧克力', '拿铁') if n in by_name]
        boost(picks, 3, 'cold_hot', '今天 %.0f°C 有点冷，喝杯热的暖暖身子' % temp)

    # 规则 3：早高峰（6~10 点）→ 美式提神
    if 6 <= hour < 10:
        boost(['美式'], 3, 'morning', '早上来杯美式提提神')

    # 规则 4：看着不开心（微笑度 < 0.2）→ 甜的安慰
    if smile is not None and smile < 0.2:
        picks = [n for n in ('焦糖玛奇朵', '摩卡') if n in by_name]
        boost(picks, 3, 'comfort', '看你心情一般，来点甜的会好一些')

    # 规则 5：笑得很开心（微笑度 > 0.7）→ 推荐特调新品
    if smile is not None and smile > 0.7:
        boost(special_names, 2, 'new_special', '看你心情不错，要不要试试我们的特调')

    # 规则 6：闷热（湿度 > 80%）→ 冰饮加权
    if humidity is not None and humidity > 80:
        boost(iced_names, 2, 'humid', '湿度大有点闷，冰饮更爽口')

    # 规则 7：疲劳（fatigue_score 来自 landmark106 后端的 FatigueMonitor）
    #   ≥0.6 → 强推提神饮品；0.3~0.6 → 轻提示。
    #   优先级：低于极端天气（规则 1/2），高于情绪规则（规则 4/5）。
    #   极端天气在场时降权到 2.5，让天气规则（3 分起步）压过它。
    extreme_weather = temp is not None and (temp >= 28 or temp <= 10)
    if fatigue_score is not None and fatigue_score >= 0.6:
        pts = 2.5 if extreme_weather else 4
        if '美式' in by_name:
            boost(['美式'], pts, 'fatigue', '看起来有点困？来杯冰美式提提神 💪')
        if 'Dirty' in by_name:
            boost(['Dirty'], pts, 'fatigue', '看起来有点困？Dirty 的热浓缩一口醒神 💪')
    elif fatigue_score is not None and fatigue_score >= 0.3:
        if '美式' in by_name:
            boost(['美式'], 2, 'fatigue_mild', '有点困意的话，美式比较提神')

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
        if weather_desc:
            reason = '现在%s，%s' % (weather_desc, reason)

    return {'drink': winner, 'reason': reason, 'tags': tags}


if __name__ == '__main__':
    # 直接运行：用当前时间做一次无上下文推荐，用于冒烟
    print(json.dumps(recommend({'hour': time.localtime().tm_hour}),
                     ensure_ascii=False, indent=2))
