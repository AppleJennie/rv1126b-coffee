# SCRFD → RKNN 转换验证报告（TASK 14）

> 日期：2026-08-25 ｜ 执行机：Ubuntu 20.04.5 **aarch64** VM（Apple Silicon 虚拟化），python 3.8.10
> 结论先行：**转换在本机 aarch64 上实际成功了**（与"大概率转不了"的事前预期相反，
> 关键转折见第 3 节）；同图对比 PASS。剩余未完成的只有"成品 .rknn 上板实机验证"。

## 1. 最终结果

| 事项 | 结果 |
| --- | --- |
| SCRFD ONNX 源 | ✅ 找到并下载官方源 `det_2.5g.onnx`（见 2.2） |
| rv1126b FP16 转换 | ✅ `rknn/scrfd_2.5g_rv1126b_fp16.rknn`（2,091,581 B），SHA256 `1171db6c…1d1b29` |
| PC 原模型 vs RKNN 同图对比 | ✅ PASS：t1.jpg 双侧各检出 6 脸，全配对，IoU≥0.999，\|Δscore\|≤0.0003（明细 `reports/compare_t1_full.log`） |
| 成品 .rknn 上板实机推理 | ⏸ BLOCKED：需 RV1126B 板子 + rknn-toolkit-lite2（模拟器拒跑成品，见 4.3） |
| INT8 量化版 | ⏸ 未做：量化校准需真实人脸图集；FP16 与厂商 scrfd.rknn 的精度策略一致，先用 FP16 |

## 2. 做了什么

### 2.1 确认仓库内无 SCRFD ONNX 源

`reference/ai_facedet/scrfd/` 与 `scrfd.zip` 内容一致：仅 `main.py` + `scrfd.rknn`，**无 onnx**。

### 2.2 从官方渠道找到并下载 SCRFD ONNX

