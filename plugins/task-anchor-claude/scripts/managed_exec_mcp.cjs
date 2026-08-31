"use strict";

const readline = require("node:readline");
const resourceManager = require("./resource_manager.cjs");

// MCP 服务名称，保持旧 Python 服务的协议身份。
const SERVER_NAME = "task-anchor";
// MCP 服务版本，保持既有版本号不变。
const SERVER_VERSION = "0.1.0";
// MCP 协议版本，保持既有客户端协商契约。
const PROTOCOL_VERSION = "2025-06-18";
// 对外暴露的唯一工具名称。
const TOOL_NAME = "managed_exec";

// managed_exec 的输入参数协议，字段与旧 Python MCP 保持一致。
const TOOL_SCHEMA = {
  type: "object",
  properties: {
    // 资源业务操作：启动、停止、查询或清理。
    operation: {
      type: "string",
      enum: ["run", "stop", "list", "cleanup"],
      default: "run",
      description: "run 启动命令；stop 停止指定资源；list 查看登记；cleanup 清理默认资源。",
    },
    // 直接启动的可执行程序。
    program: { type: "string", description: "可执行程序，例如 npm、python、java。" },
    // 直接传给 program 的参数数组。
    args: {
      type: "array",
      items: { type: "string" },
      description: "传给 program 的参数。优先使用 program+args，而不是 shell 字符串。",
    },
    // shell 模式下交给宿主 shell 的完整命令。
    command: { type: "string", description: "仅在 shell=true 时使用的完整命令。" },
    // 是否保持宿主 shell 参数语义。
    shell: { type: "boolean", default: false },
    // 子进程工作目录。
    cwd: { type: "string", description: "工作目录，默认当前工作目录。" },
    // 是否等待命令退出。
    wait: { type: "boolean", default: true, description: "是否等待命令退出。" },
    // 单次命令的毫秒级时间上限。
    timeout_ms: { type: ["integer", "null"], default: 1800000 },
    // Stop 时清理还是保留资源。
    stop_policy: {
      type: "string",
      enum: ["cleanup", "keep"],
      default: "cleanup",
      description: "默认 cleanup：Stop 时关闭；keep：Stop 时保留。",
    },
    // keep 资源用于显式 stop 的名称。
    name: { type: "string", description: "资源名称，便于后续 stop。" },
    // 显式停止目标资源的唯一 ID。
    run_id: { type: "string" },
    // 当前可信会话标识。
    session_id: { type: "string", description: "通常不需要，默认从 Task Anchor 当前上下文解析。" },
    // 当前任务标识。
    task_id: { type: "string", description: "通常不需要，默认从 Task Anchor 当前上下文解析。" },
    // stop 是否连 keep 资源一并停止。
    include_keep: { type: "boolean", default: true },
    // 传给子进程的环境变量对象。
    env: {
      type: "object",
      additionalProperties: { type: "string" },
      description: "传给子进程的环境变量；未提供时沿用 Node 进程环境。",
    },
  },
  additionalProperties: false,
};

