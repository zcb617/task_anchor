"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const manager = require(path.join(__dirname, "..", "scripts", "resource_manager.cjs"));
const mcp = require(path.join(__dirname, "..", "scripts", "managed_exec_mcp.cjs"));

/** 创建 MCP 测试工作区并绑定可信会话。 */
function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "task-anchor-mcp-node-"));
  const workspace = path.join(root, "workspace");
  fs.mkdirSync(workspace);
  const previousRuntimeRoot = process.env.TASK_ANCHOR_RUNTIME_ROOT;
  process.env.TASK_ANCHOR_RUNTIME_ROOT = path.join(root, "runtime");
  const sessionId = `mcp-session-${manager.sha256Text(root)}`;
  manager.setActiveContext(workspace, sessionId, "mcp-task");
  return {
    root,
    workspace,
    sessionId,
    restore() {
      if (previousRuntimeRoot === undefined) {
        delete process.env.TASK_ANCHOR_RUNTIME_ROOT;
      } else {
        process.env.TASK_ANCHOR_RUNTIME_ROOT = previousRuntimeRoot;
      }
      try {
        fs.rmSync(root, { recursive: true, force: true });
      } catch (error) {
        if (process.platform !== "win32" || error?.code !== "EPERM") {
          throw error;
        }
      }
    },
  };
}

/** 调用 MCP 工具并等待异步退出通知，兼容初始 running 快照。 */
async function runToCompletion(argumentsObject) {
  let resolveCompletion;
  let rejectCompletion;
  const completion = new Promise((resolve, reject) => {
    resolveCompletion = resolve;
    rejectCompletion = reject;
  });
  const initial = await mcp.executeTool(argumentsObject, {
    onCompletion: resolveCompletion,
    onError: (event) => rejectCompletion(new Error(event.error)),
  });
  if (initial.status === "exited") {
    return initial;
  }
  return completion;
}

test("initialize, ping, tools/list, and notifications follow JSON-RPC contract", async () => {
  const initialize = mcp.handleRequest({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} });
  assert.equal(initialize.result.serverInfo.name, "task-anchor");
  assert.equal(initialize.result.protocolVersion, "2025-06-18");
  assert.deepEqual(mcp.handleRequest({ jsonrpc: "2.0", id: 2, method: "ping" }).result, {});
  const tools = mcp.handleRequest({ jsonrpc: "2.0", id: 3, method: "tools/list" });
  assert.equal(tools.result.tools[0].name, "managed_exec");
  assert.equal(tools.result.tools[0].inputSchema.properties.stop_policy.default, "cleanup");
  const outputSchema = tools.result.tools[0].outputSchema;
  const runSchema = outputSchema.oneOf.find((item) => item.title === "run 操作结果");
  assert.equal(Object.hasOwn(runSchema.properties, "diagnostic_log_path"), false);
  assert.equal(runSchema.required.includes("diagnostic_log_path"), false);
  assert.equal(outputSchema.$defs.registeredResource.required.includes("diagnostic_log_path"), true);
  assert.equal(mcp.handleRequest({ jsonrpc: "2.0", method: "ping" }), null);
});

test("tool errors stay in structured content and do not terminate the service", async () => {
  const response = await mcp.handleRequest({
    jsonrpc: "2.0",
    id: 4,
    method: "tools/call",
    params: { name: "managed_exec", arguments: { operation: "invalid" } },
  });
  assert.deepEqual(response.result.content, [
    { type: "text", text: "operation 只能是 run、stop、list、cleanup 或 output。" },
  ]);
  assert.deepEqual(response.result.structuredContent, { error: "operation 只能是 run、stop、list、cleanup 或 output。" });
  assert.equal(response.result.isError, true);
  assert.deepEqual(mcp.handleRequest({ jsonrpc: "2.0", id: 5, method: "ping" }).result, {});
});

test("tools/call returns running first and emits output and exited notifications", async () => {
  const testFixture = fixture();
  const outputEvents = [];
  let resolveCompletion;
  let rejectCompletion;
  const completion = new Promise((resolve, reject) => {
    resolveCompletion = resolve;
    rejectCompletion = reject;
  });
  try {
    const initial = await mcp.executeTool(
      {
        program: process.execPath,
        args: [
          "-e",
          "process.stdout.write('mcp-out'); setTimeout(() => process.stderr.write('mcp-err'), 80)",
        ],
        cwd: testFixture.workspace,
        timeout_ms: null,
        session_id: testFixture.sessionId,
      },
      {
        onOutput: (event) => outputEvents.push(event),
        onCompletion: resolveCompletion,
        onError: (event) => rejectCompletion(new Error(event.error)),
      },
    );
    assert.equal(initial.status, "running");
    const result = await completion;
    assert.equal(result.status, "exited");
    assert.equal(result.exit_code, 0);
    assert.equal(outputEvents.some((event) => event.output.includes("mcp-out")), true);
    assert.equal(outputEvents.some((event) => event.output.includes("mcp-err")), true);
    const notification = mcp.createNotification("info", {
      event: "output",
      ...outputEvents[0],
    });
    assert.equal(notification.method, "notifications/message");
    assert.equal(Object.hasOwn(notification, "id"), false);
    assert.equal(notification.params.data.output, outputEvents[0].output);
  } finally {
    testFixture.restore();
  }
});

