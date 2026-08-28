#!/usr/bin/env python3
"""Task Anchor Hook：按 task_id 保存任务，并只恢复当前活动任务。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))
import resource_manager

SKILL_MARKER = "$task-anchor"
SKILL_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])\$task-anchor(?![A-Za-z0-9_-])")
END_SKILL_MARKER = "$task-anchor-end"
END_SKILL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])\$task-anchor-end(?![A-Za-z0-9_-])"
)
READ_ONLY_SKILL_MARKER = "$task-anchor-readonly"
READ_ONLY_SKILL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])\$task-anchor-readonly(?![A-Za-z0-9_-])"
)
WRITE_SKILL_MARKER = "$task-anchor-write"
WRITE_SKILL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])\$task-anchor-write(?![A-Za-z0-9_-])"
)
POST_COMPACT = "PostCompact"
# Codex 上下文压缩后提醒用户重新读取项目规则和最近对话。
POST_COMPACT_CONTINUITY_REMINDER = (
    "你刚刚经历了上下文压缩，请立刻重新读取AGENTS.md规则文件，以及最近的20条和用户的对话内容。"
    "以确保工作的延续性和连贯性。"
)
USER_PROMPT_SUBMIT = "UserPromptSubmit"
PRE_TOOL_USE = "PreToolUse"
STOP = "Stop"
AUDIT_LOG_RELATIVE_PATH = Path("audit") / "events.jsonl"
WORKSPACE_SHA256_FIELD = "workspace_sha256"
SESSION_KEY_FIELD = "session_key"
TASK_STATUS_ACTIVE = 1
TASK_STATUS_CLOSED = 0
CLOSED_REASON_SUPERSEDED = "superseded_by_new_task"
CLOSED_REASON_MANUAL = "manually_ended"
SCHEMA_VERSION = 2
MUTATION_POLICY_SCHEMA_VERSION = 1

MANAGED_EXEC_TOOL_NAMES = {
    "managed_exec",
    "mcp__task_anchor__managed_exec",
    "mcp__task-anchor__managed_exec",
}
FASTCTX_TOOL_NAME_PREFIX = "mcp__fastctx__"
FASTCTX_READ_ONLY_TOOL_NAMES = {
    "mcp__fastctx__inspect_local_file",
    "mcp__fastctx__grep",
    "mcp__fastctx__glob",
}
COMMAND_TOOL_NAMES = {
    "bash",
    "cmd",
    "exec",
    "exec_command",
    "local_shell",
    "powershell",
    "shell",
}
MUTATION_TOOL_TOKENS = {
    "copy",
    "delete",
    "edit",
    "mkdir",
    "move",
    "patch",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "touch",
    "truncate",
    "unlink",
    "upload",
    "write",
}
FILE_RESOURCE_TOKENS = {"directory", "file", "folder", "path"}
PROCESS_COMMAND_KEYWORDS = (
    "java",
    "python",
    "node",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "mvn",
    "maven",
    "gradle",
    "go",
    "cargo",
    "dotnet",
    "docker",
    "podman",
    "adb",
    "ffmpeg",
    "deno",
    "bun",
    "php",
    "ruby",
    "perl",
)
COMMAND_TEXT_KEYS = (
    "command",
    "cmd",
    "shell_command",
    "command_line",
    "script",
    "program",
)
NESTED_COMMAND_KEYS = ("tool_input", "toolInput", "arguments", "input")


class StorageError(RuntimeError):
    """任务存储不完整或不可信。"""


class LockUnavailable(StorageError):
    """同一会话正在切换任务。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def session_key(session_id: str) -> str:
    return sha256_bytes(session_id.encode("utf-8"))


def session_directory(data_root: Path, session_id: str) -> Path:
    return data_root / "sessions" / session_key(session_id)


def tasks_directory(data_root: Path, session_id: str) -> Path:
    return session_directory(data_root, session_id) / "tasks"


def current_task_path(data_root: Path, session_id: str) -> Path:
    return session_directory(data_root, session_id) / "current_task.json"


def task_directory(data_root: Path, session_id: str, task_id: str) -> Path:
    return tasks_directory(data_root, session_id) / task_id


def task_instruction_path(data_root: Path, session_id: str, task_id: str) -> Path:
    return task_directory(data_root, session_id, task_id) / "initial_instruction.txt"


def task_metadata_path(data_root: Path, session_id: str, task_id: str) -> Path:
    return task_directory(data_root, session_id, task_id) / "metadata.json"


def mutation_policy_path(
    data_root: Path,
    session_id: str,
    workspace_sha256: str,
) -> Path:
    return (
        session_directory(data_root, session_id)
        / "mutation-policies"
        / f"{workspace_sha256}.json"
    )


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


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
    )


def warning(message: str) -> dict[str, Any]:
    return {"continue": True, "systemMessage": message}


