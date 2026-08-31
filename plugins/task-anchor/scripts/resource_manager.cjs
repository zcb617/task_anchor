"use strict";

const crypto = require("node:crypto");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { TaskAnchorLogger } = require("./task_anchor_logger.cjs");

// 资源账本格式版本，用于兼容现有 Python Hook 读取的数据。
const SCHEMA_VERSION = 1;
// 默认资源在 Stop 或 SessionEnd 时清理。
const STOP_POLICY_CLEANUP = "cleanup";
// keep 资源只在显式 stop 操作中清理。
const STOP_POLICY_KEEP = "keep";
// 允许写入账本的资源停止策略。
const VALID_STOP_POLICIES = new Set([STOP_POLICY_CLEANUP, STOP_POLICY_KEEP]);
// Windows 资源终止时使用的逻辑平台名称。
const PLATFORM_WINDOWS = "windows";
// macOS 资源终止时使用的逻辑平台名称。
const PLATFORM_MACOS = "macos";
// Linux 资源终止时使用的逻辑平台名称。
const PLATFORM_LINUX = "linux";
// 跨 Node/Python 账本目录锁的最大等待时间。
const LOCK_TIMEOUT_MS = 10000;
// 跨 Node/Python 账本目录锁的竞争重试间隔。
const LOCK_RETRY_MS = 25;
// Codex 版本沿用 Python manager 的显式 run_id 所有者校验。
const EXPLICIT_RUN_ID_REQUIRES_OWNER = true;
// 当前 Node 进程内登记的子进程，用于等待关闭和复用进程句柄。
const LIVE_PROCESSES = new Map();
// Windows 需要经由命令解释器启动的批处理包装程序扩展名。
const WINDOWS_BATCH_EXTENSIONS = new Set([".bat", ".cmd"]);

/** 受管资源操作失败。 */
class ResourceError extends Error {
  /** 创建带业务错误信息的资源异常。 */
  constructor(message) {
    super(message);
    this.name = "ResourceError";
  }
}

/** 返回受控管理器支持的平台，拒绝静默落入未知分支。 */
function currentPlatform() {
  if (process.platform === "win32") {
    return PLATFORM_WINDOWS;
  }
  if (process.platform === "darwin") {
    return PLATFORM_MACOS;
  }
  if (process.platform === "linux") {
    return PLATFORM_LINUX;
  }
  throw new ResourceError(`暂不支持的平台：${process.platform || "unknown"}`);
}

/** 返回 UTC ISO 8601 时间，统一资源记录的时间格式。 */
function utcNow() {
  return new Date().toISOString();
}

/** 计算字符串的 SHA-256，用于会话和工作区隔离键。 */
function sha256Text(value) {
  return crypto.createHash("sha256").update(String(value), "utf8").digest("hex");
}

/** 规范化路径，保证账本中的工作目录可跨进程比较。 */
function normalizePath(value) {
  if (typeof value !== "string" || !value.trim()) {
    throw new ResourceError(`无法解析工作目录：${value}`);
  }
  let normalized = path.resolve(value);
  try {
    normalized = fs.realpathSync.native(normalized);
  } catch (error) {
    if (!error || !["ENOENT", "ENOTDIR"].includes(error.code)) {
      throw new ResourceError(`无法解析工作目录：${value}`);
    }
  }
  return process.platform === "win32" ? normalized.toLowerCase() : normalized;
}

/** 根据最近的 Git 根目录或当前目录计算工作区身份。 */
function workspaceIdentity(cwd) {
  const normalized = normalizePath(cwd);
  let candidate = normalized;
  while (true) {
    const marker = path.join(candidate, ".git");
    try {
      if (fs.statSync(marker).isDirectory() || fs.statSync(marker).isFile()) {
        return `git:${normalizePath(candidate)}`;
      }
    } catch (error) {
      if (error && !["ENOENT", "ENOTDIR"].includes(error.code)) {
        throw new ResourceError(`无法读取工作区标记：${candidate}`);
      }
    }
    const parent = path.dirname(candidate);
    if (parent === candidate) {
      break;
    }
    candidate = parent;
  }
  return `cwd:${normalized}`;
}

/** 返回工作区的稳定哈希键，避免直接暴露本地路径。 */
function workspaceKey(cwd) {
  return sha256Text(workspaceIdentity(cwd));
}

/** 返回 Task Anchor 运行时根目录，遵循现有环境变量和平台约定。 */
function runtimeRoot() {
  const override = process.env.TASK_ANCHOR_RUNTIME_ROOT;
  if (typeof override === "string" && override.trim()) {
    return path.resolve(override.trim().replace(/^~(?=$|[\\/])/, os.homedir()));
  }
  if (currentPlatform() === PLATFORM_WINDOWS) {
    const base = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
    return path.join(base, "TaskAnchor", "runtime");
  }
  const base = process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state");
  return path.join(base, "task-anchor");
}

/** 返回工作区专属运行时目录，隔离不同项目的资源账本。 */
function workspaceRuntimeDirectory(cwd) {
  return path.join(runtimeRoot(), "workspaces", workspaceKey(cwd));
}

