#!/bin/bash
# RV1126B 应用开发环境变量 —— 由 ~/.bashrc 自动 source，新开终端即生效
# 本机为 aarch64 主机，gcc 原生编译产物即为开发板可执行的 aarch64 程序

export RV1126B_ROOT="$HOME/rv1126b"
export RV1126B_PROJECTS="$RV1126B_ROOT/projects"
export RV1126B_TRANSFER="$RV1126B_ROOT/transfer"

# 开发板信息（按实际情况修改 IP）
export BOARD_IP="192.168.1.100"
export BOARD_USER="root"

# 开发板串口 console（QinHeng CH343，官方波特率 1500000）
export BOARD_SERIAL="/dev/ttyACM0"
export BOARD_BAUD="1500000"

# 以后若有额外工具（如自装 adb、工具链），放进 tools/bin 即可自动生效
export PATH="$RV1126B_ROOT/tools/bin:$PATH"

# 便捷别名
alias cdrv='cd $RV1126B_ROOT'
alias board-ssh='ssh $BOARD_USER@$BOARD_IP'
alias board-cmd='python3 $RV1126B_ROOT/tools/bin/serial_cmd.py'      # 串口发命令: board-cmd 'ls /'
alias board-push='python3 $RV1126B_ROOT/tools/bin/serial_push.py'    # 串口传文件: board-push ./a /root/a
