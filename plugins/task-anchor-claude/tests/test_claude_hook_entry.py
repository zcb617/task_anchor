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
from unittest.mock import patch


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
        self.home = self.root / "home"
        self.config_path = self.home / ".task_anchor" / "config.json"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(
            json.dumps({"excludeProjects": []}), encoding="utf-8"
        )
        self.home_patch = patch.object(STATE.Path, "home", return_value=self.home)
        self.home_patch.start()
        self.session_id = f"session-{uuid.uuid4()}"
        self.previous_runtime_root = os.environ.get("TASK_ANCHOR_RUNTIME_ROOT")
        os.environ["TASK_ANCHOR_RUNTIME_ROOT"] = str(self.data_root / "runtime")

    def tearDown(self) -> None:
        self.home_patch.stop()
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
        self.assertTrue(
            context.startswith(STATE.POST_COMPACT_CONTINUITY_REMINDER + "\n\n")
        )
        self.assertIn(instruction, context)
        self.assertIn("/task-anchor:task-anchor", context)

        rejected = HOOK.handle_hook(
            self.payload("PostCompact", trigger="manual", cwd=str(self.other_workspace)),
            self.data_root,
        )
        assert rejected is not None
        self.assertIn("跨项目", rejected["systemMessage"])
        self.assertIn(
            STATE.POST_COMPACT_CONTINUITY_REMINDER,
            rejected["hookSpecificOutput"]["additionalContext"],
        )
        self.assertNotIn(instruction, rejected["hookSpecificOutput"]["additionalContext"])

    def test_post_compact_without_anchor_emits_continuity_reminder(self) -> None:
        """验证没有锚定任务时仍向 Claude Code 注入固定连续性提醒。"""
        result = HOOK.handle_hook(
            self.payload("PostCompact", trigger="auto"), self.data_root
        )

        self.assertIsNotNone(result)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(context, STATE.POST_COMPACT_CONTINUITY_REMINDER)
        self.assertIn("CLAUDE.md", context)
        self.assertNotIn("AGENTS.md", context)

    def test_post_compact_without_data_root_emits_continuity_reminder(self) -> None:
        """验证没有插件数据根目录时仍向 Claude Code 注入固定连续性提醒。"""
        result = HOOK.handle_hook(
            self.payload("PostCompact", trigger="auto"), None
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            result["hookSpecificOutput"]["additionalContext"],
            STATE.POST_COMPACT_CONTINUITY_REMINDER,
        )

    def test_invalid_post_compact_trigger_emits_only_continuity_reminder(self) -> None:
        """验证无效 PostCompact trigger 不恢复任务正文但仍注入固定提醒。"""
        instruction = "invalid trigger task"
        self.assertIsNone(self.expand("task-anchor", instruction))
        task_id = self.current_task_id()
        result = HOOK.handle_hook(
            self.payload("PostCompact", trigger="unexpected"), self.data_root
        )

        self.assertIsNotNone(result)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(context, STATE.POST_COMPACT_CONTINUITY_REMINDER)
        self.assertNotIn(instruction, context)
        self.assertNotIn(task_id, context)

    def test_pre_tool_use_allows_non_process_commands(self) -> None:
        """验证 Bash 查询命令未命中进程关键词时允许直接执行。"""
        result = HOOK.handle_hook(
            self.payload(
                "PreToolUse",
                tool_name="Bash",
                tool_input={"command": "pwd && ls"},
            ),
            self.data_root,
        )
        self.assertIsNone(result)

        for command in ("git status", "rg --files"):
            with self.subTest(command=command):
                result = HOOK.handle_hook(
                    self.payload(
                        "PreToolUse",
                        tool_name="Bash",
                        tool_input={"command": command},
                    ),
                    self.data_root,
                )
                self.assertIsNone(result)

    def test_pre_tool_use_entry_requires_managed_exec_for_process_commands(self) -> None:
        """验证恢复的 PreToolUse 入口会拒绝绕过 managed_exec 的进程命令。"""
        for command in ("node -e \"console.log(1)\"", "npm run dev"):
            with self.subTest(command=command):
                result = HOOK.handle_hook(
                    self.payload(
                        "PreToolUse",
                        tool_name="Bash",
                        tool_input={"command": command},
                    ),
                    self.data_root,
                )
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(
                    result["hookSpecificOutput"]["permissionDecision"], "deny"
                )
                self.assertIn(
                    "mcp__plugin_task-anchor_task-anchor__managed_exec",
                    result["hookSpecificOutput"]["permissionDecisionReason"],
                )

    def test_pre_tool_use_matches_nested_command_fields(self) -> None:
        """验证 toolInput 和 arguments 中的命令仍按进程关键词拦截。"""
        nested_inputs = (
            ("toolInput", {"command": "bun run dev"}),
            ("arguments", {"command": "node -e \"console.log(1)\""}),
        )
        for field_name, field_value in nested_inputs:
            with self.subTest(field_name=field_name):
                result = HOOK.handle_hook(
                    self.payload(
                        "PreToolUse",
                        tool_name="Bash",
                        **{field_name: field_value},
                    ),
                    self.data_root,
                )
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(
                    result["hookSpecificOutput"]["permissionDecision"], "deny"
                )
                self.assertIn(
                    "mcp__plugin_task-anchor_task-anchor__managed_exec",
                    result["hookSpecificOutput"]["permissionDecisionReason"],
                )

    def test_pre_tool_use_allows_non_command_tools_without_command_text(self) -> None:
        """验证 Read 和 Write 不因缺少命令文本进入进程命令拦截。"""
        for tool_name, tool_input in (
            ("Read", {"file_path": "README.md"}),
            ("Write", {"file_path": "output.txt", "content": "content"}),
        ):
            with self.subTest(tool_name=tool_name):
                result = HOOK.handle_hook(
                    self.payload(
                        "PreToolUse",
                        tool_name=tool_name,
                        tool_input=tool_input,
                    ),
                    self.data_root,
                )
                self.assertIsNone(result)

    def test_fastctx_replace_is_allowed_without_read_only_policy(self) -> None:
        """验证未启用只读策略时 FastCtx replace 工具允许执行。"""
        result = HOOK.handle_hook(
            self.payload(
                "PreToolUse",
                tool_name="mcp__fastctx__replace",
                tool_input={},
            ),
            self.data_root,
        )

        self.assertIsNone(result)

    def test_fastctx_replace_is_denied_by_read_only_policy(self) -> None:
        """验证只读策略开启后 FastCtx replace 工具被 mutation policy 拦截。"""
        self.assertIsNone(
            HOOK.handle_hook(
                self.payload(
                    "UserPromptExpansion",
                    command_name="task-anchor:task-anchor-readonly",
                    command_args="$task-anchor-readonly",
                ),
                self.data_root,
            )
        )

        result = HOOK.handle_hook(
            self.payload(
                "PreToolUse",
                tool_name="mcp__fastctx__replace",
                tool_input={},
            ),
            self.data_root,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(
            "blocks mutation-capable tools",
            result["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_fastctx_replace_is_allowed_after_write_policy(self) -> None:
        """验证写入策略覆盖只读策略后 FastCtx replace 工具恢复执行。"""
        self.assertIsNone(
            HOOK.handle_hook(
                self.payload(
                    "UserPromptExpansion",
                    command_name="task-anchor:task-anchor-readonly",
                    command_args="$task-anchor-readonly",
                ),
                self.data_root,
            )
        )
        self.assertIsNone(
            HOOK.handle_hook(
                self.payload(
                    "UserPromptExpansion",
                    command_name="task-anchor:task-anchor-write",
                    command_args="$task-anchor-write",
                ),
                self.data_root,
            )
        )

        result = HOOK.handle_hook(
            self.payload(
                "PreToolUse",
                tool_name="mcp__fastctx__replace",
                tool_input={},
            ),
            self.data_root,
        )

        self.assertIsNone(result)

    def test_unknown_fastctx_tool_is_denied(self) -> None:
        """验证未知 FastCtx 工具继续被 FastCtx 专属白名单拒绝。"""
        result = HOOK.handle_hook(
            self.payload(
                "PreToolUse",
                tool_name="mcp__fastctx__unknown",
                tool_input={},
            ),
            self.data_root,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(
            "FastCtx only permits inspect_local_file, grep, glob, and replace.",
            result["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_fastctx_read_only_tools_remain_allowed_under_read_only_policy(self) -> None:
        """验证只读策略开启后 FastCtx 三个只读工具仍然允许执行。"""
        self.assertIsNone(
            HOOK.handle_hook(
                self.payload(
                    "UserPromptExpansion",
                    command_name="task-anchor:task-anchor-readonly",
                    command_args="$task-anchor-readonly",
                ),
                self.data_root,
            )
        )

        for tool_name in (
            "mcp__fastctx__inspect_local_file",
            "mcp__fastctx__grep",
            "mcp__fastctx__glob",
        ):
            with self.subTest(tool_name=tool_name):
                self.assertIsNone(
                    HOOK.handle_hook(
                        self.payload(
                            "PreToolUse",
                            tool_name=tool_name,
                            tool_input={},
                        ),
                        self.data_root,
                    )
                )

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
        self.assertEqual(config["hooks"]["PreToolUse"][0]["matcher"], ".*")
        for name in (
            "task-anchor",
            "task-anchor-end",
            "task-anchor-readonly",
            "task-anchor-write",
        ):
            content = (PLUGIN_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("disable-model-invocation: true", content)

    def test_mcp_registration_uses_node_launcher(self) -> None:
        """验证 Claude Code 插件注册 Node managed_exec 启动器。"""
        mcp_config = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
        server = mcp_config["mcpServers"]["task-anchor"]
        self.assertEqual(server["command"], "node")
        self.assertEqual(server["args"], ["${CLAUDE_PLUGIN_ROOT}/scripts/managed_exec_launcher.cjs"])
        self.assertEqual(server["env"]["TASK_ANCHOR_DEFAULT_CWD"], "${CLAUDE_PROJECT_DIR}")


if __name__ == "__main__":
    unittest.main()
