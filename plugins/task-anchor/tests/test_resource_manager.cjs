"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const manager = require(path.join(__dirname, "..", "scripts", "resource_manager.cjs"));

/** 在测试中等待异步子进程状态稳定。 */
function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/** 创建带独立 runtime 根目录和会话上下文的资源测试夹具。 */
function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "task-anchor-resource-node-"));
  const workspace = path.join(root, "workspace");
  fs.mkdirSync(workspace);
  const previousRuntimeRoot = process.env.TASK_ANCHOR_RUNTIME_ROOT;
  process.env.TASK_ANCHOR_RUNTIME_ROOT = path.join(root, "runtime");
  const sessionId = `session-${manager.sha256Text(root)}`;
  manager.setActiveContext(workspace, sessionId, `task-${manager.sha256Text(sessionId)}`);
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

/** 为 Windows 批处理命令测试提供包含临时目录的 PATH。 */
function windowsBatchEnvironment(directory) {
  const environment = { ...process.env };
  const pathName = Object.keys(environment).find((name) => name.toUpperCase() === "PATH") || "PATH";
  const pathExtensionsName =
    Object.keys(environment).find((name) => name.toUpperCase() === "PATHEXT") || "PATHEXT";
  environment[pathName] = [directory, environment[pathName]].filter(Boolean).join(path.delimiter);
  environment[pathExtensionsName] = ".EXE;.BAT;.CMD";
  return environment;
}

/** 启动一个不会自行退出的 Node 子进程，供停止和超时场景复用。 */
function longRunningProcess(cwd, sessionId, options = {}) {
  return manager.startProcess({
    cwd,
    program: process.execPath,
    args: ["-e", "setInterval(() => {}, 1000)"],
    wait: false,
    sessionId,
    ...options,
  });
}

test("program and args preserve environment, cwd, and non-zero exit", async () => {
  const testFixture = fixture();
  try {
    const result = await manager.startProcess({
      cwd: testFixture.workspace,
      program: process.execPath,
      args: ["-e", "process.stdout.write(process.cwd() + '|' + process.env.TASK_ANCHOR_NODE_TEST); process.exit(7)"],
      env: { ...process.env, TASK_ANCHOR_NODE_TEST: "managed" },
      sessionId: testFixture.sessionId,
    });
    assert.equal(result.status, "exited");
    assert.equal(result.exit_code, 7);
    assert.equal(result.output, `${manager.normalizePath(testFixture.workspace)}|managed`);
    assert.equal(typeof result.diagnostic_log_path, "string");
    const events = fs.readFileSync(result.diagnostic_log_path, "utf8")
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
    const expectedMessages = [
      "launch_requested",
      "execution_environment",
      "spawn_attempted",
      "spawn_succeeded",
      "process_exited",
    ];
    if (process.platform === "win32") {
      expectedMessages.splice(2, 0, "windows_batch_resolved");
    }
    assert.deepEqual(events.map((event) => event.message), expectedMessages);
    if (process.platform === "win32") {
      assert.equal(events[2].program, process.execPath);
      assert.equal(events[2].batch_program, null);
    }
    assert.equal(events.some((event) => Object.hasOwn(event, "TASK_ANCHOR_NODE_TEST")), false);
    assert.deepEqual(manager.listProcesses({ cwd: testFixture.workspace, sessionId: testFixture.sessionId }), []);
  } finally {
    testFixture.restore();
  }
});

test("windows .cmd programs work by absolute path and PATH command name", { skip: process.platform !== "win32" }, async () => {
  const testFixture = fixture();
  try {
    const batchDirectory = path.join(testFixture.root, "batch tools");
    const batchPath = path.join(batchDirectory, "managed-batch.cmd");
    fs.mkdirSync(batchDirectory);
    fs.writeFileSync(batchPath, "@echo off\r\necho batch:%~1\r\n", "utf8");
    const environment = windowsBatchEnvironment(batchDirectory);

    const direct = await manager.startProcess({
      cwd: testFixture.workspace,
      program: batchPath,
      args: ["direct"],
      env: environment,
      sessionId: testFixture.sessionId,
    });
    assert.equal(direct.exit_code, 0);
    assert.equal(direct.output.trim(), "batch:direct");

    const fromPath = await manager.startProcess({
      cwd: testFixture.workspace,
      program: "managed-batch",
      args: ["path"],
      env: environment,
      sessionId: testFixture.sessionId,
    });
    assert.equal(fromPath.exit_code, 0);
    assert.equal(fromPath.output.trim(), "batch:path");
  } finally {
    testFixture.restore();
  }
});

test("Windows 启动隐藏子进程且不创建独立控制台，POSIX 保留独立进程组", () => {
  assert.deepEqual(manager.processLaunchOptions(manager.PLATFORM_WINDOWS), {
    detached: false,
    windowsHide: true,
  });
  assert.deepEqual(manager.processLaunchOptions(manager.PLATFORM_MACOS), { detached: true });
  assert.deepEqual(manager.processLaunchOptions(manager.PLATFORM_LINUX), { detached: true });
});

