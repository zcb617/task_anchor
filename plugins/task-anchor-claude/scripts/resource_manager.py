"""Task Anchor 的受管进程登记与清理。

这个模块只管理由 ``managed_exec`` 启动并登记的进程，不按进程名扫描系统。
默认策略是 Stop 时清理；显式使用 ``stop_policy=keep`` 的记录会保留，直到
调用者按 run_id/name 手工停止。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from task_anchor_logger import TaskAnchorLogger


SCHEMA_VERSION = 1
STOP_POLICY_CLEANUP = "cleanup"
STOP_POLICY_KEEP = "keep"
VALID_STOP_POLICIES = {STOP_POLICY_CLEANUP, STOP_POLICY_KEEP}
_LIVE_PROCESSES: dict[int, subprocess.Popen[Any]] = {}

PLATFORM_WINDOWS = "windows"
PLATFORM_MACOS = "macos"
PLATFORM_LINUX = "linux"
# Windows 需要由命令解释器解析的批处理程序扩展名。
WINDOWS_BATCH_EXTENSIONS = {".bat", ".cmd"}
# 跨 Node/Python 账本目录锁的最大等待时间。
LOCK_TIMEOUT_SECONDS = 10.0
# 跨 Node/Python 账本目录锁的竞争重试间隔。
LOCK_RETRY_SECONDS = 0.025


def current_platform() -> str:
    """返回受控管理器支持的平台，拒绝静默落入未知分支。"""

    system = platform.system().lower()
    if system == "windows":
        return PLATFORM_WINDOWS
    if system == "darwin":
        return PLATFORM_MACOS
    if system == "linux":
        return PLATFORM_LINUX
    raise ResourceError(f"暂不支持的平台：{platform.system() or 'unknown'}")


class ResourceError(RuntimeError):
    """受管资源操作失败。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_path(path: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))
    except (OSError, ValueError) as exc:
        raise ResourceError(f"无法解析工作目录：{path}") from exc


def workspace_identity(cwd: str) -> str:
    normalized = Path(normalize_path(cwd))
    candidate = normalized
    while True:
        marker = candidate / ".git"
        if marker.is_dir() or marker.is_file():
            return f"git:{os.path.normcase(os.path.realpath(str(candidate)))}"
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    return f"cwd:{normalized}"


def workspace_key(cwd: str) -> str:
    return sha256_text(workspace_identity(cwd))


def runtime_root() -> Path:
    override = os.environ.get("TASK_ANCHOR_RUNTIME_ROOT")
    if override and override.strip():
        return Path(override).expanduser()

    if current_platform() == PLATFORM_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "TaskAnchor" / "runtime"

    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "task-anchor"


def workspace_runtime_directory(cwd: str) -> Path:
    return runtime_root() / "workspaces" / workspace_key(cwd)


def ledger_path(cwd: str) -> Path:
    return workspace_runtime_directory(cwd) / "resources.json"


def context_path(cwd: str) -> Path:
    return workspace_runtime_directory(cwd) / "active-context.json"


@contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """使用 .lock.d 原子目录创建 Node/Python 共享的账本互斥锁。"""

    lock_directory = lock_path
    lock_directory.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    acquired = False
    while not acquired:
        try:
            lock_directory.mkdir()
            acquired = True
        except FileExistsError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResourceError(f"资源锁等待超时：{lock_directory}") from exc
            time.sleep(min(LOCK_RETRY_SECONDS, remaining))
        except OSError as exc:
            raise ResourceError(f"无法创建资源锁：{lock_directory}") from exc

    try:
        yield
    finally:
        try:
            lock_directory.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ResourceError(f"无法释放资源锁：{lock_directory}") from exc


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
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


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        raise ResourceError(f"资源记录损坏：{path}") from exc


def _write_json(path: Path, value: Any) -> None:
    atomic_write(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
    )


def normalize_stop_policy(value: Any) -> str:
    if value is None or value == "":
        return STOP_POLICY_CLEANUP
    if value not in VALID_STOP_POLICIES:
        raise ResourceError("stop_policy 只能是 cleanup 或 keep。")
    return value


def _session_key(session_id: str | None) -> str | None:
    if isinstance(session_id, str) and session_id:
        return sha256_text(session_id)
    return None


