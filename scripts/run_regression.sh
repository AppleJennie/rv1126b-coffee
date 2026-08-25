#!/usr/bin/env bash
# run_regression.sh —— 一键全系统回归（TASK 36）
#
# 跑全部可自动化验证项，任何一项失败不中断，最后统一汇总 TOTAL/PASS/FAIL。
# 用法：scripts/run_regression.sh
# 退出码：0=全部通过；非0=失败项数（便于 CI 判断）。
#
# 覆盖：Python 语法编译 / servo_bus C 构建 / Recipe 引擎 / 旧 fsm.py simulate /
#       cafe_fsm 正常单 / 故障注入全套（含安全联锁，25 用例）/ 机械臂协议 /
#       WiFi 开关 / hardware 适配层 / kiosk 端到端（下单->完成->状态恢复）。

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0
FAILED_NAMES=()

# run <名称> <命令...>：命令退出码 0 记 PASS，否则记 FAIL 并继续
run() {
    local name="$1"; shift
    echo "----- [$name] -----"
    if "$@"; then
        echo "[PASS] $name"
        PASS=$((PASS + 1))
    else
        local rc=$?
        echo "[FAIL] $name（exit=$rc）"
        FAIL=$((FAIL + 1))
        FAILED_NAMES+=("$name")
    fi
    echo ""
}

KIOSK_PID=""
cleanup() {
    if [[ -n "$KIOSK_PID" ]]; then
        pkill -P "$KIOSK_PID" 2>/dev/null
        kill "$KIOSK_PID" 2>/dev/null
    fi
}
trap cleanup EXIT

echo "==============================================="
echo "  RV1126B 咖啡机器人 全系统回归（$(date '+%F %T')）"
echo "==============================================="
echo ""

# 1. 全部自有 Python 文件语法编译（排除 reference/ 第三方参考代码）
run "py_compile 全量语法检查" bash -c \
    'find hardware projects tools tests -name "*.py" -print0 2>/dev/null | xargs -0 python3 -m py_compile'

# 2. servo_bus C 工具构建（机械臂上位机工具链）
run "servo_bus C 构建（make）" make -C projects/servo_bus

# 3. Recipe 引擎：加载 recipes.yaml + menu.json 映射 + 未知饮品回退
run "Recipe 引擎冒烟" python3 projects/coffee_fsm/recipe.py

# 4. 旧 fsm.py simulate：兼容契约，输出与退出码不得变
run "旧 fsm.py simulate（兼容契约）" bash -c \
    'python3 projects/coffee_fsm/fsm.py simulate > /tmp/reg_fsm_sim.log 2>&1'

# 5. cafe_fsm 正常单（SIM 全模拟，必须 exit 0 且 result=completed）
run "cafe_fsm 正常单（SIM）" bash -c \
    'python3 projects/coffee_fsm/cafe_fsm.py make --drink 1 --order-id 9001 --mode SIM > /tmp/reg_cafe_ok.log 2>&1 && grep -q "\"result\": \"completed\"" /tmp/reg_cafe_ok.log'

# 6. 故障注入全套（25 用例：7 类故障 + 安全联锁 + 正常路径 + 场景文件）
run "故障注入 25 用例" python3 tests/test_fault_injection.py

# 6b. 视觉层 mock 测试（18 用例：VisionManager/疲劳窗/表情/杯检测，全合成帧）
run "视觉层 mock 测试" python3 projects/vision/test_vision.py

# 6c. AI 店员交互状态机 + 规则推荐（15 用例：去重打招呼/制作中不推荐/违禁词扫描）
run "AI 店员交互+推荐测试" python3 projects/ai_host/test_host_fsm.py

# 7. 机械臂 MCU 协议：CRC/帧解析/模拟串口全链路
run "机械臂协议自测" python3 projects/servo_bus/mock_robot_serial.py

# 8. WiFi 智能插座适配层自测（mock Tasmota）
run "WiFi 开关自测" python3 projects/coffee_fsm/wifi_switch.py

# 9. hardware 适配层：SIM 工厂建连 + 急停拦截 + 复位恢复
run "hardware 适配层冒烟" python3 -c "
from hardware.factory import make_devices
d = make_devices('SIM')
arm = d['arm']
arm.connect(); arm.home()
arm.emergency_stop()
try:
    arm.move_to('CUP'); raise SystemExit('急停后动作未被拦截')
except Exception:
    pass
arm.reset(); arm.home(); arm.stop()
print('hardware SIM 冒烟 OK')
"

# 10. kiosk 端到端：起服务 -> 下单 -> 等完成 -> 状态恢复 ok
run "kiosk 端到端（--mode SIM 下单完成）" bash -c '
    PORT=18095
    LOG=/tmp/reg_kiosk.log
    python3 projects/kiosk_server/kiosk_server.py --mode SIM --port $PORT > "$LOG" 2>&1 &
    KIOSK_PID=$!
    ok=0
    for i in $(seq 1 50); do
        curl -sf -m 1 "http://127.0.0.1:$PORT/api/menu" > /dev/null 2>&1 && { ok=1; break; }
        sleep 0.2
    done
    [ "$ok" = 1 ] || { echo "kiosk 启动超时"; kill $KIOSK_PID 2>/dev/null; exit 1; }
    curl -sf -m 5 -X POST "http://127.0.0.1:$PORT/api/order" \
        -H "Content-Type: application/json" \
        -d "{\"drink_id\":1,\"opts\":{\"cup\":\"热杯\",\"temp\":\"热\",\"sugar\":\"无糖\",\"extras\":[]},\"qty\":1}" \
        | grep -q "\"ok\": true" || { echo "下单失败"; kill $KIOSK_PID 2>/dev/null; exit 1; }
    done_flag=0
    for i in $(seq 1 120); do
        grep -q "制作完成" "$LOG" && { done_flag=1; break; }
        grep -q "制作失败" "$LOG" && { echo "订单意外失败"; kill $KIOSK_PID 2>/dev/null; exit 1; }
        sleep 0.5
    done
    [ "$done_flag" = 1 ] || { echo "等待完成超时"; kill $KIOSK_PID 2>/dev/null; exit 1; }
    sleep 1
    curl -sf -m 2 "http://127.0.0.1:$PORT/api/status" | grep -q "\"machine\": \"ok\"" \
        || { echo "完成后机器状态未恢复 ok"; kill $KIOSK_PID 2>/dev/null; exit 1; }
    kill $KIOSK_PID 2>/dev/null; wait $KIOSK_PID 2>/dev/null
    echo "kiosk 端到端 OK"
'

echo "==============================================="
echo "  回归结果：TOTAL $((PASS + FAIL)) / PASS $PASS / FAIL $FAIL"
if [[ $FAIL -gt 0 ]]; then
    printf '  失败项：%s\n' "${FAILED_NAMES[*]}"
fi
echo "==============================================="
exit "$FAIL"