test("legacy lock file does not block the new lock directory", async () => {
  const testFixture = fixture();
  let resource;
  try {
    const ledger = manager.ledgerPath(testFixture.workspace);
    const runtimeDirectory = path.dirname(ledger);
    const legacyLock = path.join(runtimeDirectory, "resources.lock");
    const newLock = manager.lockPathFor(ledger);
    fs.writeFileSync(legacyLock, "legacy lock");
    assert.equal(fs.existsSync(newLock), false);

    manager.withFileLock(newLock, () => {
      assert.equal(fs.statSync(newLock).isDirectory(), true);
      assert.equal(fs.existsSync(legacyLock), true);
    });
    assert.equal(fs.existsSync(newLock), false);

    manager.setActiveContext(testFixture.workspace, testFixture.sessionId, "legacy-lock-task");
    resource = await manager.startProcess({
      cwd: testFixture.workspace,
      program: process.execPath,
      args: ["-e", "process.stdout.write('legacy-lock')"],
      sessionId: testFixture.sessionId,
    });
    assert.equal(resource.output, "legacy-lock");
    assert.deepEqual(manager.listProcesses({ cwd: testFixture.workspace, sessionId: testFixture.sessionId }), []);
    assert.equal(fs.existsSync(legacyLock), true);
  } finally {
    testFixture.restore();
  }
});

test("timeout ends the process tree and removes the ordinary resource", async () => {
  const testFixture = fixture();
  try {
    const result = await manager.startProcess({
      cwd: testFixture.workspace,
      program: process.execPath,
      args: ["-e", "setInterval(() => {}, 1000)"],
      timeoutMs: 80,
      sessionId: testFixture.sessionId,
    });
    assert.equal(result.status, "exited");
    assert.equal(result.timed_out, true);
    const events = fs.readFileSync(result.diagnostic_log_path, "utf8")
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
    assert.equal(events.some((event) => event.message === "timeout_triggered"), true);
    assert.equal(events.some((event) => event.message === "timeout_stop_succeeded"), true);
    assert.equal(manager.processAlive(result.pid), false);
    assert.deepEqual(manager.listProcesses({ cwd: testFixture.workspace, sessionId: testFixture.sessionId }), []);
  } finally {
    testFixture.restore();
  }
});

test("wait=false still creates a timeout timer", async () => {
  const testFixture = fixture();
  try {
    const resource = await longRunningProcess(testFixture.workspace, testFixture.sessionId, { timeoutMs: 60 });
    assert.equal(resource.status, "running");
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline) {
      const processStopped = !manager.processAlive(resource.pid);
      const records = manager.listProcesses({ cwd: testFixture.workspace, sessionId: testFixture.sessionId });
      if (processStopped && records.length === 0) {
        break;
      }
      await delay(25);
    }
    assert.equal(manager.processAlive(resource.pid), false);
    assert.deepEqual(manager.listProcesses({ cwd: testFixture.workspace, sessionId: testFixture.sessionId }), []);
  } finally {
    testFixture.restore();
  }
});

test("keep and null timeout resources obey cleanup and explicit stop rules", async () => {
  const testFixture = fixture();
  let keep;
  let noTimeout;
  try {
    keep = await longRunningProcess(testFixture.workspace, testFixture.sessionId, {
      timeoutMs: 40,
      stopPolicy: "keep",
      name: "node-keep",
    });
    noTimeout = await longRunningProcess(testFixture.workspace, testFixture.sessionId, {
      timeoutMs: null,
    });
    await delay(150);
    assert.equal(manager.processAlive(keep.pid), true);
    assert.equal(manager.processAlive(noTimeout.pid), true);
    const cleanup = await manager.cleanupForStop({ cwd: testFixture.workspace, sessionId: testFixture.sessionId });
    assert.equal(cleanup.stopped.length, 1);
    assert.equal(manager.processAlive(noTimeout.pid), false);
    assert.equal(manager.processAlive(keep.pid), true);
    const stopped = await manager.stopProcess({
      cwd: testFixture.workspace,
      runId: keep.run_id,
      sessionId: testFixture.sessionId,
      includeKeep: true,
    });
    assert.equal(stopped.stopped.length, 1);
    assert.equal(manager.processAlive(keep.pid), false);
  } finally {
    if (keep) {
      await manager.stopProcess({ cwd: testFixture.workspace, runId: keep.run_id, sessionId: testFixture.sessionId, includeKeep: true });
    }
    if (noTimeout) {
      await manager.stopProcess({ cwd: testFixture.workspace, runId: noTimeout.run_id, sessionId: testFixture.sessionId, includeKeep: true });
    }
    testFixture.restore();
  }
});

test("session isolation prevents cleanup from stopping another session", async () => {
  const testFixture = fixture();
  const otherSession = `session-other-${manager.sha256Text(testFixture.root)}`;
  let first;
  let second;
  try {
    first = await longRunningProcess(testFixture.workspace, testFixture.sessionId);
    second = await longRunningProcess(testFixture.workspace, otherSession);
    const cleanup = await manager.cleanupForStop({ cwd: testFixture.workspace, sessionId: testFixture.sessionId });
    assert.deepEqual(cleanup.stopped.map((item) => item.run_id), [first.run_id]);
    assert.equal(manager.processAlive(first.pid), false);
    assert.equal(manager.processAlive(second.pid), true);
    assert.deepEqual(manager.listProcesses({ cwd: testFixture.workspace, sessionId: otherSession }).map((item) => item.run_id), [second.run_id]);
  } finally {
    if (first) {
      await manager.stopProcess({ cwd: testFixture.workspace, runId: first.run_id, sessionId: testFixture.sessionId, includeKeep: true });
    }
    if (second) {
      await manager.stopProcess({ cwd: testFixture.workspace, runId: second.run_id, sessionId: otherSession, includeKeep: true });
    }
    testFixture.restore();
  }
});
