#!/usr/bin/env python3
# cup_locate.py —— 一键定位（给主程序调用的成品）
# 用途：复用 cup_detect 的检测逻辑找到杯口圆心像素坐标，
#       再乘标定矩阵 H 换算成机械臂台面坐标，单行输出供脚本解析。
# 用法：python3 cup_locate.py [-d 23] [-c calib.json]

import argparse
import os
import sys

import cv2  # noqa: F401  (保证板端 cv2 可用性在启动时就暴露)

from cup_detect import grab_frame, detect_cup
from hand_eye_calib import load_calib, apply_homography


def main():
    ap = argparse.ArgumentParser(description="杯口一键定位")
    ap.add_argument("-d", type=int, default=23, help="摄像头设备号（MIPI 摄像头为 23/24/31/32，USB 摄像头为 52）")
    ap.add_argument("-c", default=None, help="标定文件 calib.json（可选）")
    ap.add_argument("--min-r", type=int, default=40, help="最小圆半径（像素）")
    ap.add_argument("--max-r", type=int, default=200, help="最大圆半径（像素）")
    ap.add_argument("--param1", type=float, default=100.0)
    ap.add_argument("--param2", type=float, default=50.0)
    args = ap.parse_args()

    H = None
    if args.c:
        if not os.path.exists(args.c):
            print(f"ERROR: 标定文件不存在: {args.c}", file=sys.stderr)
            sys.exit(2)
        try:
            H = load_calib(args.c)
        except Exception as e:
            print(f"ERROR: 标定文件解析失败: {e}", file=sys.stderr)
            sys.exit(2)

    frame = grab_frame(args.d)
    if frame is None:
        print("POSE NOT_FOUND")
        sys.exit(1)

    _circles, best = detect_cup(frame, args.min_r, args.max_r,
                                args.param1, args.param2)
    if best is None:
        print("POSE NOT_FOUND")
        sys.exit(1)

    u, v, _r = best
    if H is None:
        # 未提供标定文件，只输出像素坐标
        print(f"POSE px={u:.1f} py={v:.1f}")
        return
    x, y = apply_homography(H, u, v)
    print(f"POSE x={x:.1f} y={y:.1f} px={u:.1f} py={v:.1f}")


if __name__ == "__main__":
    main()