/** 返回与 Python manager 兼容的资源账本路径。 */
function ledgerPath(cwd) {
  return path.join(workspaceRuntimeDirectory(cwd), "resources.json");
}

/** 返回与 Python manager 兼容的活动上下文路径。 */
function contextPath(cwd) {
  return path.join(workspaceRuntimeDirectory(cwd), "active-context.json");
}

/** 返回与 Python manager 共享的原子锁目录路径，避开历史 .lock 文件。 */
function lockPathFor(filePath) {
  const parsed = path.parse(filePath);
  return path.join(parsed.dir, `${parsed.name}.lock.d`);
}

/** 返回会话哈希，空会话不参与资源归属。 */
function sessionKey(sessionId) {
  if (typeof sessionId !== "string" || !sessionId) {
    return null;
  }
  return sha256Text(sessionId);
}

/**
 * 使用原子目录创建实现 Node/Python 共享的账本互斥锁。
 * 目录创建失败时竞争重试，回调结束或抛错时释放目录锁。
 */
function withFileLock(lockPath, callback) {
  fs.mkdirSync(path.dirname(lockPath), { recursive: true });
  const deadline = Date.now() + LOCK_TIMEOUT_MS;
  let acquired = false;
  while (!acquired) {
    try {
      fs.mkdirSync(lockPath);
      acquired = true;
    } catch (error) {
      if (!error || error.code !== "EEXIST") {
        throw new ResourceError(`无法创建资源锁：${lockPath}：${error.message}`);
      }
      if (Date.now() >= deadline) {
        throw new ResourceError(`资源锁等待超时：${lockPath}`);
      }
      const waitMs = Math.min(LOCK_RETRY_MS, Math.max(1, deadline - Date.now()));
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, waitMs);
    }
  }
  try {
    return callback();
  } finally {
    try {
      fs.rmdirSync(lockPath);
    } catch (error) {
      if (!error || error.code !== "ENOENT") {
        throw new ResourceError(`无法释放资源锁：${lockPath}：${error.message}`);
      }
    }
  }
}

/** 以临时文件、刷盘和替换方式原子写入账本内容。 */
function atomicWrite(filePath, content) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const temporaryPath = path.join(
    path.dirname(filePath),
    `.${path.basename(filePath)}.${process.pid}.${crypto.randomUUID()}.tmp`,
  );
  let descriptor = null;
  try {
    descriptor = fs.openSync(temporaryPath, "wx", 0o600);
    fs.writeFileSync(descriptor, content);
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = null;
    try {
      fs.renameSync(temporaryPath, filePath);
    } catch (error) {
      if (process.platform !== "win32" || !error || !["EEXIST", "EPERM"].includes(error.code)) {
        throw error;
      }
      fs.rmSync(filePath, { force: true });
      fs.renameSync(temporaryPath, filePath);
    }
  } finally {
    if (descriptor !== null) {
      try {
        fs.closeSync(descriptor);
      } catch (error) {
        // 关闭失败不覆盖原始写入异常。
      }
    }
    try {
      fs.rmSync(temporaryPath, { force: true });
    } catch (error) {
      // 临时文件清理失败不影响已经完成的原子替换。
    }
  }
}

/** 读取 JSON 文件，缺失时返回指定默认值。 */
function readJson(filePath, defaultValue) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    if (error && error.code === "ENOENT") {
      return defaultValue;
    }
    throw new ResourceError(`资源记录损坏：${filePath}`);
  }
}

/** 将对象按稳定格式写入 JSON 文件。 */
function writeJson(filePath, value) {
  atomicWrite(filePath, Buffer.from(`${JSON.stringify(value, null, 2)}\n`, "utf8"));
}

/** 规范化停止策略，并拒绝未知策略进入账本。 */
function normalizeStopPolicy(value) {
  if (value === null || value === undefined || value === "") {
    return STOP_POLICY_CLEANUP;
  }
  if (!VALID_STOP_POLICIES.has(value)) {
    throw new ResourceError("stop_policy 只能是 cleanup 或 keep。");
  }
  return value;
}

/** 读取指定会话的活动上下文。 */
function activeContext(cwd, sessionId = null) {
  const value = readJson(contextPath(cwd), null);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  if (value.contexts && typeof value.contexts === "object" && !Array.isArray(value.contexts)) {
    const requestedKey = sessionKey(sessionId);
    if (requestedKey) {
      const context = value.contexts[requestedKey];
      return context && typeof context === "object" && !Array.isArray(context) ? context : null;
    }
    const latest = value.contexts[value.latest_session_key];
    return latest && typeof latest === "object" && !Array.isArray(latest) ? latest : null;
  }
  return value;
}

