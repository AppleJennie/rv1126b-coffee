# projects/common —— 跨项目共享工具包（TASK 28 起）
# 目前提供：structured_log（统一结构化日志：控制台人类可读 + logs/*.jsonl 结构化落盘）

from .structured_log import LOG_DIR, OPTIONAL_FIELDS, ROOT, emit, make_logger

__all__ = ["LOG_DIR", "OPTIONAL_FIELDS", "ROOT", "emit", "make_logger"]
