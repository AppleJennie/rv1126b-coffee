#!/usr/bin/env python3
# mock_robot_serial.py —— RV1126B ↔ 机械臂 MCU 通信协议（ROBOT_PROTOCOL v1）参考实现与全模拟自测
#
# 协议文档：docs/ROBOT_PROTOCOL.md（TASK 19）
#   帧格式：0xAA 0x55 | SEQ | CMD | LEN | PARAMS... | CRC16-CCITT（大端，高字节先发）
#   时序：  命令发出 100ms 内必须收到 ACK，否则同一 SEQ 重发，最多 2 次；
#           DONE 之前可有任意多次 BUSY(progress)；ESTOP 任何时刻立即生效。
#   原则：  协议只承载语义动作，关节角度表全部在 MCU 侧（TASK 20）。
#
# 本文件三部分（纯标准库，板端 Python 3.11 直接可跑）：
#   1) 协议层：crc16_ccitt / pack_frame / FrameParser —— 真机串口直接复用
#   2) MockMCU：socketpair 模拟 MCU 固件，动作延迟表 + 故障注入
#      （no_ack 无应答 / error_at 指定动作回 ERROR / estop 急停态直到 RESET）
#   3) RobotSerialClient：上位机客户端（SEQ 匹配、100ms ACK 超时、2 次重发、
#      BUSY 进度回调、ESTOP 可并发打断）——以后 hardware/ 里 MCU 版 RobotArm
#      适配器（StsRobotArm 的串口-MCU 形态）以它为底子。
#
# 自测：python3 mock_robot_serial.py
#   覆盖：CRC 检验值 + 故意改坏一字节能检出 / pack-parse 往返（含逐字节分片）/
#         PING→HOME→MOVE_POSE→PICK_CUP→PLACE_CUP→SERVE 成功链路 /
#         no_ack 重发与最终超时 / error_at 回 ERROR /
#         ESTOP 打断执行中动作 + 急停态拒绝动作直到 RESET。

import collections
import select
import socket
import sys
import threading
import time

_T0 = time.monotonic()  # 日志相对时间基准

# ============================== 协议常量 ==============================

FRAME_HEADER = b"\xAA\x55"          # 帧头（2 字节）

# ---- 命令码：上位机 -> MCU ----
CMD_PING      = 0x01   # 链路探测
CMD_HOME      = 0x02   # 回待机位（上电回零）
CMD_MOVE_POSE = 0x03   # 移动到语义位姿，param[0]=pose_id
CMD_PICK_CUP  = 0x04   # 取杯，可选 param=[dx,dy]（int8 mm 视觉纠偏）
CMD_PLACE_CUP = 0x05   # 放杯到冲泡位
CMD_SERVE     = 0x06   # 递杯到出餐位
CMD_STOP      = 0x07   # 平滑停止（保持扭矩）
CMD_ESTOP     = 0x08   # 急停（立即停+卸力，进入急停态）
CMD_RESET     = 0x09   # 解除急停态/清错误

# ---- 应答码：MCU -> 上位机 ----
RSP_ACK   = 0x81       # 已收到，开始处理（中间帧）
RSP_BUSY  = 0x82       # 执行中，param[0]=progress 0~99（中间帧，可多次）
RSP_DONE  = 0x83       # 成功完成（终止帧）
RSP_ERROR = 0x84       # 失败/拒绝，param[0]=err_code（终止帧）
RSP_LIMIT = 0x85       # 限位触发，param[0]=关节号（终止帧）
RSP_ESTOP = 0x86       # 急停态确认/被打断（终止帧）

TERMINAL_RSPS = (RSP_DONE, RSP_ERROR, RSP_LIMIT, RSP_ESTOP)  # 终止帧集合