/** 保存当前会话、任务和工作区的活动上下文。 */
function setActiveContext(cwd, sessionId, taskId) {
  const normalizedCwd = normalizePath(cwd);
  const key = sessionKey(sessionId);
  if (!key) {
    throw new ResourceError("无法记录没有 session_id 的任务上下文。");
  }
  const context = {
    // 资源上下文格式版本。
    schema_version: SCHEMA_VERSION,
    // 当前会话哈希。
    session_key: key,
    // 当前任务 ID。
    task_id: taskId,
    // 当前工作区哈希。
    workspace_key: workspaceKey(normalizedCwd),
    // 规范化工作目录。
    cwd: normalizedCwd,
    // 上下文更新时间。
    updated_at: utcNow(),
  };
  const filePath = contextPath(normalizedCwd);
  return withFileLock(lockPathFor(filePath), () => {
    const stored = readJson(filePath, {});
    const contexts = {};
    if (stored && typeof stored === "object" && stored.contexts && typeof stored.contexts === "object") {
      Object.assign(contexts, stored.contexts);
    } else if (stored && typeof stored === "object" && stored.session_key) {
      contexts[String(stored.session_key)] = stored;
    }
    contexts[key] = context;
    writeJson(filePath, {
      // 活动上下文格式版本。
      schema_version: SCHEMA_VERSION,
      // 按会话哈希保存的上下文。
      contexts,
      // 最近更新的会话哈希。
      latest_session_key: key,
      // 文件更新时间。
      updated_at: utcNow(),
    });
  });
}

/** 解析资源所属会话和任务，优先使用可信活动上下文。 */
function resolveOwner(cwd, sessionId = null, taskId = null) {
  const normalizedCwd = normalizePath(cwd);
  let resolvedSessionKey = sessionKey(sessionId);
  let resolvedTaskId = typeof taskId === "string" && taskId ? taskId : null;
  const context = activeContext(normalizedCwd, sessionId);
  if (context) {
    const contextSessionKey = typeof context.session_key === "string" ? context.session_key : null;
    if (!resolvedSessionKey && contextSessionKey) {
      resolvedSessionKey = contextSessionKey;
    }
    if (!resolvedTaskId && (!resolvedSessionKey || contextSessionKey === resolvedSessionKey)) {
      resolvedTaskId = typeof context.task_id === "string" && context.task_id ? context.task_id : null;
    }
  }
  if (!resolvedSessionKey) {
    throw new ResourceError("无法确定资源归属：必须提供 session_id，或先建立当前会话上下文。");
  }
  return {
    // 资源所有者键，用于同一工作区内的会话隔离。
    ownerKey: `session:${resolvedSessionKey}`,
    // 资源所属会话哈希。
    sessionKey: resolvedSessionKey,
    // 资源所属任务 ID。
    taskId: resolvedTaskId,
  };
}

/** 读取资源账本并校验其顶层数组结构。 */
function loadRecords(cwd) {
  const value = readJson(ledgerPath(cwd), []);
  if (!Array.isArray(value)) {
    throw new ResourceError("资源记录不是数组。");
  }
  return value.filter((item) => item && typeof item === "object" && !Array.isArray(item));
}

/** 保存资源账本，使用与 Python manager 相同的字段集合。 */
function saveRecords(cwd, records) {
  writeJson(ledgerPath(cwd), records);
}

/** 判断指定 PID 是否仍然存在，不通过进程名猜测归属。 */
function processAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0 || pid === process.pid) {
    return false;
  }
  try {
    if (currentPlatform() === PLATFORM_WINDOWS) {
      const result = childProcess.spawnSync(
        "tasklist",
        ["/FI", `PID eq ${pid}`, "/NH"],
        { encoding: "utf8", timeout: 10000, windowsHide: true },
      );
      const output = String(result.stdout || "").trim().toLowerCase();
      return result.status === 0 && Boolean(output) && !output.includes("no tasks are running");
    }
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return Boolean(error && error.code === "EPERM");
  }
}

/** 返回受管子进程启动选项：Windows 隐藏控制台，POSIX 创建独立进程组。 */
function processLaunchOptions(platformName) {
  if (platformName === PLATFORM_WINDOWS) {
    // Windows 的 detached 会创建独立控制台；停止仍由 taskkill /T /F 负责整棵进程树。
    return { detached: false, windowsHide: true };
  }
  if (platformName === PLATFORM_MACOS || platformName === PLATFORM_LINUX) {
    return { detached: true };
  }
  throw new ResourceError(`暂不支持的平台：${platformName}`);
}

/** 返回 detached 子进程的进程组标识，PID 即为该组组长。 */
function processGroupId(pid) {
  if (!Number.isInteger(pid) || pid <= 0 || pid === process.pid) {
    throw new ResourceError(`拒绝结束不安全的进程组：PID ${pid}`);
  }
  return pid;
}

/** 等待异步结果，避免停止操作无限等待外部进程。 */
function waitWithTimeout(promise, timeoutMs) {
  return Promise.race([
    promise,
    new Promise((resolve) => {
      setTimeout(() => resolve(null), timeoutMs);
    }),
  ]);
}

