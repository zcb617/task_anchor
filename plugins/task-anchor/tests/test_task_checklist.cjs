"use strict";

const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const manager = require(path.join(__dirname, "..", "scripts", "resource_manager.cjs"));
const checklist = require(path.join(__dirname, "..", "scripts", "task_checklist.cjs"));
const mcp = require(path.join(__dirname, "..", "scripts", "managed_exec_mcp.cjs"));

/** 创建独立工作区、运行时根目录和真实会话任务快照。 */
function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "task-anchor-checklist-node-"));
  const workspace = path.join(root, "workspace");
  fs.mkdirSync(workspace);
  const previousRuntimeRoot = process.env.TASK_ANCHOR_RUNTIME_ROOT;
  process.env.TASK_ANCHOR_RUNTIME_ROOT = path.join(root, "runtime");
  const sessionId = `session-${crypto.randomUUID()}`;
  const task = createTask({ workspace, taskId: crypto.randomUUID() });
  const taskContext = createTaskContext(sessionId, [task], task.task_id);
  return {
    root,
    workspace,
    sessionId,
    task,
    taskId: task.task_id,
    taskContext,
    restore() {
      manager.closeDb();
      if (previousRuntimeRoot === undefined) {
        delete process.env.TASK_ANCHOR_RUNTIME_ROOT;
      } else {
        process.env.TASK_ANCHOR_RUNTIME_ROOT = previousRuntimeRoot;
      }
      fs.rmSync(root, { recursive: true, force: true });
    },
  };
}

/** 创建一个带会话项目身份的任务快照记录。 */
function createTask({ workspace, taskId = crypto.randomUUID(), createdAt = manager.utcNow(), status = 1, initialInstruction = "测试任务" }) {
  const summary = {
    task_id: taskId,
    created_at: createdAt,
    status,
    workspace_sha256: manager.workspaceKey(workspace),
  };
  if (status === 0) {
    summary.closed_at = manager.utcNow();
    summary.closed_reason = "manually_ended";
  }
  return { ...summary, initial_instruction: initialInstruction };
}

/** 构造 Hook 契约中的当前会话任务快照并按创建时间倒序排列。 */
function createTaskContext(sessionId, tasks, selectedTaskId = null) {
  const orderedTasks = [...tasks].sort((left, right) => {
    const dateOrder = Date.parse(right.created_at) - Date.parse(left.created_at);
    return dateOrder || right.task_id.localeCompare(left.task_id);
  });
  const selectedTask = selectedTaskId === null
    ? null
    : orderedTasks.find((task) => task.task_id === selectedTaskId) || null;
  return {
    session_key: manager.sessionKey(sessionId),
    tasks: orderedTasks.map(({ initial_instruction, ...summary }) => summary),
    task: selectedTask,
  };
}

/** 构造当前 fixture 的任务清单输入，模拟 Hook 自动注入会话和快照。 */
function readArguments(testFixture, extra = {}) {
  return {
    operation: "read",
    cwd: testFixture.workspace,
    session_id: testFixture.sessionId,
    task_context: testFixture.taskContext,
    ...extra,
  };
}

/** 调用 Python Hook 并返回其结构化结果，供跨语言契约用例使用。 */
function invokePythonHook(payload, dataRoot) {
  const scriptDirectory = path.join(__dirname, "..", "scripts");
  const pythonCommand = process.env.TASK_ANCHOR_PYTHON
    || process.env.PYTHON
    || (process.platform === "win32" ? "python" : "python3");
  const pythonCode = [
    "import json, sys",
    "from pathlib import Path",
    "sys.path.insert(0, sys.argv[1])",
    "import hook_entry",
    "payload = json.load(sys.stdin)",
    "result = hook_entry.handle_hook(payload, Path(sys.argv[2]))",
    "print(json.dumps(result, ensure_ascii=False))",
  ].join("\n");
  const result = childProcess.spawnSync(
    pythonCommand,
    ["-c", pythonCode, scriptDirectory, dataRoot],
    {
      encoding: "utf8",
      input: JSON.stringify(payload),
      env: { ...process.env, PLUGIN_DATA: dataRoot },
    },
  );
  if (result.error) {
    assert.fail(`Python Hook 启动失败，命令 ${pythonCommand}：${result.error.message}`);
  }
  assert.equal(result.status, 0, `${pythonCommand}: ${result.stderr || result.signal || "未知退出原因"}`);
  return JSON.parse(result.stdout);
}