# ---- 错误码（ERROR 应答 param[0]）----
ERR_NO_RESP      = 0x01  # 无应答（上位机侧合成，MCU 不会发）
ERR_POS_ERR      = 0x02  # 位置超差
ERR_OVERCURRENT  = 0x03  # 过流
ERR_LIMIT        = 0x04  # 限位触发
ERR_ESTOP_ACTIVE = 0x05  # 急停态中，拒绝动作
ERR_BAD_PARAM    = 0x06  # 参数非法
ERR_NOT_HOMED    = 0x07  # 未回零
ERR_BUSY         = 0x08  # 忙冲突

# ---- 语义位姿 ID（与 config/poses.yaml 的键对应；角度表只在 MCU 侧）----
POSE_IDS = {"HOME": 0, "CUP": 1, "BREWER": 2, "WATER": 3, "SERVE": 4,
            "GROUNDS_PICK": 5, "GROUNDS_POUR": 6}

CMD_NAMES = {CMD_PING: "PING", CMD_HOME: "HOME", CMD_MOVE_POSE: "MOVE_POSE",
             CMD_PICK_CUP: "PICK_CUP", CMD_PLACE_CUP: "PLACE_CUP",
             CMD_SERVE: "SERVE", CMD_STOP: "STOP", CMD_ESTOP: "ESTOP",
             CMD_RESET: "RESET"}
RSP_NAMES = {RSP_ACK: "ACK", RSP_BUSY: "BUSY", RSP_DONE: "DONE",
             RSP_ERROR: "ERROR", RSP_LIMIT: "LIMIT", RSP_ESTOP: "ESTOP"}
ERR_NAMES = {ERR_NO_RESP: "NO_RESP", ERR_POS_ERR: "POS_ERR",
             ERR_OVERCURRENT: "OVERCURRENT", ERR_LIMIT: "LIMIT",
             ERR_ESTOP_ACTIVE: "ESTOP_ACTIVE", ERR_BAD_PARAM: "BAD_PARAM",
             ERR_NOT_HOMED: "NOT_HOMED", ERR_BUSY: "BUSY_CONFLICT"}
_NAME2CMD = {v: k for k, v in CMD_NAMES.items()}
_POSE_NAMES = {v: k for k, v in POSE_IDS.items()}

# 一帧：SEQ, CMD, PARAMS
Frame = collections.namedtuple("Frame", ["seq", "cmd", "params"])


def _log(who, arrow, seq, code, params=b"", note=""):
    """带相对时间的收发日志（自测要求打印每步收发）。"""
    name = CMD_NAMES.get(code) or RSP_NAMES.get(code) or f"0x{code:02X}"
    detail = ""
    if code == RSP_BUSY and params:
        detail = f"progress={params[0]}%"
    elif code == RSP_ERROR and params:
        detail = f"err=0x{params[0]:02X}({ERR_NAMES.get(params[0], '?')})"
    elif code == RSP_LIMIT and params:
        detail = f"axis={params[0]}"
    elif code == CMD_MOVE_POSE and params:
        detail = f"pose={_POSE_NAMES.get(params[0], '?')}"
    elif code == CMD_PICK_CUP and len(params) == 2:
        dx, dy = params[0], params[1]
        # int8 有符号还原
        detail = f"纠偏 dx={dx-256 if dx > 127 else dx},dy={dy-256 if dy > 127 else dy}mm"
    t = time.monotonic() - _T0
    parts = [f"[{t:7.3f}]", f"{who:5}", arrow, f"seq={seq:<3}", f"{name:<9}",
             f"{detail:<24}", note]
    print(" ".join(p for p in parts if p).rstrip())


# ============================== 1. 协议层 ==============================

def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE：多项式 0x1021，初值 0xFFFF，不反射，输出不异或。
    检验值：crc16_ccitt(b"123456789") == 0x29B1。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def pack_frame(seq: int, cmd: int, params: bytes = b"") -> bytes:
    """打包一帧：头 + SEQ + CMD + LEN + PARAMS + CRC16（大端）。
    CRC 计算范围：SEQ 起到 PARAMS 末字节（不含帧头）。"""
    if not 0 <= len(params) <= 255:
        raise ValueError("params 长度必须 0~255")
    body = bytes((seq & 0xFF, cmd & 0xFF, len(params))) + bytes(params)
    crc = crc16_ccitt(body)
    return FRAME_HEADER + body + bytes((crc >> 8, crc & 0xFF))


