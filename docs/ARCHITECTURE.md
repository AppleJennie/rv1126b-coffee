# 架构审计报告（TASK 1）

日期：2026-08-24 ｜ 基线：v0.1.1（62dc709）｜ 范围：`projects/` 全部模块，只读审计

审计范围：`projects/` 下 kiosk_server / coffee_fsm / ai_host / vision / ui_prototype / servo_bus。当前所有模块均为仿真验证通过、无真硬件状态。

## 1. 九域调用关系总览

### 真实依赖图（import / subprocess / 文件 / 网络）

```
┌─ Web ─────────────────────────────────────────────────────────────┐
│ ui_prototype/coffee_kiosk.html（单文件，file:// 演示 / http 联机） │
└──────┬────────────────────────────────────────────────────────────┘
       │ GET /  /api/menu  /api/status  /api/events(SSE)
       │ POST /api/order  /api/machine        （coffee_kiosk.html:498-507,534,995,1132）
       ▼
┌─ kiosk server ────────────────────────────────────────────────────┐
│ kiosk_server/kiosk_server.py                                      │
│  ├─ 读文件 ../ui_prototype/coffee_kiosk.html     (kiosk_server.py:37,253)
│  ├─ 读文件 ../ai_host/menu.json                  (kiosk_server.py:38,335)
│  └─ 真机模式 subprocess: python3 ../coffee_fsm/fsm.py run
│       解析其 stdout 日志行 → 转 SSE progress     (kiosk_server.py:199-216)
└───────────────────────────────────────────────────────────────────┘

┌─ coffee FSM ──────────────────────────────────────────────────────┐
│ coffee_fsm/fsm.py                                                 │
│  ├─ from sts import BusServo            (本目录, fsm.py:56)        │
│  ├─ from wifi_switch import make_switch (本目录, fsm.py:102)       │
│  ├─ sys.path.insert(../vision) 后 import cup_detect / hand_eye_calib
│  │     (跨目录 import, fsm.py:124-128；cmd_check 重复一次 :500-503) │
│  ├─ 读 config.json / poses.json         (fsm.py:36-43)             │
│  └─ 写 poses.json（teach）               (fsm.py:46-49,443)        │
└──────┬────────────────────────────────────────────────────────────┘
       ▼ hardware 域
  sts.py —— pyserial 直连 /dev/ttyUSB0（config.json:2）
  wifi_switch.py —— urllib HTTP → 192.168.1.61/62（config.json:34-37）
  relay.py —— sysfs GPIO 工具，**无任何调用者**（grep 全仓无引用）
  servo_bus/*.c —— 独立 CLI 工具（servo_tool/teach_record/teach_play），
                   **没有任何 Python 模块 subprocess 调它**；sts.py 是
                   sts_servo.c 的平行 Python 移植（sts.py:3-7），双实现

┌─ interaction FSM（AI 店员）───────────────────────────────────────┐
│ ai_host/host_fsm.py                                               │
│  ├─ import recommend / weather            (host_fsm.py:31-32)      │
│  │     └─ recommend.py 独立再读一次 menu.json (recommend.py:24-30) │
│  ├─ run 子命令延迟 import face_events     (host_fsm.py:274)        │
│  │     └─ face_events.py → import cv2, fatigue (face_events.py:34-36)
│  │           └─ 延迟 import numpy/rknnlite (face_events.py:108-109,268-269)
│  └─ 输出：**仅 stdout 打印事件 JSON 行**  (host_fsm.py:63-67)      │
│       没有任何进程消费它 —— kiosk_server 与 host_fsm 完全无对接     │
└───────────────────────────────────────────────────────────────────┘

┌─ vision ──────────────────────────────────────────────────────────┐
│ cup_detect.py（grab_frame+detect_cup）  ← 被 fsm.py / cup_locate 复用
│ hand_eye_calib.py（H 矩阵求解/存取）    ← 被 fsm.py / cup_locate 复用
│ snapshot.py（独立采图工具，grab_frame 与 cup_detect 重复实现）
│ cup_locate.py（成品 CLI，未被 fsm 调用——fsm 走的是 RealVision 内联路径）
└───────────────────────────────────────────────────────────────────┘

┌─ audio ───────────────────────────────────────────────────────────┐
│ voice_manifest.json（12 条文案）+ gen_audio_mac.sh（Mac 端批量合成）│
│ audio/ 目录为空；板端**无任何播放代码**；voice_key 只出现在        │
│ host_fsm 的 stdout 事件里（host_fsm.py:124,152,163,203,217）        │
└───────────────────────────────────────────────────────────────────┘

config 域读者：config.json/poses.json → fsm.py；menu.json → kiosk_server.py:335 + recommend.py:29（双读者）；voice_manifest.json → host_fsm.py:46-60。
logging 域：全部 `print` 到 stdout，格式 `[HH:MM:SS] [TAG] msg`（kiosk_server.py:56、fsm.py:22），wifi_switch 用 `[WIFI]/[MOCK]`（wifi_switch.py:84,104），host_fsm 输出 JSON 行（host_fsm.py:67）。无 logging 模块、无文件、无级别、无轮转。
```