test("test_read_lazily_initializes_and_roundtrips", () => {
  const testFixture = fixture();
  try {
    const initial = checklist.executeTool(readArguments(testFixture));
    assert.equal(initial.revision, 0);
    assert.deepEqual(initial.items, []);
    assert.equal(initial.task_id, testFixture.taskId);
    assert.equal(fs.existsSync(initial.path), true);
    const written = checklist.executeTool({
      ...readArguments(testFixture, { operation: "write" }),
      expected_revision: 0,
      items: [
        { id: "one", text: "第一步", status: "待开始" },
        { id: "two", text: "第二步", status: "已完成" },
      ],
    });
    assert.equal(written.revision, 1);
    assert.deepEqual(written.items, [
      { id: "one", text: "第一步", status: "待开始" },
      { id: "two", text: "第二步", status: "已完成" },
    ]);
    assert.deepEqual(checklist.executeTool(readArguments(testFixture)).items, written.items);
  } finally {
    testFixture.restore();
  }
});

test("test_native_plan_does_not_create_checklist", () => {
  const testFixture = fixture();
  try {
    const filePath = checklist.checklistPath(testFixture.workspace, testFixture.taskId);
    assert.equal(fs.existsSync(filePath), false);
  } finally {
    testFixture.restore();
  }
});

test("test_queries_return_consistent_tasks_and_preserve_created_order", () => {
  const testFixture = fixture();
  const oldTask = createTask({
    workspace: testFixture.workspace,
    taskId: crypto.randomUUID(),
    createdAt: "2026-01-01T00:00:00+00:00",
    initialInstruction: "旧任务原始指令",
  });
  const latestTask = createTask({
    workspace: testFixture.workspace,
    taskId: testFixture.taskId,
    createdAt: "2026-02-01T00:00:00+00:00",
    initialInstruction: "最新任务原始指令",
  });
  testFixture.taskContext = createTaskContext(testFixture.sessionId, [oldTask, latestTask], latestTask.task_id);
  try {
    const listed = checklist.executeQuery("list_tasks", readArguments(testFixture));
    assert.deepEqual(listed.tasks.map((task) => task.task_id), [latestTask.task_id, oldTask.task_id]);
    assert.equal(Object.hasOwn(listed.tasks[0], "initial_instruction"), false);
    const last = checklist.executeQuery("get_last_task", readArguments(testFixture));
    const historical = checklist.executeQuery(
      "get_task",
      readArguments(testFixture, { task_id: oldTask.task_id, task_context: createTaskContext(testFixture.sessionId, [oldTask, latestTask], oldTask.task_id) }),
    );
    assert.equal(last.task.task_id, listed.tasks[0].task_id);
    assert.equal(last.task.initial_instruction, latestTask.initial_instruction);
    assert.equal(last.task.checklist.task_id, latestTask.task_id);
    assert.equal(historical.task.task_id, oldTask.task_id);
    assert.equal(historical.task.initial_instruction, oldTask.initial_instruction);
    assert.notEqual(last.task.checklist.path, historical.task.checklist.path);
  } finally {
    testFixture.restore();
  }
});

test("test_same_workspace_sessions_and_tasks_have_independent_checklists", () => {
  const testFixture = fixture();
  const otherSession = `other-${crypto.randomUUID()}`;
  const otherTask = createTask({ workspace: testFixture.workspace, taskId: crypto.randomUUID() });
  const oldTask = createTask({ workspace: testFixture.workspace, taskId: crypto.randomUUID(), createdAt: "2026-01-01T00:00:00+00:00" });
  const latestTask = createTask({ workspace: testFixture.workspace, taskId: testFixture.taskId, createdAt: "2026-02-01T00:00:00+00:00" });
  try {
    testFixture.taskContext = createTaskContext(testFixture.sessionId, [oldTask, latestTask], latestTask.task_id);
    const first = checklist.executeTool({
      ...readArguments(testFixture),
      task_id: oldTask.task_id,
      task_context: createTaskContext(testFixture.sessionId, [oldTask, latestTask], oldTask.task_id),
    });
    const secondSessionContext = createTaskContext(otherSession, [otherTask], otherTask.task_id);
    const second = checklist.executeTool({
      operation: "read",
      cwd: testFixture.workspace,
      session_id: otherSession,
      task_context: secondSessionContext,
    });
    const latest = checklist.executeTool(readArguments(testFixture));
    assert.notEqual(first.path, second.path);
    assert.notEqual(first.path, latest.path);
    assert.equal(first.session_key, manager.sessionKey(testFixture.sessionId));
    assert.equal(second.session_key, manager.sessionKey(otherSession));
    assert.deepEqual(JSON.parse(fs.readFileSync(first.path, "utf8")).items, []);
  } finally {
    testFixture.restore();
  }
});

