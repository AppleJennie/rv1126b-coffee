# test_productization.py —— kiosk 产品化批次（TASK 9/25/35）单元测试
#
# 覆盖：
#   1. TASK 25 watchdog：模拟制作线程卡死（把当前订单 _step_ts 拨回 200s 前），
#      断言 watchdog.check_once() 报出"制作卡死"+"长期 BUSY"；拨回正常后断言恢复
#   2. TASK 25 watchdog：健康巡检线程活性（伪造 HealthManager 快照时间戳超龄）
#   3. TASK 35 stats：5 单混合结果落库，断言聚合（今日数/成功率/TOP/平均时长/失败原因）
#   4. TASK 35 stats：DB 文件损坏/路径无权限时不崩（降级仅日志，summary 返回安全默认）
#   5. TASK 9 EventBus：事件带单调递增 id；replay_since 重放缺口
#
# 运行：python3 projects/kiosk_server/test_productization.py（纯标准库，约 3s）

import os
import sys
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import kiosk_server as ks          # noqa: E402
from stats import Stats            # noqa: E402
from watchdog import Watchdog      # noqa: E402

_MENU = {"drinks": [{"id": 1, "name": "美式", "price": 15, "category": "经典咖啡", "ice": True},
                    {"id": 2, "name": "拿铁", "price": 19, "category": "奶咖", "ice": True}]}

_passed = 0


def ok(cond, name):
    global _passed
    assert cond, f"FAIL: {name}"
    _passed += 1
    print(f"  [OK] {name}")


# ---------- 1/2. TASK 25 watchdog ----------
def test_watchdog_stuck():
    print("== TASK 25 watchdog 卡死检测 ==")
    # 缩短仿真步长，让测试单快点做完（不改动模块常量，仅本进程内生效）
    ks.SIM_STEP_SEC = [0.1, 0.1, 0.1, 0.1]
    ks.SIM_TOTAL_SEC = sum(ks.SIM_STEP_SEC)
    bus = ks.EventBus()
    mgr = ks.OrderManager(bus, simulate=True, menu=dict(_MENU))
    try:
        order, err = mgr.place_order({"drink_id": 1, "opts": {}, "qty": 1})
        assert err is None
        # 等 worker 接单进入制作
        deadline = time.time() + 5
        while mgr.current is None and time.time() < deadline:
            time.sleep(0.05)
        assert mgr.current is not None, "订单未被 worker 接单"
        # 伪造卡死：当前步骤停留 200s、制作总时长 400s
        with mgr._lock:
            mgr.current["_step_ts"] = time.time() - 200
            mgr.current["_start_ts"] = time.time() - 400
        wd = Watchdog(mgr=mgr, health=None, port=None,
                      order_stuck_sec=120, busy_sec=300)
        reasons = wd.check_once()
        ok(any("制作卡死" in r for r in reasons), "检出制作卡死（步骤停留超阈值）")
        ok(any("长期 BUSY" in r for r in reasons), "检出设备长期 BUSY")
        ok(wd.section()["state"] == "degraded", "watchdog 状态标记 degraded")
        # 恢复正常：步骤时间戳拨回现在
        with mgr._lock:
            mgr.current["_step_ts"] = time.time()
            mgr.current["_start_ts"] = time.time()
        reasons = wd.check_once()
        ok(reasons == [] and wd.section()["state"] == "ok", "恢复后 watchdog 回到 ok")
    finally:
        time.sleep(0.6)     # 让仿真单跑完，线程不落井下石（daemon 线程随进程退出）


def test_watchdog_health_stale():
    print("== TASK 25 watchdog 健康巡检活性 ==")

    class FakeHealth:          # 伪造巡检线程停转：各项 ts 停在 30s 前
        def snapshot(self):
            old = time.time() - 30
            return {"items": {"arm": {"ts": old}, "coffee": {"ts": old}}}

    wd = Watchdog(mgr=None, health=FakeHealth(), port=None, health_stale_sec=10)
    reasons = wd.check_once()
    ok(any("健康巡检线程无响应" in r for r in reasons), "检出健康巡检线程停转")

    class LiveHealth:          # 正常巡检：ts 刚更新
        def snapshot(self):
            return {"items": {"arm": {"ts": time.time()}}}

    wd2 = Watchdog(mgr=None, health=LiveHealth(), port=None, health_stale_sec=10)
    ok(wd2.check_once() == [], "健康巡检正常时不误报")


