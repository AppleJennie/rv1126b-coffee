# STS3215 总线舵机控制工具集

飞特（Feetech）STS 系列总线舵机（STS3215）控制代码，目标平台为 aarch64 Linux
开发板（正点原子 ATK-DLRV1126B，Buildroot 系统）。所有工具静态链接，scp 到板子
上即可直接运行，无任何外部依赖（只用 libc/termios）。

## 编译

在 aarch64 Ubuntu 主机（或交叉环境指定 CC）下：

```sh
make            # 产出 servo_tool / teach_record / teach_play
file servo_tool # 确认是 aarch64 静态链接 ELF
```

## 接线说明

- **USB-TTL 转接板**：舵机 TTL 信号线接转接板的 TTL 数据端（单线半双工总线，
  STS 舵机是三针：信号、电源、地）。转接板 USB 插开发板，识别为 `/dev/ttyUSB*`。
- **共地铁律**：开发板 / USB-TTL 的 GND、舵机电源的 GND 必须连在一起，否则
  电平无参考，通讯必失败。
- **舵机独立供电**：STS3215 用 8.4V（2S 锂电或稳压电源）独立供电，电流按
  舵机数量和堵转余量留足。**严禁**用开发板 5V/USB 给舵机供电。
- **急停**：供电回路串一个大电流开关/急停按钮，示教或调试时手边能立刻断电；
  也可以用 `servo_tool torque <id> off` 软件卸力。

## 工具用法

### servo_tool —— 命令行控制

```sh
./servo_tool scan                      # 扫描总线, 打印在线 ID
./servo_tool read 1                    # 读 ID=1 的位置/速度/负载/电压/温度
./servo_tool move 1 2048               # 移到中位 (0~4095, 中位 2048)
./servo_tool move 1 1500 800 500       # 位置 1500, 速度 800, 时间 500ms
./servo_tool torque 1 off              # 卸力 (可手掰)
./servo_tool torque 1 on               # 上力
./servo_tool center 1 2 3              # 多个舵机回中位
./servo_tool -d /dev/ttyUSB1 -b 115200 scan   # 指定设备/波特率
```

### teach_record —— 手掰示教录制

```sh
./teach_record -d /dev/ttyUSB0 -o out.csv -r 20 -t 60 1 2
```

对 ID 1、2 卸力后以 20Hz 采样 60 秒写入 `out.csv`（`time_ms,id1,id2` 每行一帧），
录制中可随时 Ctrl-C 提前结束，结束后自动恢复上力。

### teach_play —— 示教回放

```sh
./teach_play -d /dev/ttyUSB0 -i out.csv          # 原速回放
./teach_play -d /dev/ttyUSB0 -i out.csv -s 1.5   # 1.5 倍速
```

先把各舵机缓慢移到第一帧位置（约 2.5 秒），再按原始时间戳节奏逐帧下发位置。

## 协议要点（飞特 STS 串口协议）

- 包格式：`0xFF 0xFF, ID, Length, Instruction, Param..., Checksum`
- `Length = 参数个数 + 2`，`Checksum = ~(ID + Length + Instruction + Param 和) & 0xFF`
- 多字节寄存器一律小端；目标位置寄存器 42~43，范围 0~4095（中位 2048）
- 半双工：发送后 `tcdrain` 再读应答；部分转接板有回显，读应答前 flush 输入，
  若应答开头与发送包一致则剥掉回显再解析
