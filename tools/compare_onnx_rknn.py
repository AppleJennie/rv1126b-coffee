#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SCRFD 同图对比：PC onnxruntime 原模型 vs RKNN（模拟器/板端），比 bbox 与 confidence。

运行平台要求：
  - onnxruntime 一侧：任何平台（venv 已装 onnxruntime==1.19.2）。
  - rknn 一侧：--backend simulator 需 rknn-toolkit2（x86_64 完整支持；
    本仓库 aarch64 VM 实测可用，见 reports/scrfd_rknn_validation.md）；
    --backend lite 仅 RV1126B 板端。
  - --help 任何平台可跑（重依赖延迟导入）。

完整命令示例（本项目 venv）：
    .venv-rknn/bin/python3 tools/compare_onnx_rknn.py \
        --onnx models_src/det_2.5g.onnx \
        --rknn rknn/scrfd_2.5g_rv1126b_fp16.rknn \
        --image rknn/dataset/t1.jpg \
        --conf 0.5 --iou-match 0.5 --iou-tol 0.85 --score-tol 0.05

判定标准（默认，可用参数调整）：
  - 两侧检出人脸数一致；
  - 配对（IoU≥--iou-match）后每对 bbox IoU ≥ --iou-tol（0.85）；
  - 每对置信度差 |Δscore| ≤ --score-tol（0.05）。
  FP16 转换理论上只有低比特舍入误差；INT8 量化模型应放宽到 IoU≥0.7、Δscore≤0.1。

