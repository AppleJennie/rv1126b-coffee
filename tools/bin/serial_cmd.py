#!/usr/bin/env python3
"""通过串口向开发板发命令并读取输出。
用法: serial_cmd.py <命令> [等待秒数] [波特率]
环境变量: BOARD_SERIAL(默认 /dev/ttyACM0), BOARD_BAUD(默认 1500000)
"""
import os
import sys
import time

import serial

port = os.environ.get("BOARD_SERIAL", "/dev/ttyACM0")
baud = int(sys.argv[3]) if len(sys.argv) > 3 else int(os.environ.get("BOARD_BAUD", "1500000"))
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
wait = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0

with serial.Serial(port, baud, timeout=0.3) as ser:
    ser.reset_input_buffer()
    ser.write(cmd.encode() + b"\n")
    deadline = time.time() + wait
    buf = b""
    while time.time() < deadline:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
        elif buf and time.time() < deadline - 0.5:
            # 已有输出且短暂静默，提前结束
            time.sleep(0.3)
            if not ser.read(4096):
                break
    sys.stdout.write(buf.decode("utf-8", errors="replace"))
