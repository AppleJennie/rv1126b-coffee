# -*- coding: utf-8 -*-
"""天气模块：open-meteo 免费 API（无需 key），纯标准库 urllib 实现。

断网 / 超时 / 返回异常一律返回 None，由调用方自行降级（推荐引擎对 temp_c 可空）。
城市经纬度是 config 项，默认广州（23.13, 113.26），部署到别的城市改默认值或传参即可。
"""

import json
import urllib.request

# ---- config ----
DEFAULT_LATITUDE = 23.13    # 默认城市：广州
DEFAULT_LONGITUDE = 113.26
TIMEOUT_S = 5               # 网络请求超时（秒）

API_URL = ('https://api.open-meteo.com/v1/forecast'
           '?latitude={lat}&longitude={lon}&current_weather=true')

# WMO weathercode → 中文描述
_WEATHER_DESC = {
    0: '晴',
    1: '多云', 2: '多云', 3: '多云',
    45: '雾', 48: '雾',
    51: '雨', 53: '雨', 55: '雨', 56: '雨', 57: '雨',
    61: '雨', 63: '雨', 65: '雨', 66: '雨', 67: '雨',
    71: '雪', 73: '雪', 75: '雪', 77: '雪',
    80: '阵雨', 81: '阵雨', 82: '阵雨',
    85: '阵雪', 86: '阵雪',
    95: '雷暴', 96: '雷暴', 99: '雷暴',
}


def code_to_desc(code):
    """weathercode → 中文描述；未知代码返回「未知」。"""
    return _WEATHER_DESC.get(int(code), '未知')


def get_weather(latitude=None, longitude=None, timeout=TIMEOUT_S):
    """请求当前天气。

    成功返回 {'temp_c': float, 'weathercode': int, 'desc': str}；
    断网 / 超时 / 数据异常返回 None。
    """
    lat = DEFAULT_LATITUDE if latitude is None else latitude
    lon = DEFAULT_LONGITUDE if longitude is None else longitude
    url = API_URL.format(lat=lat, lon=lon)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ai-host/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        cw = data['current_weather']
        code = int(cw['weathercode'])
        return {'temp_c': float(cw['temperature']),
                'weathercode': code,
                'desc': code_to_desc(code)}
    except Exception:
        return None


if __name__ == '__main__':
    # 直接运行：打印默认城市（广州）当前天气，用于联调
    w = get_weather()
    if w is None:
        print('天气获取失败（断网或超时），调用方请降级处理')
    else:
        print('当前天气：%s，%.1f°C（weathercode=%d）' % (w['desc'], w['temp_c'], w['weathercode']))
