#!/usr/bin/env python3
# cup_detect.py —— 杯口圆检测
# 用途：俯视画面中用 HoughCircles 找纸杯杯口圆，输出圆心像素坐标。
# 用法：python3 cup_detect.py [-d 23] [-o debug.jpg] [--min-r 40] [--max-r 200]
#
# 隐私红线（TASK 12，见 docs/PRIVACY_DESIGN.md）：-o 调试图仅在操作者
# 显式指定时保存，属现场调试手段；禁止接入视觉事件流水线
# （VisionManager 链路不落盘任何图像）。

import argparse
import sys

import cv2


def grab_frame(device, width=1920, height=1080, warmup=5):
    """打开摄像头，连采 warmup 帧取最后一帧（前几帧曝光未稳定，丢弃）。"""
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        print(f"ERROR: 无法打开摄像头设备 {device}")
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
    cap.release()
    if frame is None:
        print(f"ERROR: 摄像头 {device} 读取帧失败")
    return frame


def detect_cup(frame, min_r, max_r, param1, param2):
    """灰度 + 高斯模糊 + HoughCircles 检测圆。

    返回 (circles, best)，circles 为所有候选 [(x,y,r)...]，
    best 为挑选出的最佳圆 (x,y,r)，无结果时为 None。
    挑选策略：半径最大优先，同半径时取最接近画面下半部中心的圆
    （杯子通常放在台面中间偏下位置）。
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (9, 9), 2)

    raw = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=1.2,
        minDist=min_r * 2,
        param1=param1, param2=param2,
        minRadius=min_r, maxRadius=max_r,
    )
    if raw is None:
        return [], None

    circles = [(float(c[0]), float(c[1]), float(c[2])) for c in raw[0]]

    h, w = gray.shape
    ref_x, ref_y = w / 2.0, h * 0.66  # 画面下半部中心参考点

    max_r_found = max(c[2] for c in circles)
    # 只保留半径接近最大值的候选，再按离参考点距离挑选
    near_max = [c for c in circles if c[2] >= max_r_found * 0.9]
    best = min(near_max, key=lambda c: (c[0] - ref_x) ** 2 + (c[1] - ref_y) ** 2)
    return circles, best


def draw_debug(frame, circles, best):
    """在图像上标注候选圆（灰色）和最佳圆（绿色圆 + 圆心十字）。"""
    out = frame.copy()
    for x, y, r in circles:
        cv2.circle(out, (int(x), int(y)), int(r), (128, 128, 128), 2)
    if best is not None:
        x, y, r = best
        cx, cy = int(x), int(y)
        cv2.circle(out, (cx, cy), int(r), (0, 255, 0), 3)
        cv2.drawMarker(out, (cx, cy), (0, 0, 255),
                       cv2.MARKER_CROSS, 40, 3)
    return out


def main():
    ap = argparse.ArgumentParser(description="杯口圆检测")
    ap.add_argument("-d", type=int, default=23, help="摄像头设备号（MIPI 摄像头为 23/24/31/32，USB 摄像头为 52）")
    ap.add_argument("-o", default=None, help="调试标注图输出路径（可选）")
    ap.add_argument("--min-r", type=int, default=40, help="最小圆半径（像素）")
    ap.add_argument("--max-r", type=int, default=200, help="最大圆半径（像素）")
    ap.add_argument("--param1", type=float, default=100.0, help="HoughCircles param1（Canny 高阈值）")
    ap.add_argument("--param2", type=float, default=50.0, help="HoughCircles param2（累加器阈值）")
    args = ap.parse_args()

    frame = grab_frame(args.d)
    if frame is None:
        sys.exit(2)

    circles, best = detect_cup(frame, args.min_r, args.max_r,
                               args.param1, args.param2)

    if args.o:
        if not cv2.imwrite(args.o, draw_debug(frame, circles, best)):
            print(f"ERROR: 无法写入 {args.o}")

    if best is None:
        print("CUP NOT_FOUND")
        sys.exit(1)
    x, y, r = best
    print(f"CUP x={x:.1f} y={y:.1f} r={r:.1f}")


if __name__ == "__main__":
    main()
