# Task Anchor

Task Anchor 是一个 Codex 插件，用于在上下文压缩后重新注入同一份最初任务指令。

> [!IMPORTANT]
> **安装并启用插件，不代表 Hook 已启用。** 安装后必须进入 Codex 的 Hook 设置，把 Task Anchor 的 Hook 设为**允许**并完成**信任**。未完成这一步时，Codex 会跳过插件 Hook：最初任务指令不会保存，压缩后也不会注入，插件核心功能等于没有安装。

## 环境要求

- Codex 桌面版与 Codex CLI 0.144.1。
- 命令行可以执行 codex。
- 命令行可以执行 python。

## 从源码安装

克隆仓库并进入仓库根目录：

    git clone <仓库地址>
    cd task-anchor

注册本仓库提供的 Marketplace：

    codex plugin marketplace add .

然后：

1. 重启 Codex 桌面版。
2. 打开 Plugins。
3. 选择 Task Anchor Local Marketplace。
4. 安装并启用 Task Anchor。
5. 打开 Codex 设置中的 Hooks 页面；CLI 也可以输入 `/hooks`。
6. 找到 Task Anchor 提供的 Hook，将其设为**允许**，并完成**信任**。
7. 确认 Task Anchor Hook 不再显示“待审查”“未信任”或“已禁用”。
8. 新建一个 Codex 任务进行测试。

只有同时满足“插件已启用”和“Hook 已允许且已信任”，安装才算完成。

如果曾在 Hook 获得信任前调用 `$task-anchor`，那次调用不会自动补存。完成信任后，必须重新显式调用一次 `$task-anchor` 并附上要锚定的任务指令。

Codex CLI 0.144.1 没有 codex plugin install 或 codex plugin add 子命令。CLI 负责注册 Marketplace，插件安装动作在桌面版 Plugins 界面完成。

## 不克隆直接注册 Git 仓库

仓库发布到 GitHub 后，也可以直接执行：

    codex plugin marketplace add owner/repository

需要固定分支或标签时：

    codex plugin marketplace add owner/repository --ref main

## 从 ZIP 安装

ZIP 不是 Codex 的专用安装包。先完整解压，再进入包含 .agents 目录的仓库根目录执行：

    codex plugin marketplace add .

之后仍然在桌面版 Plugins 界面安装。

## 更新

先更新源码：

    git pull

Codex CLI 0.144.1 的 `marketplace upgrade` 不支持本地 Marketplace。请在桌面版 Plugins 中卸载 Task Anchor，再从 Task Anchor Local Marketplace 重新安装并启用。

随后重新允许并信任 Task Anchor Hook，重启 Codex 桌面版，并在新任务中测试。

Hook 文件发生变化后，Codex 会按新 Hook 哈希重新要求审查；重新信任之前，更新后的 Hook 会被跳过。

## Hook 审计日志

插件在 `PLUGIN_DATA/audit/events.jsonl` 中记录最小运行证据。日志只包含：Hook 事件、UTC 时间、会话哈希、执行状态、指令字节数和 SHA-256，不保存任务指令正文，也不记录普通用户消息。

压缩恢复成功时，同一次压缩应依次出现：

    compact_received
    anchor_loaded
    restore_emitted

如果没有 `compact_received`，说明 Codex 没有执行该恢复 Hook；如果有前两项但没有 `restore_emitted`，说明恢复处理没有完成。

## 使用

开始长任务时显式调用：

    $task-anchor <你的任务指令>

插件只在第一次显式调用时保存最初任务指令。同一个 Codex 任务内再次调用不会覆盖原文。

调用前必须确认 Task Anchor Hook 已允许且已信任；否则只会加载 Skill，不会保存任务锚点。

## 仓库结构

    .agents/plugins/marketplace.json
    plugins/task-anchor/.codex-plugin/plugin.json
    plugins/task-anchor/hooks/hooks.json
    plugins/task-anchor/scripts/hook_entry.py
    plugins/task-anchor/skills/task-anchor/SKILL.md

插件不包含 MCP、小模型、自定义 TOLIST、PostToolUse 或 PreCompact。