test("test_missing_or_foreign_task_context_is_rejected_without_directory_fallback", () => {
  const testFixture = fixture();
  try {
    assert.throws(
      () => checklist.executeTool({ operation: "read", cwd: testFixture.workspace }),
      /真实 session_id/,
    );
    assert.throws(
      () => checklist.executeTool({ ...readArguments(testFixture), task_context: undefined }),
      /缺少 Hook 注入的 task_context/,
    );
    assert.throws(
      () => checklist.executeTool({ ...readArguments(testFixture), task_context: { ...testFixture.taskContext, session_key: "0".repeat(64) } }),
      /真实会话身份不匹配/,
    );
    const invalidDateContext = {
      ...testFixture.taskContext,
      tasks: [{ ...testFixture.taskContext.tasks[0], created_at: "2026-01-01T00:00:00" }],
      task: { ...testFixture.taskContext.task, created_at: "2026-01-01T00:00:00" },
    };
    assert.throws(
      () => checklist.executeTool({ ...readArguments(testFixture), task_context: invalidDateContext }),
      /校验失败的任务摘要/,
    );
    assert.throws(
      () => checklist.executeTool(readArguments(testFixture, { task_id: crypto.randomUUID() })),
      /不属于当前 Hook 快照/,
    );
    assert.throws(
      () => checklist.executeTool(readArguments(testFixture, { task_id: "../../escape" })),
      /小写标准 UUID/,
    );
    assert.equal(fs.existsSync(path.join(testFixture.root, "directory-without-context")), false);
  } finally {
    testFixture.restore();
  }
});

test("test_empty_latest_list_and_checklist_reads_have_no_task_side_effect", () => {
  const testFixture = fixture();
  const emptyContext = createTaskContext(testFixture.sessionId, []);
  try {
    const argumentsObject = readArguments(testFixture, { task_context: emptyContext });
    assert.deepEqual(checklist.executeQuery("get_last_task", argumentsObject), { task: null });
    assert.deepEqual(checklist.executeQuery("list_tasks", argumentsObject), { tasks: [] });
    assert.deepEqual(checklist.executeTool(argumentsObject), { task: null });
    assert.throws(
      () => checklist.executeTool({ ...argumentsObject, operation: "write", expected_revision: 0, items: [] }),
      /没有任务/,
    );
    assert.equal(fs.existsSync(path.join(manager.runtimeRoot(), "workspaces")), false);
  } finally {
    testFixture.restore();
  }
});

test("test_task_id_is_required_only_for_get_task", () => {
  assert.deepEqual(checklist.TOOL_SCHEMA.required, ["operation", "cwd"]);
  const lastSchema = checklist.QUERY_TOOLS.find((tool) => tool.name === "get_last_task").inputSchema;
  const listSchema = checklist.QUERY_TOOLS.find((tool) => tool.name === "list_tasks").inputSchema;
  const taskSchema = checklist.QUERY_TOOLS.find((tool) => tool.name === "get_task").inputSchema;
  assert.deepEqual(lastSchema.required, ["cwd"]);
  assert.deepEqual(listSchema.required, ["cwd"]);
  assert.deepEqual(taskSchema.required, ["cwd", "task_id"]);
  assert.equal(Object.hasOwn(lastSchema.properties, "task_id"), false);
  assert.equal(Object.hasOwn(listSchema.properties, "task_id"), false);
  assert.equal(Object.hasOwn(taskSchema.properties, "task_id"), true);
  for (const schema of [checklist.TOOL_SCHEMA, lastSchema, listSchema, taskSchema]) {
    assert.equal(schema.required.includes("session_id"), false);
    assert.equal(schema.required.includes("task_context"), false);
  }
});

