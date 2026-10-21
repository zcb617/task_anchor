"use strict";

const fs = require("node:fs");
const path = require("node:path");
const resourceManager = require("./resource_manager.cjs");

// MCP 备用任务清单工具名称，供宿主与 Hook 通过统一名称绑定。
const TOOL_NAME = "task_checklist";
// 清单工具只允许读取或整体替换任务条目。
const VALID_OPERATIONS = new Set(["read", "write"]);
// Task Anchor 任务 ID 必须是小写标准 UUID，避免路径穿越和身份猜测。
const TASK_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
// 清单条目只允许待开始和已完成两种业务状态。
const VALID_ITEM_STATUSES = new Set(["待开始", "已完成"]);
// 三个独立读取工具名称，供 Hook 注入快照并由 MCP 注册。
const QUERY_TOOL_NAMES = ["get_last_task", "list_tasks", "get_task"];

/** 任务清单或查询业务校验失败，用于向 MCP 返回可理解的错误。 */
class CheckListError extends Error {
  /** 创建任务清单业务异常。 */
  constructor(message) {
    super(message);
    this.name = "CheckListError";
  }
}

// 任务快照中的会话哈希字段，用于验证 Hook 提供的真实会话身份。
const TASK_CONTEXT_SCHEMA = {
  type: "object",
  properties: {
    // Hook 当前真实会话的 SHA-256 摘要。
    session_key: { type: "string", pattern: "^[0-9a-f]{64}$", description: "Hook 注入的真实会话摘要。" },
    // 当前会话按创建时间倒序排列的任务摘要。
    tasks: {
      type: "array",
      description: "Hook 注入的当前真实会话任务摘要，不包含原始指令。",
      items: {
        type: "object",
        properties: {
          // 当前会话内任务的真实 UUID。
          task_id: { type: "string", pattern: "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", description: "任务真实 UUID。" },
          // 任务成功创建的带时区时间。
          created_at: { type: "string", description: "任务创建时间。" },
          // 任务业务状态，1 表示进行中，0 表示已结束。
          status: { type: "integer", enum: [0, 1], description: "任务状态。" },
          // 任务绑定的项目身份摘要。
          workspace_sha256: { type: "string", pattern: "^[0-9a-f]{64}$", description: "创建任务时的项目身份摘要。" },
          // 任务关闭时间，仅已关闭任务可能包含。
          closed_at: { type: "string", description: "任务关闭时间。" },
          // 任务关闭原因，仅已关闭任务可能包含。
          closed_reason: { type: "string", description: "任务关闭原因。" },
        },
        required: ["task_id", "created_at", "status", "workspace_sha256"],
        additionalProperties: false,
      },
    },
    // Hook 已校验身份的所选任务及其最初指令；无任务时为 null。
    task: {
      anyOf: [
        {
          type: "object",
          description: "Hook 为读取工具选择并校验的任务。",
          properties: {
            // 被选中任务的真实 UUID。
            task_id: { type: "string", pattern: "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", description: "任务真实 UUID。" },
            // 被选中任务的创建时间。
            created_at: { type: "string", description: "任务创建时间。" },
            // 被选中任务的状态。
            status: { type: "integer", enum: [0, 1], description: "任务状态。" },
            // 被选中任务绑定的项目摘要。
            workspace_sha256: { type: "string", pattern: "^[0-9a-f]{64}$", description: "创建任务时的项目身份摘要。" },
            // 任务关闭时间，仅已关闭任务可能包含。
            closed_at: { type: "string", description: "任务关闭时间。" },
            // 任务关闭原因，仅已关闭任务可能包含。
            closed_reason: { type: "string", description: "任务关闭原因。" },
            // 用户创建任务时提供的原始指令。
            initial_instruction: { type: "string", description: "任务最初指令原文。" },
          },
          required: ["task_id", "created_at", "status", "workspace_sha256", "initial_instruction"],
          additionalProperties: false,
        },
        { type: "null", description: "当前会话尚无任务。" },
      ],
    },
  },
  required: ["session_key", "tasks", "task"],
  additionalProperties: false,
};

