#!/usr/bin/env python3
# fsm.py —— 自主咖啡师主控（6 自由度总线舵机机械臂 + 视觉定位）
# 子命令：
#   simulate        仿真模式：mock 舵机总线 + mock 视觉，跑完整流程，退出码 0
#   run             真机执行完整咖啡流程（状态机）
#   teach <姿态名>  手掰示教：卸力 -> 手摆 -> 读回写入 poses.json -> 恢复上力
#   check           开机自检：扫总线、读反馈、试采摄像头
# 通用选项：--config 指定 config.json，--poses 指定 poses.json

import argparse
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOINT_NAMES = ["J1", "J2", "J3", "J4", "J5", "J6"]   # J6 为夹爪
ARM_JOINTS = JOINT_NAMES[:5]                          # 臂部关节（不含夹爪）

# TASK 28：log() 转发统一结构化日志（projects/common）；导入失败回退原 print。
# 控制台格式保持 `[HH:MM:SS] [TAG] 消息` 不变（kiosk 真机模式正则解析依赖）。
try:
    _ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from projects.common.structured_log import make_logger as _make_logger
    _slog = _make_logger("fsm")
except Exception:
    _slog = None


def log(tag, msg):
    if _slog is not None:
        _slog(tag, msg)
    else:
        print(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}", flush=True)


class ArmError(Exception):
    """机械臂动作失败（写失败 / 回读超差 / 视觉失败），触发 ERROR 状态。"""


# ---------- 配置与姿态 ----------

def _resolve(path):
    """相对路径一律相对本脚本目录解析。"""
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def load_config(path):
    with open(_resolve(path), "r", encoding="utf-8") as f:
        return json.load(f)


def load_poses(path):
    with open(_resolve(path), "r", encoding="utf-8") as f:
        return json.load(f)