def post_compact_output(task_context: str | None = None) -> dict[str, Any]:
    """构造 Codex PostCompact 的连续性提醒及可选任务恢复上下文。"""

    additional_context = POST_COMPACT_CONTINUITY_REMINDER
    if task_context:
        additional_context = f"{additional_context}\n\n{task_context}"
    return {
        "hookSpecificOutput": {
            "hookEventName": POST_COMPACT,
            "additionalContext": additional_context,
        }
    }


def post_compact_warning(message: str) -> dict[str, Any]:
    """保留 PostCompact 警告审计提示，同时注入 Codex 连续性提醒。"""

    return {**warning(message), **post_compact_output()}


def read_session_id(data: dict[str, Any]) -> str | None:
    value = data.get("session_id")
    return value if isinstance(value, str) and value else None


def workspace_identity(cwd: str) -> str | None:
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
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    identity = workspace_identity(cwd)
    return sha256_bytes(identity.encode("utf-8")) if identity is not None else None


def is_sha256_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def canonical_task_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return str(parsed) if str(parsed) == value else None


def read_json_object(path: Path, message: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(message) from exc
    if not isinstance(value, dict):
        raise StorageError(message)
    return value


def write_audit_event(
    data_root: Path | None,
    data: dict[str, Any],
    status: str,
    **details: Any,
) -> None:
    """写入不包含任务正文和真实 session_id 的最小审计记录。"""
    if data_root is None:
        return

    current_session_id = read_session_id(data)
    event_name = data.get("hook_event_name")
    source = data.get("source")
    trigger = data.get("trigger")
    record: dict[str, Any] = {
        "timestamp": utc_now(),
        "hook_event_name": event_name if isinstance(event_name, str) else None,
        "source": source if isinstance(source, str) else None,
        "trigger": trigger if isinstance(trigger, str) else None,
        SESSION_KEY_FIELD: (
            session_key(current_session_id) if current_session_id is not None else None
        ),
        WORKSPACE_SHA256_FIELD: read_workspace_sha256(data),
        "status": status,
    }
    record.update(details)
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
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


def is_explicit_end(prompt: Any) -> bool:
    return isinstance(prompt, str) and END_SKILL_PATTERN.search(prompt) is not None


def is_explicit_read_only(prompt: Any) -> bool:
    return isinstance(prompt, str) and READ_ONLY_SKILL_PATTERN.search(prompt) is not None


def is_explicit_write(prompt: Any) -> bool:
    return isinstance(prompt, str) and WRITE_SKILL_PATTERN.search(prompt) is not None


def read_mutation_policy(
    data: dict[str, Any],
    data_root: Path,
) -> bool:
    """读取当前会话、当前项目的只读标记；损坏记录拒绝按可写状态解释。"""

    current_session_id = read_session_id(data)
    if current_session_id is None:
        raise StorageError("Task Anchor 只读门控缺少 session_id。")
    workspace_sha256 = read_workspace_sha256(data)
    if workspace_sha256 is None:
        raise StorageError("Task Anchor 只读门控缺少 cwd。")

    path = mutation_policy_path(data_root, current_session_id, workspace_sha256)
    if not path.exists():
        return False
    if not path.is_file():
        raise StorageError("Task Anchor 只读门控状态无效。")
    policy = read_json_object(path, "Task Anchor 只读门控状态损坏。")
    if (
        policy.get("schema_version") != MUTATION_POLICY_SCHEMA_VERSION
        or policy.get(SESSION_KEY_FIELD) != session_key(current_session_id)
        or policy.get(WORKSPACE_SHA256_FIELD) != workspace_sha256
        or type(policy.get("read_only")) is not bool
    ):
        raise StorageError("Task Anchor 只读门控状态校验失败。")
    return policy["read_only"]


def set_mutation_policy(
    data: dict[str, Any],
    data_root: Path | None,
    *,
    read_only: bool,
) -> dict[str, Any] | None:
    """按当前会话和项目设置只读门控。"""

    if data_root is None:
        return warning("Task Anchor 无法切换只读门控：Hook 未提供 PLUGIN_DATA。")
    current_session_id = read_session_id(data)
    if current_session_id is None:
        write_audit_event(data_root, data, "mutation_policy_missing_session_id")
        return warning("Task Anchor 无法切换只读门控：Hook 输入缺少 session_id。")
    workspace_sha256 = read_workspace_sha256(data)
    if workspace_sha256 is None:
        write_audit_event(data_root, data, "mutation_policy_missing_cwd")
        return warning("Task Anchor 无法切换只读门控：Hook 输入缺少 cwd。")

    policy = {
        "schema_version": MUTATION_POLICY_SCHEMA_VERSION,
        SESSION_KEY_FIELD: session_key(current_session_id),
        WORKSPACE_SHA256_FIELD: workspace_sha256,
        "read_only": read_only,
        "updated_at": utc_now(),
    }
    try:
        with session_lock(session_directory(data_root, current_session_id)):
            atomic_write_json(
                mutation_policy_path(data_root, current_session_id, workspace_sha256),
                policy,
            )
    except LockUnavailable:
        write_audit_event(data_root, data, "mutation_policy_lock_unavailable")
        return warning("Task Anchor 只读门控状态正由另一项操作切换，请稍后重新调用。")
    except OSError:
        write_audit_event(data_root, data, "mutation_policy_write_failed")
        return warning("Task Anchor 无法确认只读门控状态已切换，请重新显式调用。")

    write_audit_event(
        data_root,
        data,
        "mutation_policy_updated",
        read_only=read_only,
    )
    return None


def _acquire_lock(handle: Any) -> None:
    handle.seek(0)
    if handle.read(1) == b"":
        handle.seek(0)
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_lock(handle: Any) -> None:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return


@contextmanager
def session_lock(session_dir: Path) -> Iterator[None]:
    session_dir.mkdir(parents=True, exist_ok=True)
    handle = (session_dir / ".task-anchor.lock").open("a+b")
    try:
        try:
            _acquire_lock(handle)
        except OSError as exc:
            raise LockUnavailable("Task Anchor 当前任务状态正由另一项操作切换。") from exc
        try:
            yield
        finally:
            _release_lock(handle)
    finally:
        handle.close()


def validate_metadata(
    metadata: dict[str, Any],
    session_id: str,
    task_id: str,
) -> None:
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise StorageError("Task Anchor 的任务元数据版本无效，已停止恢复。")
    if metadata.get("task_id") != task_id:
        raise StorageError("Task Anchor 的 task_id 校验失败，已停止恢复。")
    if metadata.get(SESSION_KEY_FIELD) != session_key(session_id):
        raise StorageError("Task Anchor 的会话校验失败，已停止恢复。")
    if not is_sha256_digest(metadata.get("sha256")):
        raise StorageError("Task Anchor 的任务指令校验值无效，已停止恢复。")
    if not is_sha256_digest(metadata.get(WORKSPACE_SHA256_FIELD)):
        raise StorageError("Task Anchor 的既有任务记录没有项目边界绑定，已停止恢复。")
    if type(metadata.get("status")) is not int or metadata["status"] not in {
        TASK_STATUS_ACTIVE,
        TASK_STATUS_CLOSED,
    }:
        raise StorageError("Task Anchor 的任务状态无效，已停止恢复。")


def read_all_task_metadata(
    data_root: Path,
    session_id: str,
) -> dict[str, dict[str, Any]]:
    directory = tasks_directory(data_root, session_id)
    if not directory.exists():
        return {}
    if not directory.is_dir():
        raise StorageError("Task Anchor 的任务目录无效，已拒绝切换任务。")

    records: dict[str, dict[str, Any]] = {}
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise StorageError("Task Anchor 无法读取既有任务记录。") from exc

    for entry in entries:
        task_id = canonical_task_id(entry.name)
        if task_id is None or not entry.is_dir():
            raise StorageError("Task Anchor 检测到无效任务记录，已拒绝切换任务。")
        metadata = read_json_object(
            entry / "metadata.json",
            "Task Anchor 的既有任务元数据损坏，已拒绝切换任务。",
        )
        validate_metadata(metadata, session_id, task_id)
        records[task_id] = metadata
    return records


def read_current_task_id(data_root: Path, session_id: str) -> str | None:
    pointer_path = current_task_path(data_root, session_id)
    if not pointer_path.exists():
        return None
    if not pointer_path.is_file():
        raise StorageError("Task Anchor 的当前任务指针无效，已停止恢复。")
    pointer = read_json_object(
        pointer_path,
        "Task Anchor 的当前任务指针损坏，已停止恢复。",
    )
    task_id = canonical_task_id(pointer.get("task_id"))
    if (
        pointer.get("schema_version") != SCHEMA_VERSION
        or pointer.get(SESSION_KEY_FIELD) != session_key(session_id)
        or task_id is None
    ):
        raise StorageError("Task Anchor 的当前任务指针校验失败，已停止恢复。")
    return task_id


def load_task(
    data: dict[str, Any],
    data_root: Path,
    session_id: str,
    task_id: str,
) -> tuple[str, dict[str, Any]]:
    instruction_path = task_instruction_path(data_root, session_id, task_id)
    metadata_path = task_metadata_path(data_root, session_id, task_id)
    if not instruction_path.is_file() or not metadata_path.is_file():
        raise StorageError("Task Anchor 的当前任务记录不完整，已停止恢复。")

    try:
        instruction_bytes = instruction_path.read_bytes()
        instruction = instruction_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StorageError("Task Anchor 无法读取当前任务指令，已停止恢复。") from exc

    metadata = read_json_object(
        metadata_path,
        "Task Anchor 的当前任务元数据损坏，已停止恢复。",
    )
    validate_metadata(metadata, session_id, task_id)
    if metadata.get("sha256") != sha256_bytes(instruction_bytes):
        raise StorageError("Task Anchor 的任务指令 SHA-256 校验失败，已停止恢复。")

    workspace_sha256 = read_workspace_sha256(data)
    if workspace_sha256 is None:
        raise StorageError("Task Anchor 无法恢复任务指令：Hook 输入缺少 cwd。")
    if metadata[WORKSPACE_SHA256_FIELD] != workspace_sha256:
        raise StorageError("Task Anchor 检测到当前项目与任务锚点不一致，已拒绝跨项目注入。")
    return instruction, metadata


def has_legacy_session_layout(data_root: Path, session_id: str) -> bool:
    directory = session_directory(data_root, session_id)
    return any(
        (directory / filename).exists()
        for filename in ("initial_instruction.txt", "metadata.json")
    )


def transition_path(data_root: Path, session_id: str) -> Path:
    return session_directory(data_root, session_id) / "transition.json"


def read_pending_transition(
    data_root: Path,
    session_id: str,
) -> dict[str, Any] | None:
    path = transition_path(data_root, session_id)
    if not path.exists():
        return None
    if not path.is_file():
        raise StorageError("Task Anchor 的任务切换记录无效，已停止处理。")

    transition = read_json_object(
        path,
        "Task Anchor 的任务切换记录损坏，已停止处理。",
    )
    new_task_id = canonical_task_id(transition.get("new_task_id"))
    raw_old_task_ids = transition.get("old_task_ids")
    if not isinstance(raw_old_task_ids, list):
        raise StorageError("Task Anchor 的任务切换记录校验失败，已停止处理。")

    old_task_ids: list[str] = []
    for raw_task_id in raw_old_task_ids:
        task_id = canonical_task_id(raw_task_id)
        if task_id is None:
            raise StorageError("Task Anchor 的任务切换记录校验失败，已停止处理。")
        old_task_ids.append(task_id)

    if (
        transition.get("schema_version") != SCHEMA_VERSION
        or transition.get(SESSION_KEY_FIELD) != session_key(session_id)
        or new_task_id is None
        or len(set(old_task_ids)) != len(old_task_ids)
        or new_task_id in old_task_ids
    ):
        raise StorageError("Task Anchor 的任务切换记录校验失败，已停止处理。")

    return {
        **transition,
        "new_task_id": new_task_id,
        "old_task_ids": old_task_ids,
    }


def complete_pending_transition(
    data_root: Path,
    session_id: str,
    transition: dict[str, Any],
) -> list[str]:
    """在会话锁内完成已持久化的任务切换。"""
    new_task_id = transition["new_task_id"]
    assert isinstance(new_task_id, str)

    records = read_all_task_metadata(data_root, session_id)
    new_metadata = records.get(new_task_id)
    if new_metadata is None:
        raise StorageError("Task Anchor 的待激活任务记录缺失，已停止处理。")

    closed_task_ids: list[str] = []
    for old_task_id, old_metadata in records.items():
        if old_task_id == new_task_id or old_metadata["status"] == TASK_STATUS_CLOSED:
            continue
        closed_metadata = dict(old_metadata)
        closed_metadata["status"] = TASK_STATUS_CLOSED
        closed_metadata["closed_at"] = utc_now()
        closed_metadata["closed_reason"] = CLOSED_REASON_SUPERSEDED
        atomic_write_json(
            task_metadata_path(data_root, session_id, old_task_id),
            closed_metadata,
        )
        closed_task_ids.append(old_task_id)

    active_metadata = dict(new_metadata)
    active_metadata["status"] = TASK_STATUS_ACTIVE
    active_metadata.pop("transition_state", None)
    active_metadata["activated_at"] = utc_now()
    atomic_write_json(
        task_metadata_path(data_root, session_id, new_task_id),
        active_metadata,
    )
    atomic_write_json(
        current_task_path(data_root, session_id),
        {
            "schema_version": SCHEMA_VERSION,
            SESSION_KEY_FIELD: session_key(session_id),
            "task_id": new_task_id,
            "updated_at": utc_now(),
        },
    )
    try:
        transition_path(data_root, session_id).unlink()
    except OSError as exc:
        raise StorageError("Task Anchor 无法完成任务切换清理，已停止处理。") from exc
    return closed_task_ids


def start_new_task(
    data: dict[str, Any],
    data_root: Path | None,
) -> dict[str, Any] | None:
    prompt = data.get("prompt")
    if not is_explicit_activation(prompt):
        return None
    assert isinstance(prompt, str)

    if data_root is None:
        return warning("Task Anchor 无法创建任务：Hook 未提供 PLUGIN_DATA。")
    current_session_id = read_session_id(data)
    if current_session_id is None:
        write_audit_event(data_root, data, "activation_missing_session_id")
        return warning("Task Anchor 无法创建任务：Hook 输入缺少 session_id。")
    workspace_sha256 = read_workspace_sha256(data)
    if workspace_sha256 is None:
        write_audit_event(data_root, data, "activation_missing_cwd")
        return warning("Task Anchor 无法创建任务：Hook 输入缺少 cwd，无法绑定项目边界。")

    policy_result = set_mutation_policy(data, data_root, read_only=False)
    if policy_result is not None:
        return policy_result

    write_audit_event(data_root, data, "activation_received")
    session_dir = session_directory(data_root, current_session_id)
    new_task_id = str(uuid.uuid4())
    instruction_bytes = prompt.encode("utf-8")
    new_metadata = {
        "schema_version": SCHEMA_VERSION,
        "task_id": new_task_id,
        SESSION_KEY_FIELD: session_key(current_session_id),
        "sha256": sha256_bytes(instruction_bytes),
        WORKSPACE_SHA256_FIELD: workspace_sha256,
        "created_at": utc_now(),
        "status": TASK_STATUS_ACTIVE,
    }
    closed_task_ids: list[str] = []
    recovered_task_id: str | None = None
    recovered_closed_task_ids: list[str] = []

    try:
        with session_lock(session_dir):
            pending_transition = read_pending_transition(data_root, current_session_id)
            if pending_transition is not None:
                recovered_task_id = pending_transition["new_task_id"]
                assert isinstance(recovered_task_id, str)
                recovered_closed_task_ids = complete_pending_transition(
                    data_root,
                    current_session_id,
                    pending_transition,
                )

            existing = read_all_task_metadata(data_root, current_session_id)
            new_directory = task_directory(data_root, current_session_id, new_task_id)
            if new_directory.exists():
                raise StorageError("Task Anchor 生成的任务 ID 已存在，请重试。")

            staged_metadata = dict(new_metadata)
            staged_metadata["status"] = TASK_STATUS_CLOSED
            staged_metadata["transition_state"] = "pending_activation"
            atomic_write(
                task_instruction_path(data_root, current_session_id, new_task_id),
                instruction_bytes,
            )
            atomic_write_json(
                task_metadata_path(data_root, current_session_id, new_task_id),
                staged_metadata,
            )
            atomic_write_json(
                transition_path(data_root, current_session_id),
                {
                    "schema_version": SCHEMA_VERSION,
                    SESSION_KEY_FIELD: session_key(current_session_id),
                    "new_task_id": new_task_id,
                    "old_task_ids": [
                        task_id
                        for task_id, metadata in existing.items()
                        if metadata["status"] == TASK_STATUS_ACTIVE
                    ],
                    "created_at": utc_now(),
                },
            )
            transition = read_pending_transition(data_root, current_session_id)
            if transition is None:
                raise StorageError("Task Anchor 的任务切换记录缺失，已停止处理。")
            closed_task_ids = complete_pending_transition(
                data_root,
                current_session_id,
                transition,
            )
    except LockUnavailable:
        write_audit_event(data_root, data, "activation_lock_unavailable")
        return warning("Task Anchor 当前任务状态正由另一项操作切换，请稍后重新调用。")
    except (OSError, StorageError):
        write_audit_event(data_root, data, "activation_write_failed")
        return warning(
            "Task Anchor 无法确认新任务已建立；请重新显式调用 $task-anchor。"
        )

    if has_legacy_session_layout(data_root, current_session_id):
        write_audit_event(data_root, data, "legacy_layout_superseded")
    if recovered_task_id is not None:
        for old_task_id in recovered_closed_task_ids:
            write_audit_event(
                data_root,
                data,
                "task_closed",
                task_id=old_task_id,
                closed_reason=CLOSED_REASON_SUPERSEDED,
            )
        write_audit_event(
            data_root,
            data,
            "activation_recovered",
            task_id=recovered_task_id,
            closed_task_count=len(recovered_closed_task_ids),
        )
    for old_task_id in closed_task_ids:
        write_audit_event(
            data_root,
            data,
            "task_closed",
            task_id=old_task_id,
            closed_reason=CLOSED_REASON_SUPERSEDED,
        )
    write_audit_event(
        data_root,
        data,
        "activation_completed",
        task_id=new_task_id,
        instruction_sha256=new_metadata["sha256"],
        instruction_bytes=len(instruction_bytes),
        closed_task_count=len(closed_task_ids),
    )
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        try:
            resource_manager.set_active_context(cwd, current_session_id, new_task_id)
        except (OSError, resource_manager.ResourceError) as exc:
            write_audit_event(
                data_root,
                data,
                "resource_context_failed",
                task_id=new_task_id,
                error=str(exc),
            )
    return None


def end_current_task(
    data: dict[str, Any],
    data_root: Path | None,
) -> dict[str, Any] | None:
    prompt = data.get("prompt")
    if not is_explicit_end(prompt):
        return None

    if data_root is None:
        return warning("Task Anchor 无法结束任务：Hook 未提供 PLUGIN_DATA。")
    current_session_id = read_session_id(data)
    if current_session_id is None:
        write_audit_event(data_root, data, "manual_end_missing_session_id")
        return warning("Task Anchor 无法结束任务：Hook 输入缺少 session_id。")

    try:
        with session_lock(session_directory(data_root, current_session_id)):
            if read_pending_transition(data_root, current_session_id) is not None:
                write_audit_event(data_root, data, "manual_end_pending_transition")
                return warning(
                    "Task Anchor 检测到未完成的任务切换，无法确认要结束的任务；"
                    "请稍后重新调用 $task-anchor-end。"
                )

            task_id = read_current_task_id(data_root, current_session_id)
            if task_id is None:
                write_audit_event(data_root, data, "manual_end_no_current_task")
                return warning("Task Anchor 当前会话没有可手工结束的任务。")

            _, metadata = load_task(
                data,
                data_root,
                current_session_id,
                task_id,
            )
            if metadata["status"] == TASK_STATUS_CLOSED:
                write_audit_event(
                    data_root,
                    data,
                    "manual_end_already_closed",
                    task_id=task_id,
                    closed_reason=metadata.get("closed_reason"),
                )
                return warning("Task Anchor 当前任务已经结束。")

            closed_metadata = dict(metadata)
            closed_metadata["status"] = TASK_STATUS_CLOSED
            closed_metadata["closed_at"] = utc_now()
            closed_metadata["closed_reason"] = CLOSED_REASON_MANUAL
            atomic_write_json(
                task_metadata_path(data_root, current_session_id, task_id),
                closed_metadata,
            )
    except LockUnavailable:
        write_audit_event(data_root, data, "manual_end_lock_unavailable")
        return warning("Task Anchor 当前任务状态正由另一项操作切换，请稍后重新调用。")
    except (OSError, StorageError) as exc:
        write_audit_event(data_root, data, "manual_end_failed")
        return warning(f"Task Anchor 无法结束当前任务：{exc}")

    write_audit_event(
        data_root,
        data,
        "task_closed",
        task_id=task_id,
        closed_reason=CLOSED_REASON_MANUAL,
    )
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        try:
            cleanup_result = resource_manager.cleanup_for_stop(
                cwd=cwd,
                session_id=current_session_id,
                task_id=task_id,
            )
            write_audit_event(
                data_root,
                data,
                "resources_cleaned_on_manual_end",
                task_id=task_id,
                stopped_count=len(cleanup_result.get("stopped", [])),
                kept_count=len(cleanup_result.get("kept", [])),
            )
        except (OSError, resource_manager.ResourceError) as exc:
            write_audit_event(
                data_root,
                data,
                "resource_cleanup_failed_on_manual_end",
                task_id=task_id,
                error=str(exc),
            )
    return None


# 在上下文压缩后注入连续性提醒，并仅恢复校验通过的当前活动任务。
def restore_after_post_compact(
    data: dict[str, Any],
    data_root: Path | None,
) -> dict[str, Any] | None:
    if data.get("trigger") not in {"auto", "manual"}:
        write_audit_event(data_root, data, "post_compact_ignored_trigger")
        return post_compact_output()
    if data_root is None:
        return post_compact_output()

    write_audit_event(data_root, data, "post_compact_received")
    current_session_id = read_session_id(data)
    if current_session_id is None:
        write_audit_event(data_root, data, "restore_missing_session_id")
        return post_compact_warning("Task Anchor 无法恢复任务：Hook 输入缺少 session_id。")

    try:
        with session_lock(session_directory(data_root, current_session_id)):
            pending_transition = read_pending_transition(data_root, current_session_id)
            if pending_transition is not None:
                task_id = pending_transition["new_task_id"]
                assert isinstance(task_id, str)
                write_audit_event(
                    data_root,
                    data,
                    "restore_pending_transition",
                    task_id=task_id,
                )
                return post_compact_warning(
                    "Task Anchor 检测到未完成的任务切换，未恢复任务指令；"
                    "请重新显式调用 $task-anchor。"
                )

            task_id = read_current_task_id(data_root, current_session_id)
            if task_id is None:
                if has_legacy_session_layout(data_root, current_session_id):
                    write_audit_event(data_root, data, "legacy_layout_skipped")
                    return post_compact_warning(
                        "Task Anchor 检测到旧版任务记录，未恢复旧指令；"
                        "请重新显式调用 $task-anchor 创建当前任务。"
                    )
                write_audit_event(data_root, data, "restore_no_current_task")
                return post_compact_output()

            instruction, metadata = load_task(
                data,
                data_root,
                current_session_id,
                task_id,
            )
            if metadata["status"] == TASK_STATUS_CLOSED:
                write_audit_event(
                    data_root,
                    data,
                    "restore_closed",
                    task_id=task_id,
                    closed_reason=metadata.get("closed_reason"),
                )
                return post_compact_output()
    except LockUnavailable:
        write_audit_event(data_root, data, "restore_lock_unavailable")
        return post_compact_warning("Task Anchor 当前任务状态正由另一项操作切换，未恢复任务指令。")
    except StorageError as exc:
        write_audit_event(data_root, data, "restore_rejected")
        return post_compact_warning(str(exc))

    task_context = (
        f"必须使用 {SKILL_MARKER} Skill 继续当前任务。\n\n"
        "[Task Anchor 状态提醒]\n"
        f"- task_id: {task_id}\n"
        "- status: 1（进行中；自动完成状态无法验证）\n"
        "- 本提醒不要求作答，不改变任务状态，也不阻断当前任务。\n\n"
        f"[最初任务指令]\n{instruction}"
    )
    write_audit_event(
        data_root,
        data,
        "restore_emitted",
        task_id=task_id,
        instruction_sha256=metadata["sha256"],
        instruction_bytes=len(instruction.encode("utf-8")),
        task_status=metadata["status"],
    )
    return post_compact_output(task_context)


def _tool_name(data: dict[str, Any]) -> str:
    for key in ("tool_name", "toolName", "name"):
        value = data.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def _is_managed_exec_tool(tool_name: str) -> bool:
    normalized = tool_name.strip().lower()
    if normalized in {item.lower() for item in MANAGED_EXEC_TOOL_NAMES}:
        return True
    return normalized.endswith("__managed_exec")


def _is_fastctx_tool(tool_name: str) -> bool:
    """只按 MCP 工具名称识别 FastCtx，不依赖其安装、配置或运行状态。"""

    return tool_name.strip().lower().startswith(FASTCTX_TOOL_NAME_PREFIX)


def _is_fastctx_read_only_tool(tool_name: str) -> bool:
    return tool_name.strip().lower() in FASTCTX_READ_ONLY_TOOL_NAMES


def _tool_name_tokens(tool_name: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", tool_name.strip().lower())
        if token
    }


def _is_mutation_capable_tool(tool_name: str) -> bool:
    normalized = tool_name.strip().lower()
    if normalized in COMMAND_TOOL_NAMES:
        return True
    if _is_managed_exec_tool(normalized):
        return True
    tokens = _tool_name_tokens(normalized)
    if tokens & MUTATION_TOOL_TOKENS:
        return True
    return "create" in tokens and bool(tokens & FILE_RESOURCE_TOKENS)


def _deny_read_only(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": PRE_TOOL_USE,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _managed_exec_input(data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """返回 managed_exec 的输入字段和值，兼容 Codex 的字段命名变体。"""

    for key in ("tool_input", "toolInput", "input"):
        value = data.get(key)
        if isinstance(value, dict):
            return key, value
    return None


def _bind_managed_exec_to_session(data: dict[str, Any]) -> dict[str, Any] | None:
    """在工具实际执行前，将当前 Hook 会话绑定到 managed_exec。"""

    current_session_id = read_session_id(data)
    tool_input = _managed_exec_input(data)
    if current_session_id is None or tool_input is None:
        return None

    _, original_input = tool_input
    explicit_session_id = original_input.get("session_id")
    if isinstance(explicit_session_id, str) and explicit_session_id.strip():
        return None

    updated_input = dict(original_input)
    updated_input["session_id"] = current_session_id
    updated_input["env"] = dict(os.environ)
    return {
        "hookSpecificOutput": {
            "hookEventName": PRE_TOOL_USE,
            "permissionDecision": "allow",
            "permissionDecisionReason": "bound managed_exec to the current session",
            "updatedInput": updated_input,
        }
    }


def _command_text(data: dict[str, Any]) -> str:
    """提取 Hook 输入中的命令字符串，不解析命令结构。"""

    parts: list[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if not isinstance(value, dict) or depth > 2:
            return
        for key in COMMAND_TEXT_KEYS:
            item = value.get(key)
            if isinstance(item, str):
                parts.append(item)
        for key in NESTED_COMMAND_KEYS:
            visit(value.get(key), depth + 1)

    visit(data)
    return " ".join(parts)


def _matched_process_keyword(command_text: str) -> str | None:
    normalized = command_text.lower()
    return next(
        (keyword for keyword in PROCESS_COMMAND_KEYWORDS if keyword in normalized),
        None,
    )


def _is_excluded_project(cwd: object) -> bool:
    """安全读取用户排除配置，判断当前工作目录是否属于排除项目。"""

    if not isinstance(cwd, str) or not cwd.strip():
        return False
    try:
        config_path = Path.home() / ".task_anchor" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(config, dict):
        return False
    excluded_projects = config.get("excludeProjects")
    if not isinstance(excluded_projects, list):
        return False
    try:
        current_path = Path(cwd).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for project in excluded_projects:
        if not isinstance(project, str) or not project.strip():
            continue
        try:
            if current_path == Path(project).resolve():
                return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


def guard_pre_tool_use(
    data: dict[str, Any],
    data_root: Path | None,
) -> dict[str, Any] | None:
    """限制 FastCtx 工具，并要求进程型命令通过 managed_exec 执行。"""

    if _is_excluded_project(data.get("cwd")):
        return None

    tool_name = _tool_name(data)
    if _is_fastctx_tool(tool_name):
        if _is_fastctx_read_only_tool(tool_name):
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": PRE_TOOL_USE,
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "FastCtx only permits inspect_local_file, grep, and glob."
                ),
            }
        }
    if _is_mutation_capable_tool(tool_name):
        if data_root is None:
            return _deny_read_only("Read-only policy context is unavailable.")
        try:
            read_only = read_mutation_policy(data, data_root)
        except StorageError as exc:
            write_audit_event(
                data_root,
                data,
                "mutation_policy_rejected",
                tool_name=tool_name,
                error=str(exc),
            )
            return _deny_read_only("Read-only policy state could not be verified.")
        if read_only:
            write_audit_event(
                data_root,
                data,
                "mutation_blocked",
                tool_name=tool_name,
            )
            return _deny_read_only(
                f"{READ_ONLY_SKILL_MARKER} blocks mutation-capable tools; "
                f"invoke {WRITE_SKILL_MARKER} to allow modifications."
            )
    if _is_managed_exec_tool(tool_name):
        return _bind_managed_exec_to_session(data)

    command_text = _command_text(data)
    matched_keyword = _matched_process_keyword(command_text)
    if matched_keyword is None:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": PRE_TOOL_USE,
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Process keyword {matched_keyword!r} requires "
                "mcp__task_anchor__managed_exec."
            ),
        },
    }


def cleanup_after_stop(data: dict[str, Any], data_root: Path | None) -> None:
    """Stop 时清理当前会话登记的默认资源，keep 资源保持不动。"""

    cwd = data.get("cwd")
    current_session_id = read_session_id(data)
    if not isinstance(cwd, str) or not cwd.strip() or current_session_id is None:
        write_audit_event(data_root, data, "resource_cleanup_skipped_missing_context")
        return
    try:
        result = resource_manager.cleanup_for_stop(
            cwd=cwd,
            session_id=current_session_id,
        )
        write_audit_event(
            data_root,
            data,
            "resources_cleaned_on_stop",
            stopped_count=len(result.get("stopped", [])),
            failed_count=len(result.get("failed", [])),
            kept_count=len(result.get("kept", [])),
        )
    except (OSError, resource_manager.ResourceError) as exc:
        # Stop 清理失败不能阻塞宿主结束当前回合，但必须留下审计线索。
        write_audit_event(data_root, data, "resource_cleanup_failed_on_stop", error=str(exc))


def handle_hook(
    data: dict[str, Any],
    data_root: Path | None,
) -> dict[str, Any] | None:
    event_name = data.get("hook_event_name")
    if event_name == USER_PROMPT_SUBMIT:
        prompt = data.get("prompt")
        read_only_requested = is_explicit_read_only(prompt)
        write_requested = is_explicit_write(prompt)
        if read_only_requested and write_requested:
            return warning(
                "Task Anchor 同一条消息同时调用了只读和可写 Skill，门控状态未改变。"
            )
        if read_only_requested:
            return set_mutation_policy(data, data_root, read_only=True)
        if write_requested:
            return set_mutation_policy(data, data_root, read_only=False)
        if is_explicit_end(data.get("prompt")):
            return end_current_task(data, data_root)
        return start_new_task(data, data_root)
    if event_name == POST_COMPACT:
        return restore_after_post_compact(data, data_root)
    if event_name == PRE_TOOL_USE:
        return guard_pre_tool_use(data, data_root)
    if event_name == STOP:
        cleanup_after_stop(data, data_root)
        return None
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.stdout.write(
            json.dumps(warning("Task Anchor 收到无效的 Hook JSON 输入。"), ensure_ascii=False)
        )
        return 0
    if not isinstance(data, dict):
        sys.stdout.write(
            json.dumps(warning("Task Anchor 收到的 Hook 输入不是 JSON 对象。"), ensure_ascii=False)
        )
        return 0

    raw_data_root = os.environ.get("PLUGIN_DATA")
    data_root = Path(raw_data_root) if raw_data_root else None
    payload = handle_hook(data, data_root)
    if payload is not None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