// 清单读写工具输入协议，会话和任务身份均由 Hook 注入快照。
const TOOL_SCHEMA = {
  type: "object",
  properties: {
    // 清单读取或整体替换操作。
    operation: { type: "string", enum: ["read", "write"], description: "读取或整体替换所选任务清单。" },
    // 当前工作区路径，用于验证任务创建时的项目边界。
    cwd: { type: "string", description: "当前任务所在工作目录。" },
    // 当前真实会话 ID，由 Hook 自动注入。
    session_id: { type: "string", description: "Hook 自动注入的真实会话 ID。" },
    // 可显式选择当前会话内的历史任务 ID；省略时选择最新任务。
    task_id: { type: "string", description: "可选的当前会话历史任务 UUID；省略时选择最新任务。" },
    // 当前真实会话的任务摘要及所选任务，由 Hook 自动注入。
    task_context: { ...TASK_CONTEXT_SCHEMA, description: "Hook 自动注入的可信任务快照，调用方不得填写。" },
    // write 操作整体替换的任务条目数组。
    items: {
      type: "array",
      description: "write 操作使用的任务条目数组。",
      items: {
        type: "object",
        properties: {
          // 条目的稳定业务标识。
          id: { type: "string", minLength: 1, description: "任务条目的唯一标识。" },
          // 条目的业务描述文本。
          text: { type: "string", minLength: 1, description: "任务条目说明。" },
          // 条目的完成状态。
          status: { type: "string", enum: ["待开始", "已完成"], description: "任务条目状态。" },
        },
        required: ["id", "text", "status"],
        additionalProperties: false,
      },
    },
    // write 操作要求的乐观并发版本。
    expected_revision: { type: "integer", minimum: 0, description: "write 操作要求的当前清单 revision。" },
  },
  required: ["operation", "cwd"],
  additionalProperties: false,
};

// 所有查询工具共用可信 cwd、session_id 和 Hook 任务快照。
const QUERY_INPUT_SCHEMA = {
  type: "object",
  properties: {
    // 当前工作区路径，由 Hook 校验项目边界。
    cwd: { type: "string", description: "当前任务所在工作目录。" },
    // 当前真实会话 ID，由 Hook 自动注入。
    session_id: { type: "string", description: "Hook 自动注入的真实会话 ID。" },
    // 当前真实会话任务摘要及 Hook 选择的任务。
    task_context: { ...TASK_CONTEXT_SCHEMA, description: "Hook 自动注入的可信任务快照，调用方不得填写。" },
  },
  required: ["cwd"],
  additionalProperties: false,
};

// 查询工具的工具名、业务说明和 MCP 输入协议。
const QUERY_TOOLS = [
  {
    // 查询当前真实会话中创建时间最新的任务。
    name: "get_last_task",
    // 返回最新任务和对应清单；没有任务时返回 null。
    description: "读取当前真实会话中创建时间最新的任务及其清单；没有任务时返回 task: null。",
    // 查询身份由 Hook 注入，不要求 task_id。
    inputSchema: QUERY_INPUT_SCHEMA,
  },
  {
    // 列出当前真实会话的所有任务摘要。
    name: "list_tasks",
    // 按创建时间倒序列出任务摘要，不返回原始指令。
    description: "按创建时间倒序列出当前真实会话的任务摘要，不返回原始指令。",
    // 查询身份由 Hook 注入，不要求 task_id。
    inputSchema: QUERY_INPUT_SCHEMA,
  },
  {
    // 按真实 UUID 查询当前会话的指定任务。
    name: "get_task",
    // 返回当前真实会话内指定任务及其清单。
    description: "按 task_id 读取当前真实会话内的指定任务及其清单。",
    // 查询身份由 Hook 注入，目标任务 ID 由调用方明确提供。
    inputSchema: {
      ...QUERY_INPUT_SCHEMA,
      properties: {
        ...QUERY_INPUT_SCHEMA.properties,
        // 当前会话中要读取的真实任务 UUID。
        task_id: { type: "string", pattern: "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", description: "要读取的当前会话任务真实 UUID。" },
      },
      required: [...QUERY_INPUT_SCHEMA.required, "task_id"],
    },
  },
];

/** 判断任务 ID 是否为小写标准 UUID。 */
function isTaskId(value) {
  return typeof value === "string" && TASK_ID_PATTERN.test(value);
}

