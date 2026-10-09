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

/** 创建临时工作区、运行时根目录和可信活动任务上下文。 */
function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "task-anchor-checklist-node-"));
  const workspace = path.join(root, "workspace");
  fs.mkdirSync(workspace);
  const previousRuntimeRoot = process.env.TASK_ANCHOR_RUNTIME_ROOT;
  process.env.TASK_ANCHOR_RUNTIME_ROOT = path.join(root, "runtime");
  const sessionId = `session-${crypto.randomUUID()}`;
  const taskId = crypto.randomUUID();
  manager.setActiveContext(workspace, sessionId, taskId);
  return {
    root,
    workspace,
    sessionId,
    taskId,
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

/** 构造当前 fixture 任务清单的工具参数。 */
function readArguments(testFixture, extra = {}) {
  return {
    operation: "read",
    cwd: testFixture.workspace,
    session_id: testFixture.sessionId,
    ...extra,
  };
}

test("test_read_lazily_initializes_and_roundtrips", () => {
  const testFixture = fixture();
  try {
    const initial = checklist.executeTool(readArguments(testFixture));
    assert.equal(initial.revision, 0);
    assert.deepEqual(initial.items, []);
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

test("test_native_context_does_not_create_checklist", () => {
  const testFixture = fixture();
  try {
    const filePath = checklist.checklistPath(testFixture.workspace, testFixture.taskId);
    assert.equal(fs.existsSync(filePath), false);
  } finally {
    testFixture.restore();
  }
});

test("test_two_tasks_have_distinct_checklists_and_reject_old_id", () => {
  const testFixture = fixture();
  try {
    const oldTaskId = testFixture.taskId;
    checklist.executeTool(readArguments(testFixture));
    manager.setActiveContext(testFixture.workspace, testFixture.sessionId, crypto.randomUUID());
    const newTaskId = manager.activeContext(testFixture.workspace, testFixture.sessionId).task_id;
    const current = checklist.executeTool(readArguments(testFixture));
    assert.notEqual(current.path, checklist.checklistPath(testFixture.workspace, oldTaskId));
    assert.deepEqual(JSON.parse(fs.readFileSync(checklist.checklistPath(testFixture.workspace, oldTaskId), "utf8")).items, []);
    assert.throws(
      () => checklist.executeTool(readArguments(testFixture, { task_id: oldTaskId })),
      /不匹配/,
    );
    assert.equal(current.task_id, newTaskId);
  } finally {
    testFixture.restore();
  }
});

test("test_sessions_and_workspaces_are_isolated", () => {
  const testFixture = fixture();
  const otherSession = `other-${crypto.randomUUID()}`;
  const otherTaskId = crypto.randomUUID();
  const otherWorkspace = path.join(testFixture.root, "other-workspace");
  fs.mkdirSync(otherWorkspace);
  try {
    manager.setActiveContext(testFixture.workspace, otherSession, otherTaskId);
    const first = checklist.executeTool(readArguments(testFixture, { session_id: testFixture.sessionId }));
    const second = checklist.executeTool(readArguments(testFixture, { session_id: otherSession }));
    assert.notEqual(first.path, second.path);
    assert.throws(
      () => checklist.executeTool(readArguments(testFixture, { session_id: otherSession, task_id: testFixture.taskId })),
      /不匹配/,
    );
    manager.setActiveContext(otherWorkspace, testFixture.sessionId, crypto.randomUUID());
    assert.throws(
      () => checklist.executeTool(readArguments({ ...testFixture, workspace: otherWorkspace }, { session_id: otherSession })),
      /可信活动任务上下文/,
    );
  } finally {
    testFixture.restore();
  }
});

test("test_missing_session_and_context_are_rejected", () => {
  const testFixture = fixture();
  try {
    assert.throws(() => checklist.executeTool({ operation: "read", cwd: testFixture.workspace }), /session_id/);
    assert.throws(
      () => checklist.executeTool(readArguments({ ...testFixture, sessionId: "missing-session" })),
      /可信活动任务上下文/,
    );
  } finally {
    testFixture.restore();
  }
});

test("test_invalid_task_and_items_and_revision_are_rejected", () => {
  const testFixture = fixture();
  try {
    assert.throws(() => checklist.executeTool(readArguments(testFixture, { task_id: "../../escape" })), /UUID/);
    assert.throws(() => checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: -1, items: [] }), /非负整数/);
    assert.throws(() => checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 0, items: [{ id: "x", text: "", status: "待开始" }] }), /text/);
    assert.throws(() => checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 0, items: [{ id: "x", text: "x", status: "进行中" }] }), /status/);
    assert.throws(() => checklist.executeTool({ ...readArguments(testFixture, { operation: "write" }), expected_revision: 0, items: [{ id: "x", text: "x", status: "待开始" }, { id: "x", text: "y", status: "待开始" }] }), /重复/);
  } finally {
    testFixture.restore();
  }
});

test("test_corrupt_metadata_is_not_overwritten", () => {
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

test("test_context_switch_waits_for_context_lock", async () => {
  /** 验证上下文锁发生真实争用时，任务清单会等待释放并保持当前任务身份。 */
  const testFixture = fixture();
  let child;
  let childExit;
  let readyTimer = null;
  let originalMkdirSync = fs.mkdirSync;
  let observedContention = false;
  let expectedSuccess = false;
  try {
    const contextLock = manager.lockPathFor(manager.contextPath(testFixture.workspace));
    const releasePath = path.join(testFixture.root, "release-context-lock");
    const managerPath = path.join(__dirname, "..", "scripts", "resource_manager.cjs");
    child = childProcess.spawn(
      process.execPath,
      [
        "-e",
        "const fs=require('node:fs'); const manager=require(process.argv[1]); const releasePath=process.argv[3]; manager.withFileLock(process.argv[2],()=>{ process.stdout.write('ready\\n'); const deadline=Date.now()+10000; while(!fs.existsSync(releasePath)){ if(Date.now()>=deadline){ throw new Error('release timeout'); } Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,10); } });",
        managerPath,
        contextLock,
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
        if (chunk.toString().includes("ready\n")) {
          resolve();
        }
      });
      child.once("error", reject);
    });
    try {
      await Promise.race([
        childReady,
        new Promise((_, reject) => {
          readyTimer = setTimeout(() => reject(new Error("子进程未报告已持有上下文锁。")), 5000);
        }),
      ]);
    } finally {
      if (readyTimer !== null) {
        clearTimeout(readyTimer);
        readyTimer = null;
      }
    }
    assert.equal(fs.existsSync(contextLock), true);
    originalMkdirSync = fs.mkdirSync;
    fs.mkdirSync = function mockedMkdirSync(targetPath, options) {
      if (targetPath === contextLock && (!options || options.recursive !== true)) {
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
    if (readyTimer !== null) {
      clearTimeout(readyTimer);
      readyTimer = null;
    }
    if (child && childExit && child.exitCode === null && child.signalCode === null) {
      fs.writeFileSync(path.join(testFixture.root, "release-context-lock"), "go", "utf8");
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
