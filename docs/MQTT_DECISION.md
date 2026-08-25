# 设备总线选型：为什么暂不上 MQTT（TASK 23）

结论先行：**当前阶段不引入 MQTT**。现有「HTTP + 串口 + 进程内事件」三条通道已经覆盖
所有通信需求，引入 MQTT broker 只会增加部署与调试成本，换不到实际收益。
本文记录评估过程与将来重新评估的触发条件。

## 当前系统的通信路径盘点

| 路径 | 现状协议 | 用途 |
|---|---|---|
| 浏览器 ↔ kiosk_server | HTTP REST + SSE | 点单、状态轮询、实时制作进度推送 |
| kiosk_server → cafe_fsm | 子进程 stdout `[EVENT]` JSON 行 | 每单制作流程驱动与进度回传 |
| RV1126B ↔ 机械臂 MCU | 串口自定义帧（seq/cmd/参数/CRC16/timeout/retry，见 docs/ROBOT_PROTOCOL.md） | 臂动作指令与 ACK/BUSY/DONE/ERROR |
| RV1126B → WiFi 智能插座 | HTTP（Tasmota / Sonoff DIY / 自定义 URL，hardware/wifi_plug.py） | 磨豆机/咖啡机/热水通断电 |
| 板内模块间（vision → host_fsm 等） | 进程内函数调用 / 事件队列 | PERSON_PRESENT 等视觉事件 |

## 评估：MQTT 能带来什么，代价是什么

MQTT 的卖点与本项目对照：

- **多对多解耦、发布订阅**：本系统是单主板集中控制（RV1126B 是唯一大脑），
  设备都只听它一个人的指令。发布订阅解决的是"多个生产者和多个消费者互不
  认识"的问题，这里不存在。
- **弱网/断连重传（QoS1/2）**：所有设备在同一个局域网甚至同一块板子上，
  串口协议已自带 seq/CRC/timeout/retry；HTTP 控制插座是短请求，失败即报错
  走 RECOVERY，语义比" broker 暂存稍后送达"更可控——**咖啡机的通电指令
  恰恰不应该被暂存重放**，延迟送达的"开机"是安全隐患。
- **设备状态统一总线（cafe/device/+/state）**：HealthManager（TASK 24）已经
  以 2s 周期主动轮询全部设备并汇总 /api/health，浏览器经 SSE 实时可见。
  状态汇集的需求已被覆盖，且轮询对安全设备更可控（设备不上报 ≠ 设备正常，
  主动问才拿得到"它死了"的证据）。

代价：

- 多一个 broker 进程（mosquitto 等）要部署、保活、配权限，开机自启链路
  多一环；1GB 内存的板子多一分常驻开销。
- 调试链路从"直连看日志"变成"发布者→broker→订阅者"三段，故障定位变难。
- 安全联锁（TASK 7）要求指令语义是**同步请求-响应+明确失败**（fail-closed），
  异步消息总线与这个模型相悖，反而要额外写对齐代码。

## 架构上已经留好的位置

不引 MQTT 不等于锁死。hardware/machines.py 的 `SmartSwitch` 抽象基类
（on/off/press/is_on）就是插座设备的总线接口：当前有 SimSmartSwitch（模拟）
和 WifiSmartSwitch（HTTP）。将来若要 MQTT，新增 `MqttSmartSwitch(SmartSwitch)`
即可，cafe_fsm、OrderManager、HealthManager 零改动——这正是 Adapter 层的意义。
机械臂侧同理：MCU 协议实现在 hardware/sts_arm.py，换成任何总线都不影响上层。

## 重新评估的触发条件（任一成立再回来讨论）

1. 设备数量多到集中轮询成为瓶颈（如 >20 个插座/传感器，2s 周期轮不完）。
2. 需要多个独立上位机（如门店平板、后台服务器）同时订阅设备状态。
3. 接入的第三方设备**只**提供 MQTT 接口（某些商用智能插座/传感器如此）。
4. 需要远程运维（门店外监控），此时 MQTT over TLS 是合理选项。

届时按上面的 Adapter 扩展点接入，并重新审查安全联锁在异步总线下的语义。
