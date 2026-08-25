#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""onnx → rknn 转换封装（rknn-toolkit2 API）。

运行平台要求：
  - 官方完整支持：x86_64 Linux PC（Ubuntu 18.04+）。
  - aarch64：rknn-toolkit2 ≥ 2.3.0 起官方发布 arm64 wheel（2.3.2 有 cp38 aarch64
    构建）。本仓库已在 Ubuntu 20.04 aarch64 + python3.8 实测装通并转换成功：
    唯一坑是依赖带入的 opencv-python 5.x wheel 含 SVE 指令（Apple 虚拟化 VM 上
    import 即 SIGILL），需 `pip install opencv-python==4.10.0.84` 降级规避。
    实测记录见 reports/scrfd_rknn_validation.md 与 reports/pip_install_rknn_toolkit2.log。
  - 开发板（RV1126B）上**不能**转换，只能装 rknn-toolkit-lite2 推理。
  - 本脚本在任何平台上 --help 都能跑（重依赖全部延迟导入）。

x86_64 PC 上的完整命令示例（SCRFD，INT8 量化）：
    python3 -m venv .venv-rknn && source .venv-rknn/bin/activate
    pip install rknn-toolkit2==2.3.2 "numpy<2"
    python3 tools/convert_rknn.py \
        --model models_src/scrfd_2.5g.onnx \
        --output models/scrfd_rv1126b.rknn \
        --target-platform rv1126b \
        --dataset rknn/dataset/scrfd_calib.txt

所需 pip 包及版本（x86_64 参考）：
    rknn-toolkit2==2.3.2   （v2.3.2 起支持 RV1126B 平台）
    numpy<2                （rknn-toolkit2 2.3.x 与 numpy 2.x 有兼容问题）
    onnx                   （由 rknn-toolkit2 依赖自动带入，>=1.14 即可）

