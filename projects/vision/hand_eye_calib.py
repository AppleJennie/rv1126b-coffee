#!/usr/bin/env python3
# hand_eye_calib.py —— 手眼标定（eye-to-hand，相机固定俯视）
# 用途：交互式采集 ≥4 对「像素坐标 ↔ 台面坐标」，用 cv2.findHomography
#       计算像素→台面的 3x3 单应矩阵 H，保存为 JSON 供 cup_locate.py 使用。
# 用法：python3 hand_eye_calib.py -o calib.json
#
# 注意：板端 numpy 不一定有。有 numpy 时优先走 cv2.findHomography；
#       没有 numpy 时 cv2 无法接受点集参数，自动改用内置的纯 Python
#       DLT 求解（Jacobi 特征值法），结果同为 3x3 纯 list。

import argparse
import json
import math
import sys

try:
    import numpy as np
except ImportError:
    np = None

# ---------- 单应矩阵的纯 Python 实现（供保存/加载/换算，不依赖 numpy） ----------

def homography_to_list(H):
    """把 findHomography 的返回值（可能是 numpy 数组）转成 3x3 纯 list。"""
    if hasattr(H, "tolist"):
        H = H.tolist()
    return [[float(H[r][c]) for c in range(3)] for r in range(3)]


def apply_homography(H, u, v):
    """H 作用到像素点 (u,v)，返回台面坐标 (x, y)。H 为 3x3 list。"""
    w = H[2][0] * u + H[2][1] * v + H[2][2]
    if abs(w) < 1e-12:
        raise ValueError("单应变换分母为零")
    x = (H[0][0] * u + H[0][1] * v + H[0][2]) / w
    y = (H[1][0] * u + H[1][1] * v + H[1][2]) / w
    return x, y


def compute_homography(pixel_pts, world_pts):
    """由像素点/台面点对计算 H。输入为 [(u,v),...] 与 [(x,y),...] 的 list。

    有 numpy 时用 cv2.findHomography；无 numpy 时用纯 Python DLT 求解。
    返回 3x3 纯 list。
    """
    if np is not None:
        import cv2
        src = np.array(pixel_pts, dtype=np.float64).reshape(-1, 1, 2)
        dst = np.array(world_pts, dtype=np.float64).reshape(-1, 1, 2)
        H, _mask = cv2.findHomography(src, dst)
        if H is None:
            raise ValueError("findHomography 失败，请检查标定点是否共线或有误")
        return homography_to_list(H)
    return _dlt_homography(pixel_pts, world_pts)


def _jacobi_min_eigvec(A):
    """对称矩阵 A (n×n list) 的 Jacobi 特征值法，返回最小特征值对应的特征向量。"""
    n = len(A)
    a = [row[:] for row in A]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(300):  # 扫描轮数，9x9 足够收敛
        # 找最大非对角元
        p, q, mx = 0, 1, 0.0
        for i in range(n):
            for j in range(i + 1, n):
                if abs(a[i][j]) > mx:
                    mx, p, q = abs(a[i][j]), i, j
        if mx < 1e-18:
            break
        app, aqq, apq = a[p][p], a[q][q], a[p][q]
        theta = 0.5 * math.atan2(2.0 * apq, aqq - app)
        c, s = math.cos(theta), math.sin(theta)
        for k in range(n):
            akp, akq = a[k][p], a[k][q]
            a[k][p], a[k][q] = c * akp - s * akq, s * akp + c * akq
        for k in range(n):
            apk, aqk = a[p][k], a[q][k]
            a[p][k], a[q][k] = c * apk - s * aqk, s * apk + c * aqk
        for k in range(n):
            vkp, vkq = v[k][p], v[k][q]
            v[k][p], v[k][q] = c * vkp - s * vkq, s * vkp + c * vkq
    idx = min(range(n), key=lambda i: a[i][i])
    return [v[i][idx] for i in range(n)]


def _dlt_homography(pixel_pts, world_pts):
    """无 numpy 时的纯 Python DLT 单应求解（Hartley 归一化 + Jacobi 特征值法）。

    对源点、目标点各做相似变换归一化（中心化 + 平均距离 sqrt(2)），
    再解归一化后的 DLT，最后反归一化，保证数值稳定。
    """
    if len(pixel_pts) < 4:
        raise ValueError("至少需要 4 对点")

    def normalize(pts):
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        mean_d = sum(math.hypot(p[0] - cx, p[1] - cy) for p in pts) / len(pts)
        if mean_d < 1e-12:
            raise ValueError("标定点全部重合，无法标定")
        s = math.sqrt(2.0) / mean_d
        T = [[s, 0.0, -s * cx],
             [0.0, s, -s * cy],
             [0.0, 0.0, 1.0]]
        normed = [((p[0] - cx) * s, (p[1] - cy) * s) for p in pts]
        return normed, T

    src, T_src = normalize(pixel_pts)
    dst, T_dst = normalize(world_pts)

    # 构造 DLT 方程 A h = 0 的 A^T A（9x9），取最小特征向量
    AtA = [[0.0] * 9 for _ in range(9)]
    for (u, v), (x, y) in zip(src, dst):
        rows = [
            [-u, -v, -1.0, 0.0, 0.0, 0.0, x * u, x * v, x],
            [0.0, 0.0, 0.0, -u, -v, -1.0, y * u, y * v, y],
        ]
        for r in rows:
            for i in range(9):
                for j in range(9):
                    AtA[i][j] += r[i] * r[j]
    h = _jacobi_min_eigvec(AtA)
    if abs(h[8]) < 1e-15:
        raise ValueError("DLT 求解退化，请检查标定点是否共线或有误")
    h = [x / h[8] for x in h]
    Hn = [h[0:3], h[3:6], h[6:9]]

    # 反归一化: H = inv(T_dst) * Hn * T_src
    def mat_mul(A, B):
        return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)]
                for i in range(3)]

    s = T_dst[0][0]
    inv_T_dst = [[1.0 / s, 0.0, -T_dst[0][2] / s],
                 [0.0, 1.0 / s, -T_dst[1][2] / s],
                 [0.0, 0.0, 1.0]]
    H = mat_mul(inv_T_dst, mat_mul(Hn, T_src))
    if abs(H[2][2]) > 1e-15:
        H = [[x / H[2][2] for x in row] for row in H]
    return H


