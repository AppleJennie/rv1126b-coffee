# 隐私设计（TASK 12）

视觉系统处理的是人脸画面，隐私红线高于功能便利。本文说明数据流、
留存原则、配置项与事件日志字段，并附一页给演示现场评委看的口头说明。

代码位置：`projects/vision/`（事件流水线入口 `vision_manager.py`）、
配置 `config/privacy.yaml`。

## 数据流图

```
摄像头/合成帧源
      │  frame（numpy 数组，只在内存）
      ▼
┌─────────────────────────────────────────────┐
│ VisionManager.step()                        │
│  ├─ 人脸有无检测（mock / Haar，纯内存推理）  │
│  ├─ 疲劳状态机（闭眼占比/哈欠计数，标量）    │
│  ├─ 表情分类（neutral/happy，标签）          │
│  └─ 杯检测（出餐位 ROI 差分，bool）          │
└─────────────────────────────────────────────┘
      │  事件 dict：{type, ts, detail}（全是标量/字符串）
      ▼
事件回调 / 队列 ──► privacy_log() 投影 ──► 日志（白名单字段）
      │
      ▼
   帧即取即弃：step() 返回后帧对象不再被引用，由 GC 回收。
   整条链路没有 cv2.imwrite / VideoWriter / 任何文件写入。
```

要点：**帧是流式的，进来、推理、出事件、丢弃**。系统持久化的只有
事件日志，且日志先经 `privacy_log()` 做白名单投影。

## 默认不留存原则

1. 事件流水线在**代码路径上不含任何图像落盘逻辑**——不是"默认关闭
   的开关"，而是"根本没有这条路"。`config/privacy.yaml` 里
   `save_face_images` / `save_raw_video` 即使误配成 `true`，
   VisionManager 初始化时只打印一条告警，流水线依然不写图。
2. 现场调试存图只有两条人工通道，与事件流水线物理隔离：
   - `snapshot.py`：摄像头 bring-up 工具，操作者显式运行才存一张 JPEG；
   - `cup_detect.py -o debug.jpg`：调杯口检测参数时显式指定才存标注图。
   两者禁止被业务代码调用。
3. 测试与演示一律用合成帧（`MockFrameSource`，纯 numpy 画的假场景），
   不涉及任何真实人脸照片。

## 配置项（config/privacy.yaml）

| 键 | 默认 | 含义 |
|---|---|---|
| `privacy.save_face_images` | `false` | 是否保存人脸照片。红线开关；流水线不实现该路径，置 `true` 仅触发告警 |
| `privacy.save_raw_video` | `false` | 是否保存原始视频流。同上 |
| `privacy.log_fields` | `[face_present, fatigue_score, expression, timestamp]` | 事件日志允许保留的字段白名单 |

加载逻辑：`load_privacy_config()`（`vision_manager.py`）。文件缺失、
损坏、结构不对都回退到默认最严配置——**配置问题永不放大留存范围**。

## 事件日志字段表

`VisionManager.privacy_log(event)` 的输出，键集合保证 ⊆ `log_fields`：

| 事件 | 日志记录内容 | 说明 |
|---|---|---|
| PERSON_PRESENT | `face_present=true, timestamp` | 只记"有人"，不记是谁 |
| PERSON_LEFT | `face_present=false, timestamp` | 同上 |
| TIRED | `fatigue_score=0~1, timestamp` | 闭眼占比标量；无图像无关键点坐标 |
| HAPPY | `expression='happy', timestamp` | 表情标签字符串 |
| CUP_PRESENT / CUP_REMOVED | `timestamp` | 与人无关，只留时间戳 |

原始事件 dict 本身（`{type, ts, detail}`）也只含标量与字符串
（detail 为闭眼占比/哈欠计数/后端名），不含图像、人脸框、landmarks
等可识别信息——由 `test_vision.py` 的隐私用例固化。

## 给评委的一页话隐私说明

> 这台咖啡机器人"看"人，但不"记"人：
>
> - **不拍照、不录像。** 摄像头画面在内存里边算边丢，代码里根本
>   没有存图的路径，不是"关了开关"而是"没修这条路"。
> - **不识别身份。** 系统只判断"有没有人""是不是可能累了""笑没笑"，
>   输出的是布尔、0~1 分数和 neutral/happy 标签，没有人脸特征值、
>   没有身份比对，换人后内部状态立即清零。
> - **日志最小化。** 事件日志只留四个白名单字段（有没有人在场、
>   疲劳分数、表情、时间戳），凭这些字段无法还原任何人的长相或身份。
> - **疲劳提示是观察不是诊断。** 文案只说"看起来有点累"，绝不涉及
>   健康结论（这条由推荐引擎的违禁词扫描测试保证）。

## 自查方法

```bash
# 事件流水线无落盘路径（仅剩两个独立调试工具，且均需显式触发）
grep -rn "imwrite\|VideoWriter" projects/vision/

# 隐私用例（配置加载 / 日志字段白名单 / 全程无图像文件生成）
python3 projects/vision/test_vision.py -k Privacy
```
