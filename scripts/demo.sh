#!/usr/bin/env bash
# demo.sh —— 一键演示（TASK 37 预热）
#
# run_full_cafe_demo.sh 的纯 SIM 快捷封装：一条命令拉起全模拟咖啡店
# （点单屏 + 制作流程 + AI 店员模拟），并打印访问地址。
#
# 用法：
#   scripts/demo.sh                 # 全 SIM，端口 8080
#   scripts/demo.sh --port 8081     # 额外参数原样透传给 run_full_cafe_demo.sh
#   scripts/demo.sh --no-ai

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/run_full_cafe_demo.sh" --mode SIM "$@"
