# WiFi 控制电器 + 网页通讯设计

日期：2026-08-23 ｜ 状态：代码已完成，仿真端到端验证通过；WiFi 插座待买、真机待联调

## 一、整体架构

```
顾客 → 触摸屏(浏览器) ──HTTP/SSE──> RV1126B 开发板 ──WiFi(局域网)──> 智能插座/点动继电器 ──> 磨豆机/滴滤机
                                     │
                                     └──串口(UART)──> 舵机转接板 ──> 6 自由度机械臂
```

- 板子跑 `kiosk_server.py`（纯 Python 标准库，板端 Python 3.11.8 直接可用）
- 屏幕上的 `coffee_kiosk.html` 由板子托管，自动进入"联机模式"
- 电器控制走 WiFi，机械臂走串口，互不影响

## 二、WiFi 控制两台机器的方案（已调研）

机器本身没有 WiFi，用成品 WiFi 开关改造，两条路线按机器的开关类型二选一：

| 机器开关类型 | 判断方法 | 方案 | 接线 |
|---|---|---|---|
| 机械锁定开关（拨到"开"就一直工作，断电再通电继续工作） | 老式滴滤机、磨豆机常见 | **WiFi 智能插座**：插座通电=开工，断电=停止 | 免改机，机器插插座上 |
| 电子轻触按键（按一下启动，自动停） | 新款机器常见 | **WiFi 点动继电器**（干接点模块，设 inching/点动模式）：吸合 1 秒=按了一次键 | 继电器两端并在按键焊点上，需拆机焊 2 根线 |

**采购建议（都能局域网本地控制，不依赖厂商云）：**

1. **可刷 Tasmota 的插座**（首选，HTTP GET 控制，最简单）：
   `GET http://<IP>/cm?cmnd=Power%20On` / `Power%20Off`
2. **Sonoff 易微联 DIY 模式**（Basic R3 / MINI R3 / 点动模块 RE5V1C，免刷机）：
   `POST http://<IP>:8081/zeroconf/switch`，JSON `{"deviceid":"...", "data":{"switch":"on"}}`
3. 涂鸦插座也能本地控制（tinytuya 库），但要装第三方库，不推荐首选。

**代码位置**：`projects/coffee_fsm/wifi_switch.py`（已实现三种驱动 + mock + 自测）。
`config.json` 里 `actuators` 配置每台机器：

```json
"grinder": { "type": "wifi", "driver": "tasmota", "host": "192.168.1.61",
             "mode": "power", "run_sec": 15 },
"brewer":  { "type": "wifi", "driver": "tasmota", "host": "192.168.1.62",
             "mode": "press", "press_sec": 1.0 }
```

- `mode: "power"` = 电源型（通电 → 运行 run_sec → 断电）
- `mode: "press"` = 点动型（吸合 press_sec 秒 = 按一次键）
- 把 `type` 改回 `"servo"` 就退回机械臂按键方案，两种可以混用

**fsm.py 流程变化**：`PRESS_GRINDER` / `PRESS_BREWER` 两个状态从"机械臂按按键"改为
`_operate_machine()` 按配置分发；机械臂仍需做的：取杯、放杯、抓粉杯、倒粉、递杯。

**注意**：插座/继电器和开发板必须在**同一局域网**，建议路由器里给插座绑固定 IP。
板子自身 WiFi 配置：板端有 wpa_supplicant，配 `/etc/wpa_supplicant.conf` 后
`wpa_supplicant -B -i wlan0 -c /etc/wpa_supplicant.conf && udhcpc -i wlan0`。

## 三、网页 ↔ 开发板 通讯设计

### 模式自动切换（零配置）

`coffee_kiosk.html` 启动时判断 `location.protocol`：

- `file://` 直接打开 → **演示模式**：全部前端模拟（原行为不变）
- `http(s)://` 被板子托管 → **联机模式**：菜单/下单/进度全走 API；
  拉菜单失败自动回退演示模式（服务端挂了屏幕还能亮）