- 官方 release：[deepinsight/insightface v0.7](https://github.com/deepinsight/insightface/releases/tag/v0.7)
  资产 `buffalo_m.zip`（275,951,529 B，SHA256 `d98264bd8f2dc75cbc2ddce2a14e636e02bb857b3051c234b737bf3b614edca9`）。
  本机网络直连 github.com 被墙（curl exit=7），经 `api.github.com` 换签名 URL 下载成功。
- 解出 `det_2.5g.onnx`（3,292,009 B，SHA256 `041f73f47371333d1d17a6fee6c8ab4e6aecabefe398ff32cca4e2d5eaee0af9`）
  → `models_src/det_2.5g.onnx`。选 2.5G 的依据：厂商 scrfd.rknn 为 FP16 且 1.6MB，
  SCRFD-2.5GF 0.67M 参数×2B≈1.4MB 量级吻合（10GF 版 3.86M 参数会 ~7.7MB）。
- onnx 元信息（venv 内 onnx 1.16.1 读取）：输入 `input.1` [1,3,?,?] 动态 H/W；
  9 输出 = 分数(446/466/486) + 框(449/469/489) + 关键点(452/472/492)，stride 8/16/32，
  与厂商 scrfd.rknn 内嵌元数据（input.1、9 输出、640² 网格 12800/3200/800）**完全对应**。
- 附带收获：buffalo_m.zip 内 `2d106det.onnx` 与仓库既有文件 **SHA256 逐位一致**
  （`f001b856…09dbf`），坐实了 `projects/ai_host/models/2d106det.onnx` 的官方出处。

### 2.3 aarch64 安装 rknn-toolkit2（预期失败，实际成功）

- `python3 -m venv --without-pip .venv-rknn`（系统缺 python3.8-venv 的 ensurepip，
  用 `get-pip.py(pip/3.8)` 引导 pip 25.0.1），全程项目目录内，未污染系统。
- `.venv-rknn/bin/pip install rknn-toolkit2==2.3.2` → **成功**（完整日志
  `reports/pip_install_rknn_toolkit2.log`）。PyPI 2.3.2 提供 cp38+aarch64 wheel，
  依赖自动带入 onnx 1.16.1 / onnxruntime 1.19.2 / torch 2.2.0 等 aarch64 版本。
- **坑**：装完 `import rknn` 即 SIGILL（Illegal instruction）。gdb 定位到肇事指令是
  **cv2 5.0.0.93 wheel 初始化里的 SVE 指令 `cntb x0`**（本 VM 虽 cpuinfo 列了 sve2，
  实际执行 SVE 即 SIGILL，Apple 虚拟化的已知怪癖）。
  **修复**：`.venv-rknn/bin/pip install opencv-python==4.10.0.84`（无 SVE 的构建）后
  `RKNN()` 构造正常。x86_64 PC 无此坑。

### 2.4 实际转换（本机 aarch64，非模拟成功声明）

三条全部实测跑通（日志 `reports/convert_scrfd_fp16.log`）：

```bash
# SCRFD（人脸检测，归一化 (x-127.5)/128 内嵌进模型，与 insightface 约定一致）
.venv-rknn/bin/python3 tools/convert_rknn.py --model models_src/det_2.5g.onnx \
    --output rknn/scrfd_2.5g_rv1126b_fp16.rknn --target-platform rv1126b \
    --no-quantization --input-shape input.1:1,3,640,640 --mean 127.5,127.5,127.5 --std 128,128,128

# 2d106det（106 关键点，动态 batch 需 --input-shape 固定；沿用参考工程 C 版直喂 uint8，不内嵌归一化）
.venv-rknn/bin/python3 tools/convert_rknn.py --model models_src/2d106det.onnx \
    --output rknn/2d106det_rv1126b_fp16.rknn --target-platform rv1126b \
    --no-quantization --input-shape data:1,3,192,192

# RetinaFace（人脸检测 320×320，均值 (104,117,123) 内嵌——出自官方 rknn_model_zoo convert.py）
.venv-rknn/bin/python3 tools/convert_rknn.py --model models_src/RetinaFace_mobile320.onnx \
    --output rknn/retinaface_mobile320_rv1126b_fp16.rknn --target-platform rv1126b \
    --no-quantization --mean 104,117,123 --std 1,1,1
```

产物完整性核验：成品头 16 字节与厂商 scrfd.rknn 同款（`RKNN` magic + 版本 6），
内嵌 `rv1126b`、`2.3.2(compiler …)`、`static_shape` 元数据段。

过程中的真实踩坑（均已修复进 tools/convert_rknn.py）：
1. `export` → 2.x 实际方法名是 `export_rknn`（脚本已做双名兼容）；
2. 动态维度 onnx（det_2.5g 的 `?` H/W、2d106det 的 `None` batch）必须用
   `--input-shape 名:N,C,H,W` 固定，否则 load_onnx 报 "input shape ... is not support"。

## 3. 同图对比验证（本机已执行）

方案：同一 letterbox 预处理；onnx 侧手动 `(x-127.5)/128` + NCHW 喂 onnxruntime；
rknn 侧原图直喂（归一化内嵌）。两侧共用同一套 SCRFD 解码（锚点解码 + NMS）。
按 IoU≥0.5 贪心配对，判定线 IoU≥0.85、|Δscore|≤0.05（FP16 容差）。

```bash
.venv-rknn/bin/python3 tools/compare_onnx_rknn.py --onnx models_src/det_2.5g.onnx \
    --rknn rknn/scrfd_2.5g_rv1126b_fp16.rknn --image rknn/dataset/t1.jpg --from-source
```

测试图 `rknn/dataset/t1.jpg`（886×1280，官方 insightface-resources 测试图，
SHA256 `47f682e9…d69489`）。

**结果：PASS** —— 双侧各检出 6 张脸，6/6 配对，bbox IoU 0.999~1.0（四对完全重合），
置信度差最大 0.0003。明细 JSON：`reports/compare_t1_full.log`。
INT8 量化版将来复测时建议放宽到 IoU≥0.7、|Δscore|≤0.1。

另外两个模型也做了同法原始输出数值对比（同一张随机输入图，onnxruntime vs 转换图模拟器）：

| 模型 | 输出 | 形状(两侧一致) | max abs diff |
| --- | --- | --- | --- |
| 2d106det | fc1（106×2，值域[-1,1]） | (212,) | 0.0033（≈0.3px@192，可忽略） |
| RetinaFace | loc / score / landms | (16800,)/(8400,)/(42000,) | 0.021 / 0.0003 / 0.035 |

RetinaFace 输出元素数 16800/8400/42000 反推 prior 数 4200，与
`face_events.py` 按 320 输入生成的 prior 网格数学自洽（40²×2+20²×2+10²×2=4200），
交叉验证了 MODEL_INVENTORY 的推断。

### 4. 仍存在的限制（诚实声明）

1. **`--from-source` 验证的是"转换图"的数值，不是成品 .rknn 文件本身**：
   rknn-toolkit2 模拟器拒绝对 `load_rknn` 的成品做本机推理
   （原话："not support inference on the simulator, please set 'target' first"）。
   成品 .rknn 的最终确认必须在板端跑：
   `python3 tools/test_rknn.py --model models/scrfd_2.5g_rv1126b_fp16.rknn --image /tmp/face.jpg --backend lite`
2. 模拟器计时无意义（CPU 数值），benchmark 须板端：
   `python3 tools/benchmark_rknn.py --model models/scrfd_2.5g_rv1126b_fp16.rknn --backend lite --runs 200`
3. INT8 量化未做：校准集应用真实摄像头人脸图（数十~数百张），合成/单图校准会掉点。
   命令备好：`tools/convert_rknn.py --model models_src/det_2.5g.onnx --output …_i8.rknn
   --dataset rknn/dataset/xxx.txt --input-shape input.1:1,3,640,640 --mean … --std …`
4. 本 VM 属特例环境（aarch64+cv2 SVE 坑）。**x86_64 PC 标准流程**（用户 Mac Docker amd64
   或 PC）只需：`pip install rknn-toolkit2==2.3.2 "numpy<2"` 后跑上面 2.4 的同款命令，
   不需要 opencv 降级这步。

## 5. 相关文件

- 工具：`tools/convert_rknn.py`、`tools/test_rknn.py`、`tools/benchmark_rknn.py`、`tools/compare_onnx_rknn.py`
- 日志：`reports/pip_install_rknn_toolkit2.log`、`reports/convert_scrfd_fp16.log`、`reports/compare_t1_full.log`
- 源模型：`models_src/det_2.5g.onnx`、`models_src/2d106det.onnx`、`models_src/RetinaFace_mobile320.onnx`
- 成品（部署用，`models/`，gitignore 不入库）：`scrfd_2.5g_rv1126b_fp16.rknn`、
  `2d106det_rv1126b_fp16.rknn`、`retinaface_mobile320_rv1126b_fp16.rknn`、厂商 `scrfd.rknn`
