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
