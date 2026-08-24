# 视觉定位工具集（ATK-DLRV1126B / Buildroot / Python 3.11 + OpenCV 4.9）

自主咖啡师机械臂项目的视觉部分：MIPI 摄像头固定俯视工作台，
检测纸杯杯口圆心像素坐标，并通过手眼标定矩阵换算成机械臂台面坐标（mm）。

纯命令行环境使用，无显示器；调试图一律保存为 JPEG 后导出查看。

## 环境

- 板端：Python 3.11.8 + OpenCV 4.9.0（cv2），通过 V4L2 取图
- 摄像头设备节点（官方例程确认）：**USB 摄像头 = 52**（本项目用 IMX335 5MP UVC 免驱模组，
  插板子 USB 口即用）；MIPI 摄像头 = 23/24/31/32
- 分辨率建议 1280×720（5MP 全分辨率无必要且拖慢帧率），用 `-d 52 -W 1280 -H 720` 这类参数
- 2026-08-23 已在虚拟机用 VMware 虚拟摄像头实拍验证 snapshot.py 可用
- 无其他依赖；numpy 不是必须的（标定矩阵用纯 Python list 处理）

## 使用顺序

### 1. snapshot.py —— 确认摄像头能取图

```sh
python3 snapshot.py -d 0 -o out.jpg
```

打印实际分辨率和帧均值亮度（判断画面全黑/过曝），保存 `out.jpg`。
把 `out.jpg` 拷到电脑上确认视野覆盖整个工作台。

### 2. cup_detect.py —— 调杯口圆检测参数

```sh
python3 cup_detect.py -d 0 -o debug.jpg --min-r 40 --max-r 200 --param1 100 --param2 50
```

- 检测到杯口时输出单行 `CUP x=<cx> y=<cy> r=<半径>`，退出码 0
- 未检出输出 `CUP NOT_FOUND`，退出码 1
- `debug.jpg` 中：灰色圆为所有候选，绿色圆 + 红十字为最终选中的杯口
- 若误检/漏检，调整 `--min-r/--max-r`（量一下杯口在 debug 图中的像素半径）
  和 `--param2`（漏检调小，误检调大），直到稳定检出

### 3. hand_eye_calib.py —— 手眼标定

```sh
python3 hand_eye_calib.py -o calib.json
```

交互式流程（至少 4 点，建议 6~9 点、分散覆盖台面四角和中心）：

1. 把机械臂夹爪尖移动到台面上一个位置，用臂控界面读出当前臂坐标
2. 程序提示输入 `x,y`（mm），回车
3. 保持夹爪不动，另开一个终端跑 `python3 cup_detect.py -o debug.jpg`，
   或直接拍一张 `snapshot.py -o out.jpg`，在图上读出夹爪尖的像素坐标
   （也可打印一个圆纸片放在夹爪尖处，直接用 cup_detect 检出的圆心）
4. 程序提示输入 `u,v`，回车，完成一个点
5. 全部点录完后直接回车结束采集

程序用 `cv2.findHomography` 计算像素→台面的 3x3 单应矩阵 H，
打印每个点的重投影误差（>5mm 的点会提示重标），结果存入 `calib.json`。

### 4. cup_locate.py —— 一键输出台面坐标（主程序调用）

```sh
python3 cup_locate.py -d 0 -c calib.json
```

- 成功输出单行：`POSE x=<台面X mm> y=<台面Y mm> px=<u> py=<v>`，退出码 0
- 失败输出：`POSE NOT_FOUND`，退出码 1
- 省略 `-c` 时只输出像素坐标：`POSE px=<u> py=<v>`（标定前调试用）

主程序用 subprocess 调用并解析首行即可。

## 典型会话示例

```sh
# 1. 验证摄像头
$ python3 snapshot.py -d 0 -o out.jpg
实际分辨率: 1920x1080
帧均值亮度: 132.4 (0=全黑, 255=全白)
已保存: out.jpg

# 2. 调检测参数
$ python3 cup_detect.py -d 0 -o debug.jpg
CUP x=962.5 y=618.0 r=87.3

# 3. 标定（节选）
$ python3 hand_eye_calib.py -o calib.json
[点 1] 台面坐标 x,y (mm)，直接回车结束: 100,100
[点 1] 对应像素坐标 u,v: 420,380
  已记录: 像素(420.0,380.0) -> 台面(100.0,100.0)
...
RMS 误差: 1.842 mm
标定数据已保存: calib.json

# 4. 一键定位
$ python3 cup_locate.py -d 0 -c calib.json
POSE x=185.3 y=142.7 px=962.5 py=618.0
```

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `snapshot.py` | 采图测试，验证摄像头 |
| `cup_detect.py` | HoughCircles 杯口圆检测，输出像素坐标 |
| `hand_eye_calib.py` | 交互式手眼标定，生成 `calib.json` |
| `cup_locate.py` | 成品：像素坐标 + 标定矩阵 → 台面坐标 |
| `calib.json` | 标定结果（H 矩阵、点对、重投影误差），由步骤 3 生成 |