class FrameParser:
    """流式帧解析器：往 feed() 灌字节，返回解析出的完整帧列表。
    帧头错位 / CRC 错误时逐字节滑动重同步，坏帧计数到 bad_frames。"""

    MIN_LEN = 7  # 头2 + SEQ + CMD + LEN + CRC2

    def __init__(self):
        self._buf = bytearray()
        self.bad_frames = 0

    def feed(self, data: bytes):
        self._buf += data
        frames = []
        while True:
            # 1) 找帧头
            i = self._buf.find(FRAME_HEADER)
            if i < 0:
                # 末尾单独的 0xAA 可能是半个帧头，保留
                keep = 1 if self._buf.endswith(FRAME_HEADER[:1]) else 0
                del self._buf[: len(self._buf) - keep]
                break
            if i > 0:
                self.bad_frames += 1  # 帧头前有垃圾，丢弃重同步
                del self._buf[:i]
            if len(self._buf) < self.MIN_LEN:
                break  # 最小帧都没收全，等更多字节
            plen = self._buf[4]
            total = self.MIN_LEN + plen
            if len(self._buf) < total:
                break  # 帧没收全
            body = bytes(self._buf[2: 5 + plen])
            crc_got = (self._buf[5 + plen] << 8) | self._buf[6 + plen]
            if crc16_ccitt(body) != crc_got:
                self.bad_frames += 1  # CRC 错：丢 1 字节重新找帧头
                del self._buf[0]
                continue
            frames.append(Frame(seq=self._buf[2], cmd=self._buf[3],
                                params=bytes(self._buf[5: 5 + plen])))
            del self._buf[:total]
        return frames


# ============================== 2. MockMCU ==============================

def _as_cmd(x):
    """故障注入表允许用动作名（如 "PICK_CUP"）或命令码，统一转成命令码。"""
    return _NAME2CMD[x] if isinstance(x, str) else x


