# 模型清单（MODEL INVENTORY）— TASK 13

> 盘点日期：2026-08-25 ｜ 盘点机器：Ubuntu 20.04 aarch64 VM（python 3.8.10）
> 信息可信度逐条标注；凡标「未知」处一律需 x86_64 工具链或源模型确认，未做任何臆测。

## 目录约定

| 目录 | 用途 |
| --- | --- |
| `models/` | 部署用 RKNN 成品（拷板上用），当前有 `scrfd.rknn` |
| `models_src/` | 原始模型源（onnx/pt 等），当前有 `2d106det.onnx`（复制品，真身在 `projects/ai_host/models/`） |
| `rknn/` | 转换中间产物 / 量化校准数据集（`rknn/dataset/`） |
| `projects/ai_host/models/` | 既有留档模型（RV1106 目标，见下） |

## 一句话结论

- **scrfd.rknn**：已是 RV1126B 成品（rknn-toolkit2 2.3.2、FP16 未量化），可直接上板推理。✅
- **2d106det.rknn / retinaface.rknn**（`projects/ai_host/models/` 既有成品）：均为 RV1106 目标编译，**不能**在 RV1126B 上跑。两模型的 ONNX 源均已备齐（2d106det 仓库既有，retinaface 为官方同款 mobile320，见文末 2026-08-25 更新），三模型已全部重转出 RV1126B FP16 成品（`models/`，gitignore 不入库）。⚠→✅

---

## 1. SCRFD 人脸检测 —— `reference/ai_facedet/scrfd/scrfd.rknn`（复制品 `models/scrfd.rknn`）

- SHA256：`f76c46401d0fdd4a88d9fcc24f39a703b460068bebe8400a1cf7fe4f2d963ed1`，1,645,744 B
- **来源**：厂商 SDK 示例 `reference/ai_facedet/scrfd.zip`（内含同款 main.py + scrfd.rknn），
  SCRFD（InsightFace 人脸检测，层名 `neck.pafpn_convs.*` 显示为 mmdetection 风格导出）。
  **仓库内无 ONNX 源。**
- **目标平台**：`rv1126b`（rknn 文件内嵌字符串 ×2）；编译器 **rknn-toolkit2 2.3.2**
  （`e045de294f@2025-04-07T19:48:25`）；**未量化**（quant_tab 全空，计算 dtype float16）。
  可信度：高（直接读 rknn 内嵌元数据）。
- **输入**：`input.1`，float32 `[1,3,640,640]` NCHW；内嵌预处理 mean=std=127.5
  （即 `(x-127.5)/127.5` 由模型内部完成），`rgb2bgr=False`；推理时按 NHWC 喂图
  （rknnlite 自动处理 NHWC→NCHW 与归一化）。可信度：高（内嵌 attrs）。
- **输出**：9 个 tensor，三档 stride（8/16/32）各一组，float32：
  - `score_8/16/32`：`[1,12800/3200/800,1]` 分类得分
  - `bbox_8/16/32`：`[1,N,4]` 锚点中心到四边距离（**需 ×stride**）
  - `kps_8/16/32`：`[1,N,10]` 5 关键点偏移（**需 ×stride**）
  - 数量关系：80²×2=12800、40²×2=3200、20²×2=800（每格 2 anchor）。可信度：高。
- **预处理**（`reference/ai_facedet/scrfd/main.py`）：letterbox 等比缩放至 640×640 并补黑边
  （记录 newh/neww/padh/padw）；`np.expand_dims(img,0)` 直接喂 BGR 图。
- **后处理**（同 main.py 与 `projects/ai_host/face_events.py:163-233`）：
  输出重排 `outs[::3]+outs[1::3]+outs[2::3]` → 逐 stride 生成锚点网格 →
  score≥0.5 过滤 → distance2bbox / distance2kps 解码 → 坐标按 letterbox 参数映射回原图 →
  `cv2.dnn.NMSBoxes`（conf 0.5 / nms 0.5）。

## 2. 106 关键点 —— `2d106det.onnx` + `2d106det.rknn`（`projects/ai_host/models/`）

- ONNX SHA256：`f001b856447c413801ef5c42091ed0cd516fcd21f2d6b79635b1e733a7109dbf`，5,030,888 B
- RKNN SHA256：`ca50999f4522e99bb3911c9f9bf3654cc1d11df6c3cf857fd23c427002988388`，1,378,583 B
- **来源**：参考工程 `hand_capture_right`（RV1106 驾驶员疲劳监测 DMS，在用户 Mac 共享目录，
  不在仓库）；ONNX 为 InsightFace 106 关键点模型，MXNet 转出（graph 名
  `mxnet_converted_model`，ir_version 7）。可信度：高（自写 protobuf wire 解析器直接读 onnx，
  本机无 onnx 包）。
- **ONNX 输入/输出**：输入 `data` float32 `[None,3,192,192]`（batch 动态）；
  输出 `fc1` float32 `[1,212]` = 106 点 × (x,y)，值域 [-1,1]。可信度：高。
- **RKNN 目标平台**：`rv1106`（文件内嵌字符串 ×2；无 `static_shape` 元数据段）。
  **不能在 RV1126B 上运行**，需用 ONNX 源按 rv1126b 重转。可信度：高。
