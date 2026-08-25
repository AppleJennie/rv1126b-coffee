#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N 次推理计时：输出 mean / P95 / FPS。

运行平台要求（同 tools/test_rknn.py）：
  - --backend simulator：rknn-toolkit2（x86_64 PC 完整支持；aarch64 实测可装，
    需 opencv-python==4.10.0.84 规避 SVE SIGILL，见 reports/scrfd_rknn_validation.md）。
    ⚠ 模拟器不能推理 load_rknn 成品（需 target 连板），且模拟器计时是 CPU 数值，
    不代表板端 NPU 性能，只能用于相对对比。
  - --backend lite：rknn-toolkit-lite2，仅 RV1126B 板端，计时才是真 NPU 耗时。
  - --help 任何平台可跑（重依赖延迟导入）。

x86_64 PC 上的完整命令示例：
    pip install rknn-toolkit2==2.3.2 "numpy<2"
    python3 tools/benchmark_rknn.py --model models/scrfd_rv1126b.rknn \
        --input-size 640 --runs 100 --warmup 10 --backend simulator

板端（RV1126B + rknn-toolkit-lite2）：
    python3 tools/benchmark_rknn.py --model models/scrfd.rknn \
        --input-size 640 --runs 200 --warmup 20 --backend lite

所需 pip 包及版本：
    PC：rknn-toolkit2==2.3.2, numpy<2
    板端：rknn-toolkit-lite2 2.3.x, numpy

输出 JSON：{runs, warmup, mean_ms, p50_ms, p95_ms, min_ms, max_ms, fps}
"""

import argparse
import json
import platform
import sys


def parse_args():
    p = argparse.ArgumentParser(
        description='N 次推理计时，输出 mean/P95/FPS（JSON）。',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--model', required=True, help='.rknn 模型路径')
    p.add_argument('--backend', choices=['auto', 'simulator', 'lite'], default='auto',
                   help='simulator=PC 模拟器（计时仅供参考）；lite=板端真 NPU')
    p.add_argument('--input-size', default='640x640',
                   help='输入尺寸 WxH（NHWC 3 通道 uint8 随机输入）')
    p.add_argument('--runs', type=int, default=100, help='计时推理次数')
    p.add_argument('--warmup', type=int, default=10, help='预热次数（不计入统计）')
    p.add_argument('--target', default=None,
                   help='simulator 连板推理目标（如 rv1126b），不填=本机模拟')
    return p.parse_args()


def main():
    args = parse_args()
    print('[benchmark_rknn] 平台: %s, python %s'
          % (platform.machine(), platform.python_version()))
    try:
        w, h = (int(x) for x in args.input_size.lower().split('x'))
    except ValueError:
        print('[benchmark_rknn] ✗ --input-size 格式应为 WxH，如 640x640')
        return 2

    try:
        import time
        import numpy as np
        from test_rknn import load_backend  # 复用后端加载（同目录）
    except ImportError as e:
        print('[benchmark_rknn] ✗ 依赖缺失（numpy 或同目录 test_rknn.py）: %s' % e)
        return 2

    try:
        net, used = load_backend(args.model, args.backend, args.target)
    except ImportError as e:
        print('[benchmark_rknn] ✗ 后端库不可用: %s' % e)
        print('  PC: pip install rknn-toolkit2==2.3.2；板端: rknn-toolkit-lite2')
        print('  aarch64 实测结论见 reports/scrfd_rknn_validation.md')
        return 2
    except RuntimeError as e:
        print('[benchmark_rknn] ✗ %s' % e)
        return 1
    print('[benchmark_rknn] 后端: %s（%s 计时%s代表板端 NPU 性能）'
          % (used, used, '不' if used == 'simulator' else ''))

    blob = np.random.randint(0, 255, (1, h, w, 3), dtype=np.uint8)
    for _ in range(max(0, args.warmup)):
        net.inference(inputs=[blob])
    times = []
    for _ in range(max(1, args.runs)):
        t0 = time.perf_counter()
        net.inference(inputs=[blob])
        times.append((time.perf_counter() - t0) * 1000.0)
    net.release()

    t = np.sort(np.array(times))
    mean_ms = float(t.mean())

    def pct(p):  # 最近秩百分位，兼容 numpy 1.17（无 method 参数）
        return float(t[min(len(t) - 1, int(np.ceil(p / 100.0 * len(t))) - 1)])

    result = {'model': args.model, 'backend': used,
              'runs': len(times), 'warmup': args.warmup,
              'mean_ms': round(mean_ms, 3),
              'p50_ms': round(pct(50), 3),
              'p95_ms': round(pct(95), 3),
              'min_ms': round(float(t[0]), 3),
              'max_ms': round(float(t[-1]), 3),
              'fps': round(1000.0 / mean_ms, 2) if mean_ms > 0 else 0.0}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
