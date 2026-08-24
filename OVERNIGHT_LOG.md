# OVERNIGHT_LOG — 自主开发日志

> 按时间倒序追加。每个阶段记录：做了什么、验证命令与结果、commit hash、遗留问题。

## 2026-08-24 开工

- 目标：按 40 任务清单推进（P0→P1→P2），无真硬件全部先模拟，最终过 PRE-HARDWARE GATE
- 基线：v0.1.1（62dc709），仿真点单全链路绿，已推 GitHub
- 原则确认：不破坏 `fsm.py simulate` / `kiosk_server.py --simulate` 现有行为；新架构增量添加