def save_poses(path, poses):
    with open(_resolve(path), "w", encoding="utf-8") as f:
        json.dump(poses, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------- 舵机总线 ----------

def open_bus(cfg):
    """按 config 打开真实舵机总线，失败抛异常（调用方负责友好提示）。"""
    from sts import BusServo
    return BusServo(cfg["serial_port"], cfg["baud_rate"])


class MockBus:
    """仿真舵机总线：接口与 BusServo 一致，位置即时到位并记录，供回读校验。"""

    def __init__(self, joint_ids):
        self.pos = {sid: 2048 for sid in joint_ids.values()}
        self.torque_on = {sid: True for sid in joint_ids.values()}

    def ping(self, sid):
        ok = sid in self.pos
        log("MOCK", f"ping 舵机 {sid} -> {'在线' if ok else '无应答'}")
        return ok

    def scan(self, id_min=0, id_max=253):
        return sorted(sid for sid in self.pos if id_min <= sid <= id_max)

    def read_position(self, sid):
        return self.pos.get(sid)

    def read_feedback(self, sid):
        if sid not in self.pos:
            return None
        return {"pos": self.pos[sid], "speed": 0, "load": 0,
                "voltage": 12.0, "temp": 36, "moving": 0}

    def write_position(self, sid, pos, speed=0, time_ms=0):
        if sid not in self.pos or not (0 <= pos <= 4095):
            return False
        log("MOCK", f"舵机 {sid} 目标位置 -> {pos} (speed={speed}, time={time_ms}ms)")
        self.pos[sid] = pos          # 仿真：立即到位
        return True

    def torque(self, sid, on):
        if sid not in self.pos:
            return False
        log("MOCK", f"舵机 {sid} {'上力' if on else '卸力'}")
        self.torque_on[sid] = on
        return True


def open_switches(cfg, mock=False):
    """按 config['actuators'] 创建 WiFi 开关（type=wifi 的项），返回 {名称: 开关对象}。
    磨豆机/滴滤机为 WiFi 插座或点动继电器控制时，由 _operate_machine 使用。"""
    from wifi_switch import make_switch
    switches = {}
    for key, act in cfg.get("actuators", {}).items():
        if act.get("type") == "wifi":
            switches[key] = make_switch(key, act, mock=mock)
    return switches


# ---------- 视觉 ----------

class MockVision:
    """仿真视觉：返回固定杯位（像素 + 台面坐标）。"""

    def locate_cup(self):
        log("MOCK", "摄像头采帧 -> 检测到杯口圆 (u=960.0, v=540.0, r=85.0)")
        return {"u": 960.0, "v": 540.0, "x_mm": 152.3, "y_mm": 88.6}


class RealVision:
    """真实视觉：复用 ../vision 的 cup_detect + hand_eye_calib。"""

    def __init__(self, cfg):
        vision_dir = _resolve(cfg["vision_dir"])
        if vision_dir not in sys.path:
            sys.path.insert(0, vision_dir)
        from cup_detect import grab_frame, detect_cup   # noqa: 延迟导入，无 cv2 时只在真机暴露
        from hand_eye_calib import load_calib, apply_homography
        self._grab_frame = grab_frame
        self._detect_cup = detect_cup
        self._apply_homography = apply_homography
        self.device = cfg["camera_device"]
        self.hough = (cfg["hough_min_r"], cfg["hough_max_r"],
                      cfg["hough_param1"], cfg["hough_param2"])
        calib_path = _resolve(cfg["calib_file"])
        if os.path.exists(calib_path):
            self.H = load_calib(calib_path)
            log("VISION", f"已加载标定文件 {calib_path}")
        else:
            self.H = None
            log("VISION", f"警告：标定文件 {calib_path} 不存在，只输出像素坐标")

    def locate_cup(self):
        """找杯子，返回 {u,v,x_mm,y_mm}；无标定时 x_mm/y_mm 为 None；找不到返回 None。"""
        frame = self._grab_frame(self.device)
        if frame is None:
            return None
        _circles, best = self._detect_cup(frame, *self.hough)
        if best is None:
            return None
        u, v, _r = best
        x_mm = y_mm = None
        if self.H is not None:
            x_mm, y_mm = self._apply_homography(self.H, u, v)
        return {"u": u, "v": v, "x_mm": x_mm, "y_mm": y_mm}


# ---------- 姿态运动 ----------

def move_pose(bus, cfg, name, pose, deltas=None, skip=(), fast=False):
    """执行一个命名姿态：逐关节写目标位置 -> 等待 time_ms -> 回读校验。
    deltas: 视觉纠偏量 {关节名: 步数增量}；skip: 本步不动的关节名。
    任何一步失败抛 ArmError。"""
    deltas = deltas or {}
    speed = pose.get("speed", cfg["default_speed"])
    time_ms = cfg["default_time_ms"]
    tol = cfg["position_tolerance"]
    ids = cfg["joint_ids"]
    log("MOVE", f"姿态 [{name}] speed={speed} time={time_ms}ms"
                + (f" 纠偏={deltas}" if deltas else ""))

    targets = {}
    for jname in ARM_JOINTS:
        if jname in skip:
            continue
        target = pose["joints"][jname] + deltas.get(jname, 0)
        target = max(0, min(4095, target))
        targets[jname] = target
        # 多关节并发发送：全部发出后统一等待
        if not bus.write_position(ids[jname], target, speed, time_ms):
            raise ArmError(f"姿态 {name}: 舵机 {ids[jname]}({jname}) 写位置失败")

    # 夹爪：open/close 用 config 里的张合位置，hold 不动
    grip = pose.get("gripper", "hold")
    if grip not in ("open", "close", "hold"):
        raise ArmError(f"姿态 {name}: 未知 gripper 值 {grip}")
    if grip != "hold":
        gpos = cfg["gripper_open_pos"] if grip == "open" else cfg["gripper_close_pos"]
        targets["J6"] = gpos
        if not bus.write_position(ids["J6"], gpos, speed, time_ms):
            raise ArmError(f"姿态 {name}: 夹爪舵机 {ids['J6']} 写位置失败")

    # 等待运动完成（仿真模式缩短等待）
    time.sleep(min(time_ms, 200) / 1000.0 if fast else time_ms / 1000.0)

    # 回读校验
    for jname, target in targets.items():
        sid = ids[jname]
        cur = bus.read_position(sid)
        if cur is None:
            raise ArmError(f"姿态 {name}: 舵机 {sid}({jname}) 回读失败")
        err = abs(cur - target)
        if err > tol:
            raise ArmError(
                f"姿态 {name}: {jname}(舵机{sid}) 位置误差 {err} 超容差 {tol}"
                f"（目标 {target} 实际 {cur}）")
        log("CHECK", f"{jname}(舵机{sid}) 目标 {target} 实际 {cur} 误差 {err} OK")


# ---------- 咖啡流程状态机 ----------

class CoffeeFSM:
    """IDLE -> LOCATE_CUP -> PICK_CUP -> PLACE_CUP -> PRESS_GRINDER
       -> POUR_GROUNDS -> PRESS_BREWER -> WAIT_BREW -> SERVE -> IDLE"""

    def __init__(self, bus, vision, cfg, poses, fast=False, actuators=None):
        self.bus = bus
        self.vision = vision
        self.cfg = cfg
        self.poses = poses
        self.fast = fast                # 仿真模式：缩短等待/冲泡时间
        self.actuators = actuators or {}  # WiFi 开关 {"grinder": ..., "brewer": ...}
        self.state = "IDLE"
        self.deltas = {}                # 视觉纠偏量

    def set_state(self, s):
        log("FSM", f"状态转换 {self.state} -> {s}")
        self.state = s

    def run(self):
        try:
            self._flow()
        except ArmError as e:
            # ERROR 状态：全部卸力，打印故障步骤，退出码非 0
            self.set_state("ERROR")
            log("ERROR", f"流程中止于状态 {self.state}，故障：{e}")
            self._safe_shutdown()
            return 1
        return 0

    def _flow(self):
        cfg = self.cfg

        log("FSM", "===== 咖啡流程开始 =====")
        move_pose(self.bus, cfg, "idle", self.poses["idle"], fast=self.fast)

        # LOCATE_CUP：视觉找杯子，算台面坐标与参考位偏差
        self.set_state("LOCATE_CUP")
        cup = self.vision.locate_cup()
        if cup is None:
            raise ArmError("LOCATE_CUP: 未找到杯子（采帧失败或检测无圆）")
        log("VISION", f"杯口圆心 像素({cup['u']:.1f}, {cup['v']:.1f})")
        if cup["x_mm"] is None:
            log("VISION", "警告：无标定，跳过台面坐标换算与纠偏")
        else:
            dx = cup["x_mm"] - cfg["cup_ref_x_mm"]
            dy = cup["y_mm"] - cfg["cup_ref_y_mm"]
            log("VISION", f"台面坐标 ({cup['x_mm']:.1f}, {cup['y_mm']:.1f}) mm，"
                          f"相对参考位偏差 dx={dx:+.1f} dy={dy:+.1f} mm")
            self.deltas = {
                "J1": int(round(dx * cfg["correct_j1_steps_per_mm"])),
                "J2": int(round(dy * cfg["correct_j2_steps_per_mm"])),
            }

        # PICK_CUP：按 cup_pick 姿态 + 视觉纠偏量下抓，夹爪闭合，回读校验
        self.set_state("PICK_CUP")
        move_pose(self.bus, cfg, "cup_pick", self.poses["cup_pick"],
                  deltas=self.deltas, fast=self.fast)

        # PLACE_CUP：放杯到出水口下方，夹爪张开
        self.set_state("PLACE_CUP")
        move_pose(self.bus, cfg, "cup_place", self.poses["cup_place"], fast=self.fast)

        # PRESS_GRINDER：操作磨豆机（WiFi 或舵机按键），保持片刻后回 idle
        self.set_state("PRESS_GRINDER")
        self._operate_machine("grinder", "grinder_press", "磨豆机")

        # POUR_GROUNDS：取粉杯 -> 移到滤篮上方 -> 腕旋转(J5)分 3 步慢倒
        self.set_state("POUR_GROUNDS")
        move_pose(self.bus, cfg, "grounds_pick", self.poses["grounds_pick"],
                  fast=self.fast)
        pour = self.poses["grounds_pour"]
        move_pose(self.bus, cfg, "grounds_pour(到位,J5保持)", pour,
                  skip=("J5",), fast=self.fast)
        self._pour_wrist(pour)
        move_pose(self.bus, cfg, "idle", self.poses["idle"], fast=self.fast)

        # PRESS_BREWER：操作滴滤机（WiFi 或舵机按键）
        self.set_state("PRESS_BREWER")
        self._operate_machine("brewer", "brewer_press", "滴滤机")

        # WAIT_BREW：冲泡等待，倒计时打印
        self.set_state("WAIT_BREW")
        total = cfg["simulate_brew_sec"] if self.fast else cfg["brew_wait_sec"]
        for left in range(total, 0, -1):
            log("BREW", f"冲泡中... 剩余 {left}s / 共 {total}s")
            time.sleep(1)

        # SERVE：递杯出品，夹爪张开
        self.set_state("SERVE")
        move_pose(self.bus, cfg, "serve", self.poses["serve"], fast=self.fast)

        # 回到 IDLE
        self.set_state("IDLE")
        move_pose(self.bus, cfg, "idle", self.poses["idle"], fast=self.fast)
        log("FSM", "===== 咖啡流程完成 =====")

    def _operate_machine(self, key, servo_pose, label):
        """操作一台电器（磨豆机/滴滤机）。
        config['actuators'][key] type=wifi 时走 WiFi 开关（插座/点动继电器），
        否则回退为机械臂舵机按键姿态。"""
        act = self.cfg.get("actuators", {}).get(key, {"type": "servo"})
        if act.get("type") == "wifi":
            sw = self.actuators.get(key)
            if sw is None:
                raise ArmError(f"{key}: 配置了 WiFi 控制但开关未初始化")
            mode = act.get("mode", "press")
            if mode == "press":
                # 点动型：继电器吸合模拟按一次键（电子按键的机器）
                sec = act.get("press_sec", 1.0)
                sw.press(0.05 if self.fast else sec)
            else:
                # 电源型：插座通电=开工，运行 run_sec 后断电（机械锁定开关的机器）
                sw.set_power(True)
                self._hold(act.get("run_sec", 10.0), f"{label}运行中（WiFi 插座供电）")
                sw.set_power(False)
            log("WIFI", f"{label} 已通过 WiFi 开关操作完成（{act.get('driver', '?')}/{mode}）")
            return
        # 默认：机械臂按键姿态
        move_pose(self.bus, self.cfg, servo_pose, self.poses[servo_pose], fast=self.fast)
        self._hold(1.0, f"保持按压{label}键")
        move_pose(self.bus, self.cfg, "idle", self.poses["idle"], fast=self.fast)

    def _pour_wrist(self, pour):
        """腕旋转(J5)分步慢倒：当前位置 -> 姿态目标位置，分 pour_steps 步。"""
        cfg = self.cfg
        sid = cfg["joint_ids"]["J5"]
        steps = cfg["pour_steps"]
        step_time = cfg["pour_step_time_ms"]
        speed = cfg["pour_speed"]
        start = self.bus.read_position(sid)
        if start is None:
            raise ArmError(f"POUR_GROUNDS: 舵机 {sid}(J5) 回读失败")
        end = pour["joints"]["J5"]
        tol = cfg["position_tolerance"]
        for i in range(1, steps + 1):
            target = int(round(start + (end - start) * i / steps))
            log("POUR", f"倒粉第 {i}/{steps} 步：J5 -> {target}")
            if not self.bus.write_position(sid, target, speed, step_time):
                raise ArmError(f"POUR_GROUNDS: 舵机 {sid}(J5) 写位置失败")
            time.sleep(min(step_time, 200) / 1000.0 if self.fast else step_time / 1000.0)
            cur = self.bus.read_position(sid)
            if cur is None:
                raise ArmError(f"POUR_GROUNDS: 舵机 {sid}(J5) 回读失败")
            if abs(cur - target) > tol:
                raise ArmError(
                    f"POUR_GROUNDS: J5 位置误差 {abs(cur - target)} 超容差 {tol}"
                    f"（目标 {target} 实际 {cur}）")
        log("POUR", "倒粉完成")

    def _hold(self, sec, msg):
        log("HOLD", msg)
        time.sleep(min(sec, 0.2) if self.fast else sec)

    def _safe_shutdown(self):
        """ERROR 状态收尾：全部关节卸力，防止憋电机。"""
        for jname in JOINT_NAMES:
            sid = self.cfg["joint_ids"][jname]
            try:
                self.bus.torque(sid, False)
            except Exception:
                pass
        log("ERROR", "全部关节已卸力")


# ---------- 子命令 ----------

def cmd_simulate(args):
    cfg = load_config(args.config)
    poses = load_poses(args.poses)
    log("SIM", "仿真模式：mock 舵机总线 + mock 视觉 + mock WiFi 开关（无硬件）")
    bus = MockBus(cfg["joint_ids"])
    vision = MockVision()
    switches = open_switches(cfg, mock=True)
    return CoffeeFSM(bus, vision, cfg, poses, fast=True, actuators=switches).run()


def cmd_run(args):
    cfg = load_config(args.config)
    poses = load_poses(args.poses)
    try:
        bus = open_bus(cfg)
    except Exception as e:
        log("ERROR", f"无法打开串口 {cfg['serial_port']}: {e}")
        log("ERROR", "请检查：舵机转接板是否接好、serial_port 配置是否正确、权限是否足够")
        return 2
    try:
        vision = RealVision(cfg)
    except Exception as e:
        log("ERROR", f"视觉模块初始化失败: {e}")
        bus.close()
        return 2
    try:
        switches = open_switches(cfg, mock=False)
    except Exception as e:
        log("ERROR", f"WiFi 开关初始化失败: {e}（检查 actuators 配置）")
        bus.close()
        return 2
    try:
        return CoffeeFSM(bus, vision, cfg, poses, fast=False, actuators=switches).run()
    finally:
        bus.close()


def cmd_teach(args):
    cfg = load_config(args.config)
    poses = load_poses(args.poses)
    if args.name not in poses:
        log("ERROR", f"未知姿态 {args.name}，可选：{', '.join(poses.keys())}")
        return 2
    try:
        bus = open_bus(cfg)
    except Exception as e:
        log("ERROR", f"无法打开串口 {cfg['serial_port']}: {e}")
        return 2
    ids = cfg["joint_ids"]
    try:
        # 全部关节卸力，允许手掰
        for jname in JOINT_NAMES:
            bus.torque(ids[jname], False)
        log("TEACH", "全部关节已卸力")
        input("请手摆机械臂到目标姿态，完成后按回车继续...")
        # 读当前所有关节位置
        joints = {}
        for jname in JOINT_NAMES:
            pos = bus.read_position(ids[jname])
            if pos is None:
                log("ERROR", f"舵机 {ids[jname]}({jname}) 回读失败，姿态未保存")
                return 1
            joints[jname] = pos
            log("TEACH", f"{jname}(舵机{ids[jname]}) 当前位置 {pos}")
        poses[args.name]["joints"] = joints
        save_poses(args.poses, poses)
        log("TEACH", f"姿态 [{args.name}] 已写入 {args.poses}")
        return 0
    except (EOFError, KeyboardInterrupt):
        log("TEACH", "已取消，姿态未保存")
        return 1
    finally:
        # 无论成败都恢复上力
        for jname in JOINT_NAMES:
            try:
                bus.torque(ids[jname], True)
            except Exception:
                pass
        log("TEACH", "全部关节已恢复上力")
        bus.close()


def cmd_check(args):
    cfg = load_config(args.config)
    ok = True
    print("===== 开机自检 =====")

    # 1. 舵机总线
    print(f"-- 舵机总线 {cfg['serial_port']} @ {cfg['baud_rate']}")
    bus = None
    try:
        bus = open_bus(cfg)
    except Exception as e:
        print(f"   [NG] 无法打开串口: {e}")
        print("        请检查转接板接线、serial_port 配置与串口权限")
        ok = False
    if bus is not None:
        try:
            bus.timeout_ms = 30     # 自检用较短超时，避免扫总线太久
            online = bus.scan()
            print(f"   总线扫描 0~253，在线舵机: {online if online else '无'}")
            ids = cfg["joint_ids"]
            for jname in JOINT_NAMES:
                sid = ids[jname]
                fb = bus.read_feedback(sid)
                if fb is None:
                    print(f"   [NG] {jname}(舵机{sid}) 无应答")
                    ok = False
                else:
                    print(f"   [OK] {jname}(舵机{sid}) 位置 {fb['pos']}"
                          f" 电压 {fb['voltage']:.1f}V 温度 {fb['temp']}°C"
                          f" 负载 {fb['load']} moving {fb['moving']}")
            missing = [j for j in JOINT_NAMES if ids[j] not in online]
            if missing:
                print(f"   [NG] 配置关节未在线: {', '.join(missing)}")
                ok = False
        finally:
            bus.close()

    # 2. 摄像头
    print(f"-- 摄像头设备 {cfg['camera_device']}")
    try:
        vision_dir = _resolve(cfg["vision_dir"])
        if vision_dir not in sys.path:
            sys.path.insert(0, vision_dir)
        from cup_detect import grab_frame
        frame = grab_frame(cfg["camera_device"])
        if frame is None:
            print("   [NG] 采帧失败（设备不存在或无法读取）")
            ok = False
        else:
            h, w = frame.shape[:2]
            print(f"   [OK] 采帧成功，分辨率 {w}x{h}")
    except Exception as e:
        print(f"   [NG] 视觉模块不可用: {e}")
        ok = False

    # 3. 标定文件
    calib_path = _resolve(cfg["calib_file"])
    if os.path.exists(calib_path):
        print(f"-- 标定文件 [OK] {calib_path}")
    else:
        print(f"-- 标定文件 [提示] {calib_path} 不存在，run 前请先标定"
              f"（见 docs/modules/vision.md）")

    print(f"===== 自检{'通过' if ok else '未通过'} =====")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(prog="fsm.py", description="自主咖啡师主控")
    ap.add_argument("--config", default="config.json", help="配置文件路径")
    ap.add_argument("--poses", default="poses.json", help="姿态库路径")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("simulate", help="仿真模式（无硬件跑完整流程）")
    sub.add_parser("run", help="真机执行咖啡流程")
    p_teach = sub.add_parser("teach", help="手掰示教录制姿态")
    p_teach.add_argument("name", help="姿态名（poses.json 中的键）")
    sub.add_parser("check", help="开机自检")
    args = ap.parse_args()

    handlers = {"simulate": cmd_simulate, "run": cmd_run,
                "teach": cmd_teach, "check": cmd_check}
    sys.exit(handlers[args.cmd](args))


if __name__ == "__main__":
    main()
