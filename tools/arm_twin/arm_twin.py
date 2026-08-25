#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""机械臂数字孪生（TASK 21）—— 纯 Python 二维平面臂仿真 + 可视化。

目的：真机到位前，在无显示器的开发 VM 上直观验证「语义动作序列」的
合法性与轨迹形态 —— 吃一段动作脚本（HOME→PICK_CUP→MOVE_TO BREWER→…），
逐动作输出 ASCII 轨迹帧，最后落一张 SVG 总览图（默认 /tmp，不污染仓库）。

模型（简化二维，侧视图）：
  底座在原点，三连杆平面臂：J2 肩 / J3 肘 / J4 腕（俯仰），
  J1 底座偏航在二维里只画成表盘读数，J5 腕旋不影响侧视轮廓（忽略）。
  舵机值 → 角度：0~4095 线性映射 -180°~+180°（中位 2048 = 0°）。
  角度约定：J2=0 指臂竖直向上，正角度向前（+x）倾；J3/J4 为相对上一节。

位姿来源：config/poses.yaml（与真机共用一份，teach 后自动准确）。
  当前 poses.yaml 的 joints 全是占位中位值（各 pose 相同），画出来姿态
  无差异；检测到这种占位情况时孪生打印警告并改用内置演示角度
  （DEMO_POSES_DEG，只用于可视化），真机 teach 后该回退自动失效。

语义动作与 holding 语义对齐 hardware/arm.py + hardware/sim.py：
  PICK_CUP（须空手→持杯）/ PLACE_CUP（须持杯→放空）/ SERVE（须持杯→放空）
  PICK_FINISHED_DRINK（须空手→持杯）/ POUR_GROUNDS / MOVE_TO <pose> / HOME
  RELEASE / EMERGENCY_STOP（幂等，之后一切动作被拒绝）/ RESET（解除急停）

验证点（tools/arm_twin/test_arm_twin.py 固化）：
  - 动作顺序合法：未 PICK_CUP 不能 PLACE_CUP / SERVE（TwinStateError）
  - 每动作有模拟耗时（基础时长 + 关节行程/转速），超 timeout 报 TwinTimeoutError
  - 急停插入后轨迹立即中止（aborted=True，后续动作不再产生轨迹帧）

用法：
  python3 tools/arm_twin/arm_twin.py --demo                 # 内置咖啡出品全流程
  python3 tools/arm_twin/arm_twin.py --script my_script.txt # 自定义动作脚本
  python3 tools/arm_twin/arm_twin.py --demo --out /tmp/xx   # 指定 SVG 输出目录
脚本格式：每行一个动作（# 开头为注释）：
  HOME | MOVE_TO <位姿名> | PICK_CUP | PLACE_CUP | POUR_GROUNDS |
  PICK_FINISHED_DRINK | SERVE | RELEASE | EMERGENCY_STOP | RESET