# ---------- 3/4. TASK 35 stats ----------
def test_stats_aggregate():
    print("== TASK 35 SQLite 统计聚合 ==")
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    st = Stats(db)
    base = {"qty": 1, "total": 15.0, "_drink_id": 1, "drink": "美式"}
    # 5 单混合：2 美式成功 + 1 拿铁成功 + 1 失败（机械臂超时）+ 1 取消
    st.record(dict(base, order_id=1), "success", duration_sec=19.0, mode="SIM")
    st.record(dict(base, order_id=2), "success", duration_sec=21.0, mode="SIM")
    st.record(dict(base, order_id=3, drink="拿铁", _drink_id=2, total=19.0),
              "success", duration_sec=20.0, mode="SIM")
    st.record(dict(base, order_id=4), "failed", fail_reason="机械臂超时",
              duration_sec=30.0, mode="SIM")
    st.record(dict(base, order_id=5), "cancelled", mode="SIM")
    ok(st.today_count() == 5, "今日订单数=5")
    ok(st.today_success_count() == 3, "今日成功=3")
    ok(abs(st.summary()["success_rate"] - 0.6) < 1e-6, "成功率=0.6")
    ok(st.avg_duration() == 20.0, "平均制作时长=20.0（仅成功单）")
    top = st.top_drinks()
    ok(top[0]["drink_name"] == "美式" and top[0]["count"] == 3, "TOP1=美式 x3")
    ok(st.fail_reasons() == {"机械臂超时": 1}, "失败原因归类（设备故障计数）")
    ok(len(st.recent(50)) == 5 and st.recent(50)[0]["order_id"] == 5, "最近订单新到旧")


def test_stats_degrade():
    print("== TASK 35 统计库损坏/无权限降级 ==")
    # 场景 A：路径指向一个目录（打不开）
    st = Stats(db_path=tempfile.mkdtemp())       # 传目录当文件用，必然打不开
    ok(st._disabled, "路径不可打开 -> 降级")
    ok(st.record({"order_id": 1}, "success") is False, "降级后写入静默失败")
    ok(st.today_count() == 0, "降级后查询返回安全默认值")
    # 场景 B：文件内容是垃圾（不是 sqlite 库）
    bad = os.path.join(tempfile.mkdtemp(), "bad.db")
    with open(bad, "wb") as f:
        f.write(b"this is not sqlite" * 64)
    st2 = Stats(bad)             # sqlite 惰性打开，首次读写才报 DatabaseError
    st2.record({"order_id": 1}, "success")        # 触发写 -> 异常 -> 降级
    ok(st2._disabled, "损坏库写入 -> 降级不崩")
    ok(st2.summary()["today_count"] == 0, "损坏库 summary 安全默认")


# ---------- 5. TASK 9 EventBus 环形缓冲 ----------
def test_eventbus_replay():
    print("== TASK 9 EventBus 环形缓冲与重放 ==")
    bus = ks.EventBus(history=10)
    for i in range(12):
        bus.publish({"type": "machine", "state": "ok", "n": i})
    ok(bus._seq == 12, "事件 id 单调递增")
    ok(len(bus._hist) == 10, "环形缓冲截断到 history")
    replayed = bus.replay_since(9)
    ok([e["n"] for e in replayed] == [9, 10, 11], "replay_since(9) 重放缺口")
    ok(bus.replay_since(0) == [], "last_id=0 不重放（只收快照+新流）")


if __name__ == "__main__":
    test_watchdog_stuck()
    test_watchdog_health_stale()
    test_stats_aggregate()
    test_stats_degrade()
    test_eventbus_replay()
    print(f"\n全部通过：{_passed} 项断言 OK")
