#!/usr/bin/env bash
# rv1126b_gate.sh —— RV1126B 部署就绪检查（TASK 38）
#
# 在开发主机（VM）上运行，通过串口 console（tools/bin/serial_cmd.py）逐项检查开发板：
#   Python / OpenCV / 存储 / 网络 / 摄像头 / RKNN runtime / 模型文件 / 音频 /
#   Web 端口 / systemd / 日志目录
# 最终输出 RV1126B READY，或明确列出缺什么。
#
# 用法：
#   scripts/rv1126b_gate.sh                 # 完整检查（需板子串口已连）
#   BOARD_SERIAL=/dev/ttyUSB0 scripts/rv1126b_gate.sh   # 换串口
#
# 退出码：0 = READY（板端全过）；1 = 有缺失项（明细见输出）。
# 板子没连串口时所有板端项记 MISSING 并提示，不会误报 READY。

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERIAL="${BOARD_SERIAL:-/dev/ttyACM0}"
SERIAL_CMD="$ROOT/tools/bin/serial_cmd.py"
# 板端部署路径约定（与 deploy/README.md 一致）
BOARD_ROOT="${BOARD_ROOT:-/root/rv1126b}"

PASS=0
MISS=0
MISSING=()

ok()   { echo "  [OK]      $1"; PASS=$((PASS + 1)); }
miss() { echo "  [MISSING] $1"; MISS=$((MISS + 1)); MISSING+=("$1"); }

echo "==============================================="
echo "  RV1126B 部署就绪检查（$(date '+%F %T')）"
echo "==============================================="

# ---------- 第 0 关：本地（仓库侧）前提 ----------
echo "[本地] 仓库文件"
for f in models/scrfd_2.5g_rv1126b_fp16.rknn models/2d106det_rv1126b_fp16.rknn \
         models/retinaface_mobile320_rv1126b_fp16.rknn deploy/cafe-backend.service \
         scripts/demo.sh scripts/run_regression.sh; do
    [[ -f "$ROOT/$f" ]] && ok "本地文件 $f" || miss "本地文件缺失 $f"
done

# ---------- 板端检查：串口可用性 ----------
echo "[板端] 串口连接（$SERIAL）"
BOARD_OK=0
if [[ -e "$SERIAL" ]]; then
    # 发一个 echo 探活，能收到回显即认为 console 已登录可用
    if python3 "$SERIAL_CMD" "echo GATE_ALIVE_$RANDOM" 3 2>/dev/null | grep -q "GATE_ALIVE"; then
        BOARD_OK=1
        ok "串口 console 可用（$SERIAL）"
    else
        miss "串口无应答（板子未开机/未登录 console？先手动跑 board-cmd 'ls /' 确认）"
    fi
else
    miss "串口设备 $SERIAL 不存在（板子未连接）"
fi

# board_check <名称> <远程命令> <输出中须包含的子串>
board_check() {
    local name="$1" cmd="$2" expect="$3"
    if [[ "$BOARD_OK" != "1" ]]; then
        miss "$name（跳过：串口不可用）"
        return
    fi
    local out
    out="$(python3 "$SERIAL_CMD" "$cmd" 6 2>/dev/null)"
    if echo "$out" | grep -q "$expect"; then
        ok "$name"
    else
        miss "$name（命令: $cmd；实际输出: $(echo "$out" | tail -2 | tr '\n' ' ')）"
    fi
}

echo "[板端] 系统环境"
board_check "Python 3"        "python3 --version"                                   "Python 3"
board_check "OpenCV"          "python3 -c 'import cv2; print(cv2.__version__)'"     "[0-9]"
board_check "存储剩余>500MB"  "df -m / | tail -1 | awk '{print (\$4>500)?\"STOR_OK\":\"STOR_LOW\"}'" "STOR_OK"
board_check "网络（默认路由）" "grep -q . /proc/net/route && echo NET_OK"            "NET_OK"
board_check "摄像头 /dev/video*" "ls /dev/video* 2>/dev/null | head -1"              "video"
board_check "RKNN runtime（librknnrt 或 rknnlite）" \
            "ls /usr/lib/librknnrt* /usr/lib/*/librknnrt* 2>/dev/null; python3 -c 'import rknnlite; print(\"LITE_OK\")' 2>/dev/null" \
            "rknnrt\|LITE_OK"
board_check "音频设备 /dev/snd" "ls /dev/snd 2>/dev/null | head -1"                  "snd"

echo "[板端] 项目部署"
board_check "项目目录 $BOARD_ROOT"   "ls $BOARD_ROOT/env.sh"                          "env.sh"
board_check "模型文件已上板"          "ls $BOARD_ROOT/models/*.rknn | head -1"         ".rknn"
board_check "日志目录可写"            "mkdir -p $BOARD_ROOT/logs && touch $BOARD_ROOT/logs/.w && rm $BOARD_ROOT/logs/.w && echo LOG_OK" "LOG_OK"
board_check "Web 端口 8080 状态"      "netstat -tln 2>/dev/null | grep 8080 || echo PORT_FREE" "8080\|PORT_FREE"
board_check "systemd 可用性"          "which systemctl >/dev/null 2>&1 && echo SYSTEMD_OK || echo NO_SYSTEMD" "SYSTEMD_OK\|NO_SYSTEMD"

echo "==============================================="
if [[ $MISS -eq 0 ]]; then
    echo "  RV1126B READY（$PASS 项全过）"
else
    echo "  RV1126B 未就绪：$PASS 过 / $MISS 缺"
    printf '  缺：%s\n' "${MISSING[@]}"
    echo ""
    echo "  说明：systemd 不可用（NO_SYSTEMD）的板子镜像可走"
    echo "  /etc/init.d 自启，见 deploy/README.md；其余缺失项按"
    echo "  docs/HARDWARE_REQUIREMENTS.md 与部署手册补齐。"
fi
echo "==============================================="
[[ $MISS -eq 0 ]]
