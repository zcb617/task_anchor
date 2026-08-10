from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import shutil
import unittest
import uuid
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PLUGIN_ROOT.parents[1]
TEST_TEMP_ROOT = WORKSPACE_ROOT / ".tmp" / "task-anchor-tests"
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "hook_entry.py"
SPEC = importlib.util.spec_from_file_location("task_anchor_hook_entry", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


class HookEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.data_root = TEST_TEMP_ROOT / f"case-{uuid.uuid4().hex}"
        self.data_root.mkdir()
        self.previous_runtime_root = os.environ.get("TASK_ANCHOR_RUNTIME_ROOT")
        self.runtime_root = self.data_root / "runtime"
        os.environ["TASK_ANCHOR_RUNTIME_ROOT"] = str(self.runtime_root)
        self.session_id = "session/含中文/../安全"
        self.workspace = self.data_root / "workspace-a"
        self.other_workspace = self.data_root / "workspace-b"
        self.workspace.mkdir()
        self.other_workspace.mkdir()

    def tearDown(self) -> None:
        if self.previous_runtime_root is None:
            os.environ.pop("TASK_ANCHOR_RUNTIME_ROOT", None)
        else:
            os.environ["TASK_ANCHOR_RUNTIME_ROOT"] = self.previous_runtime_root
        shutil.rmtree(self.data_root)

    def user_prompt(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, object]:
        return {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_id or self.session_id,
            "turn_id": "turn-1",
            "prompt": prompt,
            "cwd": str(cwd or self.workspace),
        }

    def post_compact(
        self,
        trigger: str = "auto",
        *,
        session_id: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, object]:
        return {
            "hook_event_name": "PostCompact",
            "session_id": session_id or self.session_id,
            "trigger": trigger,
            "cwd": str(cwd or self.workspace),
        }

    def stop(self) -> dict[str, object]:
        return {
            "hook_event_name": "Stop",
            "session_id": self.session_id,
            "turn_id": "turn-1",
            "cwd": str(self.workspace),
        }

    def session_dir(self, session_id: str | None = None) -> Path:
        return HOOK.session_directory(self.data_root, session_id or self.session_id)

    def current_task_id(self, session_id: str | None = None) -> str:
        current_session_id = session_id or self.session_id
        pointer = json.loads(
            HOOK.current_task_path(self.data_root, current_session_id).read_text(
                encoding="utf-8"
            )
        )
        return pointer["task_id"]

    def task_ids(self, session_id: str | None = None) -> set[str]:
        current_session_id = session_id or self.session_id
        directory = HOOK.tasks_directory(self.data_root, current_session_id)
        if not directory.exists():
            return set()
        return {entry.name for entry in directory.iterdir() if entry.is_dir()}

    def task_metadata(self, task_id: str, session_id: str | None = None) -> dict[str, object]:
        current_session_id = session_id or self.session_id
        return json.loads(
            HOOK.task_metadata_path(self.data_root, current_session_id, task_id).read_text(
                encoding="utf-8"
            )
        )

    def write_task_metadata(
        self,
        task_id: str,
        metadata: dict[str, object],
        session_id: str | None = None,
    ) -> None:
        current_session_id = session_id or self.session_id
        HOOK.task_metadata_path(self.data_root, current_session_id, task_id).write_text(
            json.dumps(metadata, ensure_ascii=False),
            encoding="utf-8",
        )

    def task_instruction(self, task_id: str, session_id: str | None = None) -> str:
        current_session_id = session_id or self.session_id
        return HOOK.task_instruction_path(
            self.data_root, current_session_id, task_id
        ).read_text(encoding="utf-8")

    def audit_events(self) -> list[dict[str, object]]:
        path = self.data_root / HOOK.AUDIT_LOG_RELATIVE_PATH
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def activate(self, prompt: str) -> str:
        self.assertIsNone(HOOK.handle_hook(self.user_prompt(prompt), self.data_root))
        return self.current_task_id()

    def test_ordinary_prompt_is_ignored(self) -> None:
        self.assertIsNone(HOOK.handle_hook(self.user_prompt("普通对话"), self.data_root))
        self.assertFalse(HOOK.current_task_path(self.data_root, self.session_id).exists())

    def test_explicit_activation_creates_a_task_record(self) -> None:
        prompt = "$task-anchor 保留这条最初任务指令。"
        task_id = self.activate(prompt)

        self.assertEqual(str(uuid.UUID(task_id)), task_id)
        self.assertEqual(self.task_instruction(task_id), prompt)
        metadata = self.task_metadata(task_id)
        self.assertEqual(metadata["task_id"], task_id)
        self.assertEqual(metadata["status"], HOOK.TASK_STATUS_ACTIVE)
        self.assertEqual(metadata[HOOK.SESSION_KEY_FIELD], HOOK.session_key(self.session_id))
        self.assertEqual(
            metadata["sha256"],
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        )
        self.assertNotIn("session_id", metadata)

    def test_every_explicit_activation_creates_a_new_task_and_closes_old_one(self) -> None:
        first_prompt = "$task-anchor 第一个独立任务"
        second_prompt = "$task-anchor 第二个独立任务"
        first_task_id = self.activate(first_prompt)
        second_task_id = self.activate(second_prompt)

        self.assertNotEqual(first_task_id, second_task_id)
        self.assertEqual(self.current_task_id(), second_task_id)
        self.assertEqual(self.task_instruction(first_task_id), first_prompt)
        self.assertEqual(self.task_instruction(second_task_id), second_prompt)
        old_metadata = self.task_metadata(first_task_id)
        self.assertEqual(old_metadata["status"], HOOK.TASK_STATUS_CLOSED)
        self.assertEqual(old_metadata["closed_reason"], HOOK.CLOSED_REASON_SUPERSEDED)
        self.assertIn("closed_at", old_metadata)
        self.assertEqual(
            self.task_metadata(second_task_id)["status"], HOOK.TASK_STATUS_ACTIVE
        )

    def test_new_activation_closes_every_existing_active_task_record(self) -> None:
        first_task_id = self.activate("$task-anchor 任务一")
        second_task_id = self.activate("$task-anchor 任务二")
        first_metadata = self.task_metadata(first_task_id)
        first_metadata["status"] = HOOK.TASK_STATUS_ACTIVE
        first_metadata.pop("closed_at", None)
        first_metadata.pop("closed_reason", None)
        self.write_task_metadata(first_task_id, first_metadata)

        third_task_id = self.activate("$task-anchor 任务三")

        for task_id in (first_task_id, second_task_id):
            metadata = self.task_metadata(task_id)
            self.assertEqual(metadata["status"], HOOK.TASK_STATUS_CLOSED)
            self.assertEqual(metadata["closed_reason"], HOOK.CLOSED_REASON_SUPERSEDED)
        self.assertEqual(self.current_task_id(), third_task_id)

    def test_post_compact_restores_only_current_task_with_passive_reminder(self) -> None:
        first_task_id = self.activate("$task-anchor 旧任务不得恢复")
        second_prompt = "$task-anchor 当前任务必须恢复"
        second_task_id = self.activate(second_prompt)

        result = HOOK.handle_hook(self.post_compact(), self.data_root)

        self.assertIsNotNone(result)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PostCompact")
        self.assertIn(second_prompt, context)
        self.assertNotIn(self.task_instruction(first_task_id), context)
        self.assertIn(f"task_id: {second_task_id}", context)
        self.assertIn("status: 1", context)
        self.assertIn("自动完成状态无法验证", context)
        self.assertIn("不要求作答，不改变任务状态，也不阻断当前任务", context)
        self.assertNotIn("$task-anchor-end", context)

    def test_many_compactions_restore_the_same_active_task(self) -> None:
        task_id = self.activate("$task-anchor 长任务")
        outputs = [
            HOOK.handle_hook(self.post_compact(), self.data_root)
            for _ in range(20)
        ]
        self.assertTrue(all(item == outputs[0] for item in outputs))
        self.assertIn(task_id, outputs[0]["hookSpecificOutput"]["additionalContext"])

    def test_normal_reply_does_not_close_task(self) -> None:
        task_id = self.activate("$task-anchor 仍在进行")
        self.assertIsNone(
            HOOK.handle_hook(self.user_prompt("任务已完成，给用户交付。"), self.data_root)
        )
        self.assertEqual(self.current_task_id(), task_id)
        self.assertEqual(
            self.task_metadata(task_id)["status"], HOOK.TASK_STATUS_ACTIVE
        )

    def test_explicit_end_closes_current_task_and_stops_restoration(self) -> None:
        task_id = self.activate("$task-anchor 需要手工结束")

        self.assertIsNone(
            HOOK.handle_hook(self.user_prompt("$task-anchor-end"), self.data_root)
        )

        self.assertEqual(self.current_task_id(), task_id)
        metadata = self.task_metadata(task_id)
        self.assertEqual(metadata["status"], HOOK.TASK_STATUS_CLOSED)
        self.assertEqual(metadata["closed_reason"], HOOK.CLOSED_REASON_MANUAL)
        self.assertIn("closed_at", metadata)
        self.assertIsNone(HOOK.handle_hook(self.post_compact(), self.data_root))
        closed_events = [
            event
            for event in self.audit_events()
            if event["status"] == "task_closed"
        ]
        self.assertEqual(closed_events[-1]["task_id"], task_id)
        self.assertEqual(
            closed_events[-1]["closed_reason"], HOOK.CLOSED_REASON_MANUAL
        )

    def test_explicit_end_without_current_task_reports_no_task(self) -> None:
        result = HOOK.handle_hook(self.user_prompt("$task-anchor-end"), self.data_root)

        self.assertIn("没有可手工结束的任务", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)
        self.assertFalse(HOOK.current_task_path(self.data_root, self.session_id).exists())

    def test_explicit_end_rejects_cross_workspace_request(self) -> None:
        self.activate("$task-anchor 不得跨项目结束")
        (self.other_workspace / ".git").mkdir()

        result = HOOK.handle_hook(
            self.user_prompt("$task-anchor-end", cwd=self.other_workspace), self.data_root
        )

        self.assertIn("当前项目与任务锚点不一致", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)
        self.assertEqual(
            self.task_metadata(self.current_task_id())["status"], HOOK.TASK_STATUS_ACTIVE
        )

    def test_post_compact_does_not_complete_pending_task_transition(self) -> None:
        old_task_id = self.activate("$task-anchor 旧任务仍保持活动")
        pending_task_id = str(uuid.uuid4())
        pending_prompt = "$task-anchor 尚未完成切换的任务"
        pending_metadata = {
            "schema_version": HOOK.SCHEMA_VERSION,
            "task_id": pending_task_id,
            HOOK.SESSION_KEY_FIELD: HOOK.session_key(self.session_id),
            "sha256": hashlib.sha256(pending_prompt.encode("utf-8")).hexdigest(),
            HOOK.WORKSPACE_SHA256_FIELD: HOOK.read_workspace_sha256(
                self.user_prompt("普通消息")
            ),
            "created_at": HOOK.utc_now(),
            "status": HOOK.TASK_STATUS_CLOSED,
            "transition_state": "pending_activation",
        }
        HOOK.atomic_write(
            HOOK.task_instruction_path(self.data_root, self.session_id, pending_task_id),
            pending_prompt.encode("utf-8"),
        )
        HOOK.atomic_write_json(
            HOOK.task_metadata_path(self.data_root, self.session_id, pending_task_id),
            pending_metadata,
        )
        HOOK.atomic_write_json(
            HOOK.transition_path(self.data_root, self.session_id),
            {
                "schema_version": HOOK.SCHEMA_VERSION,
                HOOK.SESSION_KEY_FIELD: HOOK.session_key(self.session_id),
                "new_task_id": pending_task_id,
                "old_task_ids": [old_task_id],
                "created_at": HOOK.utc_now(),
            },
        )

        result = HOOK.handle_hook(self.post_compact(), self.data_root)

        self.assertIn("未完成的任务切换", result["systemMessage"])
        self.assertEqual(self.current_task_id(), old_task_id)
        self.assertEqual(
            self.task_metadata(old_task_id)["status"], HOOK.TASK_STATUS_ACTIVE
        )
        self.assertEqual(
            self.task_metadata(pending_task_id)["status"], HOOK.TASK_STATUS_CLOSED
        )
        self.assertTrue(HOOK.transition_path(self.data_root, self.session_id).exists())
        self.assertIn(
            "restore_pending_transition",
            [event["status"] for event in self.audit_events()],
        )

    def test_closed_current_task_is_not_injected(self) -> None:
        task_id = self.activate("$task-anchor 已关闭任务")
        metadata = self.task_metadata(task_id)
        metadata["status"] = HOOK.TASK_STATUS_CLOSED
        metadata["closed_reason"] = HOOK.CLOSED_REASON_SUPERSEDED
        self.write_task_metadata(task_id, metadata)

        self.assertIsNone(HOOK.handle_hook(self.post_compact(), self.data_root))
        self.assertIn("restore_closed", [event["status"] for event in self.audit_events()])

    def test_manual_post_compact_restores_active_task(self) -> None:
        task_id = self.activate("$task-anchor 手动压缩")
        result = HOOK.handle_hook(self.post_compact("manual"), self.data_root)
        self.assertIn(task_id, result["hookSpecificOutput"]["additionalContext"])

    def test_invalid_post_compact_trigger_is_ignored(self) -> None:
        self.activate("$task-anchor 指令")
        self.assertIsNone(
            HOOK.handle_hook(self.post_compact("unexpected"), self.data_root)
        )

    def test_pre_tool_use_requires_managed_exec_for_process_commands(self) -> None:
        for command in ["npm run dev", r"C:\Java\bin\java.exe -jar app.jar"]:
            denied = HOOK.handle_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "exec",
                    "tool_input": {"cmd": command},
                },
                self.data_root,
            )
            self.assertEqual(
                denied["hookSpecificOutput"]["permissionDecision"], "deny"
            )
            self.assertNotIn("continue", denied)

        for command in ["rg -n pattern .", "Get-Content -Path README.md"]:
            self.assertIsNone(
                HOOK.handle_hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "exec",
                        "tool_input": {"cmd": command},
                    },
                    self.data_root,
                )
            )

        self.assertIsNone(
            HOOK.handle_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "mcp__task_anchor__managed_exec",
                    "tool_input": {"program": "npm", "args": ["run", "dev"]},
                },
                self.data_root,
            )
        )

    def test_stop_cleans_default_resources_but_keeps_explicit_keep_resource(self) -> None:
        HOOK.resource_manager.set_active_context(
            str(self.workspace), self.session_id, f"hook-task-{uuid.uuid4()}"
        )
        cleanup_resource = HOOK.resource_manager.start_process(
            cwd=str(self.workspace),
            program=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            wait=False,
            session_id=self.session_id,
        )
        keep_resource = HOOK.resource_manager.start_process(
            cwd=str(self.workspace),
            program=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            wait=False,
            stop_policy="keep",
            name="hook-test-keep",
            session_id=self.session_id,
        )
        try:
            self.assertIsNone(HOOK.handle_hook(self.stop(), self.data_root))
            self.assertFalse(
                HOOK.resource_manager._process_alive(cleanup_resource["pid"])
            )
            self.assertTrue(
                HOOK.resource_manager._process_alive(keep_resource["pid"])
            )
        finally:
            HOOK.resource_manager.stop_process(
                cwd=str(self.workspace),
                run_id=keep_resource["run_id"],
                include_keep=True,
            )

    def test_cross_workspace_post_compact_is_not_injected(self) -> None:
        (self.other_workspace / ".git").mkdir()
        self.activate("$task-anchor 不得跨项目继续")
        result = HOOK.handle_hook(
            self.post_compact(cwd=self.other_workspace), self.data_root
        )
        self.assertIn("当前项目与任务锚点不一致", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)

    def test_missing_cwd_is_rejected_on_activation_and_restore(self) -> None:
        activation = self.user_prompt("$task-anchor 必须绑定项目")
        del activation["cwd"]
        activation_result = HOOK.handle_hook(activation, self.data_root)
        self.assertIn("缺少 cwd", activation_result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", activation_result)
        self.assertFalse(HOOK.current_task_path(self.data_root, self.session_id).exists())

        self.activate("$task-anchor 有效任务")
        compact = self.post_compact()
        del compact["cwd"]
        compact_result = HOOK.handle_hook(compact, self.data_root)
        self.assertIn("缺少 cwd", compact_result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", compact_result)

    def test_corrupted_instruction_is_not_injected(self) -> None:
        task_id = self.activate("$task-anchor 原始指令")
        HOOK.task_instruction_path(self.data_root, self.session_id, task_id).write_text(
            "已被修改", encoding="utf-8"
        )
        result = HOOK.handle_hook(self.post_compact(), self.data_root)
        self.assertIn("SHA-256 校验失败", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)

    def test_legacy_session_layout_is_not_restored(self) -> None:
        directory = self.session_dir()
        directory.mkdir(parents=True)
        (directory / "initial_instruction.txt").write_text(
            "$task-anchor 旧版任务", encoding="utf-8"
        )
        (directory / "metadata.json").write_text("{}", encoding="utf-8")

        result = HOOK.handle_hook(self.post_compact(), self.data_root)

        self.assertIn("旧版任务记录", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)

    def test_sessions_are_isolated(self) -> None:
        first_task_id = self.activate("$task-anchor 第一个会话")
        other_session = "another-session"
        second = self.user_prompt("$task-anchor 第二个会话", session_id=other_session)
        self.assertIsNone(HOOK.handle_hook(second, self.data_root))
        second_task_id = self.current_task_id(other_session)

        first_result = HOOK.handle_hook(self.post_compact(), self.data_root)
        second_result = HOOK.handle_hook(
            self.post_compact(session_id=other_session), self.data_root
        )
        self.assertIn(first_task_id, first_result["hookSpecificOutput"]["additionalContext"])
        self.assertIn(second_task_id, second_result["hookSpecificOutput"]["additionalContext"])

    def test_audit_log_excludes_instruction_and_real_session_id(self) -> None:
        prompt = "$task-anchor 保密任务正文"
        self.activate(prompt)
        HOOK.handle_hook(self.post_compact(), self.data_root)

        serialized = json.dumps(self.audit_events(), ensure_ascii=False)
        self.assertNotIn(prompt, serialized)
        self.assertNotIn(self.session_id, serialized)
        self.assertIn(HOOK.SESSION_KEY_FIELD, serialized)

    def test_command_entrypoint_creates_a_new_task_on_each_activation(self) -> None:
        environment = os.environ.copy()
        environment["PLUGIN_DATA"] = str(self.data_root)
        first_prompt = "$task-anchor 命令行任务一"
        second_prompt = "$task-anchor 命令行任务二"

        first = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            input=json.dumps(self.user_prompt(first_prompt)),
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout, "")
        first_task_id = self.current_task_id()

        second = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            input=json.dumps(self.user_prompt(second_prompt)),
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stdout, "")
        second_task_id = self.current_task_id()
        self.assertNotEqual(first_task_id, second_task_id)

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
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn(second_prompt, context)
        self.assertNotIn(first_prompt, context)


