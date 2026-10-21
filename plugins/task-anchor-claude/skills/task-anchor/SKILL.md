---
name: task-anchor
description: "开始需要跨上下文压缩持续推进的独立任务。"
disable-model-invocation: true
---

# Task Anchor

此技能只能由用户显式调用：`/task-anchor:task-anchor <任务指令>`。

`UserPromptExpansion` Hook 会在技能展开前将命令参数保存为新的独立任务，并将当前会话、当前项目的只读门控切换为可写。若 Hook 报告失败，以 Hook 提示为准，不要声称任务已经建立。

## 推进规则

1. 宿主原生计划工具可用时，优先使用并维护原生计划；否则调用 `get_last_task` 读取当前任务、原始指令和清单。结果为 `task: null` 时不要自行创建任务。
2. `get_last_task` 查询创建时间最新的任务，`list_tasks` 按创建时间倒序列出当前会话的任务摘要，`get_task` 使用当前会话内明确的 `task_id` 读取历史任务。前三个工具都不需要模型提供会话 ID；只有 `get_task` 需要任务 UUID。
3. 没有原生计划能力时，使用 `task_checklist` 维护备用进度：通过 `operation: "read"` 读取，或用 `operation: "write"` 提交完整 `items` 和当前 `expected_revision`。写入后保留返回的真实 `task_id`、`revision` 和 `items`，后续更新沿用 revision 并保留已完成条目。
4. 用户明确改变目标、约束或方向时，以较新的明确指令为准。
5. 任务按 `created_at` 判断最新项；已结束任务只允许读取。普通交付、工具调用完成、计划清空、最终回复、`Stop` 或 `SessionEnd` 都不结束任务；只有显式调用 `/task-anchor:task-anchor-end`（`-end`）结束当前任务，或显式创建下一个锚定任务时，旧任务才结束。

## 受管命令执行

- 查询命令使用宿主原生工具。查询/只读命令（明确包括 `pwd`、`ls`、`git status`、`rg` 和读取文件）使用 Codex/Claude Code 宿主原生工具，不调用 `mcp__plugin_task-anchor_task-anchor__managed_exec`；Windows 下不要把 Unix 命令名作为 managed_exec 的 program，除非明确使用宿主支持的 shell 命令。\n- node/bun/npm/python/java 等进程命令使用 managed_exec。只有会启动本地进程、启动开发服务、测试服务或持续运行程序的命令（包括 Codex 现有关键词范围）才调用 `mcp__plugin_task-anchor_task-anchor__managed_exec`。\n- Node managed_exec 负责启动、登记、超时和停止本地进程；普通资源到达 `timeout_ms` 会结束整个进程树。
- 默认 `stop_policy` 为 `cleanup`，Stop/SessionEnd 会清理普通资源；`keep` 资源不受 timeout_ms 和 Stop/SessionEnd 影响，必须设置 `name`，并在不需要时显式调用 `operation: "stop"`。
- 优先使用 `program` 与 `args`；只有确实需要管道、重定向或复合命令时才使用 `shell: true` 与 `command`。

## 压缩后继续

收到 Task Anchor 注入的状态提醒时，先按原生计划工具是否可用选择原生计划或调用 `get_last_task` 恢复进度，再读取提醒中的原始任务指令并继续未完成工作。`get_last_task` 为空时不要自行创建任务；该提醒不要求回复、不会建立新任务，也不会改变当前状态。


# SKILL作者 
>嘉兴云祥软件 https://www.jxfjkf.cn 中国专业企业级应用开发，专注ERP,CRM,OA,MES,SRM,财务等领域的系统研发。服务长三角、珠三角的各类企业
## 作者其他作品
>vertool https://www.vertool.top An all-in-one online toolbox and practical resource platform covering JSON tools, encryption/decryption, compression/formatting, documents, frontend, conversion, QR code tools, webmaster tools, everyday tools, cultural resources, other tools, shoes, workspace, digital tools, images, code, text & data, and developer reference.
