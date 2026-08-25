#!/usr/bin/env bash
# run_full_cafe_demo.sh —— 一键全链路演示（TASK 5）
#
# 一条命令启动完整模拟咖啡店（无需真硬件）：
#   1. kiosk_server 点单屏后台（默认 --mode SIM，制作后端为 cafe_fsm.py 子进程）
#   2. ai_host 的 host_fsm.py simulate（AI 店员互动演示，stdout 事件并入本日志，
#      可用 --no-ai 关闭；一次性脚本回放，播完即退出，不影响点单链路）
#
# 用法：
#   scripts/run_full_cafe_demo.sh                      # 全 SIM，端口 8080
#   scripts/run_full_cafe_demo.sh --port 8081          # 换端口（也可用环境变量 PORT=8081）
#   scripts/run_full_cafe_demo.sh --mode HYBRID        # 混合模式（设备真/假见
#                                                      #   projects/coffee_fsm/config.json devices 段）
#   scripts/run_full_cafe_demo.sh --scenario x.yaml    # 故障注入场景（透传给 cafe_fsm.py）
#   scripts/run_full_cafe_demo.sh --no-ai              # 不启动 AI 店员模拟
#
# 环境变量：PORT（默认 8080）；命令行 --port 优先于环境变量。
# Ctrl-C 干净退出：trap 会杀掉全部子进程（含 kiosk 正在跑的 cafe_fsm 制作子进程）。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KIOSK_PY="$ROOT/projects/kiosk_server/kiosk_server.py"
AI_HOST_PY="$ROOT/projects/ai_host/host_fsm.py"

PORT="${PORT:-8080}"
MODE="SIM"
SCENARIO=""
WITH_AI=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)     PORT="$2"; shift 2 ;;
        --mode)     MODE="$2"; shift 2 ;;
        --scenario) SCENARIO="$2"; shift 2 ;;
        --with-ai)  WITH_AI=1; shift ;;
        --no-ai)    WITH_AI=0; shift ;;
        -h|--help)  sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "未知参数: $1（-h 看用法）" >&2; exit 2 ;;
    esac
done

# 本机局域网 IP，获取失败回退 127.0.0.1
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP="${IP:-127.0.0.1}"

KIOSK_PID=""
AI_PID=""

cleanup() {
    trap - INT TERM EXIT           # 防重入
    echo ""
    echo "[demo] 正在关闭所有子进程..."
    if [[ -n "$KIOSK_PID" ]]; then
        # 先杀 kiosk 的制作子进程（cafe_fsm.py），再杀 kiosk 本身
        pkill -P "$KIOSK_PID" 2>/dev/null || true
        kill "$KIOSK_PID" 2>/dev/null || true
    fi
    if [[ -n "$AI_PID" ]]; then
        kill "$AI_PID" 2>/dev/null || true
    fi
    wait 2>/dev/null || true       # 等子进程（含日志前缀管道）收尸
    echo "[demo] 已全部退出"
}
trap cleanup INT TERM EXIT

echo "[demo] 项目根目录: $ROOT"
echo "[demo] 启动 kiosk_server（mode=$MODE, port=$PORT${SCENARIO:+, scenario=$SCENARIO}）..."

KIOSK_ARGS=(--host 0.0.0.0 --port "$PORT" --mode "$MODE")
if [[ -n "$SCENARIO" ]]; then
    KIOSK_ARGS+=(--scenario "$SCENARIO")
fi
# stdout 加 [kiosk] 前缀并入本脚本日志；$! 即 python 进程 PID（进程替换的 sed 随 EOF 退出）
python3 "$KIOSK_PY" "${KIOSK_ARGS[@]}" > >(sed 's/^/[kiosk] /') 2>&1 &
KIOSK_PID=$!

# 等服务就绪（最多 ~10s），失败说明端口被占/启动报错，直接退出
ready=0
for _ in $(seq 1 100); do
    if curl -sf -m 1 "http://127.0.0.1:${PORT}/api/menu" > /dev/null 2>&1; then
        ready=1
        break
    fi
    if ! kill -0 "$KIOSK_PID" 2>/dev/null; then
        echo "[demo] kiosk_server 启动失败（日志见上），退出" >&2
        exit 1
    fi
    sleep 0.1
done
if [[ "$ready" != "1" ]]; then
    echo "[demo] 等待 kiosk_server 就绪超时（10s），退出" >&2
    exit 1
fi

if [[ "$WITH_AI" == "1" ]]; then
    echo "[demo] 启动 AI 店员模拟（host_fsm.py simulate，一次性回放）..."
    python3 "$AI_HOST_PY" simulate --no-weather > >(sed 's/^/[ai] /') 2>&1 &
    AI_PID=$!
fi

echo ""
echo "==============================================="
echo "  咖啡店演示已就绪（mode=$MODE）"
echo "  本机访问:   http://127.0.0.1:${PORT}/"
echo "  局域网访问: http://${IP}:${PORT}/"
echo "  Ctrl-C 退出并清理全部子进程"
echo "==============================================="
echo ""

wait     # 阻塞直到子进程退出或收到信号（trap 负责清理）