- **预处理**（`face_events.py:353-382`，移植自 `dms_face_landmark_106.c`）：
  以人脸框中心取边长 `max(w,h)*1.5` 正方形松散裁剪 → resize 192×192 → **BGR→RGB** 后喂模型。
  C 版无显式减均值/除方差；uint8 0~255 直喂还是模型内部归一化——**未知，需 x86 转换时
  用 Netron/config 确认 mean/std**。
- **后处理**：`fc1` reshape(-1,2) → `(p+1)*96` 映射到 192×192 裁剪坐标 → 按裁剪框
  缩放平移回原图。无 NMS（回归模型）。

## 3. RetinaFace 人脸检测 —— `retinaface.rknn`（`projects/ai_host/models/`）

- SHA256：`e36603e131196e81d8efe28b04407df34bdd44bc1f75a9ccb67e8413a1f90bf5`，651,491 B
- **来源**：同参考工程 `hand_capture_right`；**ONNX 源不在仓库**（在用户 Mac 共享目录）。
- **RKNN 目标平台**：`rv1106`（内嵌字符串 ×2，无 static_shape 段）。**不能直接在 RV1126B 跑**。
  可信度：高。
- **输入尺寸**：320×320 正方形（`face_events.py` `det_input_size=320` 默认值，且与 prior
  数学自洽：40²×2+20²×2+10²×2=4200 priors）。确切 tensor shape/dtype：**未知，需源 ONNX
  或 x86 工具链 `rknn_query` 确认**。可信度：中。
- **输出**：3 个 tensor，按元素数识别（`face_events.py:312-322`）：
  `loc` = N×4（框回归）、`score` = N×2（背景/人脸 softmax）、`landms` = N×10（5 关键点），
  N=4200（按 320 输入推）。可信度：中（移植代码自洽，未经工具验证）。
- **预处理**：整图 **stretch** resize 到 320×320（非 letterbox），cv2 帧 BGR 直喂；
  是否减均值 **未知，需源确认**。
- **后处理**（`face_events.py:288-351`，移植自 `dms_retinaface.c`）：
  prior boxes（min_sizes (16,32)/(64,128)/(256,512)，steps 8/16/32）→ score[:,1]>0.5 →
  variance(0.1,0.2) 解码 cx/cy/w/h → clip[0,1] → NMS 0.2 → 取最高分 →
  归一化坐标按拉伸比映射回原图。

## 附：RV1126B 转换目标平台写法依据

`target_platform='rv1126b'`：① rknn-toolkit2 **v2.3.2 changelog 明示 "Support for RV1126B
platform"**（[airockchip/rknn-toolkit2 releases](https://github.com/airockchip/rknn-toolkit2/releases)）；
② 本仓库 `scrfd.rknn` 内嵌目标字符串字面量即 `rv1126b`（2.3.2 编译）；
③ [rknn_model_zoo RetinaFace 示例 convert.py](https://github.com/airockchip/rknn_model_zoo/blob/main/examples/RetinaFace/python/convert.py)
的 platform 列表含 `rv1126b`。三条证据互洽。

## TASK 14 更新（2026-08-25，aarch64 实转成功）

> 全流程细节与日志见 `reports/scrfd_rknn_validation.md`。

- **SCRFD ONNX 源已入库**：`models_src/det_2.5g.onnx`（官方 insightface v0.7 release
  buffalo_m.zip 解出，SHA256 `041f73f4…0af9`），输入 `input.1` 动态 H/W，9 输出与
  厂商 scrfd.rknn 元数据完全对应 → 厂商成品即 SCRFD-2.5GF 的 FP16 转换。
- **2d106det 出处坐实**：buffalo_m.zip 内同名文件与仓库既有 `2d106det.onnx`
  SHA256 逐位一致（`f001b856…09dbf`）。
- **RetinaFace ONNX 源补全**：`models_src/RetinaFace_mobile320.onnx`（官方
  [rknn_model_zoo RetinaFace 示例](https://github.com/airockchip/rknn_model_zoo/tree/main/examples/RetinaFace)
  下载，SHA256 `1061ac88…86fa`）；输入 `input0` [1,3,320,320]，输出 loc/score/landms
  三组，与上文推断完全吻合；官方 convert.py 证实预处理为减均值 (104,117,123)、std=1
  （此前"未知"项解决）。与既有 rv1106 版 `retinaface.rknn` 是否同源同权重未经逐位比对，
  但架构、输入尺寸、出处（biubug6/Pytorch_Retinaface）一致。
- **三模型均已在本机重转出 RV1126B FP16 成品**（`models/`，gitignore 不入库）：
  `scrfd_2.5g_rv1126b_fp16.rknn`（SHA256 `1171db6c…1d1b29`）、
  `2d106det_rv1126b_fp16.rknn`（`7e173c6e…425c0b`）、
  `retinaface_mobile320_rv1126b_fp16.rknn`（`c09a478c…58ac`）。
- **SCRFD 同图对比 PASS**：onnxruntime vs 转换图模拟器，t1.jpg 双侧各 6 脸全配对，
  IoU≥0.999、|Δscore|≤0.0003（`reports/compare_t1_full.log`）。
- 待办：成品 .rknn 板端实机验证（rknn-toolkit-lite2）、INT8 量化（需真实人脸校准图集）。
