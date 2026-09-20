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
      manager.closeDb();
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

/** 启动一个不会自行退出的 Node 子进程，返回立即可管理的 running 快照。 */
async function longRunningProcess(cwd, sessionId, options = {}) {
  return manager.startProcess({
    cwd,
    program: process.execPath,
    args: ["-e", "setInterval(() => {}, 1000)"],
    sessionId,
    ...options,
  });
}

/** 启动命令并等待内部 completion 回调，兼容瞬时 exited 与持续 running 两种快照。 */
async function runToCompletion(options) {
  let resolveCompletion;
  let rejectCompletion;
  const completion = new Promise((resolve, reject) => {
    resolveCompletion = resolve;
    rejectCompletion = reject;
  });
  const initial = await manager.startProcess({
    ...options,
    onCompletion: resolveCompletion,
    onError: (event) => rejectCompletion(new Error(event.error)),
  });
  if (initial.status === "exited") {
    return initial;
  }
  return completion;
}

/** 发起永久命令并确认首次调用返回 running，供生命周期停止测试取得资源。 */
async function startBackgroundLongRunningResource(cwd, sessionId, options = {}) {
  const resource = await longRunningProcess(cwd, sessionId, options);
  if (resource.status !== "running") {
    throw new Error("永久命令未返回 running 快照。");
  }
  return resource;
}

test("stdout and stderr callbacks arrive before completion with full output", async () => {
  const testFixture = fixture();
  const outputEvents = [];
  let resolveCompletion;
  let rejectCompletion;
  const completion = new Promise((resolve, reject) => {
    resolveCompletion = resolve;
    rejectCompletion = reject;
  });
  try {
    const initial = await manager.startProcess({
      cwd: testFixture.workspace,
      program: process.execPath,
      args: [
        "-e",
        "process.stdout.write('out-1'); setTimeout(() => { process.stderr.write('err-2'); process.stdout.write('out-3'); }, 80)",
      ],
      timeoutMs: null,
      sessionId: testFixture.sessionId,
      onOutput: (event) => outputEvents.push(event),
      onCompletion: resolveCompletion,
      onError: (event) => rejectCompletion(new Error(event.error)),
    });
    assert.equal(initial.status, "running");
    const result = await completion;
    assert.equal(result.status, "exited");
    assert.equal(result.exit_code, 0);
    assert.equal(outputEvents.length >= 2, true);
    assert.equal(outputEvents.every((event) => event.run_id === result.run_id), true);
    assert.equal(outputEvents.every((event) => event.pid === result.pid), true);
    assert.deepEqual(new Set(outputEvents.map((event) => event.stream)), new Set(["stdout", "stderr"]));
    assert.equal(outputEvents.some((event) => event.output.includes("out-1")), true);
    assert.equal(outputEvents.some((event) => event.output.includes("err-2")), true);
    assert.equal(result.output, "out-1err-2out-3");
  } finally {
    testFixture.restore();
  }
});

