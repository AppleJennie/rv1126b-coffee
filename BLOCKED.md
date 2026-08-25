# BLOCKED — 必须真实硬件才能继续的事项

> 格式：日期 ｜ 事项 ｜ 卡在什么硬件 ｜ 已做的替代/准备 ｜ 解锁条件

（暂无）

2026-08-25 ｜ RV1126B 成品 .rknn 实机推理验证＋NPU 实测 benchmark（SCRFD/2d106det/RetinaFace 三个 FP16 成品在 models/）｜ RV1126B 开发板（板端需 rknn-toolkit-lite2 2.3.x 与固件 librknnrt 配套）｜ 转换与 PC 侧数值对比已在 aarch64 VM 完成：onnx→rknn 全通、onnxruntime vs 转换图模拟器同图对比 PASS（6 脸全配对，IoU≥0.999，|Δscore|≤0.0003，见 reports/scrfd_rknn_validation.md、reports/compare_t1_full.log）；板端验证命令已备好：`python3 tools/test_rknn.py --model models/scrfd_2.5g_rv1126b_fp16.rknn --image <图> --backend lite` 与 `tools/benchmark_rknn.py --backend lite --runs 200`；模拟器拒跑 load_rknn 成品，故成品文件只能板端验 ｜ 拿到板子后 test_rknn 输出 detections 与 compare_t1_full.log 基准比对（IoU≥0.85、|Δscore|≤0.05），benchmark 出实测 FPS；INT8 量化届时用板端真实人脸图集做校准再转
