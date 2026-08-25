#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表情分类轻量接口（TASK 16）—— 只做 neutral / happy 两分类。

后端：
  MockExpression  脚本化返回（默认；测试/演示用，确定性，不依赖任何模型）
  CPUExpression   cv2 Haar 微笑级联，纯 CPU，开发 VM 可跑
  RKNNExpression  桩：__init__ 只记录模型路径；infer 抛 NotImplementedError，
                  说明需在 RV1126B 真机 NPU 上用 rknnlite 推理，不假装能跑。

VisionManager 通过 make_expression_backend() 工厂选择，默认 mock。
标签集合固定为 'neutral' / 'happy'。
"""

import os

import cv2

LABELS = ('neutral', 'happy')

# haarcascade 模型目录候选（与 projects/ai_host/face_events.py 同一套路径发现逻辑：
# pip 版 opencv 在 cv2.data.haarcascades；Ubuntu apt 版 4.2 在系统目录）
_CASCADE_DIR_CANDIDATES = [
    '/usr/share/opencv4/haarcascades/',
    '/usr/share/opencv/haarcascades/',
    '/usr/local/share/opencv4/haarcascades/',
]
if hasattr(cv2, 'data') and hasattr(cv2.data, 'haarcascades'):
    _CASCADE_DIR_CANDIDATES.insert(0, cv2.data.haarcascades)


def find_cascade(filename):
    """在候选目录里找级联文件，找不到抛 RuntimeError。"""
    for d in _CASCADE_DIR_CANDIDATES:
        p = os.path.join(d, filename)
        if os.path.isfile(p):
            return p
    raise RuntimeError('找不到级联文件 %s（试过: %s）' % (filename, _CASCADE_DIR_CANDIDATES))


class ExpressionBackend(object):
    """表情分类后端抽象：infer(frame, face_box=None) -> 'neutral' | 'happy'。"""

    name = 'abstract'

    def infer(self, frame, face_box=None):
        raise NotImplementedError


class MockExpression(ExpressionBackend):
    """脚本化后端：按 script 顺序依次返回，用完后保持最后一个标签。

    set_label() 可外部钉住当前标签（VisionManager 的 mock 帧源按帧真值驱动），
    clear_label() 解除钉住回到脚本模式。
    """

    name = 'mock'

    def __init__(self, script=('neutral',)):
        self._script = list(script)
        for lab in self._script:
            if lab not in LABELS:
                raise ValueError('非法表情标签: %r（仅支持 %s）' % (lab, LABELS))
        self._idx = 0
        self._label = None

    def set_label(self, label):
        if label not in LABELS:
            raise ValueError('非法表情标签: %r（仅支持 %s）' % (label, LABELS))
        self._label = label

    def clear_label(self):
        self._label = None

    def infer(self, frame, face_box=None):
        if self._label is not None:
            return self._label
        if not self._script:
            return 'neutral'
        if self._idx < len(self._script):
            lab = self._script[self._idx]
            self._idx += 1
            return lab
        return self._script[-1]


class CPUExpression(ExpressionBackend):
    """CPU 后端：Haar 微笑级联。在下半张脸找微笑，找到判 happy，否则 neutral。"""

    name = 'cpu'

    def __init__(self, cascade_path=None):
        path = cascade_path or find_cascade('haarcascade_smile.xml')
        self.cascade_path = path
        self._cascade = cv2.CascadeClassifier(path)
        if self._cascade.empty():
            raise RuntimeError('加载微笑级联失败: %s' % path)

    def infer(self, frame, face_box=None):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if face_box is not None:
            x, y, w, h = [int(v) for v in face_box]
            fh, fw = gray.shape[:2]
            # 微笑只可能出现在下半张脸，缩小搜索区域压低误检
            x0, x1 = max(0, x), min(fw, x + w)
            y0, y1 = max(0, y + h // 2), min(fh, y + h)
            roi = gray[y0:y1, x0:x1]
        else:
            roi = gray[gray.shape[0] // 2:, :]
        if roi.size == 0:
            return 'neutral'
        smiles = self._cascade.detectMultiScale(roi, 1.8, 20, minSize=(25, 25))
        return 'happy' if len(smiles) > 0 else 'neutral'


class RKNNExpression(ExpressionBackend):
    """RKNN 桩后端：只记录模型路径，不在本机假装推理。

    真机部署时用 rknnlite 加载轻量表情分类 .rknn（需按 RV1126B 重新转换）。
    """

    name = 'rknn'

    def __init__(self, model_path='expression.rknn'):
        self.model_path = model_path   # 仅记录，不加载

    def infer(self, frame, face_box=None):
        raise NotImplementedError(
            'RKNNExpression 是桩：表情分类模型需在 RV1126B 真机 NPU 上用 rknnlite '
            '推理；开发机（无 NPU）请改用 mock 或 cpu 后端。'
            '已记录模型路径: %s' % self.model_path)


def make_expression_backend(name='mock', **kw):
    """工厂：按名字建后端。默认 mock（VisionManager 默认走它）。"""
    if name == 'mock':
        return MockExpression(**kw)
    if name == 'cpu':
        return CPUExpression(**kw)
    if name == 'rknn':
        return RKNNExpression(**kw)
    raise ValueError('未知表情后端: %r（可选 mock/cpu/rknn）' % (name,))