class MockMCU:
    """模拟机械臂 MCU 固件：独立线程从 sock 收命令帧、按协议回应。

    参数：
      action_delays: dict 命令码/动作名 -> 执行耗时(秒)，期间按 busy_steps 次
                     BUSY 汇报递增进度，最后 DONE
      busy_steps:    每个动作的 BUSY 进度汇报次数
      no_ack:        命令码/动作名集合（或 True=全部）：匹配的命令直接不应答，
                     用于触发上位机重发与最终超时
      error_at:      dict 命令码/动作名 -> err_code：匹配的动作 ACK 后立即回 ERROR

    ESTOP 行为：收到 ESTOP 命令 -> 立即中止执行中动作（给该动作回 ESTOP 终止），
    进入急停态；急停态中动作命令一律回 ERROR(ERR_ESTOP_ACTIVE)，直到 RESET。
    """

    def __init__(self, sock, action_delays=None, busy_steps=3,
                 no_ack=(), error_at=None):
        self._sock = sock
        self.action_delays = {_as_cmd(k): v for k, v in (action_delays or {}).items()}
        self.busy_steps = busy_steps
        self._no_ack = set()
        self._error_at = {}
        self.no_ack = no_ack        # 走 setter 做动作名归一化，允许运行中改
        self.error_at = error_at
        self.estopped = False
        self._parser = FrameParser()
        self._running = False
        self._thread = None
        self._busy = None  # 执行中动作: {seq, cmd, start_at, duration, busy_idx}

    # 故障注入开关做成 property：赋动作名（如 "PICK_CUP"）或命令码都行
    @property
    def no_ack(self):
        return self._no_ack

    @no_ack.setter
    def no_ack(self, v):
        self._no_ack = True if v is True else {_as_cmd(c) for c in v}

    @property
    def error_at(self):
        return self._error_at

    @error_at.setter
    def error_at(self, v):
        self._error_at = {_as_cmd(k): e for k, e in (v or {}).items()}

    # ---------- 生命周期 ----------

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="MockMCU", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    # ---------- 内部 ----------

    def _send(self, seq, rsp, params=b""):
        self._sock.sendall(pack_frame(seq, rsp, params))
        _log("MCU", ">>", seq, rsp, params)

    def _dropped(self, cmd):
        return self.no_ack is True or cmd in self.no_ack

    def _loop(self):
        while self._running:
            r, _, _ = select.select([self._sock], [], [], 0.002)
            if r:
                data = self._sock.recv(4096)
                if not data:
                    break  # 对端关闭
                for fr in self._parser.feed(data):
                    self._handle(fr)
            self._pump_busy()

    def _handle(self, fr):
        seq, cmd, params = fr.seq, fr.cmd, fr.params
        _log("MCU", "<<", seq, cmd, params)
        if self._dropped(cmd):
            _log("MCU", "!!", seq, cmd, b"", "故障注入 no_ack：不应答")
            return
        if cmd == CMD_ESTOP:
            self._do_estop(seq)
            return
        if cmd == CMD_RESET:
            self.estopped = False
            self._send(seq, RSP_ACK)
            self._send(seq, RSP_DONE)
            _log("MCU", "--", seq, cmd, b"", "急停态/错误已清除")
            return
        if cmd == CMD_PING:
            self._send(seq, RSP_ACK)
            self._send(seq, RSP_DONE)
            return
        if cmd == CMD_STOP:
            if self._busy is not None:
                _log("MCU", "--", self._busy["seq"], cmd, b"", "STOP：平滑停止当前动作")
                self._busy = None
            self._send(seq, RSP_ACK)
            self._send(seq, RSP_DONE)
            return
        # 以下为动作命令：HOME / MOVE_POSE / PICK_CUP / PLACE_CUP / SERVE
        if cmd not in (CMD_HOME, CMD_MOVE_POSE, CMD_PICK_CUP, CMD_PLACE_CUP, CMD_SERVE):
            self._send(seq, RSP_ACK)
            self._send(seq, RSP_ERROR, bytes((ERR_BAD_PARAM,)))
            return
        if cmd == CMD_MOVE_POSE and (len(params) != 1 or params[0] not in _POSE_NAMES):
            self._send(seq, RSP_ACK)
            self._send(seq, RSP_ERROR, bytes((ERR_BAD_PARAM,)))
            return
        if self.estopped:
            self._send(seq, RSP_ACK)
            self._send(seq, RSP_ERROR, bytes((ERR_ESTOP_ACTIVE,)))
            return
        if self._busy is not None:
            self._send(seq, RSP_ACK)
            self._send(seq, RSP_ERROR, bytes((ERR_BUSY,)))
            return
        self._send(seq, RSP_ACK)
        if cmd in self.error_at:
            self._send(seq, RSP_ERROR, bytes((self.error_at[cmd],)))
            return
        now = time.monotonic()
        duration = self.action_delays.get(cmd, 0.15)
        self._busy = {"seq": seq, "cmd": cmd, "start_at": now,
                      "duration": duration, "busy_idx": 0}

    def _pump_busy(self):
        """执行中动作的进度推进：busy_steps 次递增 BUSY，时间到发 DONE。"""
        b = self._busy
        if b is None:
            return
        now = time.monotonic()
        i = b["busy_idx"]
        if i < self.busy_steps and \
                now >= b["start_at"] + b["duration"] * (i + 1) / (self.busy_steps + 1):
            progress = int(100 * (i + 1) / (self.busy_steps + 1))
            self._send(b["seq"], RSP_BUSY, bytes((progress,)))
            b["busy_idx"] += 1
            return
        if now >= b["start_at"] + b["duration"]:
            self._busy = None
            self._send(b["seq"], RSP_DONE)

    def _do_estop(self, seq):
        """急停：立即中止执行中动作（回显其 SEQ 发 ESTOP 终止），进入急停态。"""
        if self._busy is not None:
            victim = self._busy
            self._busy = None
            self._send(victim["seq"], RSP_ESTOP)
            _log("MCU", "--", victim["seq"], CMD_ESTOP, b"", "急停打断执行中动作")
        self.estopped = True
        self._send(seq, RSP_ACK)
        self._send(seq, RSP_ESTOP)


