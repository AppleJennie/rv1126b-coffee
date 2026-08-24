# -*- coding: utf-8 -*-
"""疲劳检测：基于 106 点人脸关键点的 EAR/MAR/head_down + 事件状态机。

纯 Python（只用 math，不依赖 numpy），板端无 numpy 也能跑。
算法忠实移植自 C 版驾驶员疲劳监测系统（DMS）：
  /mnt/hgfs/hand_capture_right/src/dms/dms_fatigue_features.c
  /mnt/hgfs/hand_capture_right/src/dms/dms_fatigue_logic.c
阈值参数默认值与 C 版 include/common.h 中的 DMS_* 宏一一对应。

106 点段定义（与 C 版一致，段内按 min/max x/y 动态取点，不写死索引）：
  轮廓/下巴 0~32，眼A 33~42，眼B 87~96（左右由 x 坐标动态区分），
  眉毛 43~51/97~105，鼻 72~86，嘴 52~71。

用法：
  fm = FatigueMonitor()
  r = fm.update(landmarks, ts)   # landmarks: [(x,y), ...] 共 106 个；None 表示本帧无人脸
  # r = {present, ear, mar, head_down, fatigue_score, events, level, ...}
"""

# ---- 段定义（同 C 版 IDX_*） ----
IDX_FACE_A = (33, 42)
IDX_FACE_B = (87, 96)
IDX_NOSE = (72, 86)
IDX_MOUTH = (52, 71)
IDX_JAW = (0, 32)


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _seg(points, seg):
    s, e = seg
    return points[s:e + 1]


def _eye_ear(points, seg):
    """单眼 EAR：段内 min/max x 得眼宽，y 排序后最小两个为上睑、最大两个为下睑。

    对应 C 版 eye_ear()。眼宽 <1px 或点数 <6 视为退化，返回 0。
    """
    pts = _seg(points, seg)
    n = len(pts)
    if n < 6:
        return 0.0
    minx = min(p[0] for p in pts)
    maxx = max(p[0] for p in pts)
    w = maxx - minx
    if w < 1.0:
        return 0.0
    ys = sorted(p[1] for p in pts)
    upper = (ys[0] + ys[1]) * 0.5
    lower = (ys[-1] + ys[-2]) * 0.5
    return _clamp((lower - upper) / w, 0.0, 1.0)


def _mouth_mar(points):
    """MAR：嘴段 min/max x 得嘴宽，取中央 50% 带内的 min/max y 算开合比。

    对应 C 版 mouth_mar()。中央带内点数 <4 时回退到全段 min/max y。
    """
    pts = _seg(points, IDX_MOUTH)
    n = len(pts)
    if n < 8:
        return 0.0
    minx = min(p[0] for p in pts)
    maxx = max(p[0] for p in pts)
    w = maxx - minx
    if w < 1.0:
        return 0.0
    cx0, cx1 = minx + w * 0.25, minx + w * 0.75
    band = [p[1] for p in pts if cx0 <= p[0] <= cx1]
    if len(band) < 4:
        band = [p[1] for p in pts]
    return _clamp((max(band) - min(band)) / w, 0.0, 2.0)


def _head_ratio(points):
    """低头比例：(鼻底 y - 眼中心 y) / (下巴 y - 眼中心 y)，对应 C 版 head_ratio()。"""
    eye_a = _seg(points, IDX_FACE_A)
    eye_b = _seg(points, IDX_FACE_B)
    if not eye_a or not eye_b:
        return 0.0
    eye_y = (sum(p[1] for p in eye_a) / len(eye_a)
             + sum(p[1] for p in eye_b) / len(eye_b)) * 0.5
    nose_y = max(p[1] for p in _seg(points, IDX_NOSE))
    chin_y = max(p[1] for p in _seg(points, IDX_JAW))
    denom = chin_y - eye_y
    if denom < 1.0:
        return 0.0
    return _clamp((nose_y - eye_y) / denom, 0.0, 1.0)


