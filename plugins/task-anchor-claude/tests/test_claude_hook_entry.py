from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))
ENTRY_PATH = SCRIPTS_ROOT / "claude_hook_entry.py"
SPEC = importlib.util.spec_from_file_location("task_anchor_claude_hook", ENTRY_PATH)
assert SPEC is not None and SPEC.loader is not None
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)
STATE = HOOK.task_state


class ClaudeHookEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="task-anchor-claude-hook-test-"))
        self.data_root = self.root / "plugin-data"
        self.data_root.mkdir()
        self.workspace = self.root / "workspace"
        self.other_workspace = self.root / "other-workspace"
        self.workspace.mkdir()
        self.other_workspace.mkdir()
        self.session_id = f"session-{uuid.uuid4()}"
        self.previous_runtime_root = os.environ.get("TASK_ANCHOR_RUNTIME_ROOT")
        os.environ["TASK_ANCHOR_RUNTIME_ROOT"] = str(self.data_root / "runtime")

    def tearDown(self) -> None:
        if self.previous_runtime_root is None:
            os.environ.pop("TASK_ANCHOR_RUNTIME_ROOT", None)
        else:
            os.environ["TASK_ANCHOR_RUNTIME_ROOT"] = self.previous_runtime_root
        shutil.rmtree(self.root, ignore_errors=True)

    def payload(self, event_name: str, **extra: object) -> dict[str, object]:
        return {
            "hook_event_name": event_name,
            "session_id": self.session_id,
            "cwd": str(self.workspace),
            **extra,
        }

    def expand(self, command_name: str, command_args: str = "") -> dict[str, object] | None:
        return HOOK.handle_hook(
            self.payload(
                "UserPromptExpansion",
                command_name=command_name,
                command_args=command_args,
            ),
            self.data_root,
        )

    def current_task_id(self) -> str:
        pointer = json.loads(
            STATE.current_task_path(self.data_root, self.session_id).read_text(encoding="utf-8")
        )
        return pointer["task_id"]

    def task_instruction(self, task_id: str) -> str:
        return STATE.task_instruction_path(self.data_root, self.session_id, task_id).read_text(
            encoding="utf-8"
        )

    def test_start_command_stores_only_command_arguments(self) -> None:
        instruction = "实现 Claude Code 插件支持"
        self.assertIsNone(self.expand("task-anchor:task-anchor", instruction))

        task_id = self.current_task_id()
        self.assertEqual(self.task_instruction(task_id), instruction)
        metadata = json.loads(
            STATE.task_metadata_path(self.data_root, self.session_id, task_id).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            metadata["sha256"], hashlib.sha256(instruction.encode("utf-8")).hexdigest()
        )

    def test_unrelated_prompt_and_command_do_not_activate_task(self) -> None:
        self.assertIsNone(
            HOOK.handle_hook(
                self.payload("UserPromptSubmit", prompt="$task-anchor 只是示例文本"),
                self.data_root,
            )
        )
        self.assertIsNone(self.expand("another-plugin:task-anchor", "not ours"))
        self.assertFalse(STATE.current_task_path(self.data_root, self.session_id).exists())

    def test_readonly_and_write_commands_change_only_current_policy(self) -> None:
        self.assertIsNone(self.expand("task-anchor:task-anchor-readonly"))
        self.assertTrue(
            STATE.read_mutation_policy(
                self.payload("PreToolUse"), self.data_root
            )
        )
        self.assertIsNone(self.expand("task-anchor:task-anchor-write"))
        self.assertFalse(
            STATE.read_mutation_policy(
                self.payload("PreToolUse"), self.data_root
            )
        )

    def test_end_command_closes_active_task(self) -> None:
        self.assertIsNone(self.expand("task-anchor", "task to end"))
        task_id = self.current_task_id()
        self.assertIsNone(self.expand("task-anchor:task-anchor-end"))
        metadata = json.loads(
            STATE.task_metadata_path(self.data_root, self.session_id, task_id).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(metadata["status"], STATE.TASK_STATUS_CLOSED)
        self.assertEqual(metadata["closed_reason"], STATE.CLOSED_REASON_MANUAL)

    def test_post_compact_restores_only_current_workspace_task(self) -> None:
        instruction = "continue from compaction"
        self.assertIsNone(self.expand("task-anchor", instruction))
        restored = HOOK.handle_hook(
            self.payload("PostCompact", trigger="auto"), self.data_root
        )
        assert restored is not None
        context = restored["hookSpecificOutput"]["additionalContext"]
        self.assertIn(instruction, context)
        self.assertIn("/task-anchor:task-anchor", context)

        rejected = HOOK.handle_hook(
            self.payload("PostCompact", trigger="manual", cwd=str(self.other_workspace)),
            self.data_root,
        )
        assert rejected is not None
        self.assertIn("跨项目", rejected["systemMessage"])

    def test_readonly_blocks_claude_mutation_tools(self) -> None:
        self.assertIsNone(self.expand("task-anchor", "read-only task"))
        self.assertIsNone(self.expand("task-anchor:task-anchor-readonly"))
        result = HOOK.handle_hook(
            self.payload(
                "PreToolUse",
                tool_name="Write",
                tool_input={"file_path": str(self.workspace / "new.txt"), "content": "x"},
            ),
            self.data_root,
        )
        assert result is not None
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("task-anchor-write", output["permissionDecisionReason"])

    def test_direct_command_tools_require_managed_exec_without_text_heuristics(self) -> None:
        command_result = HOOK.handle_hook(
            self.payload(
                "PreToolUse",
                tool_name="Bash",
                tool_input={"command": "sh -c 'sleep 600'"},
            ),
            self.data_root,
        )
        assert command_result is not None
        command_output = command_result["hookSpecificOutput"]
        self.assertEqual(command_output["permissionDecision"], "deny")
        self.assertIn("managed_exec", command_output["permissionDecisionReason"])

        self.assertIsNone(
            HOOK.handle_hook(
                self.payload(
                    "PreToolUse",
                    tool_name="Read",
                    tool_input={"program": "node_modules"},
                ),
                self.data_root,
            )
        )

    def test_readonly_blocks_direct_command_tools_with_write_remediation(self) -> None:
        self.assertIsNone(self.expand("task-anchor", "read-only command policy"))
        self.assertIsNone(self.expand("task-anchor:task-anchor-readonly"))
        result = HOOK.handle_hook(
            self.payload(
                "PreToolUse",
                tool_name="Bash",
                tool_input={"command": "sh -c 'printf safe'"},
            ),
            self.data_root,
        )
        assert result is not None
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("task-anchor-write", output["permissionDecisionReason"])

    def test_managed_exec_binding_overwrites_untrusted_context(self) -> None:
        result = HOOK.handle_hook(
            self.payload(
                "PreToolUse",
                tool_name="mcp__plugin_task-anchor_task-anchor__managed_exec",
                tool_input={
                    "program": "python",
                    "args": ["-V"],
                    "session_id": "attacker-session",
                    "cwd": "C:/attacker",
                    "env": {"SECRET": "do-not-copy"},
                },
            ),
            self.data_root,
        )
        assert result is not None
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        updated = output["updatedInput"]
        self.assertEqual(updated["session_id"], self.session_id)
        self.assertEqual(updated["cwd"], str(self.workspace))
        self.assertEqual(updated["env"], {"SECRET": "do-not-copy"})

    def test_other_mcp_managed_exec_suffix_is_not_trusted(self) -> None:
        result = HOOK.handle_hook(
            self.payload(
                "PreToolUse",
                tool_name="mcp__other__managed_exec",
                tool_input={"program": "python", "args": ["-V"]},
            ),
            self.data_root,
        )
        assert result is not None
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertNotIn("updatedInput", output)

    def test_session_end_cleanup_is_idempotent(self) -> None:
        self.assertIsNone(self.expand("task-anchor", "background task"))
        result = STATE.resource_manager.start_process(
            cwd=str(self.workspace),
            program=os.environ.get("PYTHON", "python"),
            args=["-c", "import time; time.sleep(30)"],
            wait=False,
            session_id=self.session_id,
        )
        self.assertEqual(result["status"], "running")
        self.assertIsNone(HOOK.handle_hook(self.payload("SessionEnd"), self.data_root))
        resources = STATE.resource_manager.list_processes(
            cwd=str(self.workspace), session_id=self.session_id
        )
        self.assertEqual(resources, [])


class ClaudePluginContractTests(unittest.TestCase):
    def test_manifest_and_marketplace_are_claude_code_layouts(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        marketplace = json.loads(
            (PLUGIN_ROOT.parents[1] / ".claude-plugin" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["name"], "task-anchor")
        self.assertNotIn("skills", manifest)
        self.assertNotIn("hooks", manifest)
        self.assertNotIn("mcpServers", manifest)
        self.assertTrue((PLUGIN_ROOT / "skills").is_dir())
        self.assertTrue((PLUGIN_ROOT / "hooks" / "hooks.json").is_file())
        self.assertTrue((PLUGIN_ROOT / ".mcp.json").is_file())
        self.assertEqual(marketplace["plugins"][0]["source"], "./plugins/task-anchor-claude")

    def test_claude_hook_events_and_skill_visibility_are_registered(self) -> None:
        config = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(config["hooks"]),
            {"UserPromptExpansion", "PostCompact", "PreToolUse", "Stop", "SessionEnd"},
        )
        self.assertEqual(config["hooks"]["PostCompact"][0]["matcher"], "auto|manual")
        for name in (
            "task-anchor",
            "task-anchor-end",
            "task-anchor-readonly",
            "task-anchor-write",
        ):
            content = (PLUGIN_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("disable-model-invocation: true", content)

    def test_managed_exec_requires_user_interaction(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "task_anchor_claude_mcp", SCRIPTS_ROOT / "managed_exec_mcp.py"
        )
        assert spec is not None and spec.loader is not None
        mcp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp)
        response = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response is not None
        tool = response["result"]["tools"][0]
        self.assertEqual(tool["_meta"]["anthropic/requiresUserInteraction"], True)


if __name__ == "__main__":
    unittest.main()
