#!/usr/bin/env python3
# relay.py —— sysfs GPIO 继电器小工具（预留）
# 用法：python3 relay.py <gpio号> on|off
# 逻辑参照 ../../reference/c_app/03_gpio/gpio_out.c：
#   未导出则写 /sys/class/gpio/export -> direction=out -> active_low=0 -> value

import os
import sys

SYSFS_GPIO = "/sys/class/gpio"


def gpio_write(path, val):
    """写 sysfs 属性文件，失败打印错误并返回 False。"""
    try:
        with open(path, "w") as f:
            f.write(val)
        return True
    except OSError as e:
        print(f"ERROR: 写 {path} 失败: {e}", file=sys.stderr)
        return False


def main():
    if len(sys.argv) != 3 or sys.argv[2] not in ("on", "off"):
        print(f"usage: {sys.argv[0]} <gpio号> on|off", file=sys.stderr)
        sys.exit(2)
    num = sys.argv[1]
    if not num.isdigit():
        print(f"ERROR: gpio号必须是数字: {num}", file=sys.stderr)
        sys.exit(2)
    value = "1" if sys.argv[2] == "on" else "0"

    gpio_path = os.path.join(SYSFS_GPIO, f"gpio{num}")

    # 目录不存在则先导出
    if not os.path.exists(gpio_path):
        if not gpio_write(os.path.join(SYSFS_GPIO, "export"), num):
            sys.exit(1)

    # 输出模式 + 极性 + 电平
    if not gpio_write(os.path.join(gpio_path, "direction"), "out"):
        sys.exit(1)
    if not gpio_write(os.path.join(gpio_path, "active_low"), "0"):
        sys.exit(1)
    if not gpio_write(os.path.join(gpio_path, "value"), value):
        sys.exit(1)

    print(f"GPIO {num} -> {sys.argv[2]} (value={value})")


if __name__ == "__main__":
    main()