/** 终止指定 PID 的整棵进程树，并兼容 Windows 与 POSIX。 */
async function terminatePid(pid, graceSeconds = 2) {
  const numericPid = Number(pid);
  if (!Number.isInteger(numericPid) || numericPid <= 0 || numericPid === process.pid) {
    throw new ResourceError(`无法结束无效 PID：${pid}`);
  }
  const tracked = LIVE_PROCESSES.get(numericPid);
  if (!processAlive(numericPid)) {
    LIVE_PROCESSES.delete(numericPid);
    if (tracked) {
      await waitWithTimeout(tracked.completion, 10000);
    }
    return { status: "already_stopped", pid: numericPid };
  }

  if (currentPlatform() === PLATFORM_WINDOWS) {
    const result = childProcess.spawnSync(
      "taskkill",
      ["/PID", String(numericPid), "/T", "/F"],
      { encoding: "utf8", timeout: 30000, windowsHide: true },
    );
    if (result.error || (result.status !== 0 && processAlive(numericPid))) {
      const detail = result.error ? result.error.message : String(result.stderr || result.stdout || "").trim();
      throw new ResourceError(`无法结束 PID ${numericPid}：${detail}`);
    }
    if (tracked) {
      await waitWithTimeout(tracked.completion, 10000);
    }
    LIVE_PROCESSES.delete(numericPid);
    return { status: "stopped", pid: numericPid };
  }

  const groupId = processGroupId(numericPid);
  try {
    process.kill(-groupId, "SIGTERM");
  } catch (error) {
    if (!error || error.code !== "ESRCH") {
      throw new ResourceError(`无法结束 PID ${numericPid} 所在进程组：${error.message}`);
    }
  }
  const deadline = Date.now() + Math.max(0, graceSeconds * 1000);
  while (Date.now() < deadline && processAlive(numericPid)) {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  if (processAlive(numericPid)) {
    try {
      process.kill(-groupId, "SIGKILL");
    } catch (error) {
      if (!error || error.code !== "ESRCH") {
        throw new ResourceError(`无法强制结束 PID ${numericPid} 所在进程组：${error.message}`);
      }
    }
  }
  if (tracked) {
    await waitWithTimeout(tracked.completion, 10000);
  }
  LIVE_PROCESSES.delete(numericPid);
  return { status: "stopped", pid: numericPid };
}

/** 生成资源展示命令，不改变宿主传入的 shell 或参数语义。 */
function commandText(program, args, command) {
  if (typeof command === "string" && command.trim()) {
    return command.trim();
  }
  if (typeof program !== "string" || !program.trim()) {
    throw new ResourceError("run 操作必须提供 program 或 command。");
  }
  return [program, ...args].join(" ");
}

/** 按 Windows 不区分大小写的环境变量规则读取值。 */
function windowsEnvironmentValue(environment, name) {
  const expected = String(name).toUpperCase();
  const matchedName = Object.keys(environment || {}).find((key) => key.toUpperCase() === expected);
  return matchedName ? environment[matchedName] : undefined;
}

/** 判断指定路径是否为存在的 Windows 批处理包装程序。 */
function existingWindowsBatchPath(candidate) {
  if (!WINDOWS_BATCH_EXTENSIONS.has(path.extname(candidate).toLowerCase())) {
    return null;
  }
  try {
    return fs.statSync(candidate).isFile() ? candidate : null;
  } catch (error) {
    if (error && ["ENOENT", "ENOTDIR"].includes(error.code)) {
      return null;
    }
    throw new ResourceError(`无法读取 Windows 命令包装程序：${candidate}`);
  }
}

/** 在 Windows 的当前目录和 PATH 中解析 .bat/.cmd 命令包装程序。 */
function resolveWindowsBatchProgram(program, cwd, environment = process.env, platform = process.platform) {
  if (platform !== "win32" || typeof program !== "string" || !program.trim()) {
    return null;
  }
  const normalizedProgram = program.trim();
  const hasPath = path.isAbsolute(normalizedProgram) || /[\\/]/.test(normalizedProgram);
  const programExtension = path.extname(normalizedProgram).toLowerCase();
  const pathExtensions = String(windowsEnvironmentValue(environment, "PATHEXT") || ".BAT;.CMD")
    .split(";")
    .map((extension) => extension.trim().toLowerCase())
    .filter((extension) => WINDOWS_BATCH_EXTENSIONS.has(extension));
  const extensions = programExtension ? [""] : [...new Set(pathExtensions)];
  if (programExtension && !WINDOWS_BATCH_EXTENSIONS.has(programExtension)) {
    return null;
  }
  if (!extensions.length) {
    return null;
  }
  const searchDirectories = hasPath
    ? [path.dirname(path.resolve(cwd, normalizedProgram))]
    : [
        cwd,
        ...String(windowsEnvironmentValue(environment, "PATH") || "")
          .split(path.delimiter)
          .map((entry) => entry.trim().replace(/^"(.*)"$/, "$1"))
          .filter(Boolean),
      ];
  const basename = hasPath ? path.basename(normalizedProgram) : normalizedProgram;
  for (const directory of searchDirectories) {
    for (const extension of extensions) {
      const batchPath = existingWindowsBatchPath(path.join(directory, `${basename}${extension}`));
      if (batchPath) {
        return batchPath;
      }
    }
  }
  return null;
}

/** 校验并复制 program 参数数组，避免异步执行期间被调用方修改。 */
function validateArgs(value) {
  if (value === null || value === undefined) {
    return [];
  }
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    throw new ResourceError("args 必须是字符串数组。");
  }
  return [...value];
}

/** 读取合并 stdout/stderr 日志，并保留末尾固定长度。 */
function readLog(logPath, limit = 20000) {
  let content = "";
  try {
    content = fs.readFileSync(logPath, "utf8");
  } catch (error) {
    return "";
  }
  if (content.length <= limit) {
    return content;
  }
  return `[输出已截断，保留末尾 ${limit} 个字符]\n${content.slice(-limit)}`;
}

