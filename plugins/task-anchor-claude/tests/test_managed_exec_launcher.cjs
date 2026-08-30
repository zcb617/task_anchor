"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const launcherPath = path.join(__dirname, "..", "scripts", "managed_exec_launcher.cjs");
const launcher = require(launcherPath);

// 验证 Claude 启动器仍保留供 Hook 使用的 Python runtime selector。
test("Claude launcher retains runtime selector exports", () => {
  assert.deepEqual(launcher.runtimeCandidates("win32", ""), [
    { command: "py", prefixArgs: ["-3"], source: "Windows launcher" },
    { command: "python", prefixArgs: [], source: "PATH" },
  ]);
  assert.equal(typeof launcher.selectRuntime, "function");
});

// 验证 MCP 入口只指向同目录 Node 服务，不再指向 Python MCP。
test("Claude launcher loads Node MCP without Python server path", () => {
  const source = fs.readFileSync(launcherPath, "utf8");
  assert.match(source, /managed_exec_mcp\.cjs/);
  assert.doesNotMatch(source, /managed_exec_mcp\.py/);
});
