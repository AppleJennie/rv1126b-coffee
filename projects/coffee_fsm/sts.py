#!/usr/bin/env python3
# sts.py —— 飞特 STS 系列总线舵机 (STS3215) 协议层，Python 移植
# 逐条参照 ../servo_bus/sts_servo.c：
#   包格式: 0xFF 0xFF, ID, Length, Instruction, Param1..ParamN, Checksum
#   Length   = 参数个数 + 2 (Instruction + Checksum)
#   Checksum = ~(ID + Length + Instruction + 所有Param) & 0xFF
# 发送后 flush（tcdrain 式等待）+ 短延时再读；处理转接板回显；校验应答 checksum。

import time

import serial

# 指令码
INST_PING = 0x01
INST_READ = 0x02
INST_WRITE = 0x03
INST_REG_WRITE = 0x04
INST_ACTION = 0x05

# 广播 ID（只发不收）
ID_BROADCAST = 0xFE

# STS3215 寄存器地址
REG_TORQUE_SWITCH = 40   # 1 字节: 0=卸力, 1=上力
REG_TARGET_POS = 42      # 2 字节小端, 0~4095, 中位 2048
REG_RUNNING_TIME = 44    # 2 字节小端
REG_RUNNING_SPEED = 46   # 2 字节小端
REG_CURRENT_POS = 56     # 2 字节小端, 只读
REG_CURRENT_SPEED = 58   # 2 字节, 只读
REG_CURRENT_LOAD = 60    # 2 字节, 只读
REG_CURRENT_VOLT = 62    # 1 字节, 单位 0.1V, 只读
REG_CURRENT_TEMP = 63    # 1 字节, 单位 °C, 只读
REG_MOVING = 66          # 1 字节, 只读

POS_MIN = 0
POS_MAX = 4095
POS_CENTER = 2048

_TX_DELAY = 0.0005       # 发送完成后额外等待 500us，等收发器换向
_READ_STEP = 0.002       # 读轮询步长 2ms


def _checksum(data):
    """~(求和) & 0xFF，data 为从 ID 起到末尾参数的字节序列。"""
    return (~sum(data)) & 0xFF


def _build_pkt(sid, inst, params=b""):
    """构造发包：0xFF 0xFF ID Length Inst Params... Checksum。"""
    body = bytes([sid, len(params) + 2, inst]) + bytes(params)
    return b"\xff\xff" + body + bytes([_checksum(body)])


