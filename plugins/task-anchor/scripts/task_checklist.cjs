"use strict";

const fs = require("node:fs");
const path = require("node:path");
const resourceManager = require("./resource_manager.cjs");

// MCP 备用任务清单工具名称，供宿主与 Hook 通过统一名称绑定。
const TOOL_NAME = "task_checklist";
// 任务清单只允许这两个业务操作。
const VALID_OPERATIONS = new Set(["read", "write"]);
// Task Anchor 任务 ID 必须是小写标准 UUID，避免路径穿越和任务串线。
const TASK_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
// 清单条目只允许待开始和已完成两种业务状态。
const VALID_ITEM_STATUSES = new Set(["待开始", "已完成"]);

/** 任务清单业务异常，用于向 MCP 返回可理解的校验错误。 */
class CheckListError extends Error {
  /** 创建任务清单业务异常。 */
  constructor(message) {
    super(message);
    this.name = "CheckListError";
  }
}

// 任务清单 MCP 输入参数协议，session_id 由 Hook 注入而不要求模型填写。
const TOOL_SCHEMA = {
  type: "object",
  properties: {
    // 清单读取或整体替换操作。
    operation: { type: "string", enum: ["read", "write"], description: "读取或整体替换当前任务清单。" },
    // 当前工作区路径，用于派生可信工作区隔离键。
    cwd: { type: "string", description: "当前任务所在工作目录。" },
    // Hook 注入的可信会话标识。
    session_id: { type: "string", description: "由 Task Anchor Hook 注入的内部会话标识，模型无需填写。" },
    // 预期当前任务的 UUID，可用于防止任务边界误用。
    task_id: { type: "string", description: "预期当前任务的标准小写 UUID，可选。" },
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

/** 返回任务清单文件的受信任路径，不接受调用方传入的外部文件路径。 */
function checklistPath(cwd, taskId) {
  if (typeof cwd !== "string" || !cwd.trim()) {
    throw new CheckListError("cwd 必须是非空字符串。");
  }
  if (!isTaskId(taskId)) {
    throw new CheckListError("task_id 必须是小写标准 UUID。");
  }
  return path.join(resourceManager.workspaceRuntimeDirectory(cwd), "tasks", taskId, "checklist.json");
}

/** 判断任务 ID 是否为小写标准 UUID。 */
function isTaskId(value) {
  return typeof value === "string" && TASK_ID_PATTERN.test(value);
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

/** 校验已存在清单的数据格式和任务身份，损坏内容不得被覆盖。 */
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
    || value.workspace_key !== resourceManager.workspaceKey(cwd)
    || value.session_key !== resourceManager.sessionKey(sessionId)
    || value.task_id !== taskId
    || !Number.isInteger(value.revision)
    || value.revision < 0
  ) {
    throw new CheckListError("任务清单身份或 revision 校验失败。");
  }
  if (typeof value.created_at !== "string" || typeof value.updated_at !== "string") {
    throw new CheckListError("任务清单时间字段无效。");
  }
  const items = validateItems(value.items);
  return { ...value, items };
}

/** 创建任务清单初始数据，保持统一版本、身份和时间字段。 */
function emptyChecklist(cwd, sessionId, taskId) {
  const now = resourceManager.utcNow();
  return {
    schema_version: 1,
    workspace_key: resourceManager.workspaceKey(cwd),
    session_key: resourceManager.sessionKey(sessionId),
    task_id: taskId,
    revision: 0,
    created_at: now,
    updated_at: now,
    items: [],
  };
}

/** 在任务清单文件锁内读取已有清单或懒初始化空清单。 */
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
      revision: current.revision + 1,
      updated_at: resourceManager.utcNow(),
      items: requestedItems,
    };
    resourceManager.writeJson(filePath, next);
    return next;
  });
}

/** 在上下文锁内校验当前 session、workspace 和 task，防止并发任务越界。 */
function withTrustedContext(argumentsObject, callback) {
  const cwd = argumentsObject.cwd;
  const sessionId = argumentsObject.session_id;
  if (typeof cwd !== "string" || !cwd.trim()) {
    throw new CheckListError("cwd 必须是非空字符串。");
  }
  if (typeof sessionId !== "string" || !sessionId.trim()) {
    throw new CheckListError("session_id 必须是非空字符串。");
  }
  const explicitTaskId = argumentsObject.task_id;
  if (explicitTaskId !== undefined && !isTaskId(explicitTaskId)) {
    throw new CheckListError("task_id 必须是小写标准 UUID。");
  }
  const contextFile = resourceManager.contextPath(cwd);
  return resourceManager.withFileLock(resourceManager.lockPathFor(contextFile), () => {
    const context = resourceManager.activeContext(cwd, sessionId);
    if (!context || typeof context !== "object") {
      throw new CheckListError("当前 session 没有可信活动任务上下文。");
    }
    const derivedSessionKey = resourceManager.sessionKey(sessionId);
    const derivedWorkspaceKey = resourceManager.workspaceKey(cwd);
    if (context.session_key !== derivedSessionKey || context.workspace_key !== derivedWorkspaceKey) {
      throw new CheckListError("活动任务上下文的 session 或 workspace 校验失败。");
    }
    const currentTaskId = context.task_id;
    if (!isTaskId(currentTaskId)) {
      throw new CheckListError("活动任务上下文的 task_id 无效。");
    }
    if (explicitTaskId !== undefined && explicitTaskId !== currentTaskId) {
      throw new CheckListError("task_id 与当前活动任务不匹配。");
    }
    const filePath = checklistPath(cwd, currentTaskId);
    const result = callback(cwd, sessionId, currentTaskId, filePath);
    return { ...result, path: filePath };
  });
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
  return withTrustedContext(argumentsObject, (cwd, sessionId, taskId, filePath) => {
    const checklist = operation === "read"
      ? readChecklist(filePath, cwd, sessionId, taskId)
      : writeChecklist(
        filePath,
        cwd,
        sessionId,
        taskId,
        argumentsObject.items,
        argumentsObject.expected_revision,
      );
    return { ...checklist };
  });
}

module.exports = {
  TOOL_NAME,
  TOOL_SCHEMA,
  executeTool,
  checklistPath,
  CheckListError,
};