test("test_closed_tasks_are_readable_but_not_writable", () => {
  const testFixture = fixture();
  const closedTask = createTask({ workspace: testFixture.workspace, taskId: testFixture.taskId, status: 0 });
  testFixture.taskContext = createTaskContext(testFixture.sessionId, [closedTask], closedTask.task_id);
  try {
    const read = checklist.executeTool(readArguments(testFixture));
    assert.equal(read.task_id, closedTask.task_id);
    assert.throws(
      () => checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 0, items: [] }),
      /已结束任务/,
    );
  } finally {
    testFixture.restore();
  }
});

test("test_created_at_is_preserved_when_checklist_is_written", () => {
  const testFixture = fixture();
  const createdAt = testFixture.task.created_at;
  try {
    checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 0, items: [] });
    const task = checklist.executeQuery("get_last_task", readArguments(testFixture)).task;
    assert.equal(task.created_at, createdAt);
  } finally {
    testFixture.restore();
  }
});

test("test_old_shared_checklist_is_not_merged_into_real_task", () => {
  const testFixture = fixture();
  const oldSharedTaskId = "00000000-0000-5000-8000-000000000001";
  try {
    const oldPath = checklist.checklistPath(testFixture.workspace, oldSharedTaskId);
    fs.mkdirSync(path.dirname(oldPath), { recursive: true });
    fs.writeFileSync(oldPath, JSON.stringify({ items: [{ id: "old", text: "旧目录共享进度", status: "已完成" }] }), "utf8");
    const current = checklist.executeTool(readArguments(testFixture));
    assert.deepEqual(current.items, []);
    assert.deepEqual(JSON.parse(fs.readFileSync(oldPath, "utf8")).items, [
      { id: "old", text: "旧目录共享进度", status: "已完成" },
    ]);
    assert.notEqual(current.path, oldPath);
  } finally {
    testFixture.restore();
  }
});

test("test_invalid_task_items_and_revision_are_rejected", () => {
  const testFixture = fixture();
  try {
    assert.throws(() => checklist.executeTool(readArguments(testFixture, { task_id: "../../escape" })), /小写标准 UUID/);
    assert.throws(() => checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: -1, items: [] }), /非负整数/);
    assert.throws(() => checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 0, items: [{ id: "x", text: "", status: "待开始" }] }), /text/);
    assert.throws(() => checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 0, items: [{ id: "x", text: "x", status: "进行中" }] }), /status/);
    assert.throws(() => checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 0, items: [{ id: "x", text: "x", status: "待开始" }, { id: "x", text: "y", status: "待开始" }] }), /重复/);
  } finally {
    testFixture.restore();
  }
});

test("test_corrupt_and_mismatched_checklists_are_not_overwritten", () => {
  const testFixture = fixture();
  try {
    const filePath = checklist.checklistPath(testFixture.workspace, testFixture.taskId);
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(filePath, "corrupt", "utf8");
    const before = fs.readFileSync(filePath);
    assert.throws(() => checklist.executeTool(readArguments(testFixture)), /损坏/);
    assert.deepEqual(fs.readFileSync(filePath), before);
    fs.rmSync(filePath);
    checklist.executeTool(readArguments(testFixture));
    const mismatched = JSON.parse(fs.readFileSync(filePath, "utf8"));
    mismatched.session_key = "0".repeat(64);
    fs.writeFileSync(filePath, `${JSON.stringify(mismatched)}\n`, "utf8");
    assert.throws(() => checklist.executeTool(readArguments(testFixture)), /身份/);
    mismatched.session_key = manager.sessionKey(testFixture.sessionId);
    mismatched.task_id = crypto.randomUUID();
    fs.writeFileSync(filePath, `${JSON.stringify(mismatched)}\n`, "utf8");
    assert.throws(() => checklist.executeTool(readArguments(testFixture)), /身份/);
    mismatched.task_id = testFixture.taskId;
    mismatched.workspace_key = "0".repeat(64);
    fs.writeFileSync(filePath, `${JSON.stringify(mismatched)}\n`, "utf8");
    const mismatchedBefore = fs.readFileSync(filePath);
    assert.throws(() => checklist.executeTool(readArguments(testFixture)), /身份/);
    assert.throws(
      () => checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 0, items: [] }),
      /身份/,
    );
    assert.deepEqual(fs.readFileSync(filePath), mismatchedBefore);
  } finally {
    testFixture.restore();
  }
});

