# 安全联锁清单（TASK 7）

RV1126B 自助咖啡机器人的安全联锁（safety interlock）汇总。联锁 = 不满足条件就**禁止动作**
的硬性规则，内置不可关；违规一律抛 `DeviceError(retryable=False)` 或在接单前直接否决，
订单进 ERROR 终态（exit 1），宁可误停不可误动（fail-closed）。

当前无真硬件，全部经 Simulation Adapter 验证。每条联锁列出：规则 / 实现位置 /
对应测试（`tests/test_fault_injection.py`，`python3 tests/test_fault_injection.py` 直接运行）/
真机注意事项。

## 联锁清单

### 1. 无杯禁止出液（无杯禁止 GRIND/BREW）

- **规则**：杯未经视觉定位 + 机械臂取杯 + 放置确认，磨豆机/咖啡机绝不通电。
- **实现位置**：
  - `projects/coffee_fsm/cafe_fsm.py` `_h_check_cup`：取杯位无杯重试 3 次仍无杯 →
    RECOVERY 一次后再失败 → ERROR，流程根本不进入 GRIND/BREW。
  - `cafe_fsm.py` `_h_grind` / `_h_brew`：检查 `self._cup_picked and self._cup_placed`
    （杯状态只由臂动作**成功返回**确认，见 `_h_pick_cup` / `_h_move_to_machine`），
    未确认抛 `DeviceError(retryable=False)`。
- **对应测试**：`SafetyInterlockTest.test_interlock_no_cup_no_dispense`
  （cup_missing 时事件流 GRIND/BREW/WAIT_BREW 永不出现）；
  `FaultInjectionTest.test_fault_cup_missing`。
- **真机注意事项**：杯检测失效方向必须 fail-closed（传感器损坏报"无杯"，禁止出液）。
  视觉/光电任一报无杯即视为无杯；定期自检传感器卡死恒报"有杯"的失效模式。

### 2. 机械臂未到位禁止启动咖啡机

- **规则**：臂未取到杯、未把杯放置到冲泡位并确认之前，禁止启动咖啡机（BREW）。
- **实现位置**：
  - `cafe_fsm.py` `_h_move_to_machine`：未取杯（`_cup_picked=False`）禁止放杯。
  - `cafe_fsm.py` `_h_brew`：`_cup_placed=False`（place_cup 未成功返回）禁止
    `coffee.run()`。
  - 臂动作失败（如 robot_arm_fail）在 PICK_CUP 处直接 ERROR，流程推进不到 BREW。
- **对应测试**：`FaultInjectionTest.test_fault_robot_arm_fail`（失败点之后状态不再推进）；
  `SafetyInterlockTest.test_interlock_no_cup_no_dispense`。
- **真机注意事项**：放置确认应来自臂控器到位回读 / 夹爪力反馈，而非"指令已发出"；
  真机适配器（`hardware/sts_arm.py`）必须读回执行结果，失败如实抛错。

### 3. 冲泡期间禁止臂动作

- **规则**：GRIND / BREW / WAIT_BREW 期间，机械臂任何动作一律拒绝（热水/粉已就位，
  臂入冲泡区有烫伤与机械干涉风险）。
- **实现位置**：`cafe_fsm.py` `_arm()` 统一入口：`_in_brew_phase` 置位期间任何臂动作抛
  `DeviceError("安全联锁：冲泡阶段禁止臂动作", retryable=False)`；
  `_h_grind` / `_h_brew` / `_h_wait_brew` 用 try/finally 置位与复位，异常路径也不留位。
- **对应测试**：`SafetyInterlockTest.test_interlock_brew_phase_blocks_arm`（直接调用层）。
- **真机注意事项**：软件联锁只是第一道。真机应有独立硬件保护（冲泡区光幕 / 互锁继电器
  切断臂动力电），臂控器固件侧也应拒绝进入冲泡区的运动指令，不依赖上位机软件运行正常。

### 4. 出餐位有杯禁止出餐

- **规则**：出餐位被占用（上一单杯未取走/异物）时，机械臂不得把成品杯送去出餐位。
- **实现位置**：`cafe_fsm.py` `_h_move_to_serve`：`cup_present("serve")` 占用检查最多
  3 次（间隔 2s），仍占用 → `DeviceError(retryable=True)` → RECOVERY 一次后重试，
  再占用 → ERROR，SERVE 不执行。
- **对应测试**：`SafetyInterlockTest.test_interlock_serve_occupied_no_serve` 与
  `FaultInjectionTest.test_fault_customer_not_take_cup`（customer_not_take_cup 注入 =
  出餐位永久占用，SERVE 永不出现，exit 1——联锁优先，规格内正确行为）。
- **真机注意事项**：出餐位传感器失效恒报"有杯"会导致永远不出餐——fail-closed 方向正确，
  但需配人工清理/旁路流程与运维告警，避免整机卡死无提示。

