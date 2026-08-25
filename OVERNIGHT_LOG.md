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
