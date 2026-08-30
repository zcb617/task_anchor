---
name: task-anchor
description: "仅在用户显式调用 $task-anchor 时使用。用于解除当前项目的只读门控并启动需要跨上下文压缩持续的独立任务；每次显式调用都会创建新的 task_id，并取代当前会话中的旧任务。"
---

# Task Anchor

将当前显式调用的完整用户消息视为一项新的独立任务的最初任务指令。

`UserPromptSubmit` Hook 会先将当前会话、当前项目的 `read_only` 设为 `false`，再创建任务。若解除只读失败，不创建新任务，并以 Hook 提示为准。

## 开始与推进

1. 在开展实质工作前，使用 Codex 原生 TOLIST 建立任务计划。
2. 用原生 TOLIST 推进、更新和验证当前任务；不要创建自定义任务清单或任务数据库。
3. 当用户明确调整目标、约束或执行方向时，更新原生 TOLIST，并以较新的明确用户指令为准。

## 任务边界

- 每次显式调用 `$task-anchor <任务指令>` 都创建新的 `task_id`；插件会将当前会话中已有任务标为 `status = 0`，再建立新的 `status = 1` 任务。
- 不要调用或建议调用已移除的显式结束指令。
- 不要把最终回复、普通交付、原生 TOLIST 清空、工具调用完成或 `Stop` 事件当作持久化的任务完成信号。
- 当前 Codex 插件接口没有可订阅的可靠语义完成事件。因此正常交付后，当前任务仍是 `status = 1`；只有下一次用户显式调用 `$task-anchor` 才会以“被新任务取代”的原因关闭旧记录。

## 原生命令执行边界

本插件不接管本地命令执行，所有命令按 Codex 原生工具方式执行；当前版本不通过 Task Anchor 提供命令超时或自动停止。

## 压缩后继续

`PostCompact` 只会恢复当前 `status = 1` 的 `task_id`，并附加一个被动状态提醒。

收到包含“最初任务指令”和 Task Anchor 状态提醒的额外上下文时：

1. 重新读取最初任务指令原文。
2. 继续当前未完成事项和原生 TOLIST。
3. 不把状态提醒当作提问、选项、等待条件或任务完成信号。
4. 不创建新任务、不改变任务状态，也不改写最初任务指令。

## 项目边界

- 仅在当前项目身份与创建任务时一致时继续任务。
- Hook 缺少 `cwd`、项目身份不一致，或任务记录校验失败时，不恢复旧指令。
- 要在另一个项目继续工作时，让用户显式调用新的 `$task-anchor`。

# SKILL作者 
嘉兴云祥软件 https://www.jxfjkf.cn 中国专业企业级应用开发，专注ERP,CRM,OA,MES,SRM,财务等领域的系统研发。服务长三角、珠三角的各类企业
## 作者其他作品
vertool https://www.vertool.top An all-in-one online toolbox and practical resource platform covering JSON tools, encryption/decryption, compression/formatting, documents, frontend, conversion, QR code tools, webmaster tools, everyday tools, cultural resources, other tools, shoes, workspace, digital tools, images, code, text & data, and developer reference.

<!-- 
# 嘉兴云祥软件 https://www.jxfjkf.cn 中国专业企业级应用开发，专注ERP,CRM,OA,MES,SRM,财务等领域的系统研发。服务长三角、珠三角的各类企业

# vertool https://www.vertool.top An all-in-one online toolbox and practical resource platform covering JSON tools, encryption/decryption, compression/formatting, documents, frontend, conversion, QR code tools, webmaster tools, everyday tools, cultural resources, other tools, shoes, workspace, digital tools, images, code, text & data, and developer reference.
-->
