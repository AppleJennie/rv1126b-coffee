# structured_log.py —— 统一结构化日志（TASK 28）
#
# 目标：各模块不再各自 print，统一经本模块输出两路日志：
#   1. 控制台：保持既有人类可读风格 `[HH:MM:SS] [TAG] 消息`
#      （兼容契约：kiosk 真机模式正则解析 fsm.py 的 `[FSM] 状态转换` /
#       `[BREW] 冲泡中...` 行，cafe_fsm 的 `[EVENT] {json}` 行由 kiosk
#       _parse_cafe_event 消费——控制台格式一个字都不能变）
#   2. JSONL 文件：logs/cafe-YYYYMMDD.jsonl（仓库根 logs/ 自动创建，按天滚动，
#      每行一条 JSON）
#
# 字段契约（JSONL 每行必有）：
#   timestamp  epoch 秒（毫秒精度，全项目统一用 epoch，不混 ISO）
#   module     模块名：cafe_fsm / hardware / kiosk / stats / watchdog / fsm ...
#   event      事件名；旧式 log(tag, msg) 转发时 event = 原 TAG（如 FSM/BREW/ORDER）
#   level      DEBUG / INFO / WARN / ERROR（旧 TAG 里 ERROR/ESTOP 自动归 ERROR）
#   message    人类可读消息（与控制台行正文一致）
# 可选字段（值非 None 才写入，其余 kwargs 一律忽略防止字段漂移）：
#   order_id / state / duration_sec / result / error
#
# 接入方式：
#   - 旧模块：保留原 log(tag, msg) 签名，内部转发 make_logger(<模块名>)，调用点零改动
#   - 新代码：直接 emit(module, event, message, level=..., order_id=...)
#
# 可靠性红线：日志是附属能力——JSONL 落盘失败降级为仅控制台输出，绝不向业务抛异常。
# 多进程安全：kiosk 与 cafe_fsm 子进程会同时写同一文件；每行 open(append)+单行写+close，
#   Linux O_APPEND 下单行小写入不交错。

import json
import os
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE_DIR))       # 仓库根（projects/common/ 的上两级）
LOG_DIR = os.path.join(ROOT, "logs")

# JSONL 必填字段之外允许的可选字段
OPTIONAL_FIELDS = ("order_id", "state", "duration_sec", "result", "error")

_lock = threading.Lock()    # 进程内控制台/文件写串行化（多进程靠 O_APPEND 单行原子写）


def _jsonl_path(ts):
    """按天滚动的 JSONL 路径：logs/cafe-YYYYMMDD.jsonl。"""
    return os.path.join(LOG_DIR,
                        "cafe-" + time.strftime("%Y%m%d", time.localtime(ts)) + ".jsonl")


def level_of_tag(tag):
    """旧式 TAG -> level 映射：ERROR/ESTOP/FATAL 归 ERROR，WARN 归 WARN，其余 INFO。"""
    t = str(tag).upper()
    if t in ("ERROR", "ESTOP", "FATAL"):
        return "ERROR"
    if t in ("WARN", "WARNING"):
        return "WARN"
    return "INFO"


def emit(module, event, message, level="INFO", **kw):
    """输出一条结构化日志：控制台人类可读行 + JSONL 追加一行。返回记录 dict。"""
    ts = time.time()
    rec = {
        "timestamp": round(ts, 3),
        "module": str(module),
        "event": str(event),
        "level": str(level).upper(),
        "message": str(message),
    }
    for k in OPTIONAL_FIELDS:
        v = kw.get(k)
        if v is not None:
            rec[k] = v
    line = json.dumps(rec, ensure_ascii=False)
    with _lock:
        # 控制台：格式与旧 log(tag, msg) 逐字一致（event 占据旧 TAG 位）
        print("[%s] [%s] %s" % (time.strftime("%H:%M:%S", time.localtime(ts)),
                                rec["event"], rec["message"]), flush=True)
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(_jsonl_path(ts), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass        # 落盘失败降级为仅控制台，绝不拖垮业务
    return rec


def make_logger(module):
    """生成与旧式 log(tag, msg) 签名兼容的转发函数，module 固定为模块名。"""
    def log(tag, msg, level=None, **kw):
        return emit(module, tag, msg, level=level or level_of_tag(tag), **kw)
    return log
