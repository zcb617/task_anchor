"""Task Anchor 统一诊断日志记录器。"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_FIXED_FIELDS = {"timestamp", "level", "message"}


class TaskAnchorLogger:
    """负责将受管命令和 Hook 审计事件追加到 UTF-8 JSONL 文件。"""

    def __init__(self, log_path: str | os.PathLike[str] | None = None) -> None:
        self._log_path = Path(log_path).expanduser().resolve() if log_path else None

    def info(self, message: str, context: object = {}) -> None:
        """记录需要长期保留的业务生命周期事件。"""
        self._write("info", message, context)

    def warning(self, message: str, context: object = {}) -> None:
        """记录失败、超时或兼容性降级等警告事件。"""
        self._write("warning", message, context)

    def debug(self, message: str, context: object = {}) -> None:
        """记录用于定位执行细节的调试事件。"""
        self._write("debug", message, context)

    def _write(self, level: str, message: str, context: object) -> None:
        if self._log_path is None:
            return
        try:
            record = _flatten_context(context)
            record.update(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": level,
                    "message": str(message),
                }
            )
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
            descriptor = os.open(self._log_path, flags, 0o600)
            try:
                payload = (
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                ).encode("utf-8")
                os.write(descriptor, payload)
            finally:
                os.close(descriptor)
        except Exception:
            try:
                sys.stderr.write("Task Anchor logger write failed\n")
            except Exception:
                pass


def _flatten_context(value: object, prefix: str = "", output: dict[str, Any] | None = None) -> dict[str, Any]:
    """将上下文对象转换为顶层字段，并阻止覆盖 Logger 固定字段。"""
    fields = output if output is not None else {}
    if not isinstance(value, dict):
        if prefix and prefix not in _FIXED_FIELDS:
            fields[prefix] = value
        return fields
    for key, child in value.items():
        field_name = f"{prefix}.{key}" if prefix else str(key)
        if str(key) in _FIXED_FIELDS or field_name in _FIXED_FIELDS:
            continue
        if isinstance(child, dict):
            _flatten_context(child, field_name, fields)
        else:
            fields[field_name] = child
    return fields


__all__ = ["TaskAnchorLogger"]
