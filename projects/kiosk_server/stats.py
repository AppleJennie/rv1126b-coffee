# stats.py —— SQLite 订单数据统计（TASK 35）
#
# 职责：
#   1. 订单进入终结态（completed/failed/cancelled）时由 OrderManager 落库
#   2. 聚合查询：今日订单数 / 最受欢迎饮品 TOP / 平均制作时长 / 失败原因计数
#      （设备故障次数 = failed 单按 fail_reason 归类）
#   3. 供 /admin 管理后台与 /api/admin/stats 读取
#
# 隐私红线：本库只存订单与制作数据（饮品、数量、金额、时长、结果），
#   绝不写入任何人脸、图像、视觉识别或顾客身份数据。视觉模块的任何输出
#   （人脸框、表情、疲劳标志等）一律不允许进入本模块。
#
# 可靠性：统计是附属能力，绝不能拖垮点单服务——
#   任何 sqlite 异常（DB 文件损坏、无写权限、磁盘满）都只记日志并置
#   self._disabled=True 降级，后续操作全部静默跳过，kiosk 照常运行。
#
# 线程安全：单连接 check_same_thread=False + 全局锁串行化所有读写
#   （写入频率极低——每单一条，锁竞争可忽略）。

import os
import sqlite3
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "kiosk_stats.db")