def reprojection_errors(H, pixel_pts, world_pts):
    """每对点用 H 换算后与真实台面坐标的误差（mm）。"""
    errs = []
    for (u, v), (x, y) in zip(pixel_pts, world_pts):
        px, py = apply_homography(H, u, v)
        errs.append(math.hypot(px - x, py - y))
    return errs


def save_calib(path, H, pixel_pts, world_pts, errors):
    data = {
        "type": "eye_to_hand_homography",
        "unit": "mm",
        "H": H,  # 像素 -> 台面
        "pixel_points": pixel_pts,
        "world_points": world_pts,
        "reprojection_errors_mm": errors,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_calib(path):
    """加载标定文件，返回 3x3 H（list）。供 cup_locate.py 复用。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    H = data["H"]
    return [[float(H[r][c]) for c in range(3)] for r in range(3)]


# ---------- 交互流程 ----------

def parse_xy(s):
    """解析 'x,y' 输入为两个浮点数。"""
    parts = s.replace("，", ",").split(",")
    if len(parts) != 2:
        raise ValueError
    return float(parts[0]), float(parts[1])


def main():
    ap = argparse.ArgumentParser(description="手眼标定（eye-to-hand）")
    ap.add_argument("-o", default="calib.json", help="标定结果输出 JSON 路径")
    ap.add_argument("-n", type=int, default=4, help="最少标定点数")
    args = ap.parse_args()

    print("=== 手眼标定（相机固定俯视，像素 -> 台面坐标） ===")
    print(f"请依次标定至少 {args.n} 个点，尽量分散覆盖台面四个角落。")
    print("每个点：先把机械臂夹爪尖移到台面上一个已知位置，")
    print("输入该点臂坐标 'x,y'（单位 mm，回车确认），")
    print("再输入夹爪尖在图像中的像素坐标 'u,v'")
    print("（可先运行 cup_detect.py -o debug.jpg 或用图像查看工具读出）。")
    print(f"输满 {args.n} 点后，再输入一个点会追加；直接回车结束采集。\n")

    pixel_pts, world_pts = [], []

    idx = 1
    while True:
        try:
            s = input(f"[点 {idx}] 台面坐标 x,y (mm)，直接回车结束: ").strip()
        except EOFError:
            break
        if not s:
            break
        try:
            x, y = parse_xy(s)
        except ValueError:
            print("  格式错误，请输入形如 120.5,80 的两个数字")
            continue
        try:
            s2 = input(f"[点 {idx}] 对应像素坐标 u,v: ").strip()
            u, v = parse_xy(s2)
        except (EOFError, ValueError):
            print("  格式错误，请输入形如 960,540 的两个数字")
            continue
        world_pts.append((x, y))
        pixel_pts.append((u, v))
        print(f"  已记录: 像素({u},{v}) -> 台面({x},{y})")
        idx += 1

    if len(pixel_pts) < args.n:
        print(f"ERROR: 只有 {len(pixel_pts)} 个点，至少需要 {args.n} 个")
        sys.exit(1)

    try:
        H = compute_homography(pixel_pts, world_pts)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    errors = reprojection_errors(H, pixel_pts, world_pts)
    print("\n=== 标定结果 ===")
    print("H (像素 -> 台面):")
    for row in H:
        print("  " + "  ".join(f"{v: .8e}" for v in row))
    print("\n重投影误差:")
    bad = 0
    for i, (err, wp, pp) in enumerate(zip(errors, world_pts, pixel_pts), 1):
        flag = ""
        if err > 5.0:
            flag = "  <-- 误差 >5mm，建议重新标定此点"
            bad += 1
        print(f"  点{i}: 台面{wp} 像素{pp} 误差 {err:.3f} mm{flag}")
    rms = math.sqrt(sum(e * e for e in errors) / len(errors))
    print(f"\nRMS 误差: {rms:.3f} mm")

    save_calib(args.o, H, pixel_pts, world_pts, errors)
    print(f"标定数据已保存: {args.o}")
    if bad:
        print(f"WARNING: 有 {bad} 个点误差超过 5mm，请考虑重新标定")
        sys.exit(2)


if __name__ == "__main__":
    main()