class BusServo:
    """STS 总线舵机控制器。所有读方法失败返回 None，写方法失败返回 False。"""

    def __init__(self, port, baud=115200, timeout_ms=100):
        # 8N1 无流控；timeout=0 非阻塞，读超时由 _read_timeout 自行轮询
        self.ser = serial.Serial(port, baud, bytesize=serial.EIGHTBITS,
                                 parity=serial.PARITY_NONE,
                                 stopbits=serial.STOPBITS_ONE, timeout=0)
        self.timeout_ms = timeout_ms

    def close(self):
        if self.ser is not None and self.ser.is_open:
            self.ser.close()

    # ---------- 底层收发 ----------

    def _read_timeout(self, n, timeout_ms):
        """带总超时地读 n 字节，返回实际读到的 bytes。"""
        buf = bytearray()
        deadline = time.monotonic() + timeout_ms / 1000.0
        while len(buf) < n and time.monotonic() < deadline:
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf += chunk
            else:
                time.sleep(_READ_STEP)
        return bytes(buf)

    def _xfer(self, pkt, resp_len):
        """发一个包并读应答，自动剥回显，校验头和 checksum。
        成功返回应答 bytes（含 0xFF 0xFF 头），失败返回 None。"""
        # 清掉残留，防止上一次的回显/垃圾影响本次解析
        self.ser.reset_input_buffer()
        if self.ser.write(pkt) != len(pkt):
            return None
        self.ser.flush()            # tcdrain 式等待发送完成
        time.sleep(_TX_DELAY)

        # 最坏情况：回显 len(pkt) 字节 + 应答 resp_len 字节
        raw = self._read_timeout(len(pkt) + resp_len, self.timeout_ms)

        # 三种情况与 sts_servo.c 的 sts_xfer() 一一对应
        if len(raw) >= resp_len and raw[0] == 0xFF and raw[1] == 0xFF \
                and raw[:len(pkt)] != pkt:
            resp = raw[:resp_len]                       # 情况 1: 无回显
        elif len(raw) >= len(pkt) + resp_len and raw[:len(pkt)] == pkt:
            resp = raw[len(pkt):len(pkt) + resp_len]    # 情况 2: 剥掉回显
        elif len(raw) == resp_len and raw[0] == 0xFF and raw[1] == 0xFF:
            resp = raw                                  # 情况 3: 恰好与发包前缀相同
        else:
            return None

        # 校验应答头、checksum、Error 字节
        if resp[0] != 0xFF or resp[1] != 0xFF:
            return None
        if _checksum(resp[2:-1]) != resp[-1]:
            return None
        if resp[4] != 0:
            return None
        return resp

    def _send_only(self, pkt):
        """只发不收（广播或不需要应答时）。"""
        self.ser.reset_input_buffer()
        if self.ser.write(pkt) != len(pkt):
            return False
        self.ser.flush()
        time.sleep(_TX_DELAY)
        return True

    # ---------- 通用寄存器读写 ----------

    def _read_regs(self, sid, addr, length):
        """读 addr 起 length 字节，成功返回 bytes，失败 None。"""
        pkt = _build_pkt(sid, INST_READ, bytes([addr, length]))
        # 应答: FF FF ID Length(=len+2) Err data... Chk => 总长 6+len
        resp = self._xfer(pkt, 6 + length)
        if resp is None:
            return None
        return resp[5:5 + length]

    def _write_regs(self, sid, addr, data):
        """写 addr 起 data，广播只发不收。成功 True / 失败 False。"""
        pkt = _build_pkt(sid, INST_WRITE, bytes([addr]) + bytes(data))
        if sid == ID_BROADCAST:
            return self._send_only(pkt)
        return self._xfer(pkt, 6) is not None

    # ---------- 对外接口 ----------

    def ping(self, sid):
        """ping 指定舵机，在线 True，否则 False。"""
        # 应答固定 6 字节: FF FF ID 02 Err Chk
        return self._xfer(_build_pkt(sid, INST_PING), 6) is not None

    def scan(self, id_min=0, id_max=253):
        """扫描总线 id_min~id_max，返回在线 ID 列表。"""
        found = []
        for sid in range(id_min, id_max + 1):
            if self.ping(sid):
                found.append(sid)
        return found

    def read_position(self, sid):
        """读当前位置 0~4095，失败 None。"""
        d = self._read_regs(sid, REG_CURRENT_POS, 2)
        if d is None:
            return None
        return d[0] | (d[1] << 8)      # 小端

    def read_feedback(self, sid):
        """读全部反馈，返回 dict: pos/speed/load/voltage/temp/moving，失败 None。"""
        # 56 起连续读 8 字节: pos(2) speed(2) load(2) volt(1) temp(1)
        d = self._read_regs(sid, REG_CURRENT_POS, 8)
        if d is None:
            return None
        m = self._read_regs(sid, REG_MOVING, 1)
        if m is None:
            return None
        return {
            "pos": d[0] | (d[1] << 8),
            "speed": d[2] | (d[3] << 8),
            "load": d[4] | (d[5] << 8),
            "voltage": d[6] / 10.0,    # 单位 V
            "temp": d[7],              # 单位 °C
            "moving": 1 if m[0] else 0,
        }

    def write_position(self, sid, pos, speed=0, time_ms=0):
        """写目标位置；speed=运行速度, time_ms=运行时间, 可为 0 表示默认。"""
        if pos < POS_MIN or pos > POS_MAX:
            return False
        # 42 起: Target Position(2), Running Time(2), Running Speed(2)，均小端
        d = bytes([pos & 0xFF, (pos >> 8) & 0xFF,
                   time_ms & 0xFF, (time_ms >> 8) & 0xFF,
                   speed & 0xFF, (speed >> 8) & 0xFF])
        return self._write_regs(sid, REG_TARGET_POS, d)

    def torque(self, sid, on):
        """扭矩开关: on=True 上力, False 卸力(可手掰)。"""
        return self._write_regs(sid, REG_TORQUE_SWITCH, bytes([1 if on else 0]))
