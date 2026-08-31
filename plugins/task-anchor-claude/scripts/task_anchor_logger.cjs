"use strict";

const fs = require("node:fs");
const path = require("node:path");

const FIXED_FIELDS = new Set(["timestamp", "level", "message"]);

/**
 * Task Anchor 统一诊断日志记录器，负责写入受管命令和 Hook 审计事件。
 */
class TaskAnchorLogger {
  /**
   * 创建指定 JSONL 文件的诊断日志记录器。
   * @param {string} logPath 日志文件绝对路径。
   */
  constructor(logPath) {
    this.logPath = typeof logPath === "string" && logPath.trim() ? path.resolve(logPath) : null;
  }

  /**
   * 记录需要长期保留的业务生命周期事件。
   * @param {string} message 业务事件名称。
   * @param {object} context 事件上下文扁平字段。
   */
  info(message, context = {}) {
    this.#write("info", message, context);
  }

  /**
   * 记录失败、超时或兼容性降级等警告事件。
   * @param {string} message 警告事件名称。
   * @param {object} context 警告上下文扁平字段。
   */
  warning(message, context = {}) {
    this.#write("warning", message, context);
  }

  /**
   * 记录用于定位执行细节的调试事件。
   * @param {string} message 调试事件名称。
   * @param {object} context 调试上下文扁平字段。
   */
  debug(message, context = {}) {
    this.#write("debug", message, context);
  }

  #write(level, message, context) {
    if (!this.logPath) {
      return;
    }
    try {
      const record = {
        ...flattenContext(context),
        timestamp: new Date().toISOString(),
        level,
        message: String(message),
      };
      fs.mkdirSync(path.dirname(this.logPath), { recursive: true });
      fs.appendFileSync(this.logPath, `${JSON.stringify(record)}\n`, { encoding: "utf8", mode: 0o600 });
    } catch (error) {
      try {
        process.stderr.write("Task Anchor logger write failed\n");
      } catch (ignored) {
        // 日志失败不能覆盖业务异常。
      }
    }
  }
}

/**
 * 将上下文对象转换为顶层字段，并阻止覆盖 Logger 固定字段。
 * @param {unknown} value 待扁平化的上下文值。
 * @param {string} prefix 当前字段路径前缀。
 * @param {object} output 扁平化输出对象。
 * @returns {object} 扁平化后的上下文字段。
 */
function flattenContext(value, prefix = "", output = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    if (prefix && !FIXED_FIELDS.has(prefix)) {
      output[prefix] = value;
    }
    return output;
  }
  for (const [key, child] of Object.entries(value)) {
    const fieldName = prefix ? `${prefix}.${key}` : key;
    if (FIXED_FIELDS.has(fieldName) || FIXED_FIELDS.has(key)) {
      continue;
    }
    if (child && typeof child === "object" && !Array.isArray(child)) {
      flattenContext(child, fieldName, output);
    } else {
      output[fieldName] = child;
    }
  }
  return output;
}

module.exports = { TaskAnchorLogger };
