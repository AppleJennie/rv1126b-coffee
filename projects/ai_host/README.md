# ai_host —— 「AI 店员」互动引擎

用在自助咖啡机上的人脸互动与推荐模块。目标平台：正点原子 ATK-DLRV1126B
（Buildroot，Python 3.11.8 + OpenCV 4.9.0）；开发验证机：Ubuntu 20.04
（Python 3.8 + opencv-python-headless）。除标准库、cv2、pyserial 外零依赖。

## 模块关系

```
host_fsm.py        互动状态机（主程序，含 simulate / run / recommend 三个子命令）
  ├── face_events.py   人脸事件源：poll() -> {present, face_ratio, smile, fatigue, ts}
  │     ├── HaarBackend        CPU，本机可测（正脸级联 + 微笑级联），fatigue 恒为 None
  │     ├── ScrfdBackend       板端 NPU（rknnlite + scrfd.rknn，5 关键点估算微笑）
  │     └── Landmark106Backend 板端 NPU 疲劳后端（retinaface.rknn + 2d106det.rknn
  │                            → 106 关键点 → FatigueMonitor），无 rknnlite / 模型
  │                            加载失败时自动降级 haar
  ├── fatigue.py       疲劳检测：106 关键点 → EAR/MAR/head_down + 2s 个人基线
  │                    + 事件状态机（long_blink/yawn/nod），纯 Python 无 numpy
  ├── weather.py       open-meteo 天气（urllib，5s 超时，失败返回 None 由调用方降级）
  ├── recommend.py     规则推荐引擎：recommend(ctx) -> {drink, reason, tags}
  ├── menu.json        菜单数据（提取自 ui_prototype/coffee_kiosk.html 的 MENU）
  ├── voice_manifest.json  语音播报文案清单（key -> 中文文案）
  └── models/          landmark106 后端模型（留档，RV1126B 上板前需重转，见其 README）
gen_audio_mac.sh   在 Mac 上用 say + afconvert 批量生成 audio/*.wav
```

## 疲劳检测模块（fatigue.py）

算法移植自 C 版驾驶员疲劳监测系统（DMS）参考工程
`/mnt/hgfs/hand_capture_right/`（`src/dms/dms_fatigue_features.c` +
`src/dms/dms_fatigue_logic.c`），只读参考、未改动原工程。

- 输入 106 点关键点（段定义：轮廓 0~32，眼A 33~42 / 眼B 87~96 左右按 x
  动态区分，眉毛 43~51/97~105，鼻 72~86，嘴 52~71），段内按 min/max x/y
  动态选取角点与上下眼睑/唇，不写死索引。
- EAR（双眼均值）/ MAR / head_down 比例 + EMA 平滑 + 前 2 秒个人基线校准；
  闭眼超时（800ms/1500ms）、哈欠（1s，60s 窗口计数）、低头（1.5s）状态机。
- `FatigueMonitor.update(landmarks, ts)` 返回
  `{ear, mar, head_down, fatigue_score(0~1), events, level}`；
  `events` 边沿触发，含 `long_blink` / `yawn` / `nod`；
  `level` 为 `alert`（<0.3）/ `mild`（0.3~0.6）/ `tired`（≥0.6）。
- `fatigue_score` 是 Python 侧新增的连续评分（C 版只有离散状态）：
  `0.6×闭眼进度 + 0.3×哈欠进度 + 0.3×低头进度`，截断到 0~1。
- 链路：`face_events.py --backend landmark106` 产出 fatigue dict →
  `host_fsm.py` ENGAGED 状态下 ≥0.6 触发一次 `fatigue_tip` 事件并追加一条
  提神推荐 → `recommend.py` 的疲劳规则（≥0.6 强推美式/Dirty，0.3~0.6 轻提示；
  优先级低于极端天气、高于情绪规则）。

## 运行示例

