import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RESOURCE_MANAGER = load_module(
    "task_anchor_resource_manager", SCRIPTS_ROOT / "resource_manager.py"
)
MCP = load_module("task_anchor_managed_exec_mcp", SCRIPTS_ROOT / "managed_exec_mcp.py")


class ResourceManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="task-anchor-resource-test-"))
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.previous_runtime_root = os.environ.get("TASK_ANCHOR_RUNTIME_ROOT")
        os.environ["TASK_ANCHOR_RUNTIME_ROOT"] = str(self.root / "runtime")
        self.session_id = f"session-{uuid.uuid4()}"
        self.task_id = f"task-{uuid.uuid4()}"
        RESOURCE_MANAGER.set_active_context(
            str(self.workspace), self.session_id, self.task_id
        )

    def tearDown(self) -> None:
        if self.previous_runtime_root is None:
            os.environ.pop("TASK_ANCHOR_RUNTIME_ROOT", None)
        else:
            os.environ["TASK_ANCHOR_RUNTIME_ROOT"] = self.previous_runtime_root
        shutil.rmtree(self.root, ignore_errors=True)

    def start_sleep(self, stop_policy=None):
        return RESOURCE_MANAGER.start_process(
            cwd=str(self.workspace),
            program=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            wait=False,
            stop_policy=stop_policy,
            name="resource-test-keep" if stop_policy == "keep" else None,
            session_id=self.session_id,
        )

    def test_start_process_uses_provided_environment(self) -> None:
        environment = dict(os.environ)
        environment["TASK_ANCHOR_ENV_TEST"] = "managed-environment"
        result = RESOURCE_MANAGER.start_process(
            cwd=str(self.workspace),
            program=sys.executable,
            args=[
                "-c",
                "import os; print(os.environ['TASK_ANCHOR_ENV_TEST'])",
            ],
            env=environment,
            session_id=self.session_id,
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output"], "managed-environment\n")

    def test_platform_detection_and_launch_options(self) -> None:
        cases = {
            "Windows": RESOURCE_MANAGER.PLATFORM_WINDOWS,
            "Darwin": RESOURCE_MANAGER.PLATFORM_MACOS,
            "Linux": RESOURCE_MANAGER.PLATFORM_LINUX,
        }
        for system_name, expected in cases.items():
            with self.subTest(system=system_name):
                with patch.object(
                    RESOURCE_MANAGER.platform, "system", return_value=system_name
                ):
                    self.assertEqual(RESOURCE_MANAGER.current_platform(), expected)

        windows_options = RESOURCE_MANAGER._process_launch_options(
            RESOURCE_MANAGER.PLATFORM_WINDOWS
        )
        self.assertIn("creationflags", windows_options)
        self.assertNotIn("start_new_session", windows_options)

        for posix_platform in (
            RESOURCE_MANAGER.PLATFORM_MACOS,
            RESOURCE_MANAGER.PLATFORM_LINUX,
        ):
            with self.subTest(platform=posix_platform):
                options = RESOURCE_MANAGER._process_launch_options(posix_platform)
                self.assertTrue(options["start_new_session"])
                self.assertNotIn("creationflags", options)

        with patch.object(
            RESOURCE_MANAGER.platform, "system", return_value="FreeBSD"
        ):
            with self.assertRaises(RESOURCE_MANAGER.ResourceError):
                RESOURCE_MANAGER.current_platform()

    def test_linux_termination_uses_process_group(self) -> None:
        with patch.object(
            RESOURCE_MANAGER, "current_platform", return_value=RESOURCE_MANAGER.PLATFORM_LINUX
        ), patch.object(
            RESOURCE_MANAGER, "_process_alive", side_effect=[True, False]
        ), patch.object(
            RESOURCE_MANAGER, "_process_group_id", return_value=4321
        ), patch.object(RESOURCE_MANAGER.os, "killpg", create=True) as killpg:
            result = RESOURCE_MANAGER._terminate_pid(1234, grace_seconds=0)

        self.assertEqual(result["status"], "stopped")
        killpg.assert_called_once_with(4321, RESOURCE_MANAGER.signal.SIGTERM)

    def test_windows_termination_uses_taskkill_tree(self) -> None:
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch.object(
            RESOURCE_MANAGER, "current_platform", return_value=RESOURCE_MANAGER.PLATFORM_WINDOWS
        ), patch.object(
            RESOURCE_MANAGER, "_process_alive", side_effect=[True, False]
        ), patch.object(
            RESOURCE_MANAGER.subprocess, "run", return_value=completed
        ) as run:
            result = RESOURCE_MANAGER._terminate_pid(1234)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            run.call_args.args[0], ["taskkill", "/PID", "1234", "/T", "/F"]
        )

    def test_default_policy_is_cleanup(self) -> None:
        resource = self.start_sleep()
        try:
            self.assertEqual(resource["stop_policy"], "cleanup")
            result = RESOURCE_MANAGER.cleanup_for_stop(
                cwd=str(self.workspace), session_id=self.session_id
            )
            self.assertEqual(len(result["stopped"]), 1)
            self.assertFalse(RESOURCE_MANAGER._process_alive(resource["pid"]))
        finally:
            RESOURCE_MANAGER.stop_process(
                cwd=str(self.workspace), run_id=resource["run_id"], include_keep=True
            )

    def test_cleanup_groups_all_resources_within_same_session(self) -> None:
        task_a = f"task-a-{uuid.uuid4()}"
        task_b = f"task-b-{uuid.uuid4()}"
        resource_a = RESOURCE_MANAGER.start_process(
            cwd=str(self.workspace),
            program=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            wait=False,
            session_id=self.session_id,
            task_id=task_a,
            name="drawing-a",
        )
        resource_b = RESOURCE_MANAGER.start_process(
            cwd=str(self.workspace),
            program=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            wait=False,
            session_id=self.session_id,
            task_id=task_b,
            name="drawing-b",
        )
        try:
            result = RESOURCE_MANAGER.cleanup_for_stop(
                cwd=str(self.workspace),
                session_id=self.session_id,
                task_id=task_a,
            )
            self.assertEqual(
                {item["run_id"] for item in result["stopped"]},
                {resource_a["run_id"], resource_b["run_id"]},
            )
            self.assertFalse(RESOURCE_MANAGER._process_alive(resource_a["pid"]))
            self.assertFalse(RESOURCE_MANAGER._process_alive(resource_b["pid"]))
        finally:
            RESOURCE_MANAGER.stop_process(
                cwd=str(self.workspace), run_id=resource_a["run_id"], include_keep=True
            )
            RESOURCE_MANAGER.stop_process(
                cwd=str(self.workspace), run_id=resource_b["run_id"], include_keep=True
            )

    def test_cleanup_isolated_by_session_in_same_workspace(self) -> None:
        session_a = f"session-a-{uuid.uuid4()}"
        session_b = f"session-b-{uuid.uuid4()}"
        task_a = f"task-a-{uuid.uuid4()}"
        task_b = f"task-b-{uuid.uuid4()}"
        RESOURCE_MANAGER.set_active_context(str(self.workspace), session_a, task_a)
        resource_a = RESOURCE_MANAGER.start_process(
            cwd=str(self.workspace),
            program=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            wait=False,
            session_id=session_a,
        )
        RESOURCE_MANAGER.set_active_context(str(self.workspace), session_b, task_b)
        resource_b = RESOURCE_MANAGER.start_process(
            cwd=str(self.workspace),
            program=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            wait=False,
            session_id=session_b,
        )
        try:
            result = RESOURCE_MANAGER.cleanup_for_stop(
                cwd=str(self.workspace), session_id=session_a
            )
            self.assertEqual(
                [item["run_id"] for item in result["stopped"]],
                [resource_a["run_id"]],
            )
            self.assertFalse(RESOURCE_MANAGER._process_alive(resource_a["pid"]))
            self.assertTrue(RESOURCE_MANAGER._process_alive(resource_b["pid"]))
        finally:
            RESOURCE_MANAGER.stop_process(
                cwd=str(self.workspace), run_id=resource_a["run_id"], include_keep=True
            )
            RESOURCE_MANAGER.stop_process(
                cwd=str(self.workspace), run_id=resource_b["run_id"], include_keep=True
            )

    def test_keep_requires_a_name(self) -> None:
        with self.assertRaises(RESOURCE_MANAGER.ResourceError):
            RESOURCE_MANAGER.start_process(
                cwd=str(self.workspace),
                program=sys.executable,
                args=["-c", "import time; time.sleep(30)"],
                wait=False,
                stop_policy="keep",
                session_id=self.session_id,
            )

    def test_keep_policy_survives_cleanup_until_explicit_stop(self) -> None:
        resource = self.start_sleep("keep")
        try:
            result = RESOURCE_MANAGER.cleanup_for_stop(
                cwd=str(self.workspace), session_id=self.session_id
            )
            self.assertEqual(result["stopped"], [])
            self.assertTrue(RESOURCE_MANAGER._process_alive(resource["pid"]))
            stop_result = RESOURCE_MANAGER.stop_process(
                cwd=str(self.workspace), run_id=resource["run_id"], include_keep=True
            )
            self.assertEqual(len(stop_result["stopped"]), 1)
        finally:
            RESOURCE_MANAGER.stop_process(
                cwd=str(self.workspace), run_id=resource["run_id"], include_keep=True
            )

    def test_waiting_command_is_removed_after_exit(self) -> None:
        result = RESOURCE_MANAGER.start_process(
            cwd=str(self.workspace),
            program=sys.executable,
            args=["-c", "print('managed-ok')"],
            wait=True,
            session_id=self.session_id,
        )
        self.assertEqual(result["status"], "exited")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("managed-ok", result["output"])
        self.assertEqual(
            RESOURCE_MANAGER.list_processes(
                cwd=str(self.workspace), session_id=self.session_id
            ),
            [],
        )


class ManagedExecMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="task-anchor-mcp-test-"))
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.previous_runtime_root = os.environ.get("TASK_ANCHOR_RUNTIME_ROOT")
        os.environ["TASK_ANCHOR_RUNTIME_ROOT"] = str(self.root / "runtime")

    def tearDown(self) -> None:
        if self.previous_runtime_root is None:
            os.environ.pop("TASK_ANCHOR_RUNTIME_ROOT", None)
        else:
            os.environ["TASK_ANCHOR_RUNTIME_ROOT"] = self.previous_runtime_root
        shutil.rmtree(self.root, ignore_errors=True)

    def test_initialize_and_tool_list(self) -> None:
        initialize = MCP.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        self.assertEqual(initialize["result"]["serverInfo"]["name"], "task-anchor")
        self.assertEqual(initialize["result"]["protocolVersion"], "2025-06-18")
        tools = MCP.handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        self.assertEqual(tools["result"]["tools"][0]["name"], "managed_exec")
        self.assertEqual(
            tools["result"]["tools"][0]["inputSchema"]["properties"]["stop_policy"][
                "default"
            ],
            "cleanup",
        )
        output_schema = tools["result"]["tools"][0]["outputSchema"]
        self.assertEqual(output_schema["type"], "object")
        self.assertIn("oneOf", output_schema)
        run_schema = next(
            item for item in output_schema["oneOf"] if item["title"] == "run 操作结果"
        )
        self.assertIn("不应按固定字段解析", run_schema["properties"]["output"]["description"])

    def test_tool_call_returns_only_structured_content(self) -> None:
        response = MCP.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "managed_exec",
                    "arguments": {
                        "operation": "list",
                        "cwd": str(self.workspace),
                        "session_id": "mcp-session",
                    },
                },
            }
        )
        result = response["result"]
        self.assertEqual(result["content"], [])
        self.assertEqual(result["structuredContent"], {"resources": []})
        self.assertFalse(result["isError"])

    def test_tool_error_returns_structured_content(self) -> None:
        response = MCP.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "managed_exec",
                    "arguments": {"operation": "unknown"},
                },
            }
        )
        result = response["result"]
        self.assertEqual(result["content"], [])
        self.assertEqual(
            result["structuredContent"],
            {"error": "operation 只能是 run、stop、list 或 cleanup。"},
        )
        self.assertTrue(result["isError"])

    def test_tool_call_passes_environment_to_managed_process(self) -> None:
        environment = dict(os.environ)
        environment["TASK_ANCHOR_MCP_ENV_TEST"] = "mcp-environment"
        result = MCP.execute_tool(
            {
                "program": sys.executable,
                "args": [
                    "-c",
                    "import os; print(os.environ['TASK_ANCHOR_MCP_ENV_TEST'])",
                ],
                "cwd": str(self.workspace),
                "env": environment,
                "session_id": "mcp-environment-session",
            }
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output"], "mcp-environment\n")

    def test_tool_call_starts_and_stops_a_managed_process(self) -> None:
        resource = MCP.execute_tool(
            {
                "program": sys.executable,
                "args": ["-c", "import time; time.sleep(30)"],
                "cwd": str(self.workspace),
                "wait": False,
                "session_id": "mcp-session",
                "task_id": "mcp-task",
            }
        )
        try:
            self.assertEqual(resource["status"], "running")
            result = MCP.execute_tool(
                {
                    "operation": "stop",
                    "run_id": resource["run_id"],
                    "cwd": str(self.workspace),
                    "include_keep": True,
                }
            )
            self.assertEqual(len(result["stopped"]), 1)
        finally:
            MCP.execute_tool(
                {
                    "operation": "stop",
                    "run_id": resource["run_id"],
                    "cwd": str(self.workspace),
                    "include_keep": True,
                }
            )


if __name__ == "__main__":
    unittest.main()
