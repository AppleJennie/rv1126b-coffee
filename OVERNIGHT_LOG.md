# OVERNIGHT_LOG — 自主开发日志

> 按时间倒序追加。每个阶段记录：做了什么、验证命令与结果、commit hash、遗留问题。

## 2026-08-24 开工

- 目标：按 40 任务清单推进（P0→P1→P2），无真硬件全部先模拟，最终过 PRE-HARDWARE GATE
- 基线：v0.1.1（62dc709），仿真点单全链路绿，已推 GitHub
- 原则确认：不破坏 `fsm.py simulate` / `kiosk_server.py --simulate` 现有行为；新架构增量添加

### TASK 1 架构审计（explore 子代理执行）✅
- 产出 `docs/ARCHITECTURE.md`：九域调用关系图；重复状态 10 处、硬编码、阻塞调用、
  全局状态、耦合、无 timeout、无异常恢复七类问题（全带文件:行号）；15 项重构兼容契约
- 关键发现：fsm.run 只捕 ArmError（SwitchError/断电保持通电风险）；kiosk 用正则解析
  fsm 日志（最脆接口）；host_fsm 事件无消费者；订单队列无界
- 结论：新架构必须用结构化事件（`[EVENT] json`）替代日志解析；FSM 兜底 catch-all + 卸力

### TASK 2 hardware/ 适配层 ✅
- 新增 `hardware/`：base（统一接口+异常）、arm（RobotArm 语义动作，业务不见角度）、
  machines（SmartSwitch/Appliance 带最大运行时间保护+断电回读校验）、cup、
  sim（全 Sim 实现+故障注入钩子）、sts_arm/wifi_plug/vision_cup（真实适配器薄封装，
  不 connect 不碰硬件）、factory（make_devices SIM/REAL/HYBRID）
- 新增 `config/poses.yaml`：语义位姿库（HOME/CUP/BREWER/WATER/SERVE/GROUNDS_*，占位值）
- 验证：冒烟脚本 20+ 断言全过（正常序列+8 种故障注入+真实适配器可构造）；
  修了一处实测发现的 bug：wait_done 正常到期路径不校验断电结果（stuck_on 会静默通过）
- 旧 `fsm.py simulate` 回归 exit=0

### TASK 4 Recipe 引擎 ✅
- `config/recipes.yaml`（5 配方：americano/double_americano/hot_water/coffee/demo_drink）
  + `projects/coffee_fsm/recipe.py`（RecipeEngine：match_ids 映射，未知饮品回 default）
- 加载期校验：缺字段/重复 match/粉量与磨豆时长矛盾均拒绝
- 验证：menu.json 12 饮品全部可解析；坏配方正确报错
- 依赖备注：板端需 `pip3 install pyyaml`（Buildroot Python 3.11 默认无 yaml）

### 并行中：TASK 3（cafe_fsm 状态机）/ TASK 8（OrderManager）/ TASK 19（机械臂协议）

### TASK 19 机械臂协议 ✅（commit b3a0de2）
- docs/ROBOT_PROTOCOL.md + projects/servo_bus/mock_robot_serial.py（MockMCU+RobotSerialClient）
- 主代理复跑自测 exit=0 后入库

### TASK 8 OrderManager ✅（commit aab0787）
- 状态机/排队上限/取消/ETA/线程锁/真机子进程超时保护；主代理独立复测通过

### TASK 3 cafe_fsm 17 态状态机 ✅（commit b707a18）
- 主代理独立复测：正常单 14 跳 exit=0、robot_arm_fail/cup_missing/wifi_disconnect exit=1、
  hot_water 跳磨豆 30 tick、旧 fsm.py simulate 回归 exit=0
- 记录在案的设计取舍：customer_not_take_cup 在 sim 下表现为"出餐位永久占用"
  →MOVE_TO_SERVE 联锁拦截（安全优先），臂收回分支真机才可达

