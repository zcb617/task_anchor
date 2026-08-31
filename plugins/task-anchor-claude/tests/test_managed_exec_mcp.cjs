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
      fs.rmSync(root, { recursive: true, force: true });
    },
  };
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
  assert.equal(runSchema.properties.diagnostic_log_path.type, "string");
  assert.equal(runSchema.required.includes("diagnostic_log_path"), true);
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
    { type: "text", text: "operation 只能是 run、stop、list 或 cleanup。" },
  ]);
  assert.deepEqual(response.result.structuredContent, { error: "operation 只能是 run、stop、list 或 cleanup。" });
  assert.equal(response.result.isError, true);
  assert.deepEqual(mcp.handleRequest({ jsonrpc: "2.0", id: 5, method: "ping" }).result, {});
});

test("tools/call preserves program args, shell command, and environment", async () => {
  const testFixture = fixture();
  try {
    const direct = await mcp.executeTool({
      program: process.execPath,
      args: ["-e", "process.stdout.write(process.env.TASK_ANCHOR_MCP_TEST)"],
      cwd: testFixture.workspace,
      env: { ...process.env, TASK_ANCHOR_MCP_TEST: "direct" },
      session_id: testFixture.sessionId,
    });
    assert.equal(direct.exit_code, 0);
    assert.equal(direct.output, "direct");
    assert.equal(typeof direct.diagnostic_log_path, "string");

    const shell = await mcp.executeTool({
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

test("tools/call returns the stable structured list and supports explicit stop", async () => {
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
    resource = await mcp.executeTool({
      program: process.execPath,
      args: ["-e", "setInterval(() => {}, 1000)"],
      cwd: testFixture.workspace,
      wait: false,
      session_id: testFixture.sessionId,
    });
    const stopped = await mcp.executeTool({
      operation: "stop",
      run_id: resource.run_id,
      cwd: testFixture.workspace,
      session_id: testFixture.sessionId,
      include_keep: true,
    });
    assert.equal(stopped.stopped.length, 1);
  } finally {
    if (resource) {
      await mcp.executeTool({ operation: "stop", run_id: resource.run_id, cwd: testFixture.workspace, session_id: testFixture.sessionId, include_keep: true });
    }
    testFixture.restore();
  }
});