def active_context(
    cwd: str,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    value = _read_json(context_path(cwd), None)
    if not isinstance(value, dict):
        return None

    contexts = value.get("contexts")
    if isinstance(contexts, dict):
        requested_session_key = _session_key(session_id)
        if requested_session_key:
            context = contexts.get(requested_session_key)
            return context if isinstance(context, dict) else None
        latest_session_key = value.get("latest_session_key")
        context = contexts.get(latest_session_key)
        return context if isinstance(context, dict) else None

    # 兼容 0.2.0 之前的单一 active-context.json。
    return value


def set_active_context(cwd: str, session_id: str, task_id: str) -> None:
    normalized_cwd = normalize_path(cwd)
    context = {
        "schema_version": SCHEMA_VERSION,
        "session_key": _session_key(session_id),
        "task_id": task_id,
        "workspace_key": workspace_key(normalized_cwd),
        "cwd": normalized_cwd,
        "updated_at": utc_now(),
    }
    session_key = _session_key(session_id)
    if session_key is None:
        raise ResourceError("无法记录没有 session_id 的任务上下文。")
    path = context_path(normalized_cwd)
    with file_lock(path.with_suffix(".lock.d")):
        stored = _read_json(path, {})
        contexts: dict[str, Any] = {}
        if isinstance(stored, dict) and isinstance(stored.get("contexts"), dict):
            contexts.update(stored["contexts"])
        elif isinstance(stored, dict) and stored.get("session_key"):
            contexts[str(stored["session_key"])] = stored
        contexts[session_key] = context
        _write_json(
            path,
            {
                "schema_version": SCHEMA_VERSION,
                "contexts": contexts,
                "latest_session_key": session_key,
                "updated_at": utc_now(),
            },
        )


def resolve_owner(
    cwd: str,
    session_id: str | None = None,
    task_id: str | None = None,
) -> tuple[str, str | None, str | None]:
    normalized_cwd = normalize_path(cwd)
    session_key = _session_key(session_id)
    resolved_task_id = task_id if isinstance(task_id, str) and task_id else None
    context = active_context(normalized_cwd, session_id=session_id)
    if context:
        context_session_key = context.get("session_key")
        if session_key is None and isinstance(context_session_key, str):
            session_key = context_session_key
        if resolved_task_id is None and (
            session_key is None or context_session_key == session_key
        ):
            context_task_id = context.get("task_id")
            if isinstance(context_task_id, str) and context_task_id:
                resolved_task_id = context_task_id
    if not session_key:
        raise ResourceError(
            "无法确定资源归属：必须提供 session_id，或先建立当前会话上下文。"
        )
    owner_key = f"session:{session_key}"
    return owner_key, session_key, resolved_task_id


def _load_records(cwd: str) -> list[dict[str, Any]]:
    value = _read_json(ledger_path(cwd), [])
    if not isinstance(value, list):
        raise ResourceError("资源记录不是数组。")
    return [item for item in value if isinstance(item, dict)]


def _save_records(cwd: str, records: list[dict[str, Any]]) -> None:
    _write_json(ledger_path(cwd), records)


def _process_alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        if current_platform() == PLATFORM_WINDOWS:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            output = result.stdout.strip().lower()
            return bool(
                result.returncode == 0
                and output
                and "no tasks are running" not in output
            )
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _process_launch_options(platform_name: str) -> dict[str, Any]:
    """为不同系统创建可被整棵结束的进程组。"""

    if platform_name == PLATFORM_WINDOWS:
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        }
    if platform_name in {PLATFORM_MACOS, PLATFORM_LINUX}:
        return {"start_new_session": True}
    raise ResourceError(f"暂不支持的平台：{platform_name}")


def _process_group_id(pid: int) -> int:
    """读取 POSIX 进程组，并避免误杀当前管理器所在的进程组。"""

    try:
        group_id = os.getpgid(pid)
        own_group_id = os.getpgrp()
    except OSError as exc:
        raise ResourceError(f"无法读取 PID {pid} 的进程组：{exc}") from exc
    if group_id <= 0 or group_id == own_group_id:
        raise ResourceError(f"拒绝结束不安全的进程组：PID {pid}，PGID {group_id}")
    return group_id


