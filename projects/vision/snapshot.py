#!/usr/bin/env python3
# snapshot.py —— 采图测试工具
# 用途：验证摄像头能否正常取图，保存一张 JPEG 供查看。
# 用法：python3 snapshot.py [-d 23] [-W 1920 -H 1080] [-o out.jpg]

import argparse
import sys

import cv2


def grab_frame(device, width, height, warmup=5):
    """打开摄像头，连采 warmup 帧取最后一帧（前几帧曝光未稳定，丢弃）。

    成功返回 frame (numpy 数组)，失败返回 None。
    """
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        print(f"ERROR: 无法打开摄像头设备 {device}（可用 -d 指定其他 /dev/videoN）")
        cap.release()
        return None
    if width > 0:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    if height > 0:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    frame = None
    for _ in range(warmup):
        ok, frame = cap.read()
        if not ok:
            frame = None
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    if frame is None:
        print(f"ERROR: 摄像头 {device} 打开成功但读取帧失败")
        return None
    print(f"实际分辨率: {actual_w}x{actual_h}")
    return frame


def main():
    ap = argparse.ArgumentParser(description="采图测试工具")
    ap.add_argument("-d", type=int, default=23, help="摄像头设备号（对应 /dev/videoN；MIPI 摄像头为 23/24/31/32，USB 摄像头为 52）")
    ap.add_argument("-W", type=int, default=1920, help="请求宽度")
    ap.add_argument("-H", type=int, default=1080, help="请求高度")
    ap.add_argument("-o", default="out.jpg", help="输出 JPEG 路径")
    args = ap.parse_args()

    frame = grab_frame(args.d, args.W, args.H)
    if frame is None:
        sys.exit(2)

    mean = float(cv2.mean(frame)[0:3][0]) + float(cv2.mean(frame)[1]) + float(cv2.mean(frame)[2])
    mean /= 3.0
    print(f"帧均值亮度: {mean:.1f} (0=全黑, 255=全白)")
    if mean < 5.0:
        print("ERROR: 画面几乎全黑，请检查镜头盖/曝光/接线")
        sys.exit(3)
    if mean > 250.0:
        print("WARNING: 画面几乎全白，可能过曝")

    if not cv2.imwrite(args.o, frame):
        print(f"ERROR: 无法写入 {args.o}")
        sys.exit(4)
    print(f"已保存: {args.o}")


if __name__ == "__main__":
    main()