test("program and args preserve environment, cwd, and non-zero exit", async () => {
  const testFixture = fixture();
  try {
    const result = await runToCompletion({
      cwd: testFixture.workspace,
      program: process.execPath,
      args: ["-e", "process.stdout.write(process.cwd() + '|' + process.env.TASK_ANCHOR_NODE_TEST); process.exit(7)"],
      env: { ...process.env, TASK_ANCHOR_NODE_TEST: "managed" },
      sessionId: testFixture.sessionId,
    });
    assert.equal(result.status, "exited");
    assert.equal(result.exit_code, 7);
    assert.equal(result.output, `${manager.normalizePath(testFixture.workspace)}|managed`);
    const normalizedCwd = manager.normalizePath(testFixture.workspace);
    const diagnosticLogPath = path.join(
      manager.workspaceRuntimeDirectory(normalizedCwd),
      "logs",
      `${result.run_id}.events.jsonl`,
    );
    assert.equal(fs.existsSync(diagnosticLogPath), true);
    const events = fs.readFileSync(diagnosticLogPath, "utf8")
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
    const record = manager.dbFindRecord(testFixture.workspace, result.run_id);
    assert.equal(record.status, "exited");
    assert.equal(record.exit_code, 7);
    const globalRecord = manager.dbFindRecordByRunId(result.run_id);
    assert.equal(globalRecord.cwd, record.cwd);
    assert.deepEqual(globalRecord.args, record.args);
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

    const direct = await runToCompletion({
      cwd: testFixture.workspace,
      program: batchPath,
      args: ["direct"],
      env: environment,
      sessionId: testFixture.sessionId,
    });
    assert.equal(direct.exit_code, 0);
    assert.equal(direct.output.trim(), "batch:direct");

    const fromPath = await runToCompletion({
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
    resource = await runToCompletion({
      cwd: testFixture.workspace,
      program: process.execPath,
      args: ["-e", "process.stdout.write('legacy-lock')"],
      sessionId: testFixture.sessionId,
    });
    assert.equal(resource.output, "legacy-lock");
    const record = manager.dbFindRecord(testFixture.workspace, resource.run_id);
    assert.equal(record.status, "exited");
    assert.equal(record.exit_code, 0);
    assert.equal(fs.existsSync(legacyLock), true);
  } finally {
    testFixture.restore();
  }
});

test("timeout ends the process tree and retains an exited record", async () => {
  const testFixture = fixture();
  try {
    const result = await runToCompletion({
      cwd: testFixture.workspace,
      program: process.execPath,
      args: ["-e", "setInterval(() => {}, 1000)"],
      timeoutMs: 80,
      sessionId: testFixture.sessionId,
    });
    assert.equal(result.status, "exited");
    assert.equal(result.timed_out, true);
    let events = [];
    const normalizedCwd = manager.normalizePath(testFixture.workspace);
    const diagnosticLogPath = path.join(
      manager.workspaceRuntimeDirectory(normalizedCwd),
      "logs",
      `${result.run_id}.events.jsonl`,
    );
    assert.equal(fs.existsSync(diagnosticLogPath), true);
    const eventDeadline = Date.now() + 3000;
    while (Date.now() < eventDeadline) {
      events = fs.readFileSync(diagnosticLogPath, "utf8")
        .trim()
        .split("\n")
        .map((line) => JSON.parse(line));
      if (
        events.some((event) => event.message === "timeout_triggered")
        && events.some((event) => event.message === "timeout_stop_succeeded")
      ) {
        break;
      }
      await delay(25);
    }
    assert.equal(events.some((event) => event.message === "timeout_triggered"), true);
    assert.equal(events.some((event) => event.message === "timeout_stop_succeeded"), true);
    assert.equal(manager.processAlive(result.pid), false);
    const record = manager.dbFindRecord(testFixture.workspace, result.run_id);
    assert.equal(record.status, "exited");
  } finally {
    testFixture.restore();
  }
});

test("long-running cleanup command is terminated by timeout and ledger keeps exited state", async () => {
  const testFixture = fixture();
  try {
    const resource = await runToCompletion({
      cwd: testFixture.workspace,
      program: process.execPath,
      args: ["-e", "setInterval(() => {}, 1000)"],
      timeoutMs: 60,
      sessionId: testFixture.sessionId,
    });
    assert.equal(resource.status, "exited");
    assert.equal(resource.timed_out, true);
    assert.equal(manager.processAlive(resource.pid), false);
    const record = manager.dbFindRecord(testFixture.workspace, resource.run_id);
    assert.equal(record.status, "exited");
  } finally {
    testFixture.restore();
  }
});

test("keep and null timeout resources obey cleanup and explicit stop rules", async () => {
  const testFixture = fixture();
  let keep;
  let noTimeout;
  try {
    keep = await startBackgroundLongRunningResource(testFixture.workspace, testFixture.sessionId, {
      timeoutMs: 40,
      stopPolicy: "keep",
      name: "node-keep",
    });
    noTimeout = await startBackgroundLongRunningResource(testFixture.workspace, testFixture.sessionId, {
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

test("POSIX shell 禁止末尾 & 后台运行，普通命令等执行完，长跑命令被超时终止", { skip: process.platform === "win32" }, async () => {
  const testFixture = fixture();
  let keepRunId = null;
  try {
    // 末尾 & 直接抛业务错误，命令根本无法启动。
    await assert.rejects(
      manager.startProcess({
        cwd: testFixture.workspace,
        command: "sleep 60 &",
        shell: true,
        stopPolicy: "keep",
        name: "posix-background",
        sessionId: testFixture.sessionId,
      }),
      (error) => error instanceof manager.ResourceError,
    );
    // 普通短命令等执行完返回 exited 与输出。
    const short = await runToCompletion({
      cwd: testFixture.workspace,
      program: process.execPath,
      args: ["-e", "process.stdout.write('posix-ok'); process.exit(3)"],
      sessionId: testFixture.sessionId,
    });
    assert.equal(short.status, "exited");
    assert.equal(short.exit_code, 3);
    assert.equal(short.output, "posix-ok");
    // 长跑命令 cleanup + 短 timeoutMs 被超时终止，账本清空。
    const long = await runToCompletion({
      cwd: testFixture.workspace,
      program: process.execPath,
      args: ["-e", "setInterval(() => {}, 1000)"],
      timeoutMs: 60,
      sessionId: testFixture.sessionId,
    });
    assert.equal(long.status, "exited");
    assert.equal(long.timed_out, true);
    assert.equal(manager.processAlive(long.pid), false);
    const record = manager.dbFindRecord(testFixture.workspace, long.run_id);
    assert.equal(record.status, "exited");
  } finally {
    if (keepRunId) {
      await manager.stopProcess({ cwd: testFixture.workspace, runId: keepRunId, sessionId: testFixture.sessionId, includeKeep: true });
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
    first = await startBackgroundLongRunningResource(testFixture.workspace, testFixture.sessionId, { timeoutMs: null });
    second = await startBackgroundLongRunningResource(testFixture.workspace, otherSession, { timeoutMs: null });
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

test("explicit run_id requires matching session owner", async () => {
  const testFixture = fixture();
  const otherSession = `session-other-${manager.sha256Text(testFixture.root)}`;
  let resource;
  try {
    resource = await startBackgroundLongRunningResource(testFixture.workspace, testFixture.sessionId, { timeoutMs: null });
    await assert.rejects(
      manager.stopProcess({ runId: resource.run_id, sessionId: otherSession, includeKeep: true }),
      (error) => error.message === `run_id 不属于当前受控会话：${resource.run_id}`,
    );
    assert.equal(manager.processAlive(resource.pid), true);
    const missingStopped = await manager.stopProcess({ runId: "missing-run-id", sessionId: testFixture.sessionId, includeKeep: true });
    assert.deepEqual(missingStopped, { stopped: [], failed: [], kept: [] });
  } finally {
    if (resource) {
      await manager.stopProcess({
        runId: resource.run_id,
        sessionId: testFixture.sessionId,
        includeKeep: true,
      });
    }
    testFixture.restore();
  }
});