### 接口一览

| 方法 | 路径 | 内容 | 说明 |
|---|---|---|---|
| GET | `/` | 点单屏页面 | 板子托管 HTML |
| GET | `/api/menu` | `{categories, menu[12], machine}` | 菜单唯一数据源是 `ai_host/menu.json` |
| POST | `/api/order` | 请求 `{drink_id, opts:{cup,temp,sugar,extras}, qty}` → 响应 `{ok, order_id, pickup_no, total}` | **服务端重算价格**，不信页面金额；机器故障返回 409 `{ok:false, reason:"machine_nowater"}` |
| GET | `/api/events` | SSE 事件流（见下） | 页面用 `EventSource` 订阅，断线原生自动重连 |
| GET | `/api/status` | `{machine, queue_len, current}` | 调试快照 |
| POST | `/api/machine` | `{state:"ok\|nowater\|nobeans"}` | 维护面板切换（将来接水位/豆位传感器） |

### SSE 事件（服务端 → 屏幕推送）

```json
{"type":"hello",    "machine":"ok", "queue_len":0}                          // 连接即下发
{"type":"progress", "order_id":1, "pickup_no":"632",
 "steps":["取杯","磨豆","冲泡","出品"], "step_index":1,
 "step_name":"磨豆", "remain_sec":5}                                        // 制作进度
{"type":"done",     "order_id":1, "pickup_no":"632"}                        // 完成→完成页
{"type":"error",    "order_id":1, "message":"..."}                          // 失败→提示回菜单
{"type":"machine",  "state":"nowater"}                                      // 机器故障→横幅+置灰
```

选 SSE 而不是 WebSocket 的原因：状态推送是**单向**的（下单走普通 POST），
SSE 跑在纯 HTTP 上、浏览器原生支持自动重连、板端用标准库 5 行就能写，零依赖。

### 真机进度怎么来的

`kiosk_server.py` 每单 fork 一个子进程跑 `fsm.py run`，逐行解析它的日志：

- `[FSM] 状态转换 X -> Y` → 映射到屏幕步骤：
  LOCATE_CUP/PICK_CUP/PLACE_CUP→取杯，PRESS_GRINDER/POUR_GROUNDS→磨豆，
  PRESS_BREWER/WAIT_BREW→冲泡，SERVE→出品
- `[BREW] 冲泡中... 剩余 Ns` → 屏幕显示剩余秒数
- 退出码非 0 → 推 `error` 事件，屏幕提示后回菜单页

`--simulate` 模式用内置时间线（取杯3s→磨豆5s→冲泡8s→出品3s），无硬件也能演示全链路。

### 订单队列

只有一条机械臂，订单串行：先下单先制作，队首正在做的订单状态见 `/api/status`。
（当前版本屏幕只看自己那单的进度；排队提示是后续增强项。）

## 四、验证记录（仿真）

- `wifi_switch.py` 内置 mock HTTP 服务器自测：三种驱动请求格式、点动 on→off、失败重试 ✅
- `fsm.py simulate`：WiFi 开关 mock 接入，全流程通过 ✅
- `kiosk_server.py --simulate`：curl 下单（生椰拿铁大杯+燕麦奶×2=56 元，服务端算价正确）、
  缺水拒单（409 machine_nowater）、SSE 事件流 hello→progress×4→done 完整 ✅

## 五、待办

1. 买 WiFi 插座/点动继电器（Tasmota 或 Sonoff DIY），路由器绑固定 IP，改 `config.json` 的 host
2. 确认两台机器的开关类型 → 选 power 还是 press 模式；电子按键机器需拆机焊点动继电器
3. 板子连 WiFi（wpa_supplicant），屏幕浏览器开机自启指向 `http://127.0.0.1:8080/`
4. 缺水/缺豆传感器到位后，把 `POST /api/machine` 换成 GPIO 真实驱动
5. 真机联调时观察 fsm.py 日志解析是否匹配（格式变了要同步改 `kiosk_server.py` 的正则）