test("test_stale_revision_does_not_overwrite", () => {
  const testFixture = fixture();
  try {
    checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 0, items: [] });
    const before = fs.readFileSync(checklist.checklistPath(testFixture.workspace, testFixture.taskId));
    assert.throws(
      () => checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 0, items: [{ id: "x", text: "x", status: "待开始" }] }),
      /revision 冲突/,
    );
    assert.deepEqual(fs.readFileSync(checklist.checklistPath(testFixture.workspace, testFixture.taskId)), before);
  } finally {
    testFixture.restore();
  }
});

test("test_checklist_file_lock_serializes_reads", async () => {
  const testFixture = fixture();
  let child;
  let childExit;
  let readyTimer = null;
  const originalMkdirSync = fs.mkdirSync;
  let observedContention = false;
  let expectedSuccess = false;
  try {
    const checklistFile = checklist.checklistPath(testFixture.workspace, testFixture.taskId);
    const checklistLock = manager.lockPathFor(checklistFile);
    const releasePath = path.join(testFixture.root, "release-checklist-lock");
    const managerPath = path.join(__dirname, "..", "scripts", "resource_manager.cjs");
    child = childProcess.spawn(
      process.execPath,
      [
        "-e",
        "const fs=require('node:fs'); const manager=require(process.argv[1]); const releasePath=process.argv[3]; manager.withFileLock(process.argv[2],()=>{ process.stdout.write('ready\\n'); const deadline=Date.now()+10000; while(!fs.existsSync(releasePath)){ if(Date.now()>=deadline){ throw new Error('release timeout'); } Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,10); } });",
        managerPath,
        checklistLock,
        releasePath,
      ],
      { stdio: ["ignore", "pipe", "pipe"] },
    );
    childExit = new Promise((resolve) => {
      let settled = false;
      const finish = (result) => {
        if (!settled) {
          settled = true;
          resolve(result);
        }
      };
      child.once("error", (error) => finish({ error, code: null, signal: null }));
      child.once("close", (code, signal) => finish({ error: null, code, signal }));
    });
    const childReady = new Promise((resolve, reject) => {
      child.stdout.on("data", (chunk) => {
        if (chunk.toString().includes("ready\n")) resolve();
      });
      child.once("error", reject);
    });
    try {
      await Promise.race([
        childReady,
        new Promise((_, reject) => {
          readyTimer = setTimeout(() => reject(new Error("子进程未报告已持有清单锁。")), 5000);
        }),
      ]);
    } finally {
      if (readyTimer !== null) {
        clearTimeout(readyTimer);
        readyTimer = null;
      }
    }
    assert.equal(fs.existsSync(checklistLock), true);
    fs.mkdirSync = function mockedMkdirSync(targetPath, options) {
      if (targetPath === checklistLock && (!options || options.recursive !== true)) {
        try {
          return originalMkdirSync.call(this, targetPath, options);
        } catch (error) {
          if (error && (error.code === "EEXIST" || (process.platform === "win32" && error.code === "EPERM"))) {
            observedContention = true;
            fs.writeFileSync(releasePath, "go", "utf8");
          }
          throw error;
        }
      }
      return originalMkdirSync.call(this, targetPath, options);
    };
    const result = checklist.executeTool(readArguments(testFixture));
    fs.mkdirSync = originalMkdirSync;
    assert.equal(observedContention, true);
    assert.equal(result.task_id, testFixture.taskId);
    expectedSuccess = true;
  } finally {
    fs.mkdirSync = originalMkdirSync;
    if (readyTimer !== null) clearTimeout(readyTimer);
    if (child && childExit && child.exitCode === null && child.signalCode === null) {
      fs.writeFileSync(path.join(testFixture.root, "release-checklist-lock"), "go", "utf8");
    }
    if (childExit) {
      const exit = await childExit;
      if (expectedSuccess) {
        assert.equal(exit.error, null);
        assert.equal(exit.code, 0);
      }
    }
    testFixture.restore();
  }
});