所需 pip 包及版本：onnxruntime>=1.16, rknn-toolkit2==2.3.2（simulator）, numpy<2, opencv-python>=4.5
"""

import argparse
import json
import sys


def parse_args():
    p = argparse.ArgumentParser(
        description='SCRFD 同图对比：onnxruntime vs RKNN，输出配对结果 JSON 与 PASS/FAIL。',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--onnx', required=True, help='SCRFD onnx 原模型路径')
    p.add_argument('--rknn', required=True, help='转换后的 .rknn 路径')
    p.add_argument('--image', required=True, help='对比用图片')
    p.add_argument('--backend', choices=['simulator', 'lite'], default='simulator',
                   help='rknn 推理后端')
    p.add_argument('--input-size', type=int, default=640, help='模型输入边长')
    p.add_argument('--conf', type=float, default=0.5, help='置信度阈值')
    p.add_argument('--nms', type=float, default=0.5, help='NMS 阈值')
    p.add_argument('--iou-match', type=float, default=0.5, help='配对最小 IoU')
    p.add_argument('--iou-tol', type=float, default=0.85, help='配对 bbox 最小 IoU（判定线）')
    p.add_argument('--score-tol', type=float, default=0.05, help='置信度差容差（判定线）')
    p.add_argument('--from-source', action='store_true',
                   help='rknn 侧改为 load_onnx+build(fp)+模拟器推理（不读 .rknn 文件）。'
                        ' rknn-toolkit2 模拟器不支持直接推理 load_rknn 的成品模型'
                        '（实测报错：not support inference on the simulator, please set '
                        "'target' first），PC 上验证转换数值用此模式；"
                        '成品 .rknn 的最终验证须在板端 --backend lite 跑 tools/test_rknn.py')
    p.add_argument('--target-platform', default='rv1126b', help='--from-source 的 build 目标')
    p.add_argument('--mean', default='127.5,127.5,127.5', help='--from-source 内嵌均值')
    p.add_argument('--std', default='128,128,128', help='--from-source 内嵌方差')
    return p.parse_args()


def _iou(a, b):
    """两个 xyxy 框的 IoU。"""
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    return inter / (area_a + area_b - inter + 1e-9)


def _run_onnx(onnx_path, blob_uint8, np):
    """onnxruntime 跑原模型。blob 为 NHWC uint8 letterbox 后的图。"""
    import onnxruntime as ort
    # rknn 侧把 (x-127.5)/128 归一化内嵌进模型；onnx 侧手动做同样归一化
    x = (blob_uint8.astype(np.float32) - 127.5) / 128.0
    x = np.ascontiguousarray(x.transpose(0, 3, 1, 2))   # NHWC → NCHW
    sess = ort.InferenceSession(onnx_path, providers=['CPUExecutionProvider'])
    in_name = sess.get_inputs()[0].name
    return sess.run(None, {in_name: x})


def _run_rknn(rknn_path, blob_uint8, backend):
    from test_rknn import load_backend   # 同目录复用
    net, used = load_backend(rknn_path, backend, None)
    outs = net.inference(inputs=[blob_uint8])
    net.release()
    return outs, used


def _run_rknn_from_source(onnx_path, blob_uint8, target_platform, mean, std):
    """load_onnx → build(fp16) → 模拟器 init_runtime → inference。

    用于 PC 侧验证"转换后的图"数值（模拟器不接受 load_rknn 成品）。
    返回 (outs, 'simulator-from-source')。
    """
    from rknn.api import RKNN  # noqa: N813
    net = RKNN(verbose=False)
    if net.config(mean_values=[mean], std_values=[std],
                  target_platform=target_platform) != 0:
        raise RuntimeError('config 失败')
    # 固定输入 shape 与转换时一致（det_2.5g.onnx 输入为动态 H/W）
    # blob_uint8 为 NHWC：(1, H, W, C)
    h, w = blob_uint8.shape[1], blob_uint8.shape[2]
    if net.load_onnx(model=onnx_path, inputs=['input.1'],
                     input_size_list=[[1, 3, h, w]]) != 0:
        raise RuntimeError('load_onnx 失败: %s' % onnx_path)
    if net.build(do_quantization=False) != 0:
        raise RuntimeError('build 失败')
    if net.init_runtime() != 0:
        raise RuntimeError('init_runtime(模拟器) 失败')
    outs = net.inference(inputs=[blob_uint8])
    net.release()
    return outs, 'simulator-from-source'


def main():
    args = parse_args()
    try:
        import numpy as np
        from test_rknn import scrfd_decode, _preprocess
    except ImportError as e:
        print('[compare] ✗ 依赖缺失（numpy / 同目录 test_rknn.py）: %s' % e)
        return 2

    blob, orig_shape, pad, new_shape = _preprocess(args.image, args.input_size)
    print('[compare] 图片 %s 原图 %s → letterbox %dx%d'
          % (args.image, orig_shape, args.input_size, args.input_size))

    outs_onnx = _run_onnx(args.onnx, blob, np)
    print('[compare] onnxruntime 输出: %d 个 tensor' % len(outs_onnx))
    try:
        if args.from_source:
            mean = [float(x) for x in args.mean.split(',')]
            std = [float(x) for x in args.std.split(',')]
            outs_rknn, used = _run_rknn_from_source(args.onnx, blob,
                                                    args.target_platform, mean, std)
        else:
            outs_rknn, used = _run_rknn(args.rknn, blob, args.backend)
    except (ImportError, RuntimeError) as e:
        print('[compare] ✗ rknn 侧失败: %s' % e)
        return 2
    print('[compare] rknn(%s) 输出: %d 个 tensor' % (used, len(outs_rknn)))

    kw = dict(np=np, input_size=args.input_size, conf_th=args.conf,
              nms_th=args.nms, orig_shape=orig_shape, pad=pad, new_shape=new_shape)
    det_onnx = scrfd_decode(outs_onnx, **kw)
    det_rknn = scrfd_decode(outs_rknn, **kw)
    print('[compare] 检出数: onnx=%d rknn=%d' % (len(det_onnx), len(det_rknn)))

    # 贪心配对：按 onnx 检出顺序，找 IoU 最大的未配对 rknn 检出
    pairs, used_idx = [], set()
    for d in det_onnx:
        best, best_iou = -1, 0.0
        for j, r in enumerate(det_rknn):
            if j in used_idx:
                continue
            v = _iou(d['bbox'], r['bbox'])
            if v > best_iou:
                best, best_iou = j, v
        if best >= 0 and best_iou >= args.iou_match:
            used_idx.add(best)
            pairs.append((d, det_rknn[best], best_iou))

    report = {'onnx': args.onnx, 'rknn': args.rknn, 'image': args.image,
              'num_onnx': len(det_onnx), 'num_rknn': len(det_rknn),
              'num_paired': len(pairs), 'pairs': []}
    ok = (len(det_onnx) == len(det_rknn) == len(pairs))
    for d, r, iou in pairs:
        ds = abs(d['score'] - r['score'])
        pair_ok = (iou >= args.iou_tol) and (ds <= args.score_tol)
        ok = ok and pair_ok
        report['pairs'].append({
            'iou': round(iou, 4),
            'score_onnx': d['score'], 'score_rknn': r['score'],
            'score_diff': round(ds, 4),
            'bbox_onnx': d['bbox'], 'bbox_rknn': r['bbox'],
            'pass': bool(pair_ok)})
    report['verdict'] = 'PASS' if ok else 'FAIL'
    report['criteria'] = {'iou_match': args.iou_match, 'iou_tol': args.iou_tol,
                          'score_tol': args.score_tol}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