/** 校验带时区的 ISO 时间，拒绝缺少时区或无法解析的任务时间。 */
function isZonedDate(value) {
  return typeof value === "string"
    && /(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    && Number.isFinite(Date.parse(value));
}

/** 返回任务清单文件的可信路径，按工作区和真实任务 ID 分区。 */
function checklistPath(cwd, taskId) {
  if (typeof cwd !== "string" || !cwd.trim()) {
    throw new CheckListError("cwd 必须是非空字符串。");
  }
  if (!isTaskId(taskId)) {
    throw new CheckListError("task_id 必须是小写标准 UUID。");
  }
  return path.join(resourceManager.runtimeRoot(), "workspaces", fullDirectoryKey(cwd), "tasks", taskId, "checklist.json");
}

/** 返回完整规范化目录的稳定存储键，隔离不同工作目录的清单。 */
function fullDirectoryKey(cwd) {
  return resourceManager.sha256Text(resourceManager.normalizePath(cwd));
}

/** 校验任务条目数组的字段、文本、状态和唯一标识。 */
function validateItems(items) {
  if (!Array.isArray(items)) {
    throw new CheckListError("items 必须是数组。");
  }
  const seenIds = new Set();
  return items.map((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new CheckListError("任务清单条目必须是对象。");
    }
    if (Object.keys(item).length !== 3 || !["id", "text", "status"].every((key) => Object.hasOwn(item, key))) {
      throw new CheckListError("任务清单条目只能包含 id、text 和 status。");
    }
    if (typeof item.id !== "string" || !item.id.trim()) {
      throw new CheckListError("任务清单条目 id 不能为空。");
    }
    if (typeof item.text !== "string" || !item.text.trim()) {
      throw new CheckListError("任务清单条目 text 不能为空。");
    }
    if (!VALID_ITEM_STATUSES.has(item.status)) {
      throw new CheckListError("任务清单条目 status 只能是待开始或已完成。");
    }
    if (seenIds.has(item.id)) {
      throw new CheckListError(`任务清单条目 id 重复：${item.id}`);
    }
    seenIds.add(item.id);
    return { id: item.id, text: item.text, status: item.status };
  });
}

/** 校验 Hook 注入的会话任务快照及其所选任务身份。 */
function validateTaskContext(argumentsObject) {
  const cwd = argumentsObject.cwd;
  const sessionId = argumentsObject.session_id;
  const taskContext = argumentsObject.task_context;
  if (typeof cwd !== "string" || !cwd.trim()) {
    throw new CheckListError("缺少可信 cwd。");
  }
  if (typeof sessionId !== "string" || !sessionId.trim()) {
    throw new CheckListError("缺少 Hook 注入的真实 session_id。");
  }
  if (!taskContext || typeof taskContext !== "object" || Array.isArray(taskContext)) {
    throw new CheckListError("缺少 Hook 注入的 task_context，不能按目录猜测任务身份。");
  }
  if (
    Object.keys(taskContext).length !== 3
    || !["session_key", "tasks", "task"].every((key) => Object.hasOwn(taskContext, key))
    || taskContext.session_key !== resourceManager.sessionKey(sessionId)
    || !Array.isArray(taskContext.tasks)
  ) {
    throw new CheckListError("task_context 与真实会话身份不匹配。");
  }

  const summaries = new Map();
  let previousTask = null;
  for (const summary of taskContext.tasks) {
    if (!summary || typeof summary !== "object" || Array.isArray(summary)) {
      throw new CheckListError("task_context.tasks 包含无效任务摘要。");
    }
    if (
      !isTaskId(summary.task_id)
      || !isZonedDate(summary.created_at)
      || ![0, 1].includes(summary.status)
      || typeof summary.workspace_sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(summary.workspace_sha256)
      || (Object.hasOwn(summary, "closed_at") && !isZonedDate(summary.closed_at))
      || (Object.hasOwn(summary, "closed_reason") && typeof summary.closed_reason !== "string")
    ) {
      throw new CheckListError("task_context.tasks 包含校验失败的任务摘要。");
    }
    const allowedSummaryKeys = ["task_id", "created_at", "status", "workspace_sha256", "closed_at", "closed_reason"];
    if (
      summaries.has(summary.task_id)
      || Object.keys(summary).some((key) => !allowedSummaryKeys.includes(key))
      || (previousTask && (
        Date.parse(previousTask.created_at) < Date.parse(summary.created_at)
        || (previousTask.created_at === summary.created_at && previousTask.task_id < summary.task_id)
      ))
    ) {
      throw new CheckListError("task_context.tasks 重复或未按创建时间倒序排列。");
    }
    summaries.set(summary.task_id, summary);
    previousTask = summary;
  }

  let selectedTask = null;
  if (taskContext.task !== null) {
    const task = taskContext.task;
    if (
      !task
      || typeof task !== "object"
      || Array.isArray(task)
      || typeof task.initial_instruction !== "string"
      || !isTaskId(task.task_id)
      || !isZonedDate(task.created_at)
      || ![0, 1].includes(task.status)
      || typeof task.workspace_sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(task.workspace_sha256)
      || (Object.hasOwn(task, "closed_at") && !isZonedDate(task.closed_at))
      || (Object.hasOwn(task, "closed_reason") && typeof task.closed_reason !== "string")
      || Object.keys(task).some((key) => !["task_id", "created_at", "status", "workspace_sha256", "closed_at", "closed_reason", "initial_instruction"].includes(key))
    ) {
      throw new CheckListError("task_context.task 缺少有效的已选任务数据。");
    }
    const listedTask = summaries.get(task.task_id);
    if (
      !listedTask
      || listedTask.created_at !== task.created_at
      || listedTask.status !== task.status
      || listedTask.workspace_sha256 !== task.workspace_sha256
      || task.workspace_sha256 !== resourceManager.workspaceKey(cwd)
    ) {
      throw new CheckListError("所选任务不属于当前会话或当前项目。");
    }
    selectedTask = task;
  }
  return { cwd, sessionId, taskContext, summaries, selectedTask };
}