无循环 import。唯一的"隐性协议"是 kiosk_server 用正则解析 fsm.py 的 stdout 日志（见 §3-耦合）。

## 2. 重复状态

| 状态 | 散落位置 |
|---|---|
| 饮品菜单（12 项，含价格） | ① `coffee_kiosk.html:544-557`（内嵌 MENU，演示模式用）② `ai_host/menu.json:5-17` ③ 运行时 kiosk 从 ② 加载下发（`kiosk_server.py:335,260-267`）。两处静态副本靠手工同步 |
| 加料/杯型价格规则 | ① HTML `OPT_DEFS`：大杯+3、加料 +2/+3/+2（`coffee_kiosk.html:561-565`）② 服务端 `_price_of` 写死 `+3`、`[2,3,2][i]`（`kiosk_server.py:114-116`）。改价必须改两处 |
| 机器状态 `ok/nowater/nobeans` | ① 前端 `MACHINE.state`（`coffee_kiosk.html:482`）② 服务端 `OrderManager.machine`（`kiosk_server.py:103,143-149`）③ 文案层 `voice_manifest.json:11-12`（fault_water/fault_beans，无消费者） |
| 制作步骤序列 | ① 服务端真机步骤 `STEP_NAMES=["取杯","磨豆","冲泡","出品"]`（`kiosk_server.py:44`）② 前端演示步骤 `BREW_STEPS=['磨豆','萃取','打奶','完成']`（`coffee_kiosk.html:1027`）③ 图标映射 `LIVE_STEP_ICONS`（`coffee_kiosk.html:1029-1030`）。演示与联机是两套步骤名 |
| 仿真时间线 | ① kiosk 仿真 `SIM_STEP_SEC=[3,5,8,3]` 共 19s（`kiosk_server.py:52`）② fsm 仿真 `simulate_brew_sec=5`（`config.json:29`，`fsm.py:294`）。两个"simulate"时长不一致 |
| FSM 状态名集合 | ① 产生方 `fsm.py` 各 `set_state` ② 消费方硬编码映射 `STATE_TO_STEP`（`kiosk_server.py:45-50`）。fsm 增删/改名状态，kiosk 静默丢步骤 |
| 语音文案 | ① `voice_manifest.json` ② `host_fsm.py:39-45` `_DEFAULT_TEXTS` 兜底重复 5 条（双份维护） |
| STS 协议常量（寄存器表/指令码/0~4095） | ① C 版 `sts_servo.h:15-41` ② Python 版 `sts.py:14-37`。双实现平行演进，C 版多了 SYNC_WRITE |
| `grab_frame` 采帧逻辑 | ① `cup_detect.py:12-31` ② `snapshot.py:12-40`（近乎复制） |
| 摄像头设备号 23 | **六处**：`config.json:17`、`cup_detect.py:82`、`cup_locate.py:19`、`snapshot.py:45`、`face_events.py:402`、`host_fsm.py:330` |

## 3. 硬编码

