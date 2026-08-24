#!/usr/bin/env python3
"""通过串口把文件推到开发板（base64 分行写入，无需网络）。
用法: serial_push.py <本地文件> <板端路径>
"""
import base64
import hashlib
import os
import sys
import time

import serial

PORT = os.environ.get("BOARD_SERIAL", "/dev/ttyACM0")
BAUD = int(os.environ.get("BOARD_BAUD", "1500000"))
LINE = 400  # 每行 base64 字符数，远低于 tty 行缓冲上限


def send(ser, s, wait=0.0):
    ser.write(s.encode() + b"\n")
    if wait:
        time.sleep(wait)


def drain(ser, t=1.5):
    deadline = time.time() + t
    buf = b""
    while time.time() < deadline:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
        elif buf:
            break
    return buf.decode("utf-8", errors="replace")


def main():
    local, remote = sys.argv[1], sys.argv[2]
    data = open(local, "rb").read()
    md5 = hashlib.md5(data).hexdigest()
    b64 = base64.b64encode(data).decode()
    print(f"文件 {local}: {len(data)} 字节, md5={md5}, base64 {len(b64)} 字符")

    with serial.Serial(PORT, BAUD, timeout=0.2) as ser:
        ser.reset_input_buffer()
        send(ser, "stty -echo; rm -f /tmp/push.b64; echo READY")
        drain(ser, 2)

        t0 = time.time()
        lines = [b64[i:i + LINE] for i in range(0, len(b64), LINE)]
        for n, ln in enumerate(lines):
            send(ser, f"echo {ln} >> /tmp/push.b64")
            time.sleep(0.012)
            if n % 50 == 49:  # 定期清掉板端回显/响应，防止主机缓冲区堆积
                ser.reset_input_buffer()
                print(f"\r已发 {n + 1}/{len(lines)} 行", end="", flush=True)
        print(f"\r发送完成: {len(lines)} 行, 用时 {time.time() - t0:.1f}s")

        send(ser, f"base64 -d /tmp/push.b64 > {remote} && chmod +x {remote} && md5sum {remote}")
        out = drain(ser, 4)
        print(out.strip())
        if md5 in out:
            print("MD5 校验一致 ✔")
            return 0
        print("MD5 不一致或未返回，传输失败 ✘")
        return 1


sys.exit(main())