def _terminate_pid(pid: int, grace_seconds: float = 2.0) -> dict[str, Any]:
    if not _process_alive(pid):
        process = _LIVE_PROCESSES.pop(pid, None)
        if process is not None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        return {"status": "already_stopped", "pid": pid}

    if current_platform() == PLATFORM_WINDOWS:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0 and _process_alive(pid):
            raise ResourceError(
                f"无法结束 PID {pid}：{(result.stderr or result.stdout).strip()}"
            )
        process = _LIVE_PROCESSES.pop(pid, None)
        if process is not None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired as exc:
                raise ResourceError(f"PID {pid} 已请求结束但未能回收。") from exc
        return {"status": "stopped", "pid": pid}

    group_id = _process_group_id(pid)
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return {"status": "already_stopped", "pid": pid}
    except OSError as exc:
        raise ResourceError(f"无法结束 PID {pid} 所在进程组：{exc}") from exc
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and _process_alive(pid):
        time.sleep(0.1)
    if _process_alive(pid):
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise ResourceError(f"无法强制结束 PID {pid} 所在进程组：{exc}") from exc
    process = _LIVE_PROCESSES.pop(pid, None)
    if process is not None:
        process.wait(timeout=10)
    return {"status": "stopped", "pid": pid}


def _command_text(program: str | None, args: list[str], command: str | None) -> str:
    if isinstance(command, str) and command.strip():
        return command.strip()
    if not isinstance(program, str) or not program.strip():
        raise ResourceError("run 操作必须提供 program 或 command。")
    return " ".join([program, *args])


def _validate_args(args: Any) -> list[str]:
    if args is None:
        return []
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ResourceError("args 必须是字符串数组。")
    return list(args)


def _environment_value(environment: dict[str, str], name: str) -> str | None:
    """按不区分大小写的环境变量名读取允许写入诊断日志的值。"""
    expected = name.casefold()
    for key, value in environment.items():
        if isinstance(key, str) and key.casefold() == expected:
            return value
    return None


def _resolve_windows_batch_program(
    program: str | None,
    cwd: str,
    environment: dict[str, str],
) -> str | None:
    """解析 Windows 当前目录或 PATH 中已存在的批处理程序，仅用于诊断记录。"""
    if current_platform() != PLATFORM_WINDOWS or not isinstance(program, str) or not program.strip():
        return None
    normalized_program = program.strip()
    program_extension = Path(normalized_program).suffix.lower()
    if program_extension and program_extension not in WINDOWS_BATCH_EXTENSIONS:
        return None
    has_path = os.path.isabs(normalized_program) or "/" in normalized_program or "\\" in normalized_program
    if program_extension:
        extensions = [""]
    else:
        path_extensions = str(_environment_value(environment, "PATHEXT") or ".BAT;.CMD")
        extensions = [
            extension.strip().lower()
            for extension in path_extensions.split(";")
            if extension.strip().lower() in WINDOWS_BATCH_EXTENSIONS
        ]
    if not extensions:
        return None
    if has_path:
        search_directories = [os.path.dirname(os.path.abspath(os.path.join(cwd, normalized_program)))]
    else:
        search_directories = [cwd]
        search_directories.extend(
            entry.strip().strip('"')
            for entry in str(_environment_value(environment, "PATH") or "").split(os.pathsep)
            if entry.strip()
        )
    basename = os.path.basename(normalized_program)
    for directory in search_directories:
        for extension in extensions:
            candidate = Path(directory) / f"{basename}{extension}"
            if candidate.is_file():
                return str(candidate)
    return None


def _read_log(log_path: Path, limit: int = 20_000) -> str:
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(content) <= limit:
        return content
    return f"[输出已截断，保留末尾 {limit} 个字符]\n{content[-limit:]}"


def _is_posix_shell_background_command(
    command: Any, platform_name: str, shell: bool
) -> bool:
    """判断 POSIX shell 命令是否试图用末尾的 & 脱离 Task Anchor 生命周期管控。"""
    if shell is not True:
        return False
    if platform_name not in (PLATFORM_LINUX, PLATFORM_MACOS):
        return False
    if not isinstance(command, str):
        return False
    trimmed = command.rstrip()
    if not trimmed.endswith("&") or trimmed.endswith("&&"):
        return False
    quote: str | None = None
    for character in trimmed:
        if quote == "'":
            if character == "'":
                quote = None
            continue
        if quote == '"':
            if character == '"':
                quote = None
            continue
        if character in ("'", '"'):
            quote = character
    return quote is None