- IP：`config.json:34`（192.168.1.61 grinder）、`config.json:36`（192.168.1.62 brewer）——配置文件里尚可；`wifi_switch.py:33-36` docstring 示例写死 IP，易误导
- 端口：kiosk 默认 8080（`kiosk_server.py:331`，有 CLI 参数 ✅）；Sonoff DIY 默认 8081（`wifi_switch.py:72`）
- 路径：kiosk 跨目录相对路径写死（`kiosk_server.py:37-40`）；fsm 跨目录 `vision_dir`（`config.json:18` + `fsm.py:124`）；模型路径相对 cwd（`host_fsm.py:332-335`、`face_events.py:264,402-404`）
- 参数写死：Hough 半径 40/200 三处重复；支付倒计时 120s（`coffee_kiosk.html:980`）；取餐号 3 位随机（`kiosk_server.py:133`）；天气城市广州（`weather.py:12-13`）；MockVision 固定杯位（`fsm.py:117`）；推荐引擎用**中文饮品名**做规则匹配（`recommend.py:69-108`）——菜单改名即规则静默失效
- CORS 全开（`kiosk_server.py:234`）；`/api/machine` 无鉴权（`kiosk_server.py:319-321`），局域网内任何人可把机器置"缺水"

## 4. 阻塞调用

- **真机制作无超时**：`subprocess.Popen(fsm.py run)` 后读 stdout + `proc.wait()` 均无 timeout（`kiosk_server.py:199-216`）。fsm 挂死则订单队列永久停摆
- 仿真制作 `time.sleep` 共 19s 不可取消（`kiosk_server.py:191-193`）
- WAIT_BREW 180 次 `sleep(1)`（`fsm.py:295-297`）——流程性等待但无法响应急停
- `cv2.VideoCapture.read()` 无超时：`face_events.py:453`、`cup_detect.py:25`
- 交互式 `input()` 无超时：`fsm.py:432`（teach 期间全臂卸力）、`hand_eye_calib.py:203,214`
- 已有超时：串口读写（`sts.py:70-80` 100ms）、WiFi HTTP（`wifi_switch.py:57` 3s+2 重试）、天气（5s/3s——但在问候路径上同步阻塞最多 3s，`host_fsm.py:130`）
- 串口扫总线最坏 ~25s（`sts.py:149-155`；cmd_check 降到 30ms 缓解未参数化，`fsm.py:476`）

## 5. 全局变量 / 可变全局状态

- `OrderManager.machine/current` 无锁读写（`kiosk_server.py:103,106,146,155`）——ThreadingHTTPServer 下有竞态；`_seq` 有锁 ✅
- 前端全局 `MACHINE/ORDER/activeTimers/...`（单文件原型可接受）
- `RealVision.__init__` 的 `sys.path.insert` 全局污染（`fsm.py:125-126`，cmd_check 重复一次）
- `FaceEventSource._last_result` 缓存：单帧失败后**永久沿用旧结果**（`face_events.py:455-459`），摄像头彻底拔掉后上层永远看到陈旧"无人"，无故障上报
- `EventBus._subs` 有锁 ✅；订阅队列满丢弃是显式设计 ✅

## 6. 模块耦合

- **日志文本即接口（最脆）**：kiosk 真机模式用正则解析 fsm stdout（`kiosk_server.py:206,213` ← `fsm.py:227,296`）。改日志措辞 = 进度条静默失效，且无测试能发现
- 跨目录 import/路径硬编码（`fsm.py:124-128`、`kiosk_server.py:37-40`），目录改名三处同时坏
- 上层直接 subprocess fsm.py，无中间抽象
- **AI 店员链路断裂**：host_fsm 事件只到 stdout，kiosk 不消费；前端 mascot 问候/推荐是写死的假数据（`coffee_kiosk.html:638-641`）；`voice_manifest.json` 12 条 key 中 8 条**无消费者**；audio/ 为空、无播放链路
- 推荐引擎与菜单通过中文字面量耦合（`recommend.py:69-108`）
- 双实现漂移风险：`sts.py` vs `sts_servo.c`；`cup_detect.grab_frame` vs `snapshot.grab_frame`
- 无循环依赖 ✅

## 7. 无 timeout 的流程

- 订单队列 `queue.Queue()` **无界**（`kiosk_server.py:103`），无取消 API、无上限拒绝
- SSE `wfile.write` 无 socket timeout（`kiosk_server.py:294,304`），客户端半开时 handler 线程永久阻塞、堆积
- 支付 120s 倒计时只在前端；服务端无支付状态机，`timeout_cancel` 语音无人触发
- 等硬件到位是"固定 sleep 后单次回读"（`fsm.py:194-207`），不轮询 moving 位
- 前端 EventSource 原生自动重连 ✅

