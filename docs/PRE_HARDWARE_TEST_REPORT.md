# PRE-HARDWARE TEST REPORT —— 采购机械臂前的全系统验收（TASK 40）

> 日期：2026-08-25 ｜ 执行机：Ubuntu 20.04 aarch64 VM ｜ HEAD：`2891a9d`
> 验收方式：全部条目真实运行，`scripts/run_regression.sh` 为总入口（15 项全 PASS）。
> 本报告每一行都可复跑，复跑命令附在证据列。

## 一、门禁清单（全部 PASS，除"系统重启"为静态验证外逐项实测）

| # | 门禁项 | 结果 | 证据（复跑命令） |
|---|---|---|---|
| 1 | 网页点单 | ✅ PASS | 回归第 10 项 kiosk 端到端：下单→17 态全流程→completed→机器回 ok |
| 2 | 订单管理 | ✅ PASS | 六态机+单队列+取消+ETA；连下 2 单实测排队 queue_len=1 逐单完成；/api/order/cancel 在 22 项产品化断言中覆盖 |
| 3 | Recipe 引擎 | ✅ PASS | `python3 projects/coffee_fsm/recipe.py`：5 配方加载、menu.json 映射、未知饮品回退 demo_drink |
| 4 | AI 店员 | ✅ PASS | `python3 projects/ai_host/test_host_fsm.py` 15 用例（9 态/同人免重复招呼/制作中禁推荐/违禁词扫描） |
| 5 | Vision Mock | ✅ PASS | `python3 projects/vision/test_vision.py` 24 用例（VisionManager 去抖/疲劳窗/表情/杯检测/隐私零落盘） |
| 6 | 真实摄像头 | ✅ PASS | VM /dev/video0 实抓 720×1280 帧 + Haar 检测链路跑通（检出 0 脸=画面无人，链路正常） |
| 7 | Robot Mock | ✅ PASS | `python3 projects/servo_bus/mock_robot_serial.py`（CRC/分片解析/全链路）+ hardware SIM 冒烟含急停拦截 |
| 8 | Coffee Mock | ✅ PASS | 故障注入全套内覆盖（点动/超时/联锁），`tests/test_fault_injection.py` 25 用例 |
| 9 | WiFi Mock | ✅ PASS | `python3 projects/coffee_fsm/wifi_switch.py` 自测 + SimSmartPlug 全场景注入 |
| 10 | Audio Mock | ✅ PASS | `python3 projects/ai_host/test_audio_manager.py` 8 用例（9 语义事件映射/降级/永不抛异常） |
| 11 | 完整咖啡流程 | ✅ PASS | `python3 projects/coffee_fsm/cafe_fsm.py make --drink 1 --mode SIM` exit 0 + result=completed + reports/order_*.json |
| 12 | 20+ 故障测试 | ✅ PASS | `python3 tests/test_fault_injection.py` **25 用例全过**（7 类故障+7 联锁+正常路径+场景文件） |
| 13 | 恢复测试 | ✅ PASS | RECOVERY 重试一次→仍败进 ERROR→设备卸力→机器回 ok 可接下一单（故障用例逐项断言；kiosk 侧 cup_missing 实测拒单后恢复） |
| 14 | Demo Mode | ✅ PASS | `scripts/demo.sh` 一键拉起（点单+制作+AI 店员），开机自检 9 项缺失降级 DEMO MODE 不退出，SIGTERM 全子进程清理干净 |
| 15 | 系统重启 | ⚠ 静态 PASS | `deploy/cafe-backend.service`：Restart=on-failure+network-online 等待，`systemd-analyze verify` exit=0；**真机开机自启未实测**（VM 容器无 systemd，板端首启 checklist 在 deploy/README.md） |

回归总入口：`scripts/run_regression.sh` → **TOTAL 15 / PASS 15 / FAIL 0**
（py_compile 全量 / servo_bus C 构建 / Recipe / 旧 fsm 兼容 / cafe_fsm 正常单 /
故障注入 25 / 机械臂协议 / WiFi 开关 / hardware 冒烟 / kiosk 端到端 /
视觉 24 / AI 店员 15 / 音频 8 / 数字孪生 13 / kiosk 产品化 22）

## 二、本轮新增能力对照（40 任务清单）

- **P0（11 项）全完成**：架构审计、hardware 适配层、17 态 FSM、Recipe 引擎、
  全链路演示、25 故障用例、7 条安全联锁、订单管理、MCU 协议、三档模式、
  回归/一键演示脚本。
- **P1（11 项）全完成**：AI 店员 9 态、规则推荐引擎、RKNN 工具链与模型盘点、
  **SCRFD+2d106det+RetinaFace 三模型 aarch64 实转 RV1126B FP16 成功**
  （同图对比 IoU≥0.999）、疲劳时间窗、VisionManager、传统视觉杯检测、
  WiFi 适配审计达标、HealthManager+接单闸门、开机自检、性能监控。
- **P2 全完成**：SSE 统一事件+断线恢复、隐私模式、数字孪生、watchdog、
  结构化日志+每杯 JSON 报告、systemd、WiFi/mDNS 手册、音频事件管理、
  UI 产品化、/admin 后台、SQLite 统计、部署 Gate、硬件需求文档、MQTT 选型论证。

## 三、BLOCKED（全部等真实硬件，软件侧准备工作均已做完）

见 BLOCKED.md 逐条明细，当前仅一类：

1. **RV1126B 成品 .rknn 上板推理验证 + NPU 实测 benchmark**
   ——三个 FP16 成品已转好（models/，未入库按 gitignore），PC 侧数值对比已 PASS，
   板端命令已备好；解锁需：板子 + rknn-toolkit-lite2 2.3.x + 固件 librknnrt。

隐含的硬件待验项（不算软件阻塞，记入 docs/HARDWARE_REQUIREMENTS.md 与部署手册）：
- 真机串口舵机臂 teach 位姿（config/poses.yaml 现为占位中位值）
- HYBRID/REAL 模式下健康巡检与制作子进程两条串口连接的互斥（真机联调确认）
- NPU 的 /sys 节点名按真板补候选路径；视觉启发式阈值按现场画面重标定

## 四、结论

**PRE-HARDWARE GATE 通过**：软件闭环完整，可以按 docs/HARDWARE_REQUIREMENTS.md
启动采购评审。机械臂到货前剩余可做的纯软件工作见最终汇报第 7 点。