/** 校验已存在清单的数据格式及工作区、会话和任务身份。 */
function validateChecklist(value, cwd, sessionId, taskId) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new CheckListError("任务清单内容损坏。");
  }
  const checklistFields = ["schema_version", "workspace_key", "session_key", "task_id", "revision", "created_at", "updated_at", "items"];
  if (Object.keys(value).length !== checklistFields.length || !checklistFields.every((field) => Object.hasOwn(value, field))) {
    throw new CheckListError("任务清单字段不完整或包含未知字段。");
  }
  if (
    value.schema_version !== 1
    || value.workspace_key !== fullDirectoryKey(cwd)
    || value.session_key !== resourceManager.sessionKey(sessionId)
    || value.task_id !== taskId
    || !Number.isInteger(value.revision)
    || value.revision < 0
  ) {
    throw new CheckListError("任务清单工作区、会话、任务身份或 revision 校验失败。");
  }
  if (typeof value.created_at !== "string" || typeof value.updated_at !== "string") {
    throw new CheckListError("任务清单时间字段无效。");
  }
  const items = validateItems(value.items);
  return { ...value, items };
}

/** 创建任务清单初始数据，绑定真实会话、工作区和任务。 */
function emptyChecklist(cwd, sessionId, taskId) {
  const now = resourceManager.utcNow();
  return {
    // 清单数据结构版本。
    schema_version: 1,
    // 清单所属完整工作目录摘要。
    workspace_key: fullDirectoryKey(cwd),
    // 清单所属真实 Hook 会话摘要。
    session_key: resourceManager.sessionKey(sessionId),
    // 清单所属真实 Task Anchor 任务 ID。
    task_id: taskId,
    // 当前清单并发写入版本。
    revision: 0,
    // 清单首次创建时间。
    created_at: now,
    // 清单最后修改时间。
    updated_at: now,
    // 当前任务清单条目。
    items: [],
  };
}

/** 在清单文件锁内读取已有清单或为真实任务懒初始化空清单。 */
function readChecklist(filePath, cwd, sessionId, taskId) {
  return resourceManager.withFileLock(resourceManager.lockPathFor(filePath), () => {
    const exists = fs.existsSync(filePath);
    const stored = resourceManager.readJson(filePath, null);
    if (!exists) {
      const initialized = emptyChecklist(cwd, sessionId, taskId);
      resourceManager.writeJson(filePath, initialized);
      return initialized;
    }
    return validateChecklist(stored, cwd, sessionId, taskId);
  });
}

/** 在任务清单文件锁内执行带 revision CAS 的整体替换写入。 */
function writeChecklist(filePath, cwd, sessionId, taskId, items, expectedRevision) {
  return resourceManager.withFileLock(resourceManager.lockPathFor(filePath), () => {
    const exists = fs.existsSync(filePath);
    const stored = resourceManager.readJson(filePath, null);
    const current = !exists
      ? emptyChecklist(cwd, sessionId, taskId)
      : validateChecklist(stored, cwd, sessionId, taskId);
    if (current.revision !== expectedRevision) {
      throw new CheckListError(
        `任务清单 revision 冲突：期望 ${expectedRevision}，当前 ${current.revision}。`,
      );
    }
    const requestedItems = validateItems(items);
    const next = {
      ...current,
      // 每次成功提交整体替换时递增 revision。
      revision: current.revision + 1,
      // 记录本次清单更新的 UTC 时间。
      updated_at: resourceManager.utcNow(),
      // 保存通过业务规则校验的完整条目。
      items: requestedItems,
    };
    resourceManager.writeJson(filePath, next);
    return next;
  });
}