"""

import argparse
import math
import os
import sys

import yaml

# 仓库根目录（tools/arm_twin/arm_twin.py → 上三级）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DEFAULT_POSES_YAML = os.path.join(_REPO_ROOT, 'config', 'poses.yaml')
DEFAULT_OUT_DIR = '/tmp/arm_twin_out'     # 输出默认落 /tmp，不进 git

# ---- 模型常量 ----
LINK_LENS = (110.0, 90.0, 70.0)           # 三连杆长度 mm（演示比例）
SERVO_MID = 2048                          # 舵机中位
SERVO_SPAN = 4096                         # 0~4095 → -180°~+180°
SPEED_DPS = 120.0                         # 模拟转速（度/秒），算耗时用
FRAME_DT = 0.25                           # 轨迹帧间隔（模拟秒）

# 内置演示角度（J1/J2/J3/J4，度）——仅 poses.yaml 为占位值时启用
DEMO_POSES_DEG = {
    'HOME':          {'J1': 0, 'J2': 0,  'J3': 0,   'J4': 0},    # 竖直收起
    'CUP':           {'J1': 0, 'J2': 60, 'J3': 70,  'J4': -90},  # 前下方收拢取杯
    'BREWER':        {'J1': 0, 'J2': 35, 'J3': 25,  'J4': -50},  # 冲泡位
    'WATER':         {'J1': 0, 'J2': 25, 'J3': -15, 'J4': -30},  # 热水位（预留）
    'SERVE':         {'J1': 0, 'J2': 80, 'J3': -10, 'J4': -20},  # 大幅前伸递杯
    'GROUNDS_PICK':  {'J1': 0, 'J2': 50, 'J3': 45,  'J4': -70},  # 取粉杯
    'GROUNDS_POUR':  {'J1': 0, 'J2': 30, 'J3': 20,  'J4': -40},  # 滤篮上方倒粉
}

# 动作表：基础模拟耗时 / 超时 / 目标位姿（None=不动臂 / 'ARG'=MOVE_TO 参数）
ACTIONS = {
    'HOME':                {'base_s': 1.5, 'timeout_s': 8.0,  'pose': 'HOME'},
    'MOVE_TO':             {'base_s': 1.0, 'timeout_s': 8.0,  'pose': 'ARG'},
    'PICK_CUP':            {'base_s': 2.0, 'timeout_s': 10.0, 'pose': 'CUP'},
    'PLACE_CUP':           {'base_s': 2.0, 'timeout_s': 10.0, 'pose': 'BREWER'},
    'POUR_GROUNDS':        {'base_s': 2.5, 'timeout_s': 12.0, 'pose': 'GROUNDS_POUR'},
    'PICK_FINISHED_DRINK': {'base_s': 2.0, 'timeout_s': 10.0, 'pose': 'BREWER'},
    'SERVE':               {'base_s': 2.0, 'timeout_s': 10.0, 'pose': 'SERVE'},
    'RELEASE':             {'base_s': 0.5, 'timeout_s': 4.0,  'pose': None},
    'EMERGENCY_STOP':      {'base_s': 0.1, 'timeout_s': 2.0,  'pose': None},
    'RESET':               {'base_s': 1.0, 'timeout_s': 6.0,  'pose': 'HOME'},
}

# holding 前置条件与结果（对齐 hardware/sim.py 的 SimRobotArm 语义）
_NEEDS_EMPTY = ('PICK_CUP', 'PICK_FINISHED_DRINK')   # 须空手，成功后持杯
_NEEDS_HOLDING = ('PLACE_CUP', 'SERVE')              # 须持杯，成功后放空


class TwinError(Exception):
    """孪生异常基类。"""


class TwinStateError(TwinError):
    """动作顺序不合法（如未取杯就放杯）。"""


class TwinTimeoutError(TwinError):
    """动作模拟耗时超过 timeout。"""


class TwinEstopError(TwinError):
    """急停中拒绝动作。"""


def servo_to_deg(v):
    """舵机值（0~4095，中位 2048）→ 角度（-180°~+180°）。"""
    return (float(v) - SERVO_MID) * 360.0 / SERVO_SPAN


def load_poses(path=None):
    """从 config/poses.yaml 加载位姿，舵机值换算成角度。

    返回 (poses_deg, used_fallback)。poses.yaml 各 pose 的 J2/J3/J4 全部相同
    （占位中位值）时，打印警告并回退到内置演示角度 DEMO_POSES_DEG ——
    真机 teach 录制后 joints 产生差异，回退自动失效。
    """
    path = path or DEFAULT_POSES_YAML
    with open(path, 'r', encoding='utf-8') as f:
        doc = yaml.safe_load(f) or {}
    poses_deg = {}
    for name, node in doc.items():
        joints = (node or {}).get('joints') or {}
        poses_deg[name] = {j: servo_to_deg(joints.get(j, SERVO_MID))
                           for j in ('J1', 'J2', 'J3', 'J4')}
    # 占位检测：所有 pose 的平面关节角完全一样 → 画不出动作差异
    sigs = {(p['J2'], p['J3'], p['J4']) for p in poses_deg.values()}
    if len(sigs) <= 1:
        print('[arm_twin] %s 的 joints 全是占位中位值（各 pose 相同），'
              '孪生改用内置演示角度；真机 teach 后此回退自动失效' % path,
              file=sys.stderr)
        return {k: dict(v) for k, v in DEMO_POSES_DEG.items()}, True
    return poses_deg, False


def forward_kinematics(degs):
    """平面三连杆正运动学。degs 含 J1~J4（度）；J1 不进侧视平面。

    返回 (p0, p1, p2, p3)：底座/肩肘腕/末端点坐标（mm，y 向上）。
    """
    # J2=0 竖直向上 → 连杆1绝对角 90°；正角度前倾；J3/J4 相对累加
    a1 = math.radians(90.0 - degs['J2'])
    a2 = a1 - math.radians(degs['J3'])
    a3 = a2 - math.radians(degs['J4'])
    l1, l2, l3 = LINK_LENS
    p0 = (0.0, 0.0)
    p1 = (p0[0] + l1 * math.cos(a1), p0[1] + l1 * math.sin(a1))
    p2 = (p1[0] + l2 * math.cos(a2), p1[1] + l2 * math.sin(a2))
    p3 = (p2[0] + l3 * math.cos(a3), p2[1] + l3 * math.sin(a3))
    return (p0, p1, p2, p3)


class TwinArm(object):
    """数字孪生臂：语义动作 → 模拟耗时/超时检查 → 轨迹帧。

    timeouts: 可覆盖 ACTIONS 里的 timeout_s（测试注入超时用）。
    """

    def __init__(self, poses_deg, timeouts=None):
        self.poses = poses_deg
        self.timeouts = dict(timeouts or {})
        self.degs = dict(poses_deg['HOME'])   # 当前关节角（上电即在 HOME）
        self.pose = 'HOME'
        self.holding = False
        self.estopped = False
        self.ts = 0.0                          # 模拟时钟（秒）

    def _timeout(self, verb):
        return self.timeouts.get(verb, ACTIONS[verb]['timeout_s'])

    def act(self, verb, arg=None):
        """执行一个语义动作，返回本动作的轨迹帧列表。

        帧：{'t', 'action', 'degs', 'holding', 'points'}。
        不合法/超时/急停中分别抛 TwinStateError/TwinTimeoutError/TwinEstopError。
        """
        if verb not in ACTIONS:
            raise TwinStateError('未知动作 %r' % verb)
        if self.estopped and verb not in ('EMERGENCY_STOP', 'RESET'):
            raise TwinEstopError('急停中，拒绝 %s（先 RESET）' % verb)
        if verb in _NEEDS_EMPTY and self.holding:
            raise TwinStateError('%s 要求空手，当前持杯' % verb)
        if verb in _NEEDS_HOLDING and not self.holding:
            raise TwinStateError('%s 要求持杯，当前空手（需先取杯）' % verb)

        # 目标关节角：有 pose 的动作走到该位姿，其余原地
        spec = ACTIONS[verb]
        target_pose = arg if spec['pose'] == 'ARG' else spec['pose']
        if target_pose is not None:
            if target_pose not in self.poses:
                raise TwinStateError('未知位姿 %r' % target_pose)
            target = dict(self.poses[target_pose])
        else:
            target = dict(self.degs)

        # 模拟耗时 = 基础时长 + 最大关节行程 / 转速；超时即报错（不更新状态）
        travel = max(abs(target[j] - self.degs[j]) for j in ('J2', 'J3', 'J4'))
        duration = spec['base_s'] + travel / SPEED_DPS
        if duration > self._timeout(verb):
            raise TwinTimeoutError(
                '%s 模拟耗时 %.2fs 超过超时 %.2fs' % (verb, duration,
                                                      self._timeout(verb)))

        # 关节空间线性插值出轨迹帧
        n = max(2, int(math.ceil(duration / FRAME_DT)))
        frames = []
        start = dict(self.degs)
        for i in range(n + 1):
            r = i / float(n)
            degs = {j: start[j] + (target[j] - start[j]) * r
                    for j in ('J1', 'J2', 'J3', 'J4')}
            frames.append({'t': round(self.ts + duration * r, 3),
                           'action': verb if arg is None
                           else '%s %s' % (verb, arg),
                           'degs': degs, 'holding': self.holding,
                           'points': forward_kinematics(degs)})

        # 提交状态（holding 语义对齐 hardware/sim.py）
        self.ts += duration
        self.degs = target
        if target_pose is not None:
            self.pose = target_pose
        if verb in _NEEDS_EMPTY:
            self.holding = True
        elif verb in _NEEDS_HOLDING or verb == 'RELEASE':
            self.holding = False
        elif verb == 'EMERGENCY_STOP':
            self.estopped = True
            self.holding = False
        elif verb == 'RESET':
            self.estopped = False
        return frames


def parse_script(text):
    """解析动作脚本：每行 'VERB [ARG]'，# 注释与空行跳过。

    返回 [(verb, arg|None), ...]；语法错抛 TwinStateError。
    """
    out = []
    for ln, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        verb, arg = parts[0].upper(), (parts[1].upper() if len(parts) > 1 else None)
        if verb not in ACTIONS:
            raise TwinStateError('脚本第 %d 行：未知动作 %r' % (ln, verb))
        if verb == 'MOVE_TO' and arg is None:
            raise TwinStateError('脚本第 %d 行：MOVE_TO 缺位姿参数' % ln)
        out.append((verb, arg))
    return out


DEMO_SCRIPT = """\
# 咖啡出品全流程（语义动作序列）
HOME
MOVE_TO CUP
PICK_CUP
MOVE_TO BREWER
PLACE_CUP
POUR_GROUNDS
PICK_FINISHED_DRINK
MOVE_TO SERVE
SERVE
HOME
"""


def run_script(script_text, poses_deg):
    """跑一段动作脚本，返回结果 dict：

      frames   全部轨迹帧（急停后不再追加——轨迹立即中止）
      actions  [(verb, arg, duration_s)] 成功执行的动作
      errors   [异常对象...]
      aborted  是否因急停/错误提前中止
      total_s  总模拟耗时
    """
    arm = TwinArm(poses_deg)
    frames, actions, errors = [], [], []
    aborted = False
    for verb, arg in parse_script(script_text):
        try:
            t0 = arm.ts
            frames.extend(arm.act(verb, arg))
            actions.append((verb, arg, round(arm.ts - t0, 3)))
        except TwinEstopError as e:
            errors.append(e)
            aborted = True              # 急停：后续动作一律不执行
            break
        except TwinError as e:
            errors.append(e)
            aborted = True
            break
    return {'frames': frames, 'actions': actions, 'errors': errors,
            'aborted': aborted, 'total_s': round(arm.ts, 3),
            'final_pose': arm.pose, 'holding': arm.holding}


# ---------------- ASCII 渲染 ----------------

def render_ascii(points, title, holding):
    """把一帧姿态画成 ASCII（x 右、y 上）。底座 B，关节 o，连杆 *，末端 E/C。"""
    W, H = 56, 22
    X0, X1, Y0, Y1 = -280.0, 280.0, -30.0, 320.0
    grid = [[' '] * W for _ in range(H)]

    def put(x, y, ch):
        c = int((x - X0) / (X1 - X0) * (W - 1))
        r = int((Y1 - y) / (Y1 - Y0) * (H - 1))
        if 0 <= r < H and 0 <= c < W:
            grid[r][c] = ch

    for cx in range(W):                       # 台面线 y=0
        grid[int((Y1 - 0) / (Y1 - Y0) * (H - 1))][cx] = '-'
    for a, b in zip(points, points[1:]):      # 连杆段撒点
        for i in range(21):
            r = i / 20.0
            put(a[0] + (b[0] - a[0]) * r, a[1] + (b[1] - a[1]) * r, '*')
    put(points[0][0], points[0][1], 'B')
    for p in points[1:3]:
        put(p[0], p[1], 'o')
    put(points[3][0], points[3][1], 'C' if holding else 'E')
    lines = [''.join(row).rstrip() for row in grid]
    return '%s\n%s' % (title, '\n'.join(lines))


# ---------------- SVG 渲染 ----------------

_SVG_COLORS = ['#1f77b4', '#2ca02c', '#d62728', '#9467bd',
               '#ff7f0e', '#17becf', '#8c564b', '#e377c2']


def render_svg(result, path):
    """落一张 SVG 总览：每个动作的末姿态（不同颜色+标签）+ 末端轨迹。"""
    frames = result['frames']
    if not frames:
        return None
    W, Hh = 860, 640
    OX, OY, SC = 430.0, 560.0, 1.6           # 原点像素位置与缩放（y 翻转）

    def xy(p):
        return (OX + p[0] * SC, OY - p[1] * SC)

    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
             'viewBox="0 0 %d %d">' % (W, Hh, W, Hh),
             '<rect width="100%" height="100%" fill="#fafafa"/>',
             '<line x1="0" y1="%.1f" x2="%d" y2="%.1f" stroke="#888" '
             'stroke-width="2"/>' % (OY, W, OY)]

    # 末端执行器轨迹（全程，灰虚线）
    pts = ' '.join('%.1f,%.1f' % xy(f['points'][3]) for f in frames)
    parts.append('<polyline points="%s" fill="none" stroke="#bbb" '
                 'stroke-width="1.5" stroke-dasharray="4,3"/>' % pts)

    # 每个动作的最后一帧姿态，按序配色并标注
    blocks = []                       # [(action_name, 末帧)]，保持出现顺序
    for f in frames:
        if not blocks or blocks[-1][0] != f['action']:
            blocks.append((f['action'], f))
        else:
            blocks[-1] = (f['action'], f)
    for idx, (action, f) in enumerate(blocks, 1):
        color = _SVG_COLORS[(idx - 1) % len(_SVG_COLORS)]
        ps = [xy(p) for p in f['points']]
        for a, b in zip(ps, ps[1:]):
            parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" '
                         'stroke="%s" stroke-width="5" stroke-linecap="round" '
                         'opacity="0.75"/>' % (a + b + (color,)))
        for p in ps[1:3]:
            parts.append('<circle cx="%.1f" cy="%.1f" r="5" fill="%s"/>'
                         % (p + (color,)))
        e = ps[3]
        parts.append('<circle cx="%.1f" cy="%.1f" r="7" fill="none" '
                     'stroke="%s" stroke-width="3"/>' % (e + (color,)))
        parts.append('<text x="%.1f" y="%.1f" font-size="13" fill="%s">'
                     '%d.%s%s</text>'
                     % (e[0] + 10, e[1] - 6, color, idx, action,
                        '（持杯）' if f['holding'] else ''))

    # 底座 + 图例
    parts.append('<rect x="%.1f" y="%.1f" width="60" height="12" fill="#333"/>'
                 % (OX - 30, OY))
    legend = ['动作数 %d，总耗时 %.1fs，急停中止=%s'
              % (len(result['actions']), result['total_s'], result['aborted']),
              '灰虚线=末端轨迹；圆环=夹爪（标签含持杯状态）']
    for i, text in enumerate(legend):
        parts.append('<text x="16" y="%d" font-size="14" fill="#333">%s</text>'
                     % (24 + 20 * i, text))
    parts.append('</svg>')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts))
    return path


# ---------------- CLI ----------------

def main(argv=None):
    ap = argparse.ArgumentParser(description='机械臂数字孪生（TASK 21）')
    ap.add_argument('--demo', action='store_true', help='跑内置咖啡出品全流程')
    ap.add_argument('--script', help='自定义动作脚本路径')
    ap.add_argument('--poses', default=DEFAULT_POSES_YAML,
                    help='位姿库（默认 config/poses.yaml）')
    ap.add_argument('--out', default=DEFAULT_OUT_DIR,
                    help='SVG 输出目录（默认 %s，避免污染仓库）' % DEFAULT_OUT_DIR)
    args = ap.parse_args(argv)

    if not args.demo and not args.script:
        ap.print_help()
        return 0
    if args.script:
        with open(args.script, 'r', encoding='utf-8') as f:
            script_text = f.read()
    else:
        script_text = DEMO_SCRIPT

    poses_deg, fallback = load_poses(args.poses)
    result = run_script(script_text, poses_deg)

    print('=== 机械臂数字孪生 ===')
    print('位姿来源: %s%s' % (args.poses, '（占位值→内置演示角度）' if fallback else ''))
    # 逐动作打印该动作末帧的 ASCII 姿态（轨迹帧的文本化）
    last_of_action = {}
    for f in result['frames']:
        last_of_action[f['action']] = f
    for f in last_of_action.values():
        d = f['degs']
        title = ('[t=%5.1fs] %-20s J2=%+6.1f J3=%+6.1f J4=%+6.1f 末端=(%5.0f,%5.0f)mm'
                 % (f['t'], f['action'], d['J2'], d['J3'], d['J4'],
                    f['points'][3][0], f['points'][3][1]))
        print(render_ascii(f['points'], title, f['holding']))
        print()

    for e in result['errors']:
        print('!! %s: %s' % (type(e).__name__, e))
    print('汇总：动作 %d 个，总模拟耗时 %.1fs，中止=%s，末位姿=%s，持杯=%s'
          % (len(result['actions']), result['total_s'], result['aborted'],
             result['final_pose'], result['holding']))

    os.makedirs(args.out, exist_ok=True)
    svg_path = render_svg(result, os.path.join(args.out, 'arm_twin.svg'))
    if svg_path:
        print('SVG 已写入: %s' % svg_path)
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