def start_process(
    *,
    cwd: str,
    program: str | None = None,
    args: Any = None,
    command: str | None = None,
    shell: bool = False,
    timeout_ms: int | None = 1_800_000,
    stop_policy: Any = None,
    name: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    env: dict[str, str] | None = None,
    on_output: Callable[[dict[str, Any]], None] | None = None,
    on_completion: Callable[[dict[str, Any]], None] | None = None,
    on_error: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """启动并登记受管进程，输出和退出状态通过后台回调即时交付。"""
    platform_name = current_platform()
    normalized_cwd = normalize_path(cwd)
    if not Path(normalized_cwd).is_dir():
        raise ResourceError(f"工作目录不存在：{normalized_cwd}")
    normalized_args = _validate_args(args)
    normalized_policy = normalize_stop_policy(stop_policy)
    normalized_name = name.strip() if isinstance(name, str) and name.strip() else None
    if normalized_policy == STOP_POLICY_KEEP and normalized_name is None:
        raise ResourceError("stop_policy=keep 必须同时提供 name，便于后续显式停止。")
    display_command = _command_text(program, normalized_args, command)
    if shell:
        if not isinstance(command, str) or not command.strip():
            raise ResourceError("shell=true 时必须提供 command 字符串。")
        if _is_posix_shell_background_command(command, platform_name, True):
            raise ResourceError(
                "Task Anchor 不允许使用 & 后台运行；持续进程请直接启动，由 Task Anchor 管理生命周期。"
            )
        popen_args: Any = command
    else:
        if not isinstance(program, str) or not program.strip():
            raise ResourceError("shell=false 时必须提供 program。")
        popen_args = [program, *normalized_args]

    owner_key, resolved_session_key, resolved_task_id = resolve_owner(
        normalized_cwd, session_id, task_id
    )
    run_id = str(uuid.uuid4())
    log_path = workspace_runtime_directory(normalized_cwd) / "logs" / f"{run_id}.log"
    diagnostic_log_path = (
        workspace_runtime_directory(normalized_cwd) / "logs" / f"{run_id}.events.jsonl"
    )
    logger = TaskAnchorLogger(str(diagnostic_log_path))
    execution_environment = dict(os.environ) if env is None else env
    logger.info(
        "launch_requested",
        {
            "run_id": run_id,
            "cwd": normalized_cwd,
            "platform": platform_name,
            "program": program,
            "args": normalized_args,
            "command": command,
            "shell": bool(shell),
            "timeout_ms": timeout_ms,
            "stop_policy": normalized_policy,
            "environment_source": "process" if env is None else "provided",
        },
    )
    logger.debug(
        "execution_environment",
        {
            "path": _environment_value(execution_environment, "PATH"),
            "pathext": _environment_value(execution_environment, "PATHEXT"),
            "comspec": _environment_value(execution_environment, "ComSpec"),
        },
    )
    batch_program = _resolve_windows_batch_program(program, normalized_cwd, execution_environment)
    if platform_name == PLATFORM_WINDOWS and not shell:
        logger.debug(
            "windows_batch_resolved",
            {"program": program, "batch_program": batch_program},
        )
    launch_options = _process_launch_options(platform_name)
    logger.debug(
        "spawn_attempted",
        {
            "spawn_target": popen_args,
            "spawn_args": normalized_args if not shell else [],
            "cwd": normalized_cwd,
            "shell": bool(shell),
            **launch_options,
        },
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    popen_kwargs: dict[str, Any] = {
        "cwd": normalized_cwd,
        "shell": shell,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
    }
    popen_kwargs.update(launch_options)
    try:
        process = subprocess.Popen(popen_args, **popen_kwargs)
        logger.info("spawn_succeeded", {"pid": process.pid})
    except (OSError, ValueError) as exc:
        error_code = getattr(exc, "errno", None)
        logger.warning(
            "spawn_failed",
            {
                "error": str(exc),
                **({"code": error_code} if error_code is not None else {}),
            },
        )
        raise ResourceError(f"启动命令失败：{exc}；诊断日志：{diagnostic_log_path}") from exc
    _LIVE_PROCESSES[process.pid] = process

    record = {
        # 资源记录格式版本。
        "schema_version": SCHEMA_VERSION,
        # 本次受管运行唯一 ID。
        "run_id": run_id,
        # 资源所有者键。
        "owner_key": owner_key,
        # 资源所属会话哈希。
        "session_key": resolved_session_key,
        # 资源所属任务 ID。
        "task_id": resolved_task_id,
        # 工作区哈希。
        "workspace_key": workspace_key(normalized_cwd),
        # 规范化工作目录。
        "cwd": normalized_cwd,
        # 启动平台。
        "platform": platform_name,
        # 直接启动的程序。
        "program": program,
        # 传给程序的参数。
        "args": normalized_args,
        # 展示命令。
        "command": display_command,
        # 操作系统进程 ID。
        "pid": process.pid,
        # UTC 启动时间。
        "started_at": utc_now(),
        # Unix 启动时间戳。
        "started_at_epoch": time.time(),
        # Stop 时清理或保留的策略。
        "stop_policy": normalized_policy,
        # keep 资源名称。
        "name": normalized_name,
        # 合并 stdout/stderr 日志路径。
        "log_path": str(log_path),
        # 结构化生命周期诊断日志路径。
        "diagnostic_log_path": str(diagnostic_log_path),
        # 当前登记状态。
        "status": "running",
    }
    path = ledger_path(normalized_cwd)
    try:
        with file_lock(path.with_suffix(".lock.d")):
            records = _load_records(normalized_cwd)
            records.append(record)
            _save_records(normalized_cwd, records)
    except (OSError, ResourceError) as exc:
        logger.warning("ledger_write_failed", {"run_id": run_id, "error": str(exc)})
        try:
            _terminate_pid(process.pid)
        except (OSError, ResourceError, subprocess.SubprocessError) as termination_error:
            logger.warning("ledger_write_failed", {"run_id": run_id, "error": str(termination_error)})
        raise

    state_lock = threading.Lock()
    output_lock = threading.Lock()
    completion_event = threading.Event()
    state: dict[str, Any] = {
        # 进程和输出是否已完成。
        "finished": False,
        # 首次调用是否已经返回 running 快照。
        "returned_running": False,
        # 完成回调是否已经发送。
        "completion_sent": False,
        # 进程退出码和启动错误。
        "completion": None,
        # 最终业务结果。
        "result": None,
        # timeout 是否触发。
        "timeout_triggered": False,
        # 超时终止是否失败并需要保留账本。
        "preserve_ledger": False,
    }

    def invoke_callback(callback: Callable[[dict[str, Any]], None] | None, event: dict[str, Any], name: str) -> None:
        """安全调用后台生命周期回调，避免回调异常泄漏到工作线程。"""
        if callback is None:
            return
        try:
            callback(event)
        except Exception as exc:  # 回调属于通知路径，不覆盖进程生命周期结果。
            logger.warning(
                "callback_failed",
                {"callback": name, "error": str(exc)},
            )

    def deliver_output(stream_name: str, chunk: bytes) -> None:
        """将单个输出数据块按读取顺序写入日志并即时通知调用方。"""
        with output_lock:
            log_handle.write(chunk)
            log_handle.flush()
        output = bytes(chunk).decode("utf-8", errors="replace")
        invoke_callback(
            on_output,
            {
                # Task Anchor 分配的运行 ID。
                "run_id": run_id,
                # 被执行进程的操作系统 PID。
                "pid": process.pid,
                # 原始输出所属的流。
                "stream": stream_name,
                # 当前到达的数据块原始字符串。
                "output": output,
            },
            "on_output",
        )

    def report_output_read_failure(stream_name: str, exc: Exception) -> None:
        """记录指定输出流的读取异常，保证监控线程仍能完成生命周期收尾。"""
        logger.warning(
            "output_read_failed",
            {"run_id": run_id, "pid": process.pid, "stream": stream_name, "error": str(exc)},
        )

    def read_output(stream_name: str, pipe: Any) -> None:
        """在线程中读取单个输出管道，作为 Windows 管道选择器兼容路径。"""
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                deliver_output(stream_name, chunk)
        except Exception as exc:
            report_output_read_failure(stream_name, exc)
        finally:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

    def read_outputs() -> None:
        """统一读取 stdout/stderr，POSIX 按管道可读事件合并输出并保持先后顺序。"""
        streams = (("stdout", process.stdout), ("stderr", process.stderr))
        if platform_name == PLATFORM_WINDOWS:
            readers = [
                threading.Thread(
                    target=read_output,
                    args=(stream_name, pipe),
                    name=f"task-anchor-{run_id}-{stream_name}",
                    daemon=True,
                )
                for stream_name, pipe in streams
            ]
            for reader in readers:
                reader.start()
            for reader in readers:
                reader.join()
            return

        selector: Any = None
        registered: dict[str, Any] = {}
        closed_streams: set[str] = set()

        def unregister_and_close(stream_name: str, pipe: Any) -> None:
            """注销并关闭指定输出管道，避免 EOF 或异常路径重复关闭。"""
            if stream_name in registered:
                try:
                    selector.unregister(pipe)
                except (KeyError, OSError, ValueError):
                    pass
                registered.pop(stream_name, None)
            if stream_name not in closed_streams:
                closed_streams.add(stream_name)
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass

        try:
            try:
                selector = selectors.DefaultSelector()
            except Exception as exc:
                for stream_name, pipe in streams:
                    if pipe is not None:
                        report_output_read_failure(stream_name, exc)
                        unregister_and_close(stream_name, pipe)
                return

            for stream_name, pipe in streams:
                if pipe is None:
                    continue
                try:
                    selector.register(pipe, selectors.EVENT_READ, data=stream_name)
                    registered[stream_name] = pipe
                except Exception as exc:
                    report_output_read_failure(stream_name, exc)
                    unregister_and_close(stream_name, pipe)

            while registered:
                try:
                    events = selector.select()
                except Exception as exc:
                    for stream_name, pipe in list(registered.items()):
                        report_output_read_failure(stream_name, exc)
                        unregister_and_close(stream_name, pipe)
                    break

                for key, _ in events:
                    stream_name = key.data
                    pipe = registered.get(stream_name)
                    if pipe is None:
                        continue
                    try:
                        chunk = os.read(pipe.fileno(), 4096)
                    except Exception as exc:
                        report_output_read_failure(stream_name, exc)
                        unregister_and_close(stream_name, pipe)
                        continue
                    if not chunk:
                        unregister_and_close(stream_name, pipe)
                        continue
                    try:
                        deliver_output(stream_name, chunk)
                    except Exception as exc:
                        report_output_read_failure(stream_name, exc)
                        unregister_and_close(stream_name, pipe)
        finally:
            for stream_name, pipe in list(registered.items()):
                unregister_and_close(stream_name, pipe)
            if selector is not None:
                try:
                    selector.close()
                except (OSError, ValueError):
                    pass

    def remove_completed_record() -> None:
        """在进程完成后移除账本记录并记录移除异常。"""
        try:
            with file_lock(path.with_suffix(".lock.d")):
                records = [
                    item
                    for item in _load_records(normalized_cwd)
                    if item.get("run_id") != run_id
                ]
                _save_records(normalized_cwd, records)
        except (OSError, ResourceError) as exc:
            logger.warning("ledger_remove_failed", {"run_id": run_id, "error": str(exc)})

    def build_final_result(completion: dict[str, Any]) -> dict[str, Any]:
        """构造退出通知和同步结果共用的完整业务结果。"""
        result: dict[str, Any] = {
            # 受管运行唯一 ID。
            "run_id": run_id,
            # 操作系统进程 ID。
            "pid": process.pid,
            # 进程已经退出。
            "status": "exited",
            # 停止策略。
            "stop_policy": normalized_policy,
            # 展示命令。
            "command": display_command,
            # 规范化工作目录。
            "cwd": normalized_cwd,
            # 运行平台。
            "platform": platform_name,
            # 合并输出日志路径。
            "log_path": str(log_path),
            # 结构化生命周期诊断日志路径。
            "diagnostic_log_path": str(diagnostic_log_path),
            # 被执行程序的完整合并输出。
            "output": _read_log(log_path),
        }
        if state["timeout_triggered"]:
            # 是否因 timeout_ms 触发了整树终止。
            result["timed_out"] = True
        elif isinstance(completion.get("code"), int):
            # 正常退出时返回进程退出码。
            result["exit_code"] = completion["code"]
        return result

    timeout_timer: threading.Timer | None = None

    def monitor_process() -> None:
        """等待进程和统一输出读取线程完成，再清账本并发送退出通知。"""
        exit_code: int | None = None
        wait_error: Exception | None = None
        try:
            exit_code = process.wait()
        except Exception as exc:
            wait_error = exc
        output_reader.join()
        with output_lock:
            try:
                log_handle.flush()
            finally:
                log_handle.close()
        if timeout_timer is not None:
            timeout_timer.cancel()
        logger.info(
            "process_exited",
            {
                "run_id": run_id,
                "pid": process.pid,
                "exit_code": exit_code,
                "signal": None,
            },
        )
        completion = {
            # 进程退出码。
            "code": exit_code,
            # 等待进程时产生的生命周期错误。
            "error": wait_error,
        }
        with state_lock:
            state["completion"] = completion
        if not state["preserve_ledger"]:
            remove_completed_record()
        _LIVE_PROCESSES.pop(process.pid, None)
        final_result = build_final_result(completion)
        with state_lock:
            state["finished"] = True
            state["result"] = final_result
            should_notify = state["returned_running"] and not state["completion_sent"]
            if should_notify:
                state["completion_sent"] = True
        if wait_error is not None:
            invoke_callback(
                on_error,
                {
                    # Task Anchor 分配的运行 ID。
                    "run_id": run_id,
                    # 被执行进程的操作系统 PID。
                    "pid": process.pid,
                    # 等待失败时账本仍记录运行状态。
                    "status": "running",
                    # 异步等待错误文本。
                    "error": str(wait_error),
                },
                "on_error",
            )
        if should_notify:
            invoke_callback(on_completion, final_result, "on_completion")
        completion_event.set()

    log_handle = log_path.open("ab")
    output_reader = threading.Thread(
        target=read_outputs,
        name=f"task-anchor-{run_id}-output-reader",
        daemon=True,
    )
    output_reader.start()
    monitor = threading.Thread(
        target=monitor_process,
        name=f"task-anchor-{run_id}-monitor",
        daemon=True,
    )
    monitor.start()

    def terminate_on_timeout() -> None:
        """超时后异步终止进程树，不阻塞 start_process 的首次响应。"""
        with state_lock:
            state["timeout_triggered"] = True
        logger.warning(
            "timeout_triggered",
            {"run_id": run_id, "pid": process.pid, "timeout_ms": timeout_ms},
        )
        try:
            termination = _terminate_pid(process.pid)
            if termination.get("status") not in {"stopped", "already_stopped"}:
                raise ResourceError(
                    f"Task Anchor 超时停止 PID {process.pid} 未确认终止。"
                )
            logger.info(
                "timeout_stop_succeeded",
                {
                    "run_id": run_id,
                    "pid": process.pid,
                    "status": termination.get("status"),
                },
            )
        except (OSError, ResourceError, subprocess.SubprocessError) as exc:
            error_message = (
                f"Task Anchor 超时停止 PID {process.pid} 失败：{exc}；"
                f"资源账本已保留，可通过 operation=stop 重试。"
            )
            with state_lock:
                state["preserve_ledger"] = True
            logger.warning(
                "timeout_stop_failed",
                {"run_id": run_id, "pid": process.pid, "error": error_message},
            )
            invoke_callback(
                on_error,
                {
                    # Task Anchor 分配的运行 ID。
                    "run_id": run_id,
                    # 被执行进程的操作系统 PID。
                    "pid": process.pid,
                    # 超时终止失败时进程仍需显式停止。
                    "timed_out": True,
                    # 异步错误发生时账本仍记录运行状态。
                    "status": "running",
                    # 面向模型的错误文本。
                    "error": error_message,
                },
                "on_error",
            )

    timeout_timer: threading.Timer | None = None
    if normalized_policy == STOP_POLICY_CLEANUP and timeout_ms is not None:
        timeout_timer = threading.Timer(max(0, timeout_ms) / 1000, terminate_on_timeout)
        timeout_timer.daemon = True
        timeout_timer.start()

    if process.poll() is not None:
        completion_event.wait()
        with state_lock:
            completed_result = state["result"]
            completed = state["completion"]
        if completed and completed.get("error") is not None:
            raise ResourceError(
                f"启动命令失败：{completed['error']}；诊断日志：{diagnostic_log_path}"
            )
        return completed_result

    with state_lock:
        if state["finished"]:
            return state["result"]
        state["returned_running"] = True
    return {
        # 受管运行唯一 ID。
        "run_id": run_id,
        # 操作系统进程 ID。
        "pid": process.pid,
        # 进程仍在运行。
        "status": "running",
        # 停止策略。
        "stop_policy": normalized_policy,
        # 展示命令。
        "command": display_command,
        # 规范化工作目录。
        "cwd": normalized_cwd,
        # 运行平台。
        "platform": platform_name,
        # 合并输出日志路径。
        "log_path": str(log_path),
        # 结构化生命周期诊断日志路径。
        "diagnostic_log_path": str(diagnostic_log_path),
        # 返回快照时已经写入的完整日志。
        "output": _read_log(log_path),
    }


def _matches_owner(record: dict[str, Any], owner_key: str, workspace: str) -> bool:
    if record.get("workspace_key") != workspace:
        return False
    return record.get("owner_key") == owner_key


def stop_process(
    *,
    cwd: str,
    run_id: str | None = None,
    name: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    include_keep: bool = True,
) -> dict[str, Any]:
    normalized_cwd = normalize_path(cwd)
    owner_key, _, _ = resolve_owner(normalized_cwd, session_id, task_id)
    workspace = workspace_key(normalized_cwd)
    path = ledger_path(normalized_cwd)
    with file_lock(path.with_suffix(".lock.d")):
        records = _load_records(normalized_cwd)
        selected: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for record in records:
            if run_id is not None:
                matched = record.get("run_id") == run_id and _matches_owner(
                    record, owner_key, workspace
                )
            elif name is not None:
                matched = record.get("name") == name and _matches_owner(
                    record, owner_key, workspace
                )
            else:
                matched = _matches_owner(record, owner_key, workspace)
            if matched and (
                include_keep or record.get("stop_policy") != STOP_POLICY_KEEP
            ):
                selected.append(record)
            else:
                remaining.append(record)

        results: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        succeeded_run_ids: set[str] = set()
        for record in selected:
            diagnostic_path = record.get("diagnostic_log_path")
            logger = (
                TaskAnchorLogger(diagnostic_path)
                if isinstance(diagnostic_path, str) and diagnostic_path.strip()
                else None
            )
            if logger is not None:
                logger.info(
                    "stop_requested",
                    {"run_id": record.get("run_id"), "pid": record.get("pid")},
                )
            try:
                termination = _terminate_pid(int(record.get("pid", 0)))
                if logger is not None:
                    logger.info(
                        "stop_succeeded",
                        {
                            "run_id": record.get("run_id"),
                            "pid": record.get("pid"),
                            "status": termination.get("status"),
                        },
                    )
                results.append(
                    {
                        # 停止状态和操作系统 PID。
                        **termination,
                        # Task Anchor 分配的运行 ID。
                        "run_id": record.get("run_id"),
                        # 调用方设置的资源名称。
                        "name": record.get("name"),
                    }
                )
                succeeded_run_ids.add(str(record.get("run_id")))
            except (ResourceError, ValueError, TypeError) as exc:
                error_message = str(exc)
                if logger is not None:
                    logger.warning(
                        "stop_failed",
                        {
                            "run_id": record.get("run_id"),
                            "pid": record.get("pid"),
                            "error": error_message,
                        },
                    )
                failed.append({"run_id": record.get("run_id"), "error": error_message})
                remaining.append(record)
        try:
            _save_records(normalized_cwd, remaining)
        except (OSError, ResourceError) as exc:
            for record in selected:
                diagnostic_path = record.get("diagnostic_log_path")
                if isinstance(diagnostic_path, str) and diagnostic_path.strip():
                    TaskAnchorLogger(diagnostic_path).warning(
                        "ledger_write_failed",
                        {"run_id": record.get("run_id"), "error": str(exc)},
                    )
            raise
    return {
        "stopped": results,
        "failed": failed,
        "kept": [
            item.get("run_id")
            for item in records
            if item.get("stop_policy") == STOP_POLICY_KEEP
            and owner_key is not None
            and _matches_owner(item, owner_key, workspace)
            and str(item.get("run_id")) not in succeeded_run_ids
        ],
    }


def cleanup_for_stop(
    *,
    cwd: str,
    session_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    return stop_process(
        cwd=cwd,
        session_id=session_id,
        task_id=task_id,
        include_keep=False,
    )


def list_processes(
    *,
    cwd: str,
    session_id: str | None = None,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    normalized_cwd = normalize_path(cwd)
    owner_key, _, _ = resolve_owner(normalized_cwd, session_id, task_id)
    workspace = workspace_key(normalized_cwd)
    with file_lock(ledger_path(normalized_cwd).with_suffix(".lock.d")):
        records = _load_records(normalized_cwd)
    return [
        item
        for item in records
        if _matches_owner(item, owner_key, workspace)
    ]
