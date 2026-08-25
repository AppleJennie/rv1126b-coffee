#!/usr/bin/env python3
# system_monitor.py —— 系统性能监控（TASK 29）
#
# 纯 Python3 标准库，只读 /proc 与 /sys 标准接口，RV1126B 板端与开发 VM 通用：
#   CPU    /proc/stat 两次采样差值（busy/total）
#   内存   /proc/meminfo（MemTotal / MemAvailable）
#   负载   /proc/loadavg
#   温度   /sys/class/thermal/thermal_zone*/temp（没有该节点就跳过）
#   NPU    /sys/class/devfreq/*npu* 的 cur_freq/load（没有就报 n/a）
#   Web    可选 --web URL，用 urllib 测 HTTP 响应时间（不许装 requests）
#
# 用法：
#   python3 tools/system_monitor.py                  # 每 2s 打印一行
#   python3 tools/system_monitor.py --interval 5     # 改采样周期
#   python3 tools/system_monitor.py --once           # 只打印一次
#   python3 tools/system_monitor.py --json           # JSON 输出（周期模式每行一个 JSON）
#   python3 tools/system_monitor.py --web http://127.0.0.1:8080/api/status

import argparse
import glob
import json
import os
import time
import urllib.request


# ---------- /proc /sys 采样 ----------

def read_cpu_times():
    """读 /proc/stat 的 cpu 汇总行，返回 (busy, total) 累计 jiffies。"""
    with open("/proc/stat", "r", encoding="utf-8") as f:
        parts = f.readline().split()
    # cpu user nice system idle iowait irq softirq steal ...
    vals = [int(x) for x in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
    total = sum(vals)
    return total - idle, total


def cpu_percent(prev, cur):
    """两次采样差值算 CPU 使用率（%）。采样间隔过近返回 0.0。"""
    busy_d = cur[0] - prev[0]
    total_d = cur[1] - prev[1]
    if total_d <= 0:
        return 0.0
    return round(100.0 * busy_d / total_d, 1)


def read_meminfo():
    """读 /proc/meminfo，返回 {total_mb, avail_mb, used_mb, used_percent}。"""
    info = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if parts:
                info[parts[0].rstrip(":")] = int(parts[1])   # 单位 kB
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = total - avail
    pct = round(100.0 * used / total, 1) if total else 0.0
    return {"total_mb": total // 1024, "avail_mb": avail // 1024,
            "used_mb": used // 1024, "used_percent": pct}


def read_loadavg():
    """读 /proc/loadavg：1/5/15 分钟负载 + 运行中/总任务数。"""
    with open("/proc/loadavg", "r", encoding="utf-8") as f:
        parts = f.read().split()
    run, _, total = parts[3].partition("/")
    return {"load1": float(parts[0]), "load5": float(parts[1]),
            "load15": float(parts[2]),
            "running": int(run), "tasks": int(total or 0)}


def read_temps():
    """读全部 thermal_zone 温度（°C）。没有温度节点（如部分 VM）返回空列表。"""
    temps = []
    for path in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                milli = int(f.read().strip())
            zone_dir = os.path.dirname(path)
            try:
                type_path = os.path.join(zone_dir, "type")
                with open(type_path, "r", encoding="utf-8") as f:
                    ztype = f.read().strip()
            except OSError:
                ztype = os.path.basename(zone_dir)
            temps.append({"zone": os.path.basename(zone_dir),
                          "type": ztype, "celsius": round(milli / 1000.0, 1)})
        except (OSError, ValueError):
            continue                      # 个别节点读失败不影响其他
    return temps


def read_npu():
    """探测 RK NPU 运行状态（devfreq 电流频率/负载）。探测不到返回 None。"""
    for path in sorted(glob.glob("/sys/class/devfreq/*npu*")):
        info = {"node": os.path.basename(path)}
        for key in ("cur_freq", "load"):
            try:
                with open(os.path.join(path, key), "r", encoding="utf-8") as f:
                    info[key] = f.read().strip()
            except OSError:
                pass
        return info
    return None


def web_timing(url, timeout=5.0):
    """测 Web 接口响应时间（urllib，标准库）。失败返回 ok=False 与错误说明。"""
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            resp.read()
            code = resp.getcode()
        return {"url": url, "ok": 200 <= code < 400, "code": code,
                "ms": round((time.monotonic() - t0) * 1000.0, 1)}
    except Exception as e:
        return {"url": url, "ok": False, "code": None,
                "ms": round((time.monotonic() - t0) * 1000.0, 1),
                "error": str(e)}


# ---------- 汇总与输出 ----------

def collect(prev_cpu, web_url=None):
    """采集一轮完整指标。prev_cpu 为上次 (busy, total)，返回 (报告 dict, 本次采样)。"""
    cur_cpu = read_cpu_times()
    rep = {
        "ts": int(time.time()),
        "time": time.strftime("%H:%M:%S"),
        "cpu_percent": cpu_percent(prev_cpu, cur_cpu),
        "ram": read_meminfo(),
        "load": read_loadavg(),
        "temps": read_temps(),
        "npu": read_npu(),
    }
    if web_url:
        rep["web"] = web_timing(web_url)
    return rep, cur_cpu


def format_text(rep):
    """单行文本格式：一眼扫完所有指标。"""
    ram = rep["ram"]
    load = rep["load"]
    parts = [
        f"[{rep['time']}]",
        f"CPU {rep['cpu_percent']}%",
        f"RAM {ram['used_percent']}% ({ram['used_mb']}/{ram['total_mb']} MB)",
        f"Load {load['load1']} {load['load5']} {load['load15']}",
    ]
    if rep["temps"]:
        parts.append("Temp " + ", ".join(f"{t['type']} {t['celsius']}°C"
                                         for t in rep["temps"]))
    else:
        parts.append("Temp n/a")
    npu = rep["npu"]
    if npu:
        freq = npu.get("cur_freq", "?")
        parts.append(f"NPU {npu['node']} freq={freq}"
                     + (f" load={npu['load']}" if npu.get("load") else ""))
    else:
        parts.append("NPU n/a")
    web = rep.get("web")
    if web:
        if web["ok"]:
            parts.append(f"Web {web['ms']}ms({web['code']})")
        else:
            parts.append(f"Web FAIL({web.get('error', '?')})")
    return " | ".join(parts)


def main():
    ap = argparse.ArgumentParser(prog="system_monitor.py",
                                 description="系统性能监控（/proc /sys，板端/VM 通用）")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="采样周期秒数（默认 2）")
    ap.add_argument("--once", action="store_true", help="只采样打印一次后退出")
    ap.add_argument("--json", action="store_true",
                    help="JSON 输出（周期模式每行一个 JSON 对象）")
    ap.add_argument("--web", default=None, metavar="URL",
                    help="可选：顺带测该 URL 的 HTTP 响应时间"
                         "（如 http://127.0.0.1:8080/api/status）")
    args = ap.parse_args()

    prev = read_cpu_times()
    while True:
        # CPU 使用率依赖两次采样差值：--once 也用 0.2s 短间隔取一次真实差值
        time.sleep(0.2 if args.once else max(0.2, args.interval))
        rep, prev = collect(prev, args.web)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False), flush=True)
        else:
            print(format_text(rep), flush=True)
        if args.once:
            break


if __name__ == "__main__":
    main()
