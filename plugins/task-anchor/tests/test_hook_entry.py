from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "hook_entry.py"
SPEC = importlib.util.spec_from_file_location("task_anchor_hook_entry", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


class HookEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary_directory.name)
        self.session_id = "session/含中文/../安全"
        self.workspace = self.data_root / "workspace-a"
        self.other_workspace = self.data_root / "workspace-b"
        self.workspace.mkdir()
        self.other_workspace.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def user_prompt(self, prompt: str) -> dict[str, object]:
        return {
            "hook_event_name": "UserPromptSubmit",
            "session_id": self.session_id,
            "turn_id": "turn-1",
            "prompt": prompt,
            "cwd": str(self.workspace),
        }

    def post_compact(self, trigger: str = "auto") -> dict[str, object]:
        return {
            "hook_event_name": "PostCompact",
            "session_id": self.session_id,
            "trigger": trigger,
            "cwd": str(self.workspace),
        }

    def instruction_path(self) -> Path:
        return (
            HOOK.session_directory(self.data_root, self.session_id)
            / "initial_instruction.txt"
        )

    def metadata_path(self) -> Path:
        return HOOK.session_directory(self.data_root, self.session_id) / "metadata.json"

    def audit_events(self) -> list[dict[str, object]]:
        audit_path = self.data_root / HOOK.AUDIT_LOG_RELATIVE_PATH
        if not audit_path.exists():
            return []
        return [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]

    def test_ordinary_prompt_is_ignored(self) -> None:
        result = HOOK.handle_hook(self.user_prompt("普通消息"), self.data_root)
        self.assertIsNone(result)
        self.assertFalse((self.data_root / "sessions").exists())

    def test_explicit_activation_is_saved_exactly(self) -> None:
        prompt = "$task-anchor 第一行\r\n第二行：中文🙂"
        result = HOOK.handle_hook(self.user_prompt(prompt), self.data_root)
        self.assertIsNone(result)
        self.assertEqual(self.instruction_path().read_bytes(), prompt.encode("utf-8"))

        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(metadata["session_id"], self.session_id)
        self.assertEqual(
            metadata["sha256"],
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            metadata[HOOK.WORKSPACE_SHA256_FIELD],
            HOOK.read_workspace_sha256(self.user_prompt(prompt)),
        )
        events = self.audit_events()
        self.assertEqual(
            [event["status"] for event in events],
            ["activation_received", "activation_saved"],
        )
        audit_text = (self.data_root / HOOK.AUDIT_LOG_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
        self.assertNotIn(prompt, audit_text)
        self.assertNotIn(self.session_id, audit_text)
        self.assertNotIn(str(self.workspace), audit_text)
        self.assertEqual(events[-1]["instruction_bytes"], len(prompt.encode("utf-8")))
        self.assertEqual(events[-1]["instruction_sha256"], metadata["sha256"])
        self.assertEqual(
            events[-1]["anchor_workspace_sha256"],
            metadata[HOOK.WORKSPACE_SHA256_FIELD],
        )

    def test_repeated_activation_does_not_overwrite(self) -> None:
        first = "$task-anchor 最初指令"
        second = "$task-anchor 后来的调用"
        HOOK.handle_hook(self.user_prompt(first), self.data_root)
        HOOK.handle_hook(self.user_prompt(second), self.data_root)
        self.assertEqual(self.instruction_path().read_text(encoding="utf-8"), first)

    def test_invalid_post_compact_trigger_is_ignored(self) -> None:
        HOOK.handle_hook(self.user_prompt("$task-anchor 指令"), self.data_root)
        data = self.post_compact("unexpected")
        self.assertIsNone(HOOK.handle_hook(data, self.data_root))

    def test_post_compact_injects_exact_instruction(self) -> None:
        prompt = "$task-anchor 实现功能，不得改写这句话。"
        HOOK.handle_hook(self.user_prompt(prompt), self.data_root)
        result = HOOK.handle_hook(self.post_compact(), self.data_root)
        self.assertEqual(
            result,
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostCompact",
                    "additionalContext": (
                        "必须使用 $task-anchor Skill 继续当前任务。\n\n"
                        f"[最初任务指令]\n{prompt}"
                    ),
                }
            },
        )
        events = self.audit_events()
        self.assertEqual(
            [event["status"] for event in events[-3:]],
            ["post_compact_received", "anchor_loaded", "restore_emitted"],
        )
        self.assertEqual(events[-1]["trigger"], "auto")
        self.assertEqual(
            events[-1]["instruction_sha256"],
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        )

    def test_manual_post_compact_injects_anchor(self) -> None:
        prompt = "$task-anchor 手动压缩也必须恢复。"
        HOOK.handle_hook(self.user_prompt(prompt), self.data_root)
        result = HOOK.handle_hook(self.post_compact("manual"), self.data_root)
        self.assertIn(prompt, result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PostCompact")

    def test_cross_workspace_post_compact_is_not_injected(self) -> None:
        prompt = "$task-anchor 不得跨项目继续。"
        HOOK.handle_hook(self.user_prompt(prompt), self.data_root)
        cross_workspace = self.post_compact()
        cross_workspace["cwd"] = str(self.other_workspace)

        result = HOOK.handle_hook(cross_workspace, self.data_root)

        self.assertIn("当前项目与任务锚点不一致", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)
        self.assertEqual(
            [event["status"] for event in self.audit_events()[-3:]],
            ["post_compact_received", "workspace_mismatch", "restore_rejected"],
        )

    def test_missing_cwd_is_rejected_on_activation_and_restore(self) -> None:
        activation = self.user_prompt("$task-anchor 必须绑定项目。")
        del activation["cwd"]
        activation_result = HOOK.handle_hook(activation, self.data_root)
        self.assertIn("缺少 cwd", activation_result["systemMessage"])
        self.assertFalse(self.instruction_path().exists())

        HOOK.handle_hook(self.user_prompt("$task-anchor 有效任务"), self.data_root)
        compact = self.post_compact()
        del compact["cwd"]
        compact_result = HOOK.handle_hook(compact, self.data_root)
        self.assertIn("缺少 cwd", compact_result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", compact_result)

    def test_legacy_anchor_without_workspace_binding_is_not_injected(self) -> None:
        HOOK.handle_hook(self.user_prompt("$task-anchor 老任务"), self.data_root)
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        del metadata[HOOK.WORKSPACE_SHA256_FIELD]
        self.metadata_path().write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )

        result = HOOK.handle_hook(self.post_compact(), self.data_root)

        self.assertIn("没有项目边界绑定", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)
        self.assertIn(
            "workspace_binding_missing",
            [event["status"] for event in self.audit_events()],
        )

    def test_same_git_project_subdirectories_share_anchor(self) -> None:
        project = self.data_root / "git-project"
        source = project / "source"
        tests = project / "tests"
        (project / ".git").mkdir(parents=True)
        source.mkdir()
        tests.mkdir()

        activation = self.user_prompt("$task-anchor 同一项目可在子目录继续。")
        activation["cwd"] = str(source)
        HOOK.handle_hook(activation, self.data_root)

        compact = self.post_compact()
        compact["cwd"] = str(tests)
        result = HOOK.handle_hook(compact, self.data_root)

        self.assertIn(
            "同一项目可在子目录继续",
            result["hookSpecificOutput"]["additionalContext"],
        )

    def test_many_compactions_return_the_same_instruction(self) -> None:
        prompt = "$task-anchor 长任务"
        HOOK.handle_hook(self.user_prompt(prompt), self.data_root)
        outputs = [
            HOOK.handle_hook(self.post_compact(), self.data_root)
            for _ in range(1000)
        ]
        self.assertTrue(all(item == outputs[0] for item in outputs))
        self.assertEqual(self.instruction_path().read_text(encoding="utf-8"), prompt)

    def test_corruption_is_not_injected(self) -> None:
        HOOK.handle_hook(self.user_prompt("$task-anchor 原文"), self.data_root)
        self.instruction_path().write_text("已被修改", encoding="utf-8")
        result = HOOK.handle_hook(self.post_compact(), self.data_root)
        self.assertIn("SHA-256 校验失败", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)

    def test_invalid_metadata_shape_is_not_injected(self) -> None:
        HOOK.handle_hook(self.user_prompt("$task-anchor 原文"), self.data_root)
        self.metadata_path().write_text("[]", encoding="utf-8")
        result = HOOK.handle_hook(self.post_compact(), self.data_root)
        self.assertIn("元数据格式无效", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)

    def test_missing_plugin_data_warns_only_on_activation(self) -> None:
        ordinary = HOOK.handle_hook(self.user_prompt("普通消息"), None)
        activation = HOOK.handle_hook(self.user_prompt("$task-anchor 指令"), None)
        self.assertIsNone(ordinary)
        self.assertIn("PLUGIN_DATA", activation["systemMessage"])

    def test_sessions_are_isolated(self) -> None:
        first = "$task-anchor 第一个任务"
        second = "$task-anchor 第二个任务"
        HOOK.handle_hook(self.user_prompt(first), self.data_root)
        other = self.user_prompt(second)
        other["session_id"] = "another-session"
        HOOK.handle_hook(other, self.data_root)

        first_result = HOOK.handle_hook(self.post_compact(), self.data_root)
        other_start = self.post_compact()
        other_start["session_id"] = "another-session"
        second_result = HOOK.handle_hook(other_start, self.data_root)
        self.assertIn(first, first_result["hookSpecificOutput"]["additionalContext"])
        self.assertIn(second, second_result["hookSpecificOutput"]["additionalContext"])

    def test_command_entrypoint(self) -> None:
        environment = os.environ.copy()
        environment["PLUGIN_DATA"] = str(self.data_root)
        activation = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            input=json.dumps(self.user_prompt("$task-anchor 命令行测试")),
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(activation.returncode, 0)
        self.assertEqual(activation.stdout, "")

        compact = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            input=json.dumps(self.post_compact()),
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(compact.returncode, 0)
        payload = json.loads(compact.stdout)
        self.assertIn(
            "$task-anchor 命令行测试",
            payload["hookSpecificOutput"]["additionalContext"],
        )


class PluginContractTests(unittest.TestCase):
    def test_plugin_manifest_has_no_mcp_or_hook_override(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["name"], "task-anchor")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertNotIn("mcpServers", manifest)
        self.assertNotIn("apps", manifest)
        self.assertNotIn("hooks", manifest)

    def test_only_required_hook_events_are_registered(self) -> None:
        config = json.loads(
            (PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(config["hooks"]),
            {"UserPromptSubmit", "PostCompact"},
        )
        self.assertNotIn("matcher", config["hooks"]["PostCompact"][0])

    def test_skill_is_explicit_and_uses_native_tolist(self) -> None:
        skill = (PLUGIN_ROOT / "skills" / "task-anchor" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        metadata = (
            PLUGIN_ROOT / "skills" / "task-anchor" / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("Codex 原生 TOLIST", skill)
        self.assertIn("allow_implicit_invocation: false", metadata)


if __name__ == "__main__":
    unittest.main()
