# 自主咖啡师（coffee_fsm）

> 代码位置：`projects/coffee_fsm/`（文中 `../vision/`、`../servo_bus/` 等相对路径均基于该目录）

正点原子 ATK-DLRV1126B（Buildroot，Python 3.11 + OpenCV 4.9，纯命令行）上的
主控程序：6 自由度 STS 总线舵机机械臂 + MIPI 摄像头视觉定位，自动做胶囊/滴滤咖啡。

## 模块关系

```
fsm.py（主控状态机）
 ├── sts.py            Python 版 STS 舵机协议（移植自 ../servo_bus/sts_servo.c）
 ├── config.json       全部可调参数（串口/关节ID/速度/容差/视觉参数...）
 ├── poses.json        命名姿态库（teach 命令录制填充）
 ├── ../vision/        视觉模块（板端直接 import）
 │    ├── cup_detect.py      杯口圆检测（HoughCircles）
 │    └── hand_eye_calib.py  手眼标定（像素 -> 台面 mm 单应矩阵）
 └── ../servo_bus/     C 版舵机命令行工具（servo_tool/teach_record/teach_play，
                        调试用，与 sts.py 共用同一套协议定义）
relay.py              sysfs GPIO 继电器工具（预留，接水泵/电源用）
```

## 依赖安装

板端唯一可能缺的依赖是 pyserial：

```sh
pip3 install pyserial        # 无网时先在有网机器 pip download pyserial 再拷过去装
```

cv2（OpenCV 4.9）官方系统已自带；只有 `run`/`check` 的视觉部分需要 cv2，
`simulate`/`teach` 不依赖 cv2。

## 完整工作流

```sh
cd ~/rv1126b/projects/coffee_fsm

# 1. 开机自检：扫总线列出在线舵机、逐个读位置/电压/温度、试采一帧摄像头
python3 fsm.py check

# 2. 手掰示教：逐个录制姿态（全部关节卸力 -> 手摆 -> 回车 -> 自动读回写入 poses.json）
python3 fsm.py teach idle
python3 fsm.py teach cup_pick
python3 fsm.py teach cup_place
python3 fsm.py teach grinder_press
python3 fsm.py teach grounds_pick
python3 fsm.py teach grounds_pour
python3 fsm.py teach brewer_press
python3 fsm.py teach serve

# 3. 手眼标定（相机固定俯视，至少 4 对点，结果存到本目录 calib.json）
cd ../vision && python3 hand_eye_calib.py -o ../coffee_fsm/calib.json && cd ../coffee_fsm

# 4. 仿真演练：无硬件跑完整流程，检查动作顺序与日志
python3 fsm.py simulate

# 5. 实战
python3 fsm.py run
```

调试舵机时也可以用 C 工具，例如 `../servo_bus/servo_tool /dev/ttyS3`。

## config.json 字段说明

JSON 不支持注释，字段含义在此说明：

| 键 | 含义 |
|---|---|
| `serial_port` | 舵机总线串口设备（接转接板的 UART，按实际改） |
| `baud_rate` | 总线波特率，STS3215 默认 115200 |
| `joint_ids` | 关节名 -> 舵机 ID 映射：J1底座=1 J2肩=2 J3肘=3 J4腕俯仰=4 J5腕旋转=5 J6夹爪=6 |
| `default_speed` | 默认运行速度（舵机步/秒量级，0=舵机默认） |
| `default_time_ms` | 每次姿态运动的运行时间（毫秒），也是发送后等待回读的时长 |
| `gripper_open_pos` / `gripper_close_pos` | 夹爪张开/闭合位置（0~4095） |
| `position_tolerance` | 位置回读容差（步），超过则进 ERROR 状态 |
| `camera_device` | 摄像头设备号，MIPI 为 23/24/31/32，默认 23 |
| `vision_dir` | 视觉模块目录（相对本脚本目录） |
| `calib_file` | 手眼标定文件路径（相对本脚本目录），由 hand_eye_calib.py 生成 |
| `hough_min_r` / `hough_max_r` / `hough_param1` / `hough_param2` | HoughCircles 参数（传给 detect_cup） |
| `cup_ref_x_mm` / `cup_ref_y_mm` | 杯子参考台面坐标（示教 cup_pick 时杯子所在位置） |
| `correct_j1_steps_per_mm` / `correct_j2_steps_per_mm` | 视觉纠偏系数：台面每偏差 1mm 折算的 J1/J2 步数（粗调启发式，真机标定后修正） |
| `brew_wait_sec` | 冲泡等待秒数（真机） |
| `simulate_brew_sec` | 仿真模式下的冲泡等待秒数（缩短演示用） |
| `pour_steps` | 倒粉时腕旋转(J5)分步步数 |
| `pour_speed` | 倒粉慢倒速度 |
| `pour_step_time_ms` | 倒粉每步运行时间（毫秒） |

## poses.json 字段说明

每个姿态（键为姿态名）：

```json
"cup_pick": {
  "joints": {"J1": 2048, "J2": 2048, "J3": 2048, "J4": 2048, "J5": 2048, "J6": 2048},
  "gripper": "close",
  "speed": 500,
  "note": "下抓台面纸杯，夹爪闭合"
}
```

- `joints`：6 个关节的目标位置（0~4095，中位 2048），由 `teach` 命令录制填充，
  当前全部是中位占位值，**真机使用前必须逐个 teach**
- `gripper`：`open`/`close` 用 config 的 `gripper_open_pos`/`gripper_close_pos`
  驱动 J6；`hold` 表示本步不动夹爪（`joints.J6` 仅作 teach 记录，运动时不使用）
- `speed`：该姿态的运行速度，缺省用 `default_speed`
- `note`：中文备注

预置姿态：`idle`(待机)、`cup_pick`(取杯)、`cup_place`(放杯到出水口)、
`grinder_press`(按磨豆机键)、`grounds_pick`(取粉杯)、`grounds_pour`(倒粉入滤篮)、
`brewer_press`(按滴滤机键)、`serve`(递杯出品)。

## 运行状态机（run）

```
IDLE -> LOCATE_CUP（视觉找杯，算台面坐标与偏差）
     -> PICK_CUP（cup_pick 姿态 + 视觉纠偏量下抓，夹爪闭合，回读校验）
     -> PLACE_CUP（放杯到出水口，夹爪张开）
     -> PRESS_GRINDER（按磨豆机键）
     -> POUR_GROUNDS（取粉杯 -> 滤篮上方 -> J5 腕旋转分 3 步慢倒）
     -> PRESS_BREWER（按滴滤机键）
     -> WAIT_BREW（brew_wait_sec 倒计时）
     -> SERVE（递杯出品）
     -> IDLE
```

每个姿态运动都是：逐关节 `write_position` 并发发出 -> 等待 `default_time_ms`
-> 逐关节 `read_position` 回读校验。任一步写失败或回读误差超过
`position_tolerance`，进入 ERROR 状态：全部关节卸力、打印故障步骤、退出码非 0。

## 退出码

- `0` 成功 / 自检通过
- `1` 流程故障（ERROR 状态）/ 自检未通过 / teach 中途失败
- `2` 参数错误 / 串口或视觉初始化失败