// MCP 客户端消费的稳定结构化结果协议。
const TOOL_OUTPUT_SCHEMA = {
  type: "object",
  description:
    "managed_exec 自身保证的稳定结果外壳。output 字段是被执行程序产生的原始文本，其内部格式不固定，不应按特定结构解析。",
  $defs: {
    // stop 返回的单条资源结果。
    stoppedResource: {
      type: "object",
      properties: {
        // 资源停止结果。
        status: { type: "string", enum: ["stopped", "already_stopped"], description: "资源已被本次调用停止，或调用前已经停止。" },
        // 被停止资源的操作系统 PID。
        pid: { type: "integer", description: "操作系统进程 ID。" },
        // Task Anchor 分配的运行 ID。
        run_id: { type: ["string", "null"], description: "Task Anchor 为该次运行分配的唯一标识。" },
        // 调用方设置的资源名称。
        name: { type: ["string", "null"], description: "调用方为受管资源设置的可选名称。" },
      },
      required: ["status", "pid", "run_id", "name"],
      additionalProperties: false,
    },
    // stop 返回的失败资源结果。
    failedResource: {
      type: "object",
      properties: {
        // 停止失败的运行 ID。
        run_id: { type: ["string", "null"], description: "停止失败的受管运行标识。" },
        // 停止失败的业务原因。
        error: { type: "string", description: "停止失败的原因。" },
      },
      required: ["run_id", "error"],
      additionalProperties: false,
    },
    // list 返回的账本资源记录。
    registeredResource: {
      type: "object",
      properties: {
        // 账本格式版本。
        schema_version: { type: "integer", description: "资源记录格式版本。" },
        // 受管运行唯一 ID。
        run_id: { type: "string", description: "受管运行的唯一标识。" },
        // 会话所有者内部键。
        owner_key: { type: "string", description: "资源所属会话的内部标识。" },
        // 会话哈希。
        session_key: { type: "string", description: "资源所属会话标识。" },
        // 所属任务 ID。
        task_id: { type: ["string", "null"], description: "资源所属任务标识；无法解析时为空。" },
        // 工作区哈希。
        workspace_key: { type: "string", description: "资源所属工作区标识。" },
        // 规范化工作目录。
        cwd: { type: "string", description: "进程工作目录的规范化绝对路径。" },
        // 启动平台。
        platform: { type: "string", description: "进程运行平台。" },
        // 直接启动程序。
        program: { type: ["string", "null"], description: "直接启动的可执行程序；shell 命令模式下可能为空。" },
        // 直接启动参数。
        args: { type: "array", items: { type: "string" }, description: "传给可执行程序的参数。" },
        // 展示命令文本。
        command: { type: "string", description: "用于展示和登记的完整命令。" },
        // 操作系统 PID。
        pid: { type: "integer", description: "操作系统进程 ID。" },
        // UTC 启动时间。
        started_at: { type: "string", description: "UTC ISO 8601 启动时间。" },
        // Unix 启动时间戳。
        started_at_epoch: { type: "number", description: "Unix 启动时间戳。" },
        // 停止策略。
        stop_policy: { type: "string", enum: ["cleanup", "keep"], description: "任务停止时清理该资源，或保留到显式停止。" },
        // 资源名称。
        name: { type: ["string", "null"], description: "调用方设置的可选资源名称。" },
        // 合并输出日志路径。
        log_path: { type: "string", description: "合并输出日志文件路径。" },
        // 账本中的运行状态。
        status: { type: "string", enum: ["running"], description: "已登记资源的当前状态。" },
      },
      required: [
        "schema_version", "run_id", "owner_key", "session_key", "task_id", "workspace_key",
        "cwd", "platform", "program", "args", "command", "pid", "started_at", "started_at_epoch",
        "stop_policy", "name", "log_path", "status",
      ],
      additionalProperties: false,
    },
  },
  oneOf: [
    {
      // run 操作的结果结构。
      title: "run 操作结果",
      type: "object",
      properties: {
        // 运行唯一 ID。
        run_id: { type: "string", description: "受管运行的唯一标识。" },
        // 运行 PID。
        pid: { type: "integer", description: "操作系统进程 ID。" },
        // 运行结束状态。
        status: { type: "string", enum: ["running", "exited"], description: "进程仍在运行，或已经退出。" },
        // 是否因为时间上限被结束。
        timed_out: { type: "boolean", description: "命令已达到 timeout_ms 并结束整个进程树。" },
        // 正常退出码。
        exit_code: { type: "integer", description: "进程退出码；仅退出后返回。" },
        // 停止策略。
        stop_policy: { type: "string", enum: ["cleanup", "keep"], description: "任务停止时清理该资源，或保留到显式停止。" },
        // 展示命令。
        command: { type: "string", description: "用于展示和登记的完整命令。" },
        // 规范化工作目录。
        cwd: { type: "string", description: "进程工作目录的规范化绝对路径。" },
        // 启动平台。
        platform: { type: "string", description: "进程运行平台；运行中可能返回。" },
        // 输出日志路径。
        log_path: { type: "string", description: "合并输出日志文件路径。" },
        // 被执行程序产生的原始输出。
        output: { type: "string", description: "被执行程序写入 stdout 和 stderr 的原始合并文本。" },
      },
      required: ["run_id", "pid", "status", "stop_policy", "command", "cwd", "log_path"],
      additionalProperties: false,
    },
    {
      // stop 或 cleanup 操作的结果结构。
      title: "stop 或 cleanup 操作结果",
      type: "object",
      properties: {
        // 成功停止的资源列表。
        stopped: { type: "array", items: { $ref: "#/$defs/stoppedResource" }, description: "成功停止或此前已经停止的资源。" },
        // 停止失败的资源列表。
        failed: { type: "array", items: { $ref: "#/$defs/failedResource" }, description: "未能停止的资源及原因。" },
        // 因 keep 策略保留的运行列表。
        kept: { type: "array", items: { type: ["string", "null"] }, description: "因 stop_policy=keep 而继续保留的运行标识。" },
      },
      required: ["stopped", "failed", "kept"],
      additionalProperties: false,
    },
    {
      // list 操作的结果结构。
      title: "list 操作结果",
      type: "object",
      properties: {
        // 当前会话的资源记录。
        resources: { type: "array", items: { $ref: "#/$defs/registeredResource" }, description: "当前会话登记且仍受管理的进程资源。" },
      },
      required: ["resources"],
      additionalProperties: false,
    },
    {
      // 工具业务错误的结构。
      title: "工具错误结果",
      type: "object",
      properties: {
        // 面向模型的业务错误信息。
        error: { type: "string", description: "工具调用失败的原因。" },
      },
      required: ["error"],
      additionalProperties: false,
    },
  ],
};