## ⏸ 2026-08-24 用户暂停
- 停掉进行中子代理：TASK5+27（演示脚本+模式）、TASK6+7（故障注入测试+联锁文档）
- 暂停点工作区干净，未提交半成品；已入库 6 个 commit（62dc709..b707a18，均未推 GitHub）
- 恢复方式：用户说"继续"，从 TASK 5+27 / TASK 6+7 重新派发开始

## 2026-08-25 续：TASK 6+7 / 5+27 / 36 收尾

- TASK 6+7 独立复验：tests/test_fault_injection.py 25 用例全过（35s），联锁文档质量合格 → commit b68a879
- TASK 5+27 半成品经检查实际已写完，补做端到端复验：
  - kiosk --mode SIM 下单 #1 走完 17 态全流程 completed
  - --scenario cup_missing 注入：CHECK_CUP 重试→RECOVERY→ERROR，订单 failed 带原因，机器回 ok
  - scripts/demo.sh 一键启动打印访问地址；SIGTERM 触发 trap，kiosk/cafe_fsm 子进程全部清理干净 → commit d4c2777
- TASK 36 scripts/run_regression.sh：10 项回归（py_compile/servo_bus make/recipe/旧fsm/cafe_fsm正常单/25故障用例/机械臂协议/wifi开关/hardware冒烟/kiosk端到端），首跑 10/10 PASS → commit（本次）
- 测试陷阱复盘：pkill -f 的模式会匹配自身外层 shell 命令行导致自杀，须用 [x] 括号写法
- P0 全部完成，进入 P1（10/11/13/14/15/17/18/22/24/26/29）
- TASK 22（WiFi 设备 Adapter）审计结论：TASK 2 架构已满足——SmartSwitch ABC（machines.py）+ SimSmartPlug（sim.py）+ WifiSmartSwitch（wifi_plug.py，tasmota/sonoff/custom URL 三驱动，非单一厂商锁死）+ Appliance MAX_RUN_SEC 失控强制断电（grinder 60s/coffee 600s/water 120s）；cafe_fsm.py 全文无厂商 API 直调（经 factory 注入）。未来加 MQTT 只需新增 MqttSmartSwitch 实现同一 ABC，FSM 零改动。标记完成，无需新代码。

## 2026-08-25 P1 批次进展（一）

- TASK 10+11（agent-8，commit 2d475d4）：host_fsm 3态→9态（NO_PERSON→...→FAREWELL），同人 skip_greet 免重复招呼，WAITING/SERVING 不推荐；recommend.py 重写为规则表打分制（morning+tired→美式+双份浓缩建议等 11 条规则），违禁词 192 组合扫描；15 用例 + simulate CLI 契约保持。复验通过。
- TASK 15-18（agent-9，commit 4318ffd）：vision_manager.py 统一视觉层（人/杯去抖边沿事件，疲劳/表情边沿触发），fatigue_detector.py 10s 时间窗（40% 闭眼占比+哈欠≥2→possibly_tired，25% 滞回恢复），expression.py 三 backend（Mock/CPU/RKNN桩），cup_presence.py ROI+背景差分（无背景回退 Hough）。18 用例。复验通过。遗留：启发式阈值真机需重标定。
- TASK 24+26+29（agent-11，commit 63999b4）：health.py 2s 巡检 8 项 + 接单健康闸门（robot_arm_offline 注入实测 409 拒单）+ 开机自检 9 项 READY/DEMO MODE + 页面健康角标 + tools/system_monitor.py（CPU/RAM/负载/温度/NPU/Web 响应）。复验通过。遗留：HYBRID/REAL 下巡检与 cafe_fsm 子进程两条串口连接可能互抢（真机联调确认项）。
- 回归脚本扩到 12 项（新增视觉层 mock、AI 店员交互推荐）。
- 清理了早前会话残留的 4 个 kiosk 进程（8090/8091/8093/8085 端口）。
- P2 并行批已派：39+30+31（硬件需求/systemd/WiFi）、12+32+21（隐私/音频/数字孪生）、9+25+33+34+35（kiosk 产品化：SSE断线恢复/watchdog/UI/admin/SQLite）。

