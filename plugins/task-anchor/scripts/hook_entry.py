#!/usr/bin/env python3
"""Task Anchor 的两个命令 Hook：保存初始指令，压缩后原样注入。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SKILL_MARKER = "$task-anchor"
SKILL_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])\$task-anchor(?![A-Za-z0-9_-])")
POST_COMPACT = "PostCompact"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
AUDIT_LOG_RELATIVE_PATH = Path("audit") / "events.jsonl"
WORKSPACE_SHA256_FIELD = "workspace_sha256"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def session_directory(data_root: Path, session_id: str) -> Path:
    session_key = sha256_bytes(session_id.encode("utf-8"))
    return data_root / "sessions" / session_key


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def warning(message: str) -> dict[str, Any]:
    return {
        "continue": True,
        "systemMessage": message,
    }


def read_session_id(data: dict[str, Any]) -> str | None:
    value = data.get("session_id")
    if isinstance(value, str) and value:
        return value
    return None


def workspace_identity(cwd: str) -> str | None:
    """返回项目身份：Git 根目录优先；非 Git 目录则使用规范化工作目录。"""
    try:
        normalized_cwd = os.path.normcase(os.path.realpath(os.path.abspath(cwd)))
    except (OSError, ValueError):
        return None

    candidate = Path(normalized_cwd)
    while True:
        git_marker = candidate / ".git"
        try:
            if git_marker.is_dir() or git_marker.is_file():
                return f"git:{os.path.normcase(os.path.realpath(str(candidate)))}"
        except OSError:
            return None
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    return f"cwd:{normalized_cwd}"


def read_workspace_sha256(data: dict[str, Any]) -> str | None:
    """返回项目身份的哈希，不把真实路径写入插件状态或审计。"""
    value = data.get("cwd")
    if not isinstance(value, str) or not value.strip():
        return None
    identity = workspace_identity(value)
    if identity is None:
        return None
    return sha256_bytes(identity.encode("utf-8"))


def is_sha256_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def write_audit_event(
    data_root: Path | None,
    data: dict[str, Any],
    status: str,
    **details: Any,
) -> None:
    """追加最小审计事件；失败不能影响 Hook 主流程。"""
    if data_root is None:
        return

    session_id = read_session_id(data)
    event_name = data.get("hook_event_name")
    source = data.get("source")
    trigger = data.get("trigger")
    workspace_sha256 = read_workspace_sha256(data)
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hook_event_name": event_name if isinstance(event_name, str) else None,
        "source": source if isinstance(source, str) else None,
        "trigger": trigger if isinstance(trigger, str) else None,
        "session_key": (
            sha256_bytes(session_id.encode("utf-8"))
            if session_id is not None
            else None
        ),
        "workspace_sha256": workspace_sha256,
        "status": status,
    }
    record.update(details)
    line = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    audit_path = data_root / AUDIT_LOG_RELATIVE_PATH

    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        descriptor = os.open(audit_path, flags, 0o600)
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)
    except OSError:
        return


def is_explicit_activation(prompt: Any) -> bool:
    return isinstance(prompt, str) and SKILL_PATTERN.search(prompt) is not None


def save_initial_instruction(
    data: dict[str, Any],
    data_root: Path | None,
) -> dict[str, Any] | None:
    prompt = data.get("prompt")
    if not is_explicit_activation(prompt):
        return None

    if data_root is None:
        return warning("Task Anchor 无法保存任务指令：Hook 未提供 PLUGIN_DATA。")

    session_id = read_session_id(data)
    if session_id is None:
        write_audit_event(data_root, data, "activation_missing_session_id")
        return warning("Task Anchor 无法保存任务指令：Hook 输入缺少 session_id。")

    workspace_sha256 = read_workspace_sha256(data)
    if workspace_sha256 is None:
        write_audit_event(data_root, data, "activation_missing_cwd")
        return warning(
            "Task Anchor 无法保存任务指令：Hook 输入缺少 cwd，无法绑定项目边界。"
        )

    write_audit_event(
        data_root,
        data,
        "activation_received",
        anchor_workspace_sha256=workspace_sha256,
    )

    session_dir = session_directory(data_root, session_id)
    instruction_path = session_dir / "initial_instruction.txt"
    metadata_path = session_dir / "metadata.json"

    if instruction_path.exists() or metadata_path.exists():
        if not instruction_path.is_file() or not metadata_path.is_file():
            write_audit_event(data_root, data, "activation_existing_incomplete")
            return warning("Task Anchor 检测到不完整的既有任务记录，已拒绝覆盖。")
        instruction, problem = load_initial_instruction(data, data_root)
        if problem is not None:
            write_audit_event(data_root, data, "activation_existing_rejected")
            return problem
        assert instruction is not None
        instruction_bytes = instruction.encode("utf-8")
        write_audit_event(
            data_root,
            data,
            "activation_existing_preserved",
            instruction_sha256=sha256_bytes(instruction_bytes),
            instruction_bytes=len(instruction_bytes),
        )
        return problem

    instruction_bytes = prompt.encode("utf-8")
    digest = sha256_bytes(instruction_bytes)
    metadata = {
        "session_id": session_id,
        "sha256": digest,
        WORKSPACE_SHA256_FIELD: workspace_sha256,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "active": True,
    }

    try:
        atomic_write(instruction_path, instruction_bytes)
        atomic_write(
            metadata_path,
            json.dumps(
                metadata,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
        )
    except OSError:
        write_audit_event(data_root, data, "activation_write_failed")
        return warning("Task Anchor 写入任务指令失败，请检查插件数据目录权限。")
    write_audit_event(
        data_root,
        data,
        "activation_saved",
        instruction_sha256=digest,
        instruction_bytes=len(instruction_bytes),
        anchor_workspace_sha256=workspace_sha256,
    )
    return None


def load_initial_instruction(
    data: dict[str, Any],
    data_root: Path | None,
) -> tuple[str | None, dict[str, Any] | None]:
    if data_root is None:
        return None, warning("Task Anchor 无法恢复任务指令：Hook 未提供 PLUGIN_DATA。")

    session_id = read_session_id(data)
    if session_id is None:
        return None, warning("Task Anchor 无法恢复任务指令：Hook 输入缺少 session_id。")

    session_dir = session_directory(data_root, session_id)
    instruction_path = session_dir / "initial_instruction.txt"
    metadata_path = session_dir / "metadata.json"
    if not instruction_path.exists() and not metadata_path.exists():
        return None, None
    if not instruction_path.is_file() or not metadata_path.is_file():
        return None, warning("Task Anchor 的任务记录不完整，未注入可能损坏的指令。")

    try:
        instruction_bytes = instruction_path.read_bytes()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        instruction = instruction_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, warning("Task Anchor 无法读取任务记录，未注入可能损坏的指令。")

    if not isinstance(metadata, dict):
        return None, warning("Task Anchor 的任务元数据格式无效，已停止注入。")
    if metadata.get("session_id") != session_id:
        return None, warning("Task Anchor 的 session_id 校验失败，已停止注入。")
    if metadata.get("sha256") != sha256_bytes(instruction_bytes):
        return None, warning("Task Anchor 的任务指令 SHA-256 校验失败，已停止注入。")

    workspace_sha256 = read_workspace_sha256(data)
    if workspace_sha256 is None:
        write_audit_event(data_root, data, "workspace_cwd_missing")
        return None, warning(
            "Task Anchor 无法恢复任务指令：Hook 输入缺少 cwd，已拒绝跨项目注入。"
        )

    anchor_workspace_sha256 = metadata.get(WORKSPACE_SHA256_FIELD)
    if not is_sha256_digest(anchor_workspace_sha256):
        write_audit_event(data_root, data, "workspace_binding_missing")
        return None, warning(
            "Task Anchor 的既有任务记录没有项目边界绑定，已停止注入；"
            "请新建任务并重新显式调用 $task-anchor。"
        )
    if anchor_workspace_sha256 != workspace_sha256:
        write_audit_event(
            data_root,
            data,
            "workspace_mismatch",
            anchor_workspace_sha256=anchor_workspace_sha256,
        )
        return None, warning(
            "Task Anchor 检测到当前项目与任务锚点不一致，已拒绝跨项目注入。"
            "请在原项目继续任务，或在新项目新建并显式调用 $task-anchor。"
        )
    return instruction, None


def restore_after_post_compact(
    data: dict[str, Any],
    data_root: Path | None,
) -> dict[str, Any] | None:
    if data.get("trigger") not in {"auto", "manual"}:
        write_audit_event(data_root, data, "post_compact_ignored_trigger")
        return None

    write_audit_event(data_root, data, "post_compact_received")

    instruction, problem = load_initial_instruction(data, data_root)
    if problem is not None:
        write_audit_event(data_root, data, "restore_rejected")
        return problem
    if instruction is None:
        write_audit_event(data_root, data, "restore_no_anchor")
        return None

    instruction_bytes = instruction.encode("utf-8")
    digest = sha256_bytes(instruction_bytes)
    write_audit_event(
        data_root,
        data,
        "anchor_loaded",
        instruction_sha256=digest,
        instruction_bytes=len(instruction_bytes),
        anchor_workspace_sha256=read_workspace_sha256(data),
    )

    additional_context = (
        f"必须使用 {SKILL_MARKER} Skill 继续当前任务。\n\n"
        f"[最初任务指令]\n{instruction}"
    )
    write_audit_event(
        data_root,
        data,
        "restore_emitted",
        instruction_sha256=digest,
        instruction_bytes=len(instruction_bytes),
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": POST_COMPACT,
            "additionalContext": additional_context,
        }
    }


def handle_hook(
    data: dict[str, Any],
    data_root: Path | None,
) -> dict[str, Any] | None:
    event_name = data.get("hook_event_name")
    if event_name == USER_PROMPT_SUBMIT:
        return save_initial_instruction(data, data_root)
    if event_name == POST_COMPACT:
        return restore_after_post_compact(data, data_root)
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = warning("Task Anchor 收到无效的 Hook JSON 输入。")
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
        return 0

    if not isinstance(data, dict):
        payload = warning("Task Anchor 收到的 Hook 输入不是 JSON 对象。")
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
        return 0

    raw_data_root = os.environ.get("PLUGIN_DATA")
    data_root = Path(raw_data_root) if raw_data_root else None
    payload = handle_hook(data, data_root)
    if payload is not None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