/** 从账本中移除已结束的资源记录，保证正常退出和超时都不残留。 */
function removeRecord(cwd, runId) {
  const filePath = ledgerPath(cwd);
  return withFileLock(lockPathFor(filePath), () => {
    const records = loadRecords(cwd);
    const remaining = records.filter((item) => item.run_id !== runId);
    if (remaining.length !== records.length) {
      saveRecords(cwd, remaining);
    }
  });
}

/** 将 Node child close/error 事件及合并日志刷新转换为可等待的受管完成状态。 */
function trackProcess(child, logStream = null, logger = null) {
  const completion = new Promise((resolve) => {
    let settled = false;
    let processFinished = false;
    let outputFinished = logStream === null;
    let outputError = null;
    let result = null;
    const finish = () => {
      if (!processFinished || !outputFinished || settled) {
        return;
      }
      settled = true;
      resolve(result);
    };
    const finishOutput = () => {
      if (outputFinished) {
        finish();
        return;
      }
      outputFinished = true;
      if (logStream) {
        logStream.end(() => finish());
      } else {
        finish();
      }
    };
    if (logStream) {
      logStream.once("error", (error) => {
        outputError = error;
        outputFinished = true;
        finish();
      });
    }
    child.once("error", (error) => {
      if (processFinished) {
        return;
      }
      processFinished = true;
      if (logger) {
        logger.warning("spawn_failed", {
          error: error && error.message ? error.message : String(error),
          ...(error && error.code ? { code: error.code } : {}),
        });
      }
      result = { code: null, signal: null, error: error || outputError };
      finishOutput();
    });
    child.once("close", (code, signal) => {
      if (processFinished) {
        return;
      }
      processFinished = true;
      if (logger) {
        logger.info("process_exited", {
          pid: Number.isInteger(child.pid) ? child.pid : null,
          exit_code: code,
          signal: signal || null,
        });
      }
      result = { code, signal, error: outputError };
      finishOutput();
    });
  });
  const entry = {
    // 被跟踪的 Node 子进程对象。
    child,
    // 子进程关闭且 stdout/stderr 日志刷新后的完成 Promise。
    completion,
  };
  if (Number.isInteger(child.pid)) {
    LIVE_PROCESSES.set(child.pid, entry);
  }
  completion.then(() => {
    if (Number.isInteger(child.pid)) {
      LIVE_PROCESSES.delete(child.pid);
    }
  });
  return entry;
}