# 建表 SQL：字段契约见任务书（ts/order_id/drink_id/drink_name/qty/total/
# duration_sec/result/fail_reason/mode），result 取值 success/failed/cancelled
_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,        -- 落库时刻（epoch 秒）
    order_id     INTEGER,                 -- kiosk 订单号（本次运行内递增）
    drink_id     INTEGER,
    drink_name   TEXT,
    qty          INTEGER,
    total        REAL,                    -- 总价（元）
    duration_sec REAL,                    -- 制作时长（开始制作->终结）；取消单为 NULL
    result       TEXT,                    -- success / failed / cancelled
    fail_reason  TEXT,                    -- 失败原因（设备故障归类用）；非失败单为空串
    mode         TEXT                     -- 制作后端 SIM/HYBRID/REAL
);
"""


def log(tag, msg):
    print(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}", flush=True)


def _today_start_ts():
    """今日 00:00:00 的 epoch 秒（本地时区，与运营口径一致）。"""
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                            0, 0, 0, 0, 0, -1)))


class Stats:
    """订单统计库。所有公开方法绝不抛异常：出错即降级（_disabled=True）。"""

    def __init__(self, db_path=None):
        self._lock = threading.Lock()
        self._conn = None
        self._disabled = False
        self.db_path = db_path or DEFAULT_DB
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            with self._lock:
                self._conn.execute(_SCHEMA)
                self._conn.commit()
        except Exception as e:
            # DB 无法打开/建表（路径无权限、文件损坏等）：降级仅日志
            log("STATS", f"统计库初始化失败（降级为仅日志，不影响服务）: {e}")
            self._disabled = True
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ---------- 写入 ----------
    def record(self, order, result, fail_reason=None, duration_sec=None, mode=""):
        """订单终结落库。order 为 OrderManager 的订单 dict（可读内部字段）。
        返回是否成功写入；失败只记日志不抛异常。"""
        if self._disabled:
            return False
        try:
            row = (int(time.time()),
                   order.get("order_id"),
                   order.get("_drink_id"),
                   order.get("drink"),
                   order.get("qty"),
                   order.get("total"),
                   duration_sec,
                   result,
                   fail_reason or "",
                   mode or "")
            with self._lock:
                self._conn.execute(
                    "INSERT INTO orders(ts, order_id, drink_id, drink_name, qty,"
                    " total, duration_sec, result, fail_reason, mode)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)", row)
                self._conn.commit()
            return True
        except Exception as e:
            log("STATS", f"订单落库失败（降级，不再写入）: {e}")
            self._disabled = True
            return False

    # ---------- 聚合查询（全部容错：异常返回安全默认值） ----------
    def _query(self, sql, args=(), default=None):
        """统一查询入口：任何异常记日志并返回 default（不拖垮调用方）。"""
        if self._disabled:
            return default
        try:
            with self._lock:
                cur = self._conn.execute(sql, args)
                return cur.fetchall()
        except Exception as e:
            log("STATS", f"统计查询失败: {e}")
            return default

    def today_count(self):
        """今日订单总数（含成功/失败/取消）。"""
        rows = self._query("SELECT COUNT(*) FROM orders WHERE ts >= ?",
                           (_today_start_ts(),), default=[(0,)])
        return rows[0][0] if rows else 0

    def today_success_count(self):
        rows = self._query("SELECT COUNT(*) FROM orders WHERE ts >= ? AND result = 'success'",
                           (_today_start_ts(),), default=[(0,)])
        return rows[0][0] if rows else 0

    def top_drinks(self, limit=5):
        """最受欢迎饮品 TOP（按单数；取消单未制作不计入，成功/失败都算）。"""
        rows = self._query(
            "SELECT drink_name, COUNT(*) AS c FROM orders"
            " WHERE result != 'cancelled'"
            " GROUP BY drink_name ORDER BY c DESC LIMIT ?", (limit,), default=[])
        return [{"drink_name": r[0], "count": r[1]} for r in rows]

    def avg_duration(self):
        """平均制作时长（秒，仅成功单；无数据返回 None）。"""
        rows = self._query(
            "SELECT AVG(duration_sec) FROM orders"
            " WHERE result = 'success' AND duration_sec IS NOT NULL",
            default=[(None,)])
        v = rows[0][0] if rows else None
        return round(v, 1) if v is not None else None

    def fail_reasons(self):
        """失败原因计数（设备故障次数按 fail_reason 归类；空串归为 '未知'）。"""
        rows = self._query(
            "SELECT fail_reason, COUNT(*) FROM orders WHERE result = 'failed'"
            " GROUP BY fail_reason ORDER BY COUNT(*) DESC", default=[])
        return {(r[0] or "未知"): r[1] for r in rows}

    def recent(self, limit=50):
        """最近订单（新到旧），管理后台订单历史用。"""
        rows = self._query(
            "SELECT ts, order_id, drink_id, drink_name, qty, total,"
            "       duration_sec, result, fail_reason, mode"
            " FROM orders ORDER BY id DESC LIMIT ?", (limit,), default=[])
        keys = ("ts", "order_id", "drink_id", "drink_name", "qty", "total",
                "duration_sec", "result", "fail_reason", "mode")
        return [dict(zip(keys, r)) for r in rows]

    def summary(self):
        """/api/admin/stats 用的一揽子统计。"""
        today = self.today_count()
        success = self.today_success_count()
        return {
            "disabled": self._disabled,      # True 表示统计库降级中（仅日志）
            "today_count": today,
            "today_success": success,
            "success_rate": round(success / today, 3) if today else None,
            "avg_duration_sec": self.avg_duration(),
            "top_drinks": self.top_drinks(),
            "fail_reasons": self.fail_reasons(),
            "recent": self.recent(50),
        }


# 自测：python3 stats.py（临时库造 5 单混合结果，打印聚合）
if __name__ == "__main__":
    import tempfile
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    st = Stats(db)
    base = {"qty": 1, "total": 15.0, "_drink_id": 1, "drink": "美式"}
    for i in range(3):      # 3 单成功
        st.record(dict(base, order_id=i + 1, drink="美式" if i < 2 else "拿铁",
                       _drink_id=1 if i < 2 else 2), "success", duration_sec=19.0 + i)
    st.record(dict(base, order_id=4), "failed", fail_reason="机械臂超时", duration_sec=30.0)
    st.record(dict(base, order_id=5), "cancelled")
    import json
    print(json.dumps(st.summary(), ensure_ascii=False, indent=2))
    assert st.today_count() == 5 and st.today_success_count() == 3
    assert st.avg_duration() == 20.0
    assert st.fail_reasons() == {"机械臂超时": 1}
    print("stats 自测 OK")
