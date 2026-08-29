#!/usr/bin/env python3
"""Claude Code adapter for Task Anchor's durable task state."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import task_state


USER_PROMPT_EXPANSION = "UserPromptExpansion"
POST_COMPACT = "PostCompact"
PRE_TOOL_USE = "PreToolUse"
STOP = "Stop"
SESSION_END = "SessionEnd"

COMMAND_START = "task-anchor"
COMMAND_END = "task-anchor-end"
COMMAND_READ_ONLY = "task-anchor-readonly"
COMMAND_WRITE = "task-anchor-write"
COMMANDS = {COMMAND_START, COMMAND_END, COMMAND_READ_ONLY, COMMAND_WRITE}


def _command_name(data: dict[str, Any]) -> str | None:
    value = data.get("command_name")
    if not isinstance(value, str):
        return None
    normalized = value.strip().lstrip("/")
    if normalized.startswith("task-anchor:"):
        normalized = normalized.removeprefix("task-anchor:")
    return normalized if normalized in COMMANDS else None


def _command_args(data: dict[str, Any]) -> str:
    value = data.get("command_args")
    return value if isinstance(value, str) else ""


def _handle_prompt_expansion(
    data: dict[str, Any], data_root: Path | None
) -> dict[str, Any] | None:
    command = _command_name(data)
    if command is None:
        return None
    if command == COMMAND_START:
        return task_state.start_new_task(
            data,
            data_root,
            instruction=_command_args(data),
        )
    if command == COMMAND_END:
        return task_state.end_current_task(
            {**data, "prompt": task_state.END_SKILL_MARKER},
            data_root,
        )
    return task_state.set_mutation_policy(
        data,
        data_root,
        read_only=command == COMMAND_READ_ONLY,
    )


def handle_hook(data: dict[str, Any], data_root: Path | None) -> dict[str, Any] | None:
    event_name = data.get("hook_event_name")
    if event_name == USER_PROMPT_EXPANSION:
        return _handle_prompt_expansion(data, data_root)
    if event_name == POST_COMPACT:
        return task_state.restore_after_post_compact(data, data_root)
#    if event_name == PRE_TOOL_USE:
#        return task_state.guard_pre_tool_use(data, data_root)
    if event_name in {STOP, SESSION_END}:
        task_state.cleanup_after_stop(data, data_root)
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.stdout.write(
            json.dumps(task_state.warning("Task Anchor 收到无效的 Hook JSON 输入。"), ensure_ascii=False)
        )
        return 0
    if not isinstance(data, dict):
        sys.stdout.write(
            json.dumps(task_state.warning("Task Anchor 收到的 Hook 输入不是 JSON 对象。"), ensure_ascii=False)
        )
        return 0

    raw_data_root = os.environ.get("CLAUDE_PLUGIN_DATA")
    data_root = Path(raw_data_root) if raw_data_root else None
    payload = handle_hook(data, data_root)
    if payload is not None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