/** 启动、登记并等待或返回受管本地进程。 */
async function startProcess({
  cwd,
  program = null,
  args = null,
  command = null,
  shell = false,
  wait = true,
  timeoutMs = 1800000,
  stopPolicy = null,
  name = null,
  sessionId = null,
  taskId = null,
  env = undefined,
}) {
  const platformName = currentPlatform();
  const normalizedCwd = normalizePath(cwd);
  if (!fs.statSync(normalizedCwd).isDirectory()) {
    throw new ResourceError(`工作目录不存在：${normalizedCwd}`);
  }
  const normalizedArgs = validateArgs(args);
  const normalizedPolicy = normalizeStopPolicy(stopPolicy);
  const normalizedName = typeof name === "string" && name.trim() ? name.trim() : null;
  if (normalizedPolicy === STOP_POLICY_KEEP && !normalizedName) {
    throw new ResourceError("stop_policy=keep 必须同时提供 name，便于后续显式停止。");
  }
  if (timeoutMs !== null && (!Number.isInteger(timeoutMs) || timeoutMs < 0)) {
    throw new ResourceError("timeout_ms 必须是非负整数或 null。");
  }
  if (env !== undefined && env !== null && (typeof env !== "object" || Array.isArray(env))) {
    throw new ResourceError("env 必须是对象。");
  }
  const owner = resolveOwner(normalizedCwd, sessionId, taskId);
  const displayCommand = commandText(program, normalizedArgs, command);
  let spawnTarget;
  let spawnArgs;
  let batchProgram = null;
  const executionEnvironment = env === undefined || env === null ? process.env : env;
  if (shell) {
    if (typeof command !== "string" || !command.trim()) {
      throw new ResourceError("shell=true 时必须提供 command 字符串。");
    }
    spawnTarget = command;
    spawnArgs = [];
  } else {
    if (typeof program !== "string" || !program.trim()) {
      throw new ResourceError("shell=false 时必须提供 program。");
    }
    batchProgram = resolveWindowsBatchProgram(
      program,
      normalizedCwd,
      executionEnvironment,
    );
    if (batchProgram) {
      const commandInterpreter =
        windowsEnvironmentValue(executionEnvironment, "ComSpec") || "cmd.exe";
      spawnTarget = commandInterpreter;
      // cmd.exe 将批处理程序和参数分别接收，避免路径引号被作为命令字符解释。
      spawnArgs = ["/d", "/c", batchProgram, ...normalizedArgs];
    } else {
      spawnTarget = program;
      spawnArgs = normalizedArgs;
    }
  }

  const runId = crypto.randomUUID();
  const logPath = path.join(workspaceRuntimeDirectory(normalizedCwd), "logs", `${runId}.log`);
  const diagnosticLogPath = path.join(
    workspaceRuntimeDirectory(normalizedCwd),
    "logs",
    `${runId}.events.jsonl`,
  );
  const logger = new TaskAnchorLogger(diagnosticLogPath);
  logger.info("launch_requested", {
    run_id: runId,
    cwd: normalizedCwd,
    platform: platformName,
    program,
    args: normalizedArgs,
    command,
    shell: Boolean(shell),
    wait: Boolean(wait),
    timeout_ms: timeoutMs,
    stop_policy: normalizedPolicy,
    environment_source: env === undefined || env === null ? "process" : "provided",
  });
  logger.debug("execution_environment", {
    path: windowsEnvironmentValue(executionEnvironment, "PATH") ?? null,
    pathext: windowsEnvironmentValue(executionEnvironment, "PATHEXT") ?? null,
    comspec: windowsEnvironmentValue(executionEnvironment, "ComSpec") ?? null,
  });
  if (platformName === PLATFORM_WINDOWS && !shell) {
    logger.debug("windows_batch_resolved", {
      program,
      batch_program: batchProgram,
    });
  }
  const launchOptions = processLaunchOptions(platformName);
  logger.debug("spawn_attempted", {
    spawn_target: spawnTarget,
    spawn_args: spawnArgs,
    cwd: normalizedCwd,
    shell: Boolean(shell),
    ...launchOptions,
  });
  fs.mkdirSync(path.dirname(logPath), { recursive: true });
  let logStream = null;
  let child = null;
  try {
    logStream = fs.createWriteStream(logPath, { flags: "a" });
    const options = {
      cwd: normalizedCwd,
      shell: Boolean(shell),
      ...launchOptions,
      // 通过 Node 管道分别接收 stdout/stderr，再合并写入受管日志。
      stdio: ["ignore", "pipe", "pipe"],
    };
    if (env !== undefined && env !== null) {
      options.env = env;
    }
    child = childProcess.spawn(spawnTarget, spawnArgs, options);
    child.once("spawn", () => {
      logger.info("spawn_succeeded", {
        pid: Number.isInteger(child.pid) ? child.pid : null,
      });
    });
    if (child.stdout) {
      child.stdout.pipe(logStream, { end: false });
    }
    if (child.stderr) {
      child.stderr.pipe(logStream, { end: false });
    }
    const tracked = trackProcess(child, logStream, logger);

    const record = {
      // 资源记录格式版本。
      schema_version: SCHEMA_VERSION,
      // 本次受管运行唯一 ID。
      run_id: runId,
      // 会话所有者键。
      owner_key: owner.ownerKey,
      // 会话哈希。
      session_key: owner.sessionKey,
      // 任务 ID，可为空但保留字段。
      task_id: owner.taskId,
      // 工作区哈希。
      workspace_key: workspaceKey(normalizedCwd),
      // 规范化工作目录。
      cwd: normalizedCwd,
      // 启动平台。
      platform: platformName,
      // 直接启动的程序，shell 模式下为空。
      program: shell ? null : program,
      // 直接传入的参数数组。
      args: normalizedArgs,
      // 展示和账本中的完整命令文本。
      command: displayCommand,
      // 操作系统进程 ID。
      pid: child.pid,
      // UTC ISO 启动时间。
      started_at: utcNow(),
      // Unix 启动时间戳。
      started_at_epoch: Date.now() / 1000,
      // Stop 时清理或保留的策略。
      stop_policy: normalizedPolicy,
      // keep 资源的显式名称。
      name: normalizedName,
      // 合并输出日志路径。
      log_path: logPath,
      // 结构化生命周期诊断日志路径。
      diagnostic_log_path: diagnosticLogPath,
      // 当前登记状态。
      status: "running",
    };
    try {
      const filePath = ledgerPath(normalizedCwd);
      withFileLock(lockPathFor(filePath), () => {
        const records = loadRecords(normalizedCwd);
        records.push(record);
        saveRecords(normalizedCwd, records);
      });
    } catch (error) {
      logger.warning("ledger_write_failed", {
        run_id: runId,
        error: error && error.message ? error.message : String(error),
      });
      try {
        await terminatePid(child.pid);
      } catch (terminationError) {
        logger.warning("ledger_write_failed", {
          run_id: runId,
          error: terminationError && terminationError.message
            ? terminationError.message
            : String(terminationError),
        });
      }
      throw error instanceof ResourceError
        ? error
        : new ResourceError(`资源登记失败：${error.message}；诊断日志：${diagnosticLogPath}`);
    }

    let timer = null;
    let timeoutTriggered = false;
    // 超时终止失败的可观察错误信息，失败时保留账本供后续 stop 重试。
    let timeoutFailureMessage = null;
    let timeoutPromise = null;
    const removeOnCompletion = tracked.completion.then(async (completion) => {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      try {
        removeRecord(normalizedCwd, runId);
      } catch (error) {
        logger.warning("ledger_remove_failed", {
          run_id: runId,
          error: error && error.message ? error.message : String(error),
        });
      }
      return completion;
    });

    if (normalizedPolicy === STOP_POLICY_CLEANUP && timeoutMs !== null) {
      timeoutPromise = new Promise((resolve) => {
        timer = setTimeout(() => {
          timeoutTriggered = true;
          logger.warning("timeout_triggered", {
            run_id: runId,
            pid: child.pid,
            timeout_ms: timeoutMs,
          });
          (async () => {
            try {
              const termination = await terminatePid(child.pid);
              if (
                !termination ||
                !["stopped", "already_stopped"].includes(termination.status)
              ) {
                timeoutFailureMessage =
                  `Task Anchor 超时停止 PID ${child.pid} 未确认终止，资源账本已保留，可通过 operation=stop 重试。`;
                logger.warning("timeout_stop_failed", {
                  run_id: runId,
                  pid: child.pid,
                  error: timeoutFailureMessage,
                });
              } else {
                logger.info("timeout_stop_succeeded", {
                  run_id: runId,
                  pid: child.pid,
                  status: termination.status,
                });
                try {
                  removeRecord(normalizedCwd, runId);
                } catch (error) {
                  logger.warning("ledger_remove_failed", {
                    run_id: runId,
                    error: error && error.message ? error.message : String(error),
                  });
                }
              }
            } catch (error) {
              timeoutFailureMessage =
                `Task Anchor 超时停止 PID ${child.pid} 失败：${error.message}；资源账本已保留，可通过 operation=stop 重试。`;
              logger.warning("timeout_stop_failed", {
                run_id: runId,
                pid: child.pid,
                error: error && error.message ? error.message : String(error),
              });
            } finally {
              timer = null;
              resolve();
            }
          })();
        }, timeoutMs);
      });
    }

    if (!wait) {
      return {
        // 受管运行唯一 ID。
        run_id: runId,
        // 操作系统进程 ID。
        pid: child.pid,
        // 当前进程仍在运行。
        status: "running",
        // 停止策略。
        stop_policy: normalizedPolicy,
        // 展示命令。
        command: displayCommand,
        // 规范化工作目录。
        cwd: normalizedCwd,
        // 启动平台。
        platform: platformName,
        // 合并输出日志路径。
        log_path: logPath,
        // 结构化生命周期诊断日志路径。
        diagnostic_log_path: diagnosticLogPath,
      };
    }

    let completion;
    if (timeoutPromise) {
      const racedCompletion = await Promise.race([removeOnCompletion, timeoutPromise]);
      if (racedCompletion === undefined) {
        if (timeoutFailureMessage) {
          throw new ResourceError(timeoutFailureMessage);
        }
        completion = await waitWithTimeout(removeOnCompletion, 10000);
      } else {
        completion = racedCompletion;
      }
    } else {
      completion = await removeOnCompletion;
    }
    if (completion && completion.error) {
      throw new ResourceError(
        `启动命令失败：${completion.error.message}；诊断日志：${diagnosticLogPath}`,
      );
    }
    const result = {
      // 受管运行唯一 ID。
      run_id: runId,
      // 操作系统进程 ID。
      pid: child.pid,
      // 进程已经退出。
      status: "exited",
      // 停止策略。
      stop_policy: normalizedPolicy,
      // 展示命令。
      command: displayCommand,
      // 规范化工作目录。
      cwd: normalizedCwd,
      // 合并输出日志路径。
      log_path: logPath,
      // 结构化生命周期诊断日志路径。
      diagnostic_log_path: diagnosticLogPath,
      // 被执行进程的合并输出。
      output: readLog(logPath),
    };
    if (timeoutTriggered) {
      result.timed_out = true;
    } else if (completion && Number.isInteger(completion.code)) {
      result.exit_code = completion.code;
    }
    return result;
  } catch (error) {
    if (logStream !== null && child === null) {
      try {
        logStream.destroy();
      } catch (closeError) {
        // 关闭失败不覆盖启动异常。
      }
    }
    if (error instanceof ResourceError) {
      throw error;
    }
    throw new ResourceError(`启动命令失败：${error.message}`);
  }
}