class PluginContractTests(unittest.TestCase):
    def test_plugin_manifest_keeps_manifest_hooks_outside_plugin_json(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["name"], "task-anchor")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertNotIn("mcpServers", manifest)
        self.assertNotIn("apps", manifest)
        self.assertNotIn("hooks", manifest)
        mcp_config = json.loads(
            (PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8")
        )
        self.assertIn("task_anchor", mcp_config["mcpServers"])

    def test_task_and_resource_hook_events_are_registered(self) -> None:
        config = json.loads(
            (PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(config["hooks"]),
            {"UserPromptSubmit", "PostCompact", "PreToolUse", "Stop"},
        )
        self.assertNotIn("matcher", config["hooks"]["PostCompact"][0])
        self.assertEqual(config["hooks"]["PreToolUse"][0]["matcher"], ".*")

    def test_start_end_and_chinese_communication_skills_are_registered(self) -> None:
        skill = (PLUGIN_ROOT / "skills" / "task-anchor" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        metadata = (
            PLUGIN_ROOT / "skills" / "task-anchor" / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")
        end_skill = (
            PLUGIN_ROOT / "skills" / "task-anchor-end" / "SKILL.md"
        ).read_text(encoding="utf-8")
        end_metadata = (
            PLUGIN_ROOT / "skills" / "task-anchor-end" / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")
        chinese_communication_skill = (
            PLUGIN_ROOT / "skills" / "chinese-communication" / "SKILL.md"
        ).read_text(encoding="utf-8")
        chinese_communication_metadata = (
            PLUGIN_ROOT
            / "skills"
            / "chinese-communication"
            / "agents"
            / "openai.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("Codex 原生 TOLIST", skill)
        self.assertIn("$task-anchor-end", end_skill)
        self.assertIn("allow_implicit_invocation: false", metadata)
        self.assertIn("手工结束", end_skill)
        self.assertIn("allow_implicit_invocation: false", end_metadata)
        self.assertIn("name: chinese-communication", chinese_communication_skill)
        self.assertIn("体貌", chinese_communication_skill)
        self.assertIn("display_name: \"中文沟通与句法校验\"", chinese_communication_metadata)


if __name__ == "__main__":
    unittest.main()