/** 读取指定任务的清单，并在查询结果中附上受信任文件路径。 */
function getTask(argumentsObject) {
  const context = validateTaskContext(argumentsObject);
  const taskId = argumentsObject.task_id;
  if (!isTaskId(taskId)) {
    throw new CheckListError("task_id 必须是小写标准 UUID。");
  }
  const listedTask = context.summaries.get(taskId);
  const task = context.selectedTask;
  if (!listedTask || !task || task.task_id !== taskId) {
    throw new CheckListError("指定 task_id 不属于当前 Hook 快照或未通过项目边界校验。");
  }
  const filePath = checklistPath(context.cwd, taskId);
  const checklist = readChecklist(filePath, context.cwd, context.sessionId, taskId);
  return {
    ...task,
    // 返回所选任务的清单内容和可信文件路径。
    checklist: { ...checklist, path: filePath },
  };
}

/** 执行 task_checklist 的 read/write 业务操作并返回清单路径与数据。 */
function executeTool(argumentsObject) {
  if (!argumentsObject || typeof argumentsObject !== "object" || Array.isArray(argumentsObject)) {
    throw new CheckListError("工具参数必须是对象。");
  }
  const operation = argumentsObject.operation;
  if (!VALID_OPERATIONS.has(operation)) {
    throw new CheckListError("operation 只能是 read 或 write。");
  }
  if (operation === "write") {
    if (!Array.isArray(argumentsObject.items)) {
      throw new CheckListError("write 操作必须提供 items 数组。");
    }
    if (!Number.isInteger(argumentsObject.expected_revision) || argumentsObject.expected_revision < 0) {
      throw new CheckListError("write 操作必须提供非负整数 expected_revision。");
    }
  }
  const context = validateTaskContext(argumentsObject);
  if (!context.selectedTask) {
    if (operation === "read") {
      // 当前会话没有任务时只报告空结果，不创建任务或清单文件。
      return { task: null };
    }
    throw new CheckListError("当前会话没有任务，不能写入任务清单。");
  }
  const taskId = argumentsObject.task_id === undefined
    ? context.selectedTask.task_id
    : argumentsObject.task_id;
  if (!isTaskId(taskId)) {
    throw new CheckListError("task_id 必须是小写标准 UUID。");
  }
  if (!context.summaries.has(taskId) || context.selectedTask.task_id !== taskId) {
    throw new CheckListError("指定 task_id 不属于当前 Hook 快照或未通过项目边界校验。");
  }
  if (operation === "write" && context.summaries.get(taskId).status === 0) {
    throw new CheckListError("已结束任务的清单只允许读取，不能写入。");
  }
  const filePath = checklistPath(context.cwd, taskId);
  const checklist = operation === "read"
    ? readChecklist(filePath, context.cwd, context.sessionId, taskId)
    : writeChecklist(
      filePath,
      context.cwd,
      context.sessionId,
      taskId,
      argumentsObject.items,
      argumentsObject.expected_revision,
    );
  return { ...checklist, path: filePath };
}

/** 列出当前 Hook 快照中的任务摘要，不读取原始指令或清单文件。 */
function listTasks(argumentsObject) {
  const context = validateTaskContext(argumentsObject);
  return {
    // 返回 Hook 快照内的所有当前会话任务摘要。
    tasks: context.taskContext.tasks.map((task) => ({ ...task })),
  };
}

/** 执行三个独立任务查询工具，并按真实任务身份复用同一读取逻辑。 */
function executeQuery(toolName, argumentsObject) {
  if (!QUERY_TOOL_NAMES.includes(toolName)) {
    throw new CheckListError("未知的 Task Anchor 查询工具。");
  }
  if (!argumentsObject || typeof argumentsObject !== "object" || Array.isArray(argumentsObject)) {
    throw new CheckListError("工具参数必须是对象。");
  }
  if (toolName === "list_tasks") {
    return listTasks(argumentsObject);
  }
  if (toolName === "get_last_task") {
    const context = validateTaskContext(argumentsObject);
    const latestTask = context.taskContext.tasks[0];
    if (!latestTask) {
      // 当前会话无任务时返回明确空结果，不生成身份或文件。
      return { task: null };
    }
    // 将创建时间最新的真实 ID 交给与 get_task 相同的读取逻辑。
    return { task: getTask({ ...argumentsObject, task_id: latestTask.task_id }) };
  }
  // 读取调用方明确指定的当前会话任务。
  return { task: getTask(argumentsObject) };
}

module.exports = {
  TOOL_NAME,
  TOOL_SCHEMA,
  QUERY_TOOLS,
  executeTool,
  executeQuery,
  checklistPath,
  fullDirectoryKey,
  CheckListError,
};