/** 构造旧协议兼容的 MCP 工具结果外壳。 */
function toolResult(value, isError = false) {
  const errorMessage = value && typeof value.error === "string" ? value.error : "managed_exec 执行失败。";
  return {
    // 成功结果仅使用结构化内容；错误结果同时提供文本，供 Claude Code 可靠展示。
    content: isError ? [{ type: "text", text: errorMessage }] : [],
    // 稳定的结构化业务结果。
    structuredContent: value,
    // 工具业务错误标记。
    isError,
  };
}

/** 从请求参数读取并校验 Claude 工作目录，缺省时使用插件注入的项目目录。 */
function requireCwd(argumentsObject) {
  const cwd = argumentsObject.cwd === undefined ? process.env.TASK_ANCHOR_DEFAULT_CWD : argumentsObject.cwd;
  if (typeof cwd !== "string" || !cwd.trim()) {
    throw new resourceManager.ResourceError("cwd 必须由 Task Anchor Hook 或插件项目目录提供。");
  }
  return cwd;
}

/** 执行 managed_exec 的业务操作并返回结构化结果。 */
async function executeTool(argumentsObject) {
  if (!argumentsObject || typeof argumentsObject !== "object" || Array.isArray(argumentsObject)) {
    throw new resourceManager.ResourceError("工具参数必须是对象。");
  }
  const operation = argumentsObject.operation === undefined ? "run" : argumentsObject.operation;
  if (!["run", "stop", "list", "cleanup"].includes(operation)) {
    throw new resourceManager.ResourceError("operation 只能是 run、stop、list 或 cleanup。");
  }
  const cwd = requireCwd(argumentsObject);
  const common = {
    // 当前工作目录。
    cwd,
    // 当前可信会话标识。
    sessionId: argumentsObject.session_id,
    // 当前任务标识。
    taskId: argumentsObject.task_id,
  };
  if (operation === "run") {
    return resourceManager.startProcess({
      ...common,
      // 直接执行的程序。
      program: argumentsObject.program,
      // 直接执行参数。
      args: argumentsObject.args,
      // shell 完整命令。
      command: argumentsObject.command,
      // 是否使用 shell。
      shell: Boolean(argumentsObject.shell ?? false),
      // 是否等待关闭。
      wait: Boolean(argumentsObject.wait ?? true),
      // 单次命令时间上限。
      timeoutMs: Object.prototype.hasOwnProperty.call(argumentsObject, "timeout_ms")
        ? argumentsObject.timeout_ms
        : 1800000,
      // Stop 清理策略。
      stopPolicy: argumentsObject.stop_policy,
      // keep 资源名称。
      name: argumentsObject.name,
      // 子进程环境变量。
      env: argumentsObject.env,
    });
  }
  if (operation === "stop") {
    return resourceManager.stopProcess({
      ...common,
      // 显式停止的运行 ID。
      runId: argumentsObject.run_id,
      // 显式停止的资源名称。
      name: argumentsObject.name,
      // 是否包括 keep 资源。
      includeKeep: Boolean(argumentsObject.include_keep ?? true),
    });
  }
  if (operation === "list") {
    return { resources: resourceManager.listProcesses(common) };
  }
  return resourceManager.cleanupForStop(common);
}