/** 判断账本记录是否同时属于指定会话和工作区。 */
function matchesOwner(record, ownerKey, workspace) {
  return record.workspace_key === workspace && record.owner_key === ownerKey;
}

/** 为已有账本记录创建诊断日志器，兼容缺少诊断路径的历史记录。 */
function loggerForRecord(record) {
  return typeof record.diagnostic_log_path === "string" && record.diagnostic_log_path.trim()
    ? new TaskAnchorLogger(record.diagnostic_log_path)
    : null;
}

/** 停止指定会话和工作区内的资源，显式 run_id 遵循 Codex 既有权限边界。 */
async function stopProcess({
  cwd,
  runId = null,
  name = null,
  sessionId = null,
  taskId = null,
  includeKeep = true,
}) {
  const normalizedCwd = normalizePath(cwd);
  let owner = null;
  if (runId === null || EXPLICIT_RUN_ID_REQUIRES_OWNER) {
    owner = resolveOwner(normalizedCwd, sessionId, taskId);
  }
  const workspace = workspaceKey(normalizedCwd);
  const filePath = ledgerPath(normalizedCwd);
  const records = withFileLock(lockPathFor(filePath), () => loadRecords(normalizedCwd));
  const selected = [];
  for (const record of records) {
    let matched = false;
    if (runId !== null && runId !== undefined) {
      matched = record.run_id === runId && (!owner || matchesOwner(record, owner.ownerKey, workspace));
    } else if (name !== null && name !== undefined) {
      matched = Boolean(owner) && record.name === name && matchesOwner(record, owner.ownerKey, workspace);
    } else {
      matched = Boolean(owner) && matchesOwner(record, owner.ownerKey, workspace);
    }
    if (matched && (includeKeep || record.stop_policy !== STOP_POLICY_KEEP)) {
      selected.push(record);
    }
  }

  const stopped = [];
  const failed = [];
  const succeededIds = new Set();
  const loggerByRunId = new Map();
  for (const record of selected) {
    const logger = loggerForRecord(record);
    if (logger) {
      loggerByRunId.set(record.run_id, logger);
      logger.info("stop_requested", {
        run_id: record.run_id,
        pid: Number(record.pid),
      });
    }
    try {
      const termination = await terminatePid(Number(record.pid));
      if (logger) {
        logger.info("stop_succeeded", {
          run_id: record.run_id,
          pid: Number(record.pid),
          status: termination.status,
        });
      }
      stopped.push({
        // 停止状态和 PID。
        ...termination,
        // 资源运行 ID。
        run_id: record.run_id,
        // 资源名称。
        name: record.name ?? null,
      });
      succeededIds.add(record.run_id);
    } catch (error) {
      if (logger) {
        logger.warning("stop_failed", {
          run_id: record.run_id,
          pid: Number(record.pid),
          error: error && error.message ? error.message : String(error),
        });
      }
      failed.push({
        // 停止失败的资源运行 ID。
        run_id: record.run_id,
        // 停止失败原因。
        error: error.message,
      });
    }
  }
  try {
    withFileLock(lockPathFor(filePath), () => {
      const current = loadRecords(normalizedCwd);
      saveRecords(normalizedCwd, current.filter((record) => !succeededIds.has(record.run_id)));
    });
  } catch (error) {
    for (const [runId, logger] of loggerByRunId) {
      logger.warning("ledger_write_failed", {
        run_id: runId,
        error: error && error.message ? error.message : String(error),
      });
    }
    throw error;
  }
  return {
    // 成功停止或已经停止的资源。
    stopped,
    // 停止失败且仍保留账本的资源。
    failed,
    // 因 keep 策略而保留的同所有者资源。
    kept: owner
      ? records
          .filter(
            (record) =>
              record.stop_policy === STOP_POLICY_KEEP &&
              matchesOwner(record, owner.ownerKey, workspace) &&
              !succeededIds.has(record.run_id),
          )
          .map((record) => record.run_id)
      : [],
  };
}

