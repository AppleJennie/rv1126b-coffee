# RV1126B 自助咖啡师 — 文档中心

主控：正点原子 ATK-DLRV1126B（Buildroot，aarch64）。代码在 `projects/`，
工程全部文档收拢在本目录。仓库：[github.com/AppleJennie/rv1126b-coffee](https://github.com/AppleJennie/rv1126b-coffee)
｜ 当前版本 **v0.1.0**（仿真验证通过，待真机联调）。

## 总体文档（按阅读顺序编号）

| # | 文档 | 内容 | 什么时候看 |
|---|---|---|---|
| 01 | [快速上手](01-快速上手.md) | 开发环境、工具清单、编译部署、踩坑记录 | 换机器/新人上手第一件事 |
| 02 | [项目规划](02-项目规划.md) | 采购清单、机械结构、电气拓扑、分阶段路线 | 买硬件、装机械臂时 |
| 03 | [交付文档](03-交付文档.md) | 交付清单、整机工作流程、验收、待办、版本记录 | 看项目当前状态和下一步做什么 |
| 04 | [WiFi与网页通讯设计](04-WiFi与网页通讯设计.md) | 电器 WiFi 控制方案、点单屏 API/SSE 协议 | 改点单屏、服务器或电器控制时 |

## 模块文档（modules/）

对应 `projects/` 下的代码目录；文中相对路径（如 `models/`、`../vision/`）
均基于各自代码目录。

| 文档 | 代码目录 | 一句话 |
|---|---|---|
| [servo_bus](modules/servo_bus.md) | `projects/servo_bus/` | STS 舵机协议 C 库 + scan/示教/回放命令行工具 |
| [coffee_fsm](modules/coffee_fsm.md) | `projects/coffee_fsm/` | 咖啡流程状态机、姿态库、config 全字段说明 |
| [vision](modules/vision.md) | `projects/vision/` | 杯口圆检测 + 手眼标定 → 台面坐标 |
| [ai_host](modules/ai_host.md) | `projects/ai_host/` | AI 店员：人脸/疲劳/情绪、天气推荐、语音文案 |
| [ai_host-models](modules/ai_host-models.md) | `projects/ai_host/models/` | NPU 模型留档说明；上板前须按 rv1126b 重转（x86_64 工具） |

未单列的模块：`kiosk_server`（点单后台）和 `ui_prototype`（点单屏页面）
的设计集中在 [04](04-WiFi与网页通讯设计.md)；`hello_world` 见
[01](01-快速上手.md) 第五节。

## 不在本目录的资料

- `reference/`：厂商与参考工程（AI 人脸检测、摄像头、LVGL、Qt 例程），只读查阅，其中 Qt 两个大目录未入库
- `tools/bin/`：串口发命令/传文件脚本（用法见 [01](01-快速上手.md) 第六节）
- `docs/` 以外不再有 README——模块文档已全部收拢到 `docs/modules/`