test("test_read_preserves_completed_items_on_resume", () => {
  const testFixture = fixture();
  try {
    checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 0, items: [{ id: "done", text: "已完成", status: "已完成" }] });
    const resumed = checklist.executeTool(readArguments(testFixture));
    assert.equal(resumed.items[0].status, "已完成");
    const adjusted = checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 1, items: [{ id: "done", text: "重新开始", status: "待开始" }] });
    assert.equal(adjusted.items[0].status, "待开始");
  } finally {
    testFixture.restore();
  }
});

test("test_python_hook_and_node_mcp_share_real_session_task_identity", async () => {
  const testFixture = fixture();
  const dataRoot = path.join(testFixture.root, "plugin-data");
  fs.mkdirSync(dataRoot);
  const sessionId = `integration-${crypto.randomUUID()}`;
  const pythonSessionKey = manager.sha256Text(sessionId);
  const pointerPath = path.join(dataRoot, "sessions", pythonSessionKey, "current_task.json");
  const taskMetadataPath = (taskId) => path.join(dataRoot, "sessions", pythonSessionKey, "tasks", taskId, "metadata.json");
  const userPrompt = (prompt, selectedSession = sessionId) => ({
    hook_event_name: "UserPromptSubmit",
    session_id: selectedSession,
    turn_id: "integration-turn",
    prompt,
    cwd: testFixture.workspace,
  });
  const activate = (prompt, selectedSession = sessionId) => {
    invokePythonHook(userPrompt(prompt, selectedSession), dataRoot);
    return JSON.parse(fs.readFileSync(pointerPath.replace(pythonSessionKey, manager.sha256Text(selectedSession)), "utf8")).task_id;
  };
  const preToolInput = (toolName, input, selectedSession = sessionId) => {
    const result = invokePythonHook({
      hook_event_name: "PreToolUse",
      session_id: selectedSession,
      turn_id: "integration-turn",
      cwd: testFixture.workspace,
      tool_name: toolName,
      tool_input: input,
    }, dataRoot);
    assert.equal(result.hookSpecificOutput.permissionDecision, "allow");
    return result.hookSpecificOutput.updatedInput;
  };
  const callQuery = async (toolName, argumentsObject) => {
    const response = await mcp.handleRequest({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: toolName, arguments: argumentsObject },
    });
    assert.equal(response.result.isError, false);
    return response.result.structuredContent;
  };
  try {
    const oldTaskId = activate("$task-anchor integration old task");
    const latestTaskId = activate("$task-anchor integration latest task");
    const latestArguments = preToolInput("get_last_task", {});
    const latest = await callQuery("get_last_task", latestArguments);
    assert.equal(latest.task.task_id, latestTaskId);
    assert.equal(latest.task.checklist.task_id, latestTaskId);

    const listedArguments = preToolInput("list_tasks", {});
    const listed = await callQuery("list_tasks", listedArguments);
    assert.deepEqual(new Set(listed.tasks.map((task) => task.task_id)), new Set([oldTaskId, latestTaskId]));
    const oldArguments = preToolInput("get_task", { task_id: oldTaskId });
    const historical = await callQuery("get_task", oldArguments);
    assert.equal(historical.task.task_id, oldTaskId);

    invokePythonHook(userPrompt("$task-anchor-end"), dataRoot);
    const compacted = invokePythonHook({
      hook_event_name: "PostCompact",
      session_id: sessionId,
      trigger: "auto",
      cwd: testFixture.workspace,
    }, dataRoot);
    assert.equal(compacted.hookSpecificOutput.additionalContext.includes("integration latest task"), false);

    const otherSession = `integration-other-${crypto.randomUUID()}`;
    const otherTaskId = activate("$task-anchor integration other session", otherSession);
    const otherArguments = preToolInput("list_tasks", {}, otherSession);
    const otherList = await callQuery("list_tasks", otherArguments);
    assert.deepEqual(otherList.tasks.map((task) => task.task_id), [otherTaskId]);
    assert.equal(otherList.tasks.some((task) => task.task_id === latestTaskId), false);
  } finally {
    testFixture.restore();
  }
});