test("tools/call preserves program args, shell command, and environment", async () => {
  const testFixture = fixture();
  try {
    const direct = await runToCompletion({
      program: process.execPath,
      args: ["-e", "process.stdout.write(process.env.TASK_ANCHOR_MCP_TEST)"],
      cwd: testFixture.workspace,
      env: { ...process.env, TASK_ANCHOR_MCP_TEST: "direct" },
      session_id: testFixture.sessionId,
    });
    assert.equal(direct.exit_code, 0);
    assert.equal(direct.output, "direct");
    const normalizedCwd = manager.normalizePath(testFixture.workspace);
    const diagnosticLogPath = path.join(
      manager.workspaceRuntimeDirectory(normalizedCwd),
      "logs",
      `${direct.run_id}.events.jsonl`,
    );
    assert.equal(fs.existsSync(diagnosticLogPath), true);
    assert.equal(fs.readFileSync(diagnosticLogPath, "utf8").trim().length > 0, true);

    const shell = await runToCompletion({
      command: process.platform === "win32" ? "echo shell" : "printf shell",
      shell: true,
      cwd: testFixture.workspace,
      session_id: testFixture.sessionId,
    });
    assert.equal(shell.exit_code, 0);
    assert.equal(shell.output.trim(), "shell");
  } finally {
    testFixture.restore();
  }
});

test("operation=output validates lines and supports tail and follow", async () => {
  const testFixture = fixture();
  let resource;
  let resolveCompletion;
  let rejectCompletion;
  const outputEvents = [];
  const completion = new Promise((resolve, reject) => {
    resolveCompletion = resolve;
    rejectCompletion = reject;
  });
  try {
    await assert.rejects(
      mcp.executeTool({
        operation: "output",
        run_id: "missing",
        cwd: testFixture.workspace,
        session_id: testFixture.sessionId,
      }),
      (error) => error.message === "output 操作必须提供正整数 lines。",
    );
    await assert.rejects(
      mcp.executeTool({
        operation: "output",
        run_id: "missing",
        lines: 0,
        cwd: testFixture.workspace,
        session_id: testFixture.sessionId,
      }),
      (error) => error.message === "output 操作必须提供正整数 lines。",
    );

    resource = await mcp.executeTool(
      {
        program: process.execPath,
        args: [
          "-e",
          "const n=String.fromCharCode(10); process.stdout.write('line-1'+n+'line-2'+n+'line-3'); setTimeout(() => process.stdout.write(n+'follow-line'), 300); setTimeout(() => process.exit(0), 800)",
        ],
        cwd: testFixture.workspace,
        timeout_ms: null,
        session_id: testFixture.sessionId,
      },
      {
        onOutput: (event) => outputEvents.push(event),
        onCompletion: resolveCompletion,
        onError: (event) => rejectCompletion(new Error(event.error)),
      },
    );
    assert.equal(resource.status, "running");
    await new Promise((resolve) => setTimeout(resolve, 50));

    const tail = await mcp.executeTool({
      operation: "output",
      run_id: resource.run_id,
      lines: 2,
      follow: false,
      cwd: testFixture.workspace,
      session_id: testFixture.sessionId,
    });
    assert.equal(tail.status, "running");
    assert.equal(tail.follow, false);
    assert.equal(tail.output, ["line-2", "line-3"].join(String.fromCharCode(10)));

    const followed = await mcp.executeTool({
      operation: "output",
      run_id: resource.run_id,
      lines: 1,
      follow: true,
      cwd: testFixture.workspace,
      session_id: testFixture.sessionId,
    });
    assert.equal(followed.status, "running");
    assert.equal(followed.follow, true);
    assert.equal(followed.output, "line-3");
    const completed = await completion;
    assert.equal(completed.status, "exited");
    assert.equal(outputEvents.some((event) => event.output.includes("follow-line")), true);
  } finally {
    if (resource && manager.processAlive(resource.pid)) {
      await mcp.executeTool({
        operation: "stop",
        run_id: resource.run_id,
        cwd: testFixture.workspace,
        session_id: testFixture.sessionId,
        include_keep: true,
      });
    }
    testFixture.restore();
  }
});

test("tools/call returns the stable structured list and cleans timeout resources", async () => {
  const testFixture = fixture();
  let resource;
  try {
    const listed = await mcp.handleRequest({
      jsonrpc: "2.0",
      id: 6,
      method: "tools/call",
      params: {
        name: "managed_exec",
        arguments: { operation: "list", cwd: testFixture.workspace, session_id: testFixture.sessionId },
      },
    });
    assert.deepEqual(listed.result.structuredContent, { resources: [] });
    resource = await runToCompletion({
      program: process.execPath,
      args: ["-e", "setInterval(() => {}, 1000)"],
      cwd: testFixture.workspace,
      timeout_ms: 60,
      session_id: testFixture.sessionId,
    });
    assert.equal(resource.status, "exited");
    assert.equal(resource.timed_out, true);
    assert.equal(manager.processAlive(resource.pid), false);
    const retained = manager.dbFindRecord(testFixture.workspace, resource.run_id);
    assert.equal(retained.status, "exited");
    const stopped = await mcp.executeTool({
      operation: "stop",
      run_id: resource.run_id,
      cwd: testFixture.workspace,
      session_id: testFixture.sessionId,
      include_keep: true,
    });
    assert.equal(stopped.stopped.length, 1);
    assert.equal(manager.dbFindRecord(testFixture.workspace, resource.run_id), null);
  } finally {
    if (resource) {
      await mcp.executeTool({ operation: "stop", run_id: resource.run_id, cwd: testFixture.workspace, session_id: testFixture.sessionId, include_keep: true });
    }
    testFixture.restore();
  }
});