class FatigueMonitor(object):
    """疲劳监测器：喂关键点序列，输出疲劳特征与事件。

    所有阈值/时长参数均可通过构造函数覆盖，默认值 = C 版 common.h 宏。
    事件语义（events 列表，边沿触发，每集一次）：
      'long_blink'  闭眼持续超过 long_eye_closed_ms
      'yawn'        哈欠确认（MAR 超过阈值持续 yawn_ms），内部累计哈欠次数
      'nod'         低头持续超过 head_down_ms
    fatigue_score 综合闭眼时长 / 近期哈欠次数 / 低头时长，0~1；
    level: <0.3 'alert'，0.3~0.6 'mild'，>=0.6 'tired'。
    """

    def __init__(self,
                 ema_alpha=0.65,            # DMS_FEATURE_EMA_ALPHA
                 calib_time_s=2.0,          # DMS_CALIB_TIME_US
                 calib_min_frames=3,
                 ear_close_ratio=0.65,      # DMS_EAR_CLOSE_RATIO
                 ear_recover_ratio=0.75,    # DMS_EAR_RECOVER_RATIO
                 mar_yawn_min=0.45,         # DMS_MAR_YAWN_MIN
                 mar_yawn_ratio=1.8,        # DMS_MAR_YAWN_RATIO
                 mar_recover_ratio=1.35,    # DMS_MAR_RECOVER_RATIO
                 head_enter_delta=0.18,     # DMS_HEAD_ENTER_DELTA
                 head_recover_delta=0.10,   # DMS_HEAD_RECOVER_DELTA
                 eye_closed_ms=800,         # DMS_EYE_CLOSED_MS
                 long_eye_closed_ms=1500,   # DMS_LONG_EYE_CLOSED_MS
                 yawn_ms=1000,              # DMS_YAWN_MS
                 head_down_ms=1500,         # DMS_HEAD_DOWN_MS
                 face_lost_s=1.0,           # C 版写死 1s：超时无人脸复位事件状态
                 yawn_window_s=60.0,        # 哈欠计数窗口（Python 侧新增，见 README）
                 yawn_count_tired=3,        # 窗口内哈欠达到此次数视为明显疲劳
                 mild_score=0.3,
                 tired_score=0.6):
        self.ema_alpha = ema_alpha
        self.calib_time_s = calib_time_s
        self.calib_min_frames = calib_min_frames
        self.ear_close_ratio = ear_close_ratio
        self.ear_recover_ratio = ear_recover_ratio
        self.mar_yawn_min = mar_yawn_min
        self.mar_yawn_ratio = mar_yawn_ratio
        self.mar_recover_ratio = mar_recover_ratio
        self.head_enter_delta = head_enter_delta
        self.head_recover_delta = head_recover_delta
        self.eye_closed_s = eye_closed_ms / 1000.0
        self.long_eye_closed_s = long_eye_closed_ms / 1000.0
        self.yawn_s = yawn_ms / 1000.0
        self.head_down_s = head_down_ms / 1000.0
        self.face_lost_s = face_lost_s
        self.yawn_window_s = yawn_window_s
        self.yawn_count_tired = yawn_count_tired
        self.mild_score = mild_score
        self.tired_score = tired_score
        self.reset()

    def reset(self):
        """对应 C 版 dms_fatigue_features_reset()。"""
        self.calibrated = False
        self.calib_start_ts = None
        self.last_face_ts = None
        self._sum_ear = self._sum_mar = self._sum_head = 0.0
        self._calib_count = 0
        self.ear_ema = self.mar_ema = self.head_ema = 0.0
        self.ear_baseline = self.mar_baseline = self.head_baseline = 0.0
        # 事件状态机
        self._eye_enter = self._yawn_enter = self._head_enter = None
        self._eye_active = self._yawn_active = self._head_active = False
        self._long_blink_fired = False     # 本次闭眼集内 long_blink 是否已报
        self._yawn_times = []              # yawn_window_s 内的哈欠时间戳
        self._last_score = 0.0

    # ---- 内部 ----
    def _ema(self, old, new):
        return old + self.ema_alpha * (new - old)

    def _prune_yawns(self, ts):
        self._yawn_times = [t for t in self._yawn_times
                            if ts - t <= self.yawn_window_s]

    def _score_and_level(self, ts):
        """综合评分：闭眼 0.6 权重 + 哈欠 0.3 + 低头 0.3，截断到 0~1。

        C 版只输出离散状态（dms_fatigue_logic 的组合判断），连续评分是
        Python 侧为对接推荐引擎新增的，权重见 README 说明。
        """
        eye_prog = 0.0
        if self._eye_enter is not None:
            eye_prog = _clamp((ts - self._eye_enter) / self.long_eye_closed_s, 0.0, 1.0)
        self._prune_yawns(ts)
        yawn_prog = _clamp(len(self._yawn_times) / float(self.yawn_count_tired), 0.0, 1.0)
        head_prog = 0.0
        if self._head_enter is not None:
            head_prog = _clamp((ts - self._head_enter) / (2.0 * self.head_down_s), 0.0, 1.0)
        score = _clamp(0.6 * eye_prog + 0.3 * yawn_prog + 0.3 * head_prog, 0.0, 1.0)
        if score >= self.tired_score:
            level = 'tired'
        elif score >= self.mild_score:
            level = 'mild'
        else:
            level = 'alert'
        return score, level

    # ---- 主入口 ----
    def update(self, landmarks, ts):
        """喂一帧。landmarks 为 106 个 (x,y)，或 None 表示本帧无人脸。

        返回 dict：
          present        本帧是否有人脸
          calibrated     2 秒个人基线是否已建立
          ear/mar        EMA 平滑后的均值
          head_down      低头得分 0~1（相对个人基线）
          fatigue_score  综合疲劳分 0~1
          events         本帧新触发的事件列表
          level          'alert' | 'mild' | 'tired'
        """
        events = []

        # 无人脸：沿用 C 版逻辑，超过 face_lost_s 复位事件状态机
        if landmarks is None or len(landmarks) < 106:
            if (self.last_face_ts is not None
                    and ts - self.last_face_ts > self.face_lost_s):
                self._eye_active = self._yawn_active = self._head_active = False
                self._eye_enter = self._yawn_enter = self._head_enter = None
                self._long_blink_fired = False
            score, level = self._score_and_level(ts)
            self._last_score = score
            return {'present': False, 'calibrated': self.calibrated,
                    'ear': None, 'mar': None, 'head_down': None,
                    'fatigue_score': round(score, 3), 'events': events,
                    'level': level}
        self.last_face_ts = ts

        ear_a = _eye_ear(landmarks, IDX_FACE_A)
        ear_b = _eye_ear(landmarks, IDX_FACE_B)
        # 左右按段中心 x 坐标自动分配，避免镜像/翻转写反（同 C 版）
        ax = sum(p[0] for p in _seg(landmarks, IDX_FACE_A)) / (IDX_FACE_A[1] - IDX_FACE_A[0] + 1)
        bx = sum(p[0] for p in _seg(landmarks, IDX_FACE_B)) / (IDX_FACE_B[1] - IDX_FACE_B[0] + 1)
        if ax > bx:
            left_ear, right_ear = ear_b, ear_a
        else:
            left_ear, right_ear = ear_a, ear_b
        ear_raw = (left_ear + right_ear) * 0.5
        mar_raw = _mouth_mar(landmarks)
        head_raw = _head_ratio(landmarks)

        if self._calib_count == 0:
            self.ear_ema, self.mar_ema, self.head_ema = ear_raw, mar_raw, head_raw
            self.calib_start_ts = ts
        else:
            self.ear_ema = self._ema(self.ear_ema, ear_raw)
            self.mar_ema = self._ema(self.mar_ema, mar_raw)
            self.head_ema = self._ema(self.head_ema, head_raw)

        # 2 秒个人基线校准（同 C 版：时长够且帧数够才定基线）
        if not self.calibrated:
            self._sum_ear += ear_raw
            self._sum_mar += mar_raw
            self._sum_head += head_raw
            self._calib_count += 1
            if (ts - self.calib_start_ts >= self.calib_time_s
                    and self._calib_count >= self.calib_min_frames):
                self.ear_baseline = self._sum_ear / self._calib_count
                self.mar_baseline = self._sum_mar / self._calib_count
                self.head_baseline = self._sum_head / self._calib_count
                self.calibrated = True

        mar_threshold = max(self.mar_yawn_min, self.mar_baseline * self.mar_yawn_ratio)
        head_down_score = _clamp((self.head_ema - self.head_baseline) / 0.25, 0.0, 1.0)

        if self.calibrated:
            ear_enter = self.ear_baseline * self.ear_close_ratio
            ear_recover = self.ear_baseline * self.ear_recover_ratio
            mar_recover = max(self.mar_yawn_min * 0.8,
                              self.mar_baseline * self.mar_recover_ratio)
            head_enter = self.head_baseline + self.head_enter_delta
            head_recover = self.head_baseline + self.head_recover_delta

            # 闭眼判定用双眼 raw EAR 的较大值：真闭眼双眼同时低才触发，
            # 防单眼关键点退化误判；比 EMA 更跟手（同 C 版注释）
            ear_both = max(left_ear, right_ear)

            if not self._eye_active and ear_both < ear_enter:
                if self._eye_enter is None:
                    self._eye_enter = ts
            if self._eye_enter is not None and ear_both > ear_recover:
                self._eye_enter = None
                self._long_blink_fired = False
            if (self._eye_enter is not None
                    and ts - self._eye_enter >= self.eye_closed_s):
                self._eye_active = True
            if self._eye_active and ear_both > ear_recover:
                self._eye_active = False
                self._eye_enter = None
                self._long_blink_fired = False
            if (self._eye_active and self._eye_enter is not None
                    and ts - self._eye_enter >= self.long_eye_closed_s
                    and not self._long_blink_fired):
                self._long_blink_fired = True
                events.append('long_blink')

            if not self._yawn_active and self.mar_ema > mar_threshold:
                if self._yawn_enter is None:
                    self._yawn_enter = ts
            if self._yawn_enter is not None and self.mar_ema < mar_recover:
                self._yawn_enter = None
            if (self._yawn_enter is not None
                    and ts - self._yawn_enter >= self.yawn_s):
                if not self._yawn_active:
                    self._yawn_active = True
                    self._yawn_times.append(ts)
                    events.append('yawn')
            if self._yawn_active and self.mar_ema < mar_recover:
                self._yawn_active = False
                self._yawn_enter = None

            # 注意：C 版此处用归一化得分对比 baseline+delta，原样移植
            if not self._head_active and head_down_score > head_enter:
                if self._head_enter is None:
                    self._head_enter = ts
            if self._head_enter is not None and head_down_score < head_recover:
                self._head_enter = None
            if (self._head_enter is not None
                    and ts - self._head_enter >= self.head_down_s):
                if not self._head_active:
                    self._head_active = True
                    events.append('nod')
            if self._head_active and head_down_score < head_recover:
                self._head_active = False
                self._head_enter = None

        score, level = self._score_and_level(ts)
        self._last_score = score
        return {'present': True,
                'calibrated': self.calibrated,
                'ear': round(self.ear_ema, 4),
                'mar': round(self.mar_ema, 4),
                'head_down': round(head_down_score, 4),
                'left_ear': round(left_ear, 4),
                'right_ear': round(right_ear, 4),
                'ear_baseline': round(self.ear_baseline, 4),
                'mar_baseline': round(self.mar_baseline, 4),
                'head_baseline': round(self.head_baseline, 4),
                'yawn_count': len(self._yawn_times),
                'fatigue_score': round(score, 3),
                'events': events,
                'level': level}