## 8. 无异常恢复的流程

- **`CoffeeFSM.run` 只捕获 `ArmError`（`fsm.py:233`）**：`SwitchError`（插座离线）、`KeyboardInterrupt`、cv2 异常、串口 `OSError` 都会绕过 `_safe_shutdown` 直接崩进程，机械臂保持上力。`cmd_run` 的 finally 只关串口不卸力（`fsm.py:411-412`）
- **WiFi 点动/电源型无 finally**：`press()` 第二次 `_cmd` 失败则继电器保持吸合（`wifi_switch.py:90-92`）；`set_power(True)` 后 `_hold` 期间异常 → 磨豆机插座保持通电（`fsm.py:323-326`）
- `_make_real` 子进程无 try/finally 回收，`_progress` 抛异常则僵尸进程泄漏（`kiosk_server.py:199-216`）
- `HaarBackend.detect` docstring 声称"异常按无人处理不抛给上层"（`face_events.py:83`）但**函数体内没有 try**——cv2.error 会穿透到进程崩溃。注释与行为不符
- kiosk 启动时 menu.json 打开失败直接抛栈（`kiosk_server.py:335-336`）
- 好的示范：cmd_teach finally 恢复上力、`_safe_shutdown` 逐关节 try、weather 全异常吞掉返回 None、host_fsm 后端失败优雅降级

## 9. 重构风险清单（现有 simulate 的外部可观测契约，新架构必须保持兼容）

1. **fsm.py simulate stdout 文本**：`[FSM] 状态转换 A -> B`、`[BREW] 冲泡中... 剩余 Ns`（kiosk 真机模式正则依赖）
2. **fsm.py 退出码**：0 成功 / 1 流程失败 / 2 初始化失败
3. **fsm 状态名集合** ↔ kiosk `STATE_TO_STEP` 映射
4. **kiosk simulate 节奏**：`SIM_STEP_SEC=[3,5,8,3]` + `STEP_NAMES`（页面演示 19s）
5. **SSE 事件 schema**：`hello/machine/progress/done/error` 五类（字段见 `kiosk_server.py:160-168,284` ↔ `coffee_kiosk.html:510-530`）
6. **HTTP API 契约**：`/api/menu`（menu 项含 `id,cn,cat,iced,price,en,desc,emoji,color`）、`/api/order` 请求响应格式、`/api/machine {state}`
7. **menu.json 结构**：`drinks[]` 字段被三方消费（kiosk 下发、recommend 中文名匹配、HTML 内嵌副本）
8. **价格规则同步**：服务端 `_price_of` 与前端 `OPT_DEFS` 一致（qty 上限 9 两边一致）
9. **取餐号格式**：3 位数字字符串
10. **host_fsm simulate 行为**：回放后回到 ABSENT、退出码 0；事件 JSON 行 `event/mascot/voice_key/text` 字段
11. **voice_manifest key 与 `_DEFAULT_TEXTS` 对齐**（只取交集）
12. **config.json/poses.json 键名**：`joint_ids` J1~J6；poses 8 个姿态名；`gripper ∈ open/close/hold`
13. **Mock 行为**：MockBus 初始 2048 立即到位；MockVision 固定杯位；与 `position_tolerance=25` 组合成全绿
14. **teach 写回格式**：indent=2 + 尾换行
15. **C/Python 双实现等价性**：改 `sts_servo.c` 协议行为须同步 `sts.py`

### 优先修复建议（按风险排序，供后续 TASK 参考）

1. `fsm.py:233` `except ArmError` 扩为捕获 `Exception`（至少补 `SwitchError`、`KeyboardInterrupt`），任何失败都走卸力
2. `wifi_switch.press` 与 `_operate_machine` 电源模式加 try/finally（防插座停在通电态）
3. kiosk ↔ fsm 用结构化 JSON 事件替代正则解析日志
4. `_make_real` 子进程加超时与 finally kill；订单队列加上限
5. 菜单单一来源 menu.json；价格规则收敛到 menu.json
6. `face_events.py:83` HaarBackend.detect 补 try/except，使实现符合 docstring
7. `OrderManager.machine` 加锁