target_platform 写法依据：v2.3.2 changelog "Support for RV1126B platform" +
本仓库 scrfd.rknn 内嵌目标字符串字面量 'rv1126b'（见 models/MODEL_INVENTORY.md 附录）。
"""

import argparse
import platform
import sys

# 本仓库实测过的默认目标平台（依据见文件头注释）
DEFAULT_TARGET = 'rv1126b'


def parse_args():
    p = argparse.ArgumentParser(
        description='onnx → rknn 转换封装（rknn-toolkit2）。'
                    '默认 target_platform=%s（RV1126B）。' % DEFAULT_TARGET,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--model', required=True, help='输入 ONNX 模型路径')
    p.add_argument('--output', required=True, help='输出 .rknn 路径')
    p.add_argument('--dataset', default=None,
                   help='量化校准数据集列表 txt（每行一张图路径）；'
                        '开启量化时必填')
    p.add_argument('--target-platform', default=DEFAULT_TARGET,
                   help='目标平台字符串（rv1126b/rk3568/rk3588...）')
    p.add_argument('--no-quantization', action='store_true',
                   help='不做 INT8 量化，导出 float16 模型'
                        '（精度最高、体积大、NPU 算力利用率低）')
    p.add_argument('--mean', default=None,
                   help='逗号分隔均值，如 "127.5,127.5,127.5"；'
                        '不给则由 onnx 内嵌/默认处理')
    p.add_argument('--std', default=None,
                   help='逗号分隔方差，如 "127.5,127.5,127.5"')
    p.add_argument('--input-shape', default=None,
                   help='固定输入 shape，格式 "输入名:N,C,H,W"，如 '
                        '"input.1:1,3,640,640"。onnx 含动态维度（?/None）时必填，'
                        '否则 load_onnx 报 "input shape ... is not support"')
    return p.parse_args()


def _parse_csv_floats(s):
    return [float(x) for x in s.split(',')] if s else None


def _parse_input_shape(s):
    """解析 --input-shape "name:N,C,H,W" → (inputs, input_size_list)。"""
    if not s:
        return None, None
    name, _, dims = s.partition(':')
    if not name or not dims:
        raise ValueError('--input-shape 格式应为 "输入名:N,C,H,W"')
    return [name], [[int(x) for x in dims.split(',')]]


def _platform_banner():
    """打印平台提示；aarch64 上给出明确说明而非模糊报错。"""
    mach = platform.machine()
    print('[convert_rknn] 运行平台: %s, python %s'
          % (mach, platform.python_version()))
    if mach != 'x86_64':
        print('[convert_rknn] ⚠ 注意：模型转换官方完整支持的是 x86_64 PC。')
        print('  aarch64 仅 rknn-toolkit2>=2.3.0 起有 arm64 wheel 且限特定 python 版本；')
        print('  本机实测安装/转换结果见 reports/scrfd_rknn_validation.md。')
        print('  若下一步 import rknn 失败，请在 x86_64 PC（或 Mac Docker amd64）执行：')
        print('  python3 tools/convert_rknn.py --model <onnx> --output <rknn> \\')
        print('      --target-platform rv1126b --dataset <校准列表.txt>')


def main():
    args = parse_args()
    _platform_banner()

    # 重依赖延迟导入：保证 --help 在裸机上也能跑
    try:
        from rknn.api import RKNN  # noqa: N813
    except ImportError as e:
        print('[convert_rknn] ✗ 无法 import rknn（rknn-toolkit2 未安装或不支持本平台）: %s' % e)
        print('  x86_64 PC 安装: pip install rknn-toolkit2==2.3.2 "numpy<2"')
        print('  aarch64 实测结论见 reports/scrfd_rknn_validation.md')
        return 2

    mean = _parse_csv_floats(args.mean)
    std = _parse_csv_floats(args.std)
    do_quant = not args.no_quantization
    if do_quant and not args.dataset:
        print('[convert_rknn] ✗ 开启量化时必须给 --dataset（校准图列表 txt）；'
              '或加 --no-quantization 导出 float16')
        return 2

    rknn = RKNN(verbose=False)

    cfg = dict(target_platform=args.target_platform)
    if mean is not None:
        cfg['mean_values'] = [mean]
    if std is not None:
        cfg['std_values'] = [std]
    print('[convert_rknn] config: %s' % cfg)
    ret = rknn.config(**cfg)
    if ret != 0:
        print('[convert_rknn] ✗ config 失败 ret=%d' % ret)
        return 1

    print('[convert_rknn] 加载 ONNX: %s' % args.model)
    try:
        inputs, input_size_list = _parse_input_shape(args.input_shape)
    except ValueError as e:
        print('[convert_rknn] ✗ %s' % e)
        return 2
    if inputs:
        print('[convert_rknn] 固定输入 shape: %s -> %s' % (inputs, input_size_list))
    ret = rknn.load_onnx(model=args.model, inputs=inputs,
                         input_size_list=input_size_list)
    if ret != 0:
        print('[convert_rknn] ✗ load_onnx 失败 ret=%d（模型算子不被支持时常见；'
              '动态维度模型请加 --input-shape "输入名:N,C,H,W"）' % ret)
        return 1

    print('[convert_rknn] build: do_quantization=%s dataset=%s'
          % (do_quant, args.dataset))
    ret = rknn.build(do_quantization=do_quant, dataset=args.dataset)
    if ret != 0:
        print('[convert_rknn] ✗ build 失败 ret=%d' % ret)
        return 1

    # rknn-toolkit2 的导出方法名是 export_rknn（旧版/文档表述可能写 export，做兼容）
    export_fn = getattr(rknn, 'export_rknn', None) or getattr(rknn, 'export', None)
    if export_fn is None:
        print('[convert_rknn] ✗ RKNN 对象无 export_rknn/export 方法，'
              'rknn-toolkit2 版本不符')
        return 1
    ret = export_fn(args.output)
    if ret != 0:
        print('[convert_rknn] ✗ export 失败 ret=%d' % ret)
        return 1

    rknn.release()
    print('[convert_rknn] ✓ 导出成功: %s （target=%s, quantization=%s）'
          % (args.output, args.target_platform, do_quant))
    return 0


if __name__ == '__main__':
    sys.exit(main())
