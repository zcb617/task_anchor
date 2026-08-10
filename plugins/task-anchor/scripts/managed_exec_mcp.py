"""Task Anchor 的最小 stdio MCP 服务。

只暴露一个 ``managed_exec`` 工具；真实命令由 resource_manager 启动并登记。
不依赖第三方 Python 包，便于插件从缓存目录直接启动。
"""

from __future__ import annotations

import json
import sys
from typing import Any

try:
    from . import resource_manager
except ImportError:
    import resource_manager


SERVER_NAME = "task-anchor"
SERVER_VERSION = "0.1.0"
TOOL_NAME = "managed_exec"


TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["run", "stop", "list", "cleanup"],
            "default": "run",
            "description": "run 启动命令；stop 停止指定资源；list 查看登记；cleanup 清理默认资源。",
        },
        "program": {"type": "string", "description": "可执行程序，例如 npm、python、java。"},
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": "传给 program 的参数。优先使用 program+args，而不是 shell 字符串。",
        },
        "command": {"type": "string", "description": "仅在 shell=true 时使用的完整命令。"},
        "shell": {"type": "boolean", "default": False},
        "cwd": {"type": "string", "description": "工作目录，默认当前工作目录。"},
        "wait": {"type": "boolean", "default": True, "description": "是否等待命令退出。"},
            "timeout_ms": {"type": ["integer", "null"], "default": 1800000},
        "stop_policy": {
            "type": "string",
            "enum": ["cleanup", "keep"],
            "default": "cleanup",
            "description": "默认 cleanup：Stop 时关闭；keep：Stop 时保留。",
        },
        "name": {"type": "string", "description": "资源名称，便于后续 stop。"},
        "run_id": {"type": "string"},
        "session_id": {"type": "string", "description": "通常不需要，默认从 Task Anchor 当前上下文解析。"},
        "task_id": {"type": "string", "description": "通常不需要，默认从 Task Anchor 当前上下文解析。"},
        "include_keep": {"type": "boolean", "default": True},
    },
    "additionalProperties": False,
}


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _tool_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _json_text(value)}],
        "isError": is_error,
    }


def _require_cwd(arguments: dict[str, Any]) -> str:
    cwd = arguments.get("cwd")
    if cwd is None:
        cwd = "."
    if not isinstance(cwd, str) or not cwd.strip():
        raise resource_manager.ResourceError("cwd 必须是非空字符串。")
    return cwd


def execute_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise resource_manager.ResourceError("工具参数必须是对象。")
    operation = arguments.get("operation", "run")
    if operation not in {"run", "stop", "list", "cleanup"}:
        raise resource_manager.ResourceError("operation 只能是 run、stop、list 或 cleanup。")

    cwd = _require_cwd(arguments)
    common = {
        "cwd": cwd,
        "session_id": arguments.get("session_id"),
        "task_id": arguments.get("task_id"),
    }
    if operation == "run":
        return resource_manager.start_process(
            **common,
            program=arguments.get("program"),
            args=arguments.get("args"),
            command=arguments.get("command"),
            shell=bool(arguments.get("shell", False)),
            wait=bool(arguments.get("wait", True)),
            timeout_ms=arguments.get("timeout_ms", 1800000),
            stop_policy=arguments.get("stop_policy"),
            name=arguments.get("name"),
        )
    if operation == "stop":
        return resource_manager.stop_process(
            **common,
            run_id=arguments.get("run_id"),
            name=arguments.get("name"),
            include_keep=bool(arguments.get("include_keep", True)),
        )
    if operation == "list":
        return {"resources": resource_manager.list_processes(**common)}
    return resource_manager.cleanup_for_stop(**common)


def _error(code: int, message: str, request_id: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if not isinstance(method, str):
        return _error(-32600, "无效的 JSON-RPC 请求。", request_id)
    if "id" not in request:
        return None

    if method == "initialize":
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        protocol_version = params.get("protocolVersion", "2024-11-05")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": (
                            "通过 Task Anchor 启动、登记和停止本地进程。"
                            "默认 Stop 时清理；需要保留的服务必须显式设置 stop_policy=keep。"
                        ),
                        "inputSchema": TOOL_SCHEMA,
                    }
                ]
            },
        }
    if method == "tools/call":
        params = request.get("params")
        if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
            return _error(-32602, "未知的工具。", request_id)
        arguments = params.get("arguments", {})
        try:
            result = execute_tool(arguments)
            return {"jsonrpc": "2.0", "id": request_id, "result": _tool_result(result)}
        except Exception as exc:  # MCP 工具错误需要作为工具结果返回，避免服务退出。
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_result({"error": str(exc)}, is_error=True),
            }
    return _error(-32601, f"不支持的方法：{method}", request_id)


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = handle_request(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps(_error(-32700, "无效的 JSON。"), ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