/** 清理 Stop 或 SessionEnd 的默认资源，保留 keep 资源。 */
function cleanupForStop(options) {
  return stopProcess({ ...options, includeKeep: false });
}

/** 列出当前会话和工作区仍在账本中的资源。 */
function listProcesses({ cwd, sessionId = null, taskId = null }) {
  const normalizedCwd = normalizePath(cwd);
  const owner = resolveOwner(normalizedCwd, sessionId, taskId);
  const workspace = workspaceKey(normalizedCwd);
  const filePath = ledgerPath(normalizedCwd);
  const records = withFileLock(lockPathFor(filePath), () => loadRecords(normalizedCwd));
  return records.filter((record) => matchesOwner(record, owner.ownerKey, workspace));
}

module.exports = {
  SCHEMA_VERSION,
  STOP_POLICY_CLEANUP,
  STOP_POLICY_KEEP,
  VALID_STOP_POLICIES,
  WINDOWS_BATCH_EXTENSIONS,
  PLATFORM_WINDOWS,
  PLATFORM_MACOS,
  PLATFORM_LINUX,
  EXPLICIT_RUN_ID_REQUIRES_OWNER,
  LIVE_PROCESSES,
  ResourceError,
  currentPlatform,
  utcNow,
  sha256Text,
  normalizePath,
  workspaceIdentity,
  workspaceKey,
  runtimeRoot,
  workspaceRuntimeDirectory,
  ledgerPath,
  contextPath,
  lockPathFor,
  sessionKey,
  withFileLock,
  atomicWrite,
  readJson,
  writeJson,
  normalizeStopPolicy,
  activeContext,
  setActiveContext,
  resolveOwner,
  loadRecords,
  saveRecords,
  processAlive,
  processLaunchOptions,
  processGroupId,
  terminatePid,
  commandText,
  windowsEnvironmentValue,
  existingWindowsBatchPath,
  resolveWindowsBatchProgram,
  validateArgs,
  readLog,
  startProcess,
  matchesOwner,
  stopProcess,
  cleanupForStop,
  listProcesses,
  // Python manager 风格别名，便于旧 Hook 和回归测试核对相同语义。
  _process_alive: processAlive,
  _process_launch_options: processLaunchOptions,
  _process_group_id: processGroupId,
  _terminate_pid: terminatePid,
  start_process: startProcess,
  stop_process: stopProcess,
  cleanup_for_stop: cleanupForStop,
  list_processes: listProcesses,
  set_active_context: setActiveContext,
};
