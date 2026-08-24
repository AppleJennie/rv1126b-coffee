# models/ —— landmark106 疲劳后端模型（留档）

来源：参考工程 `/mnt/hgfs/hand_capture_right/`（RV1106 驾驶员疲劳监测系统 DMS）。

| 文件 | 说明 |
| --- | --- |
| `2d106det.onnx` | InsightFace 106 关键点模型（输入 1x3x192x192），转换输入留档 |
| `2d106det.rknn` | 上述 ONNX 转出的 RKNN（**RV1106 目标**，仅留档） |
| `retinaface.rknn` | RetinaFace 人脸检测（输入 320x320，**RV1106 目标**，仅留档） |

## ⚠ 重要：RV1126B 上板前必须重新转换

这里的两个 `.rknn` 是按 `target_platform='rv1106'` 编译的，**不能直接在
RV1126B 上运行**。上板前必须用 **rknn-toolkit2 ≥ 2.3** 按 RV1126B 重新转换。

> 注意：rknn-toolkit2（转换）官方只发布 **x86_64 Linux** 版本，**开发板上装不了、
> 转不了**；板端只能装 rknn-toolkit-lite2 做推理。转换在 x86 机器、Mac Docker
> amd64 容器或 Colab 上做，转好的 `.rknn` 拷到板子即可。

```bash
# 2d106det（参考工程自带转换脚本，改 --target 即可）
python3 /mnt/hgfs/hand_capture_right/tools/convert_landmark_rknn.py \
    --onnx 2d106det.onnx --rknn 2d106det_rv1126b.rknn --target rv1126b

# retinaface 同理：用 rv1126b 目标重新导出（参考工程 retinaface 转换流程）
```

注意：

- 参考工程 INT8 量化用的是随机合成校准数据，量产前应用真实人脸裁剪图
  重新校准（`--calib-dir`），否则关键点精度会明显掉。
- `retinaface.rknn` 的 ONNX 源不在本目录，需从参考工程的检测模型转换
  流程重新导出；模型输入尺寸若非 320，请同步修改 `face_events.py` 中
  `Landmark106Backend(det_input_size=...)`。
- 转换脚本参考：`/mnt/hgfs/hand_capture_right/tools/convert_landmark_rknn.py`。