### 5. 急停立即停所有

- **规则**：急停触发（EstopError / 外部中断）后所有设备立即停止：臂卸力、全部电器断电，
  急停锁存，不自动恢复，等待外部 reset。
- **实现位置**：
  - `cafe_fsm.py` `_enter_emergency_stop` → `_safe_shutdown(estop=True)`：
    `arm.emergency_stop()`（卸力）+ grinder/coffee/water 全部 `abort()`，单步失败不影响
    后续步骤，收尾绝不抛出；订单 result=estop，exit 1。
  - `cafe_fsm.py` `external_estop`：CLI 键盘中断按外部急停处理。
  - `hardware/sim.py` `SimRobotArm._act`：急停锁存期间任何动作抛
    `DeviceError(retryable=False)`（与真机臂控器语义一致）。
  - ERROR 路径（非急停）同样卸力：`arm.stop()` 失败降级 `emergency_stop` 保证卸力。
- **对应测试**：`DeviceLayerTest.test_estop_blocks_actions_until_reset`（急停后拒绝动作、
  状态如实上报 estop、reset 恢复）；
  `SafetyInterlockTest.test_interlock_error_path_disarm_log`（异常路径卸力日志必出现）。
- **真机注意事项**：软件急停是第二道防线。必须有硬件急停回路（急停按钮直接切断关节
  动力电与电器继电器），动作不依赖任何软件运行；reset 必须由人工现场确认后触发。

### 6. 设备 timeout 自动停止（最大运行时间保护）

- **规则**：电器运行超过最大运行时间（MAX_RUN_SEC）视为失控，自动强制断电并报错；
  运行结束断电后回读开关状态，断电无效（继电器粘连）即报不可重试故障。
- **实现位置**：
  - `hardware/machines.py` `Appliance.MAX_RUN_SEC` + `tick()`：超时强制 `abort()` 并抛
    `DeviceError(retryable=False)`；FSM `WAIT_BREW` 每秒 `coffee.tick()` 巡检。
  - `hardware/machines.py` `wait_done()`：等待结束 → `abort()` → `is_on()` 回读，
    仍通电抛 `DeviceError("断电无效（继电器粘连？）", retryable=False)`。
  - FSM 状态级超时兜底：`cafe_fsm.py` `_run_with_timeout`（daemon 线程 + join 超时
    → DeviceTimeout → RECOVERY 路由），指令无应答（DeviceTimeout）同样自动停止重试。
- **对应测试**：`DeviceLayerTest.test_stuck_on_wait_done_raises_and_reports`（直接调用层：
  粘连时 wait_done 抛错且开关状态如实上报）；`FaultInjectionTest.test_fault_grinder_stuck_on`
  / `test_fault_grinder_timeout` / `test_fault_coffee_machine_timeout` /
  `test_fault_robot_arm_hang`。
- **真机注意事项**：MAX_RUN_SEC 按设备额定工况设定并留余量；继电器粘连属硬件故障，
  更换回路前禁止复用；断电回读依赖开关状态真实可读（如 Tasmota 状态回读），
  不可只信"指令已发出"。

### 7. 设备 offline 禁止新订单

- **规则**：任一关键设备（critical）离线/不健康时，禁止开始新订单。
- **实现位置**：
  - `cafe_fsm.py` `_h_check_system`（CHECK_SYSTEM 状态）：遍历全部 critical 设备
    `health()`，任一 NG → `DeviceError(retryable=False)` → ERROR，exit 1，不进 RECOVERY
    （离线重启无意义，需人工/网络恢复）。
  - `hardware/base.py` `Device.health()`：统一健康接口 `{ok, detail, ts}`，臂/视觉/
    电器同一签名——未来 kiosk 侧 Health Manager 周期性复用同一 `health()` 在**接单前**
    裁决，离线即拒绝接新订单，与本联锁同源。
  - `hardware/machines.py` `Appliance.health` 下探到开关层：WiFi 开关离线即判电器
    不健康（wifi_disconnect 场景检出 grinder/coffee/water）。
- **对应测试**：`SafetyInterlockTest.test_interlock_offline_no_start`（PICK_CUP 永不出现）；
  `FaultInjectionTest.test_fault_wifi_disconnect` / `test_fault_robot_arm_offline`。
- **真机注意事项**：WiFi 设备离线检测有网络延迟，`health()` 应结合最后应答时间戳
  判定，避免瞬断误判；断网恢复后建议人工确认再开放接单，而非自动立即恢复。

## 测试与回归

```bash
# TASK 6 + TASK 7 全部用例（25 个，纯标准库 unittest）
timeout 600 python3 tests/test_fault_injection.py

# 旧版状态机回归（行为保持不变）
python3 projects/coffee_fsm/fsm.py simulate
```