## 2026-08-25 P1/P2 批次进展（二）

- TASK 13+14（agent-10，commit 444eda0）：**意外收获**——rknn-toolkit2 2.3.2 官方有 cp38+aarch64 wheel，本 VM 实装成功（坑：opencv-python 5.0 的 SVE 指令 SIGILL，降 4.10.0.84 修复）。从 insightface 官方 release 下载 SCRFD det_2.5g.onnx（SHA256 已记录），坐实仓库 2d106det.onnx 官方出处（逐位一致）。三模型全部实转出 RV1126B FP16 成品（models/，gitignore 不入库），SCRFD 同图对比 PASS（6 脸 IoU≥0.999，|Δscore|≤0.0003）——父代理独立重跑复现。既有 retinaface.rknn/2d106det.rknn 确认为 RV1106 目标不能上 RV1126B。成品上板验证入 BLOCKED.md（需板端 rknn-toolkit-lite2）。
- TASK 39+30+31（agent-12，commit 494e99c）：docs/HARDWARE_REQUIREMENTS.md（5+1 自由度、额定负载≥1kg、臂展≥250mm、±2mm、串口总线舵机菊链、机械锁定开关电器、禁纯云控插座）、deploy/cafe-backend.service（systemd-analyze verify exit=0）、docs/WIFI_DEPLOYMENT.md（mDNS 仅 iPhone 可靠，Android 回退固定 IP 贴纸）。
- TASK 12+32+21（agent-13，commit 17dd15d）：config/privacy.yaml + PRIVACY_DESIGN.md + VisionManager 隐私白名单投影（子进程零落盘固化测试）；audio_manager.py 复用 voice_manifest.json 13 key（Mock/CmdAudio 降级，play 永不抛异常）；tools/arm_twin/ 2D 三连杆孪生（13 用例：顺序合法性/超时/急停轨迹中止，SVG+ASCII 输出）。
- 视觉测试扩到 24 用例（+6 隐私）。

## 2026-08-25 收尾：TASK 28 / 38 / 40 与最终验收

- TASK 28（agent-15 被平台 403 配额中断后 resume 续完，commit 2891a9d）：projects/common/structured_log.py 双路输出（控制台旧格式逐字不变 + logs/cafe-YYYYMMDD.jsonl，O_APPEND 单行写并发安全），各模块 log() 最小改动转发带回退；cafe_fsm 三条终结路径写 reports/order_<id>.json（13 态进出时间+时长+result/error），result 事件追加 report 键透传 kiosk。9 项新测试。契约自查：fsm 正则/[EVENT] 行/kiosk 端到端全不破。
- TASK 38（commit 0e00e44）：scripts/rv1126b_gate.sh 串口逐项探板（Python/OpenCV/存储/网络/摄像头/RKNN runtime/模型/音频/Web端口/systemd/日志目录），本地 6 项 OK，板端 13 项因板未连接如实 MISSING，exit 1 不误报 READY。
- TASK 40：真实摄像头复验（VM /dev/video0 抓帧 720×1280 + Haar 链路 OK）；docs/PRE_HARDWARE_TEST_REPORT.md 生成：15 项门禁 14 实测 PASS + 1 静态 PASS（系统重启，systemd verify exit=0，真机自启待板端）；回归 15/15 PASS。
- docs/README.md 索引补齐全部新文档（ARCHITECTURE/SAFETY_INTERLOCK/ROBOT_PROTOCOL/MQTT_DECISION/HARDWARE_REQUIREMENTS/WIFI_DEPLOYMENT/PRE_HARDWARE_TEST_REPORT）。
- 40 任务全部闭环。遗留真硬件事项仅 BLOCKED.md 一条（RKNN 成品上板验证）+ 硬件联调注意项。