```bash
# 无摄像头演示全流程（事件 JSON 逐行打印）
python3 host_fsm.py simulate

# 命令行直接要一条推荐（可模拟上下文）
python3 host_fsm.py recommend --temp 32 --smile 0.8 --hour 14
python3 host_fsm.py recommend --weather        # 拉真实天气再推荐

# 真摄像头运行（本机 --device 0；板端 MIPI 默认 23，USB 摄像头 52）
python3 host_fsm.py run --backend haar --device 0
python3 host_fsm.py run --backend landmark106 --device 23   # 板端疲劳检测

# 单独验证天气
python3 weather.py
```

## 板端部署说明

1. 整个 `ai_host/` 目录拷到板端，如 `/usr/share/ai_host/`。
2. 把 `scrfd.rknn`（参考 `reference/ai_facedet/scrfd/` 的模型）放到同目录，
   或用 `FaceEventSource(model_path=...)` 指定路径；没有模型 / 无 rknnlite 时
   自动降级 haar 后端，功能可用但微笑检测精度较低。
3. 摄像头设备号：MIPI 摄像头为 23（默认），USB 摄像头为 52，用 `--device` 切换。
4. 语音文件：在 Mac 上跑 `./gen_audio_mac.sh` 生成 `audio/*.wav`，拷到板端，
   播放用 `aplay audio/<key>.wav` 即可。
5. 开机自启建议用 Buildroot 的 init 脚本拉起来：
   `python3 /usr/share/ai_host/host_fsm.py run >> /var/log/ai_host.log 2>&1 &`
6. 疲劳检测：`models/` 下的 `.rknn` 是 RV1106 目标编译的留档文件，
   **上板前必须用 rknn-toolkit2 ≥ 2.3 按 `target_platform='rv1126b'` 重新
   转换**（见 `models/README.md`）；转换好之前用 haar/scrfd 后端，
   `fatigue` 字段为 None，疲劳相关功能自动关闭。

## 未决事项

- `models/*.rknn` 需按 RV1126B 重新转换（见 `models/README.md`），
  retinaface 输入尺寸若非 320 需同步 `Landmark106Backend(det_input_size=...)`。
- landmark106 后端的 rknnlite 输出假定已反量化为 float32；若实际拿到
  int8 原始输出，需按 zp/scale 手动反量化（板端 bring-up 时核对）。
- landmark106 后端暂未做微笑度估算（`smile` 恒为 0），微笑彩蛋在该后端
  下不触发；可用 106 点的嘴角/嘴宽补做。
- 疲劳阈值（EAR/MAR 比例、评分权重）按 DMS 参考工程默认值移植，
  上板后应按真实摄像头画面重新标定。
- `fatigue_tip` 语音文件需用 `gen_audio_mac.sh` 重新生成后拷到板端。

## 对接约定

### 与点单屏（mascot）

本模块只输出事件，不直接驱动屏幕。`host_fsm.py` 每次状态转换 / 触发向
stdout 打印一行 JSON，字段含 `mascot`，取值约定：

- `sleep`    无人待机
- `wake`     有人在场 / 引导提示
- `greet`    进场问候
- `recommend` 展示推荐
- `happy`    微笑彩蛋
- `brewing`  制作中（由点单流程触发，本模块不产生）
- `wave`     告别

带 `voice_key` 字段的事件对应 `voice_manifest.json` 里的播报文案。

### 与 coffee_fsm（机器制作流程）

`voice_manifest.json` 里的 key 即语音事件总线，两边共用：
`order_confirm / brew_grind / brew_extract / brew_milk / ready_take /
timeout_cancel / fault_water / fault_beans` 由 coffee_fsm 侧触发播放；
`greet / smile_bonus / hesitate_help / goodbye` 由本模块触发。
传感器上下文（`sensor_temp` / `sensor_humidity`）由 coffee_fsm 或串口
守护进程写入，推荐引擎中传感器温度优先于天气 API 温度。