/** 构造 JSON-RPC 协议错误响应。 */
function errorResponse(code, message, requestId = null) {
  return {
    // JSON-RPC 协议版本。
    jsonrpc: "2.0",
    // 对应请求 ID。
    id: requestId,
    // 协议错误对象。
    error: { code, message },
  };
}

/** 处理一条 JSON-RPC 请求，通知请求按协议不返回响应。 */
function handleRequest(request) {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    return errorResponse(-32600, "无效的 JSON-RPC 请求。");
  }
  const method = request.method;
  const requestId = request.id;
  if (typeof method !== "string") {
    return errorResponse(-32600, "无效的 JSON-RPC 请求。", requestId);
  }
  if (!Object.prototype.hasOwnProperty.call(request, "id")) {
    return null;
  }
  if (method === "initialize") {
    return {
      // JSON-RPC 协议版本。
      jsonrpc: "2.0",
      // 初始化请求 ID。
      id: requestId,
      result: {
        // 服务支持的协议版本。
        protocolVersion: PROTOCOL_VERSION,
        // 服务能力集合。
        capabilities: { tools: {} },
        // 服务身份。
        serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
      },
    };
  }
  if (method === "ping") {
    return { jsonrpc: "2.0", id: requestId, result: {} };
  }
  if (method === "tools/list") {
    return {
      jsonrpc: "2.0",
      id: requestId,
      result: {
        tools: [
          {
            // 工具名称。
            name: TOOL_NAME,
            // 工具业务说明。
            description: "通过 Task Anchor 启动、登记和停止本地进程。默认 Stop 时清理；需要保留的服务必须显式设置 stop_policy=keep。",
            // 输入参数协议。
            inputSchema: TOOL_SCHEMA,
            // 结构化输出协议。
            outputSchema: TOOL_OUTPUT_SCHEMA,
          },
        ],
      },
    };
  }
  if (method === "tools/call") {
    const parameters = request.params;
    if (!parameters || typeof parameters !== "object" || parameters.name !== TOOL_NAME) {
      return errorResponse(-32602, "未知的工具。", requestId);
    }
    const argumentsObject = parameters.arguments === undefined ? {} : parameters.arguments;
    return executeTool(argumentsObject)
      .then((result) => ({ jsonrpc: "2.0", id: requestId, result: toolResult(result) }))
      .catch((error) => ({
        jsonrpc: "2.0",
        id: requestId,
        result: toolResult({ error: error.message }, true),
      }));
  }
  return errorResponse(-32601, `不支持的方法：${method}`, requestId);
}

/** 读取 stdin 的一行 JSON-RPC，并将响应仅写入 stdout。 */
async function main() {
  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  const pending = new Set();
  for await (const line of input) {
    let response;
    try {
      response = handleRequest(JSON.parse(line));
    } catch (error) {
      response = errorResponse(-32700, "无效的 JSON。");
    }
    if (response === null) {
      continue;
    }
    const responsePromise = Promise.resolve(response).then((resolved) => {
      process.stdout.write(`${JSON.stringify(resolved)}\n`);
    });
    pending.add(responsePromise);
    responsePromise.finally(() => pending.delete(responsePromise));
  }
  await Promise.all([...pending]);
  return 0;
}

module.exports = {
  SERVER_NAME,
  SERVER_VERSION,
  PROTOCOL_VERSION,
  TOOL_NAME,
  TOOL_SCHEMA,
  TOOL_OUTPUT_SCHEMA,
  toolResult,
  requireCwd,
  executeTool,
  errorResponse,
  handleRequest,
  main,
};

if (require.main === module) {
  main().then(
    (code) => {
      process.exitCode = code;
    },
    (error) => {
      process.stderr.write(`Task Anchor managed_exec MCP failed: ${error.message}\n`);
      process.exitCode = 1;
    },
  );
}