# ============================== 3. RobotSerialClient ==============================

# 命令执行结果：status ∈ DONE/ERROR/LIMIT/ESTOP/TIMEOUT；error 为错误码或 None
Result = collections.namedtuple("Result", ["status", "error", "retries"])


class RobotSerialClient:
    """上位机客户端：命令串行执行，ESTOP 可从任意线程并发打断。

    send_command(cmd, params, on_progress=None) -> Result：
      - SEQ 匹配：每发一个新命令 SEQ+1，只认回显该 SEQ 的应答帧
      - ACK 超时 100ms：收不到 ACK 用同一 SEQ 重发，最多 2 次；
        仍无应答 -> Result("TIMEOUT", ERR_NO_RESP, 2)
      - ACK 之后等终止帧（DONE/ERROR/LIMIT/ESTOP），期间 BUSY 触发 on_progress
      - 另有 cmd_timeout 作为 ACK 之后的动作总看门狗
    """

    def __init__(self, sock, ack_timeout=0.1, cmd_timeout=15.0,
                 max_retries=2, on_progress=None):
        self._sock = sock
        self.ack_timeout = ack_timeout
        self.cmd_timeout = cmd_timeout
        self.max_retries = max_retries
        self.on_progress = on_progress
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self._mb_lock = threading.Lock()
        self._parser = FrameParser()
        # 其他 SEQ 的应答暂存处（ESTOP 与被打断动作并发时的分发邮箱）
        self._mailbox = {}
        self._wanted = set()

    # ---------- 对外接口 ----------

    def send_command(self, cmd, params=b"", on_progress=None) -> Result:
        cb = on_progress or self.on_progress
        with self._seq_lock:
            seq = self._seq
            self._seq = (self._seq + 1) & 0xFF
        frame = pack_frame(seq, cmd, params)
        with self._mb_lock:
            self._wanted.add(seq)  # 先登记再发，杜绝应答早于登记的竞态
        try:
            # ---- 阶段 1：等 ACK（超时则同一 SEQ 重发，最多 max_retries 次）----
            acked, retries = False, 0
            for attempt in range(self.max_retries + 1):
                with self._send_lock:
                    self._sock.sendall(frame)
                _log("HOST", ">>", seq, cmd, params,
                     "" if attempt == 0 else f"(第{attempt}次重发)")
                deadline = time.monotonic() + self.ack_timeout
                while True:
                    fr = self._wait_frame(seq, deadline - time.monotonic())
                    if fr is None:
                        break  # ACK 超时 -> 重发
                    if fr.cmd == RSP_ACK:
                        acked = True
                        break
                    r = self._terminal_result(fr, attempt)
                    if r is not None:  # 未等到 ACK 先来终止帧（如急停中直接拒绝）
                        return r
                if acked:
                    break
                retries = attempt + 1
            if not acked:
                _log("HOST", "!!", seq, cmd, b"",
                     f"{self.max_retries + 1} 帧均无应答，判定链路故障")
                return Result("TIMEOUT", ERR_NO_RESP, self.max_retries)
            # ---- 阶段 2：ACK 已到，等 BUSY*/终止帧 ----
            deadline = time.monotonic() + self.cmd_timeout
            while True:
                fr = self._wait_frame(seq, deadline - time.monotonic())
                if fr is None:
                    _log("HOST", "!!", seq, cmd, b"", "动作总超时（看门狗）")
                    return Result("TIMEOUT", ERR_NO_RESP, retries)
                if fr.cmd == RSP_BUSY:
                    if cb:
                        cb(fr.params[0] if fr.params else 0)
                    continue
                r = self._terminal_result(fr, retries)
                if r is not None:
                    return r
        finally:
            with self._mb_lock:
                self._wanted.discard(seq)
                self._mailbox.pop(seq, None)

    def emergency_stop(self) -> Result:
        """急停：可在任意线程调用，打断正在执行的命令。幂等、永不抛异常。"""
        try:
            return self.send_command(CMD_ESTOP)
        except Exception as e:  # 急停不允许把异常扩散到业务层
            _log("HOST", "!!", 0, CMD_ESTOP, b"", f"emergency_stop 异常: {e!r}")
            return Result("ERROR", ERR_NO_RESP, 0)

    def reset(self) -> Result:
        """解除急停态/清错误。"""
        return self.send_command(CMD_RESET)

    # ---------- 内部 ----------

    def _terminal_result(self, fr, retries):
        """终止帧 -> Result；非终止帧（ACK/BUSY）-> None。"""
        if fr.cmd == RSP_DONE:
            return Result("DONE", None, retries)
        if fr.cmd == RSP_ESTOP:
            return Result("ESTOP", None, retries)
        if fr.cmd == RSP_LIMIT:
            return Result("LIMIT", fr.params[0] if fr.params else 0, retries)
        if fr.cmd == RSP_ERROR:
            return Result("ERROR", fr.params[0] if fr.params else 0, retries)
        return None

    def _wait_frame(self, seq, timeout):
        """等一个回显 seq 的应答帧；超时返回 None。
        收到其他 seq 的帧时暂存对应邮箱（并发 ESTOP 场景），无人认领的丢弃。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._mb_lock:
                box = self._mailbox.get(seq)
                if box:
                    return box.pop(0)
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            with self._recv_lock:
                r, _, _ = select.select([self._sock], [], [], min(remain, 0.05))
                if not r:
                    continue
                data = self._sock.recv(4096)
                if not data:
                    raise ConnectionError("MCU 通道断开")
                frames = self._parser.feed(data)
            for fr in frames:
                _log("HOST", "<<", fr.seq, fr.cmd, fr.params)
                with self._mb_lock:
                    if fr.seq in self._wanted:
                        self._mailbox.setdefault(fr.seq, []).append(fr)
                    else:
                        _log("HOST", "??", fr.seq, fr.cmd, fr.params, "SEQ 无匹配，丢弃")


# ============================== 自测 ==============================

def _test_protocol_layer():
    """协议层单元测试：CRC 检验值、pack/parse 往返、CRC 检错。"""
    print("=== 1. CRC16-CCITT 单元测试 ===")
    assert crc16_ccitt(b"123456789") == 0x29B1, "标准检验值应为 0x29B1"
    assert crc16_ccitt(b"") == 0xFFFF
    print(f"  CRC16/CCITT-FALSE('123456789') = 0x{crc16_ccitt(b'123456789'):04X} OK")

    print("=== 2. pack/parse 往返（整帧 + 逐字节分片）===")
    raw = pack_frame(7, CMD_PICK_CUP, bytes((1, 0xFE))) + pack_frame(8, CMD_PING)
    p = FrameParser()
    frames = p.feed(raw)
    assert [(f.seq, f.cmd, f.params) for f in frames] == \
           [(7, CMD_PICK_CUP, bytes((1, 0xFE))), (8, CMD_PING, b"")]
    p2 = FrameParser()
    out = []
    for i in range(len(raw)):  # 逐字节喂入，结果必须一致
        out += p2.feed(raw[i:i + 1])
    assert [(f.seq, f.cmd, f.params) for f in out] == \
           [(f.seq, f.cmd, f.params) for f in frames]
    print("  整帧/逐字节分片解析结果一致 OK")

    print("=== 3. CRC 故意改坏一字节必须检出 ===")
    bad = bytearray(pack_frame(9, CMD_HOME))
    bad[2] ^= 0xFF  # 改坏 SEQ 字节 -> CRC 必然不匹配
    p3 = FrameParser()
    got = p3.feed(bytes(bad))
    assert got == [] and p3.bad_frames == 1, f"坏帧应被丢弃: {got}"
    print("  改坏 1 字节 -> CRC 校验失败，帧被丢弃并计数 OK")


def _test_full_link():
    """socketpair 字节通道上的端到端自测（MockMCU + RobotSerialClient）。"""
    print("=== 4. 模拟串口全链路自测（socketpair）===")
    host_sock, mcu_sock = socket.socketpair()
    delays = {CMD_HOME: 0.30, CMD_MOVE_POSE: 0.20, CMD_PICK_CUP: 0.20,
              CMD_PLACE_CUP: 0.20, CMD_SERVE: 0.25}
    mcu = MockMCU(mcu_sock, action_delays=delays, busy_steps=3)
    mcu.start()
    client = RobotSerialClient(
        host_sock, on_progress=lambda p: print(f"        [进度回调] {p}%"))
    try:
        # ---- 4.1 成功链路 ----
        print("--- 4.1 成功链路：PING→HOME→MOVE_POSE(CUP)→PICK_CUP→PLACE_CUP→SERVE ---")
        plan = [(CMD_PING, b""), (CMD_HOME, b""),
                (CMD_MOVE_POSE, bytes((POSE_IDS["CUP"],))),
                (CMD_PICK_CUP, b""), (CMD_PLACE_CUP, b""), (CMD_SERVE, b"")]
        for cmd, params in plan:
            r = client.send_command(cmd, params)
            assert r.status == "DONE" and r.error is None, (CMD_NAMES[cmd], r)
        print("  全部 DONE OK")

        # ---- 4.2 no_ack：不应答 -> 重发 2 次 -> 最终超时 ----
        print("--- 4.2 no_ack 故障注入（无应答触发重发与最终超时）---")
        mcu.no_ack = {"PING"}  # 支持动作名注入
        r = client.send_command(CMD_PING)
        assert r.status == "TIMEOUT" and r.error == ERR_NO_RESP and r.retries == 2, r
        mcu.no_ack = set()
        r = client.send_command(CMD_PING)
        assert r.status == "DONE", r
        print("  重发 2 次仍无应答 -> TIMEOUT(ERR_NO_RESP)；恢复后 PING 正常 OK")

        # ---- 4.3 error_at：指定动作回 ERROR ----
        print("--- 4.3 error_at 故障注入（该动作回 ERROR）---")
        mcu.error_at = {"PICK_CUP": ERR_POS_ERR}
        r = client.send_command(CMD_PICK_CUP)
        assert r.status == "ERROR" and r.error == ERR_POS_ERR, r
        mcu.error_at = {}
        print("  PICK_CUP -> ERROR(0x02 位置超差) OK")

        # ---- 4.4 ESTOP：打断执行中动作 + 急停态拒绝 + RESET 恢复 ----
        print("--- 4.4 ESTOP 链路 ---")
        holder = {}

        def long_home():
            holder["r"] = client.send_command(CMD_HOME)  # 0.30s 动作，中途会被打断

        th = threading.Thread(target=long_home, name="home-cmd")
        th.start()
        time.sleep(0.08)  # 等 HOME 进入 BUSY
        r = client.emergency_stop()
        assert r.status == "ESTOP", r
        th.join(timeout=3)
        assert not th.is_alive(), "被打断的 HOME 应立即返回"
        assert holder["r"].status == "ESTOP", holder["r"]
        print("  执行中 HOME 被 ESTOP 打断，两侧均收到 ESTOP 终止 OK")

        r = client.send_command(CMD_PICK_CUP)
        assert r.status == "ERROR" and r.error == ERR_ESTOP_ACTIVE, r
        print("  急停态中 PICK_CUP 被拒：ERROR(0x05 急停中) OK")

        r = client.reset()
        assert r.status == "DONE", r
        r = client.send_command(CMD_PICK_CUP)
        assert r.status == "DONE", r
        print("  RESET 解除急停态后动作恢复 OK")
    finally:
        mcu.stop()
        host_sock.close()
        mcu_sock.close()


def self_test():
    _test_protocol_layer()
    _test_full_link()
    print("\n全部自测通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(self_test())
