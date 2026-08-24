#!/usr/bin/env python3
# wifi_switch.py —— WiFi 智能插座/点动继电器 局域网控制模块（纯标准库）
#
# 用途：让 RV1126B 开发板通过局域网 WiFi 控制磨豆机、滴滤机的电源/按键。
# 支持的驱动（driver）：
#   tasmota     刷了 Tasmota 固件的插座/通断器：
#               GET http://<host>/cm?cmnd=Power%20On / Power%20Off
#   sonoff_diy  易微联 Sonoff DIY 模式（Basic R3 / MINI 等，需跳线进 DIY 模式）：
#               POST http://<host>:8081/zeroconf/switch  {"deviceid":"...", "data":{"switch":"on"}}
#   custom      自定义 URL 模板：url_on / url_off 中的 {state} 会被替换为 on/off
#   mock        仿真：只打印日志，不发请求（simulate 模式自动使用）
#
# 两种用法（对应两类机器）：
#   set_power(True/False)   电源型：机器是机械锁定开关（拨到开就一直工作），
#                           插座通电=开工，断电=停止。
#   press(sec)              点动型：机器是按一下的电子按键。继电器并接在按键两端，
#                           吸合 sec 秒后断开 = 模拟按了一次键。
#
# 自测：python3 wifi_switch.py   （内置 mock HTTP 服务器，验证三种驱动的请求格式）

import json
import time
import urllib.request
import urllib.error


class SwitchError(Exception):
    """WiFi 开关控制失败（网络不通/设备拒绝/超时）。"""


class WiFiSwitch:
    """一个 WiFi 开关通道。cfg 示例：
    {"driver": "tasmota",    "host": "192.168.1.61"}
    {"driver": "sonoff_diy", "host": "192.168.1.62", "deviceid": "1000abcd12", "port": 8081}
    {"driver": "custom",     "url_on": "http://192.168.1.63/relay/0?turn=on",
                             "url_off": "http://192.168.1.63/relay/0?turn=off"}
    """

    def __init__(self, name, cfg, timeout=3.0, retries=2):
        self.name = name
        self.cfg = cfg
        self.timeout = timeout
        self.retries = retries
        self.driver = cfg.get("driver", "tasmota")
        if self.driver not in ("tasmota", "sonoff_diy", "custom"):
            raise SwitchError(f"{name}: 未知 driver {self.driver}")

    # ---------- 底层 HTTP ----------
    def _request(self, url, data=None):
        """发请求，带重试；成功返回响应文本，失败抛 SwitchError。"""
        last = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, data=data)
                if data is not None:
                    req.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", "replace")
            except (urllib.error.URLError, OSError) as e:
                last = e
                time.sleep(0.3 * (attempt + 1))
        raise SwitchError(f"{self.name}: 请求失败 {url} -> {last}")

    def _cmd(self, on):
        d = self.driver
        if d == "tasmota":
            cmd = "Power%20On" if on else "Power%20Off"
            user = self.cfg.get("user")
            auth = f"user={user}&password={self.cfg.get('password', '')}&" if user else ""
            return self._request(f"http://{self.cfg['host']}/cm?{auth}cmnd={cmd}")
        if d == "sonoff_diy":
            port = self.cfg.get("port", 8081)
            body = json.dumps({"deviceid": self.cfg.get("deviceid", ""),
                               "data": {"switch": "on" if on else "off"}}).encode()
            return self._request(f"http://{self.cfg['host']}:{port}/zeroconf/switch", data=body)
        # custom
        url = self.cfg["url_on" if on else "url_off"].replace("{state}", "on" if on else "off")
        return self._request(url)

    # ---------- 对外接口 ----------
    def set_power(self, on):
        """通电/断电。返回设备响应文本。"""
        resp = self._cmd(on)
        print(f"[WIFI] {self.name} {'通电' if on else '断电'} <- {resp[:80]}", flush=True)
        return resp

    def press(self, sec=1.0):
        """点动：吸合 sec 秒后断开（模拟按一次按键）。"""
        print(f"[WIFI] {self.name} 点动按压 {sec}s", flush=True)
        self._cmd(True)
        time.sleep(sec)
        self._cmd(False)


class MockSwitch:
    """仿真开关：接口与 WiFiSwitch 一致，只打印日志。"""

    def __init__(self, name, cfg=None, **_kw):
        self.name = name
        self.state = False

    def set_power(self, on):
        self.state = on
        print(f"[MOCK] WiFi开关 {self.name} -> {'通电' if on else '断电'}", flush=True)
        return "MOCK OK"

    def press(self, sec=1.0):
        print(f"[MOCK] WiFi开关 {self.name} 点动按压 {sec}s", flush=True)
        return "MOCK OK"


def make_switch(name, cfg, mock=False):
    """工厂：mock=True 或 cfg 里 driver=mock 时返回 MockSwitch。"""
    if mock or cfg.get("driver") == "mock":
        return MockSwitch(name, cfg)
    return WiFiSwitch(name, cfg)


# ---------- 自测：本地 mock HTTP 服务器记录请求 ----------

def _selftest():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    hits = []

    class Rec(BaseHTTPRequestHandler):
        def _rec(self, body=b""):
            hits.append((self.command, self.path, body.decode()))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"POWER":"ON"}')

        def do_GET(self):
            self._rec()

        def do_POST(self):
            self._rec(self.rfile.read(int(self.headers.get("Content-Length", 0))))

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Rec)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{port}"

    # tasmota
    WiFiSwitch("t", {"driver": "tasmota", "host": host}).set_power(True)
    assert hits[-1] == ("GET", "/cm?cmnd=Power%20On", ""), hits[-1]
    WiFiSwitch("t", {"driver": "tasmota", "host": host,
                     "user": "admin", "password": "pw"}).set_power(False)
    assert hits[-1][1] == "/cm?user=admin&password=pw&cmnd=Power%20Off", hits[-1]

    # sonoff_diy
    WiFiSwitch("s", {"driver": "sonoff_diy", "host": "127.0.0.1",
                     "port": port, "deviceid": "1000abcd12"}).set_power(True)
    cmd, path, body = hits[-1]
    assert (cmd, path) == ("POST", "/zeroconf/switch"), hits[-1]
    assert json.loads(body)["data"]["switch"] == "on", body

    # custom + press（点动应产生 on 再 off 两条请求）
    sw = WiFiSwitch("c", {"driver": "custom",
                          "url_on": f"http://{host}/r?turn={{state}}",
                          "url_off": f"http://{host}/r?turn={{state}}"})
    sw.press(0.1)
    assert hits[-2][1] == "/r?turn=on" and hits[-1][1] == "/r?turn=off", hits[-2:]

    # 失败重试：连一个不存在的服务，应抛 SwitchError 且耗时受控
    bad = WiFiSwitch("bad", {"driver": "tasmota", "host": "127.0.0.1:1"},
                     timeout=0.2, retries=1)
    try:
        bad.set_power(True)
        raise AssertionError("应当抛 SwitchError")
    except SwitchError:
        pass

    # mock
    make_switch("m", {"driver": "tasmota", "host": "x"}, mock=True).press(0.01)

    srv.shutdown()
    print("wifi_switch 自测全部通过")


if __name__ == "__main__":
    _selftest()
