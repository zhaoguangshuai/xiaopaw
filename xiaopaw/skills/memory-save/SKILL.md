---
name: memory-save
description: >
  Use this skill to persist important information from the conversation to
  workspace files so it survives across sessions.

  Activate proactively (without waiting for user to say "remember this") when:
  - User expresses a preference or habit ("I prefer...", "always...", "don't...")
  - User corrects Agent behavior and states how it should work instead
  - A key fact emerges that matters for future sessions (project milestone,
    decision made, important date, contact info)
  - User approves an approach ("let's do it this way going forward")

  Do NOT activate for: one-time tasks, Agent's own reasoning, info already in user.md.
allowed-tools:
  - Read
  - Write
---

# memory-save：持久化对话记忆到 workspace 文件

## 执行预算（CRITICAL）

**本 Skill 只允许执行：1 次 Read + 1 次 Write，然后立即返回。**

禁止：
- Read-back 验证（写完不再读取）
- 多次 str_replace 循环
- execute_bash 脚本（用整体写文件代替）
- 任何超出 1 Read + 1 Write 的操作

## 失败处理（CRITICAL — 严禁绕道）

**目标路径只允许是：`/workspace/{soul,user,agent}.md` 或 `/workspace/memory_<name>.md`。
不准把内容写到任何其他路径上。**

如果 Write 返回失败（任何错误，例如 `Permission denied` / `No such file or directory` / `Read-only file system`）：

- **禁止**改换路径重试（不许写到 `sessions/.../outputs/`、`/tmp/`、副本文件等任何替代位置）
- **禁止**用 `execute_bash` 走 `cp` / `sudo` / `chmod` 等命令绕过权限
- **禁止**返回 `errcode: 0` 任何"成功"消息

正确做法：直接停下，返回失败 JSON，让上层 Agent 知道记忆没保存：

```json
{"errcode": 1003, "message": "memory-save 失败：{原始错误信息}", "files": []}
```

**为什么严格**：曾出现过这样的静默 bug —— Skill 写 `/workspace/user.md` 收到 `Permission denied`，
LLM "创意"地 cp 出去再写到 sub-dir，最后返回成功；但 Bootstrap 只读 `/workspace/user.md`，
跨 session 召回失效。表面一切正常，内部完全坏掉。**宁可显式失败，不可静默绕道。**

## 写入目标

根据内容类型选择一个目标文件：

| target | 文件 | 存什么 |
|--------|------|--------|
| soul | /workspace/soul.md | XiaoPaw 名字、人设（只改已有字段）|
| user | /workspace/user.md | 用户偏好、习惯、个人信息 |
| agent | /workspace/agent.md | SOP 更新、checklist 勾选 |
| topic | /workspace/memory_\<name\>.md | 某主题的详细内容（name 用英文小写下划线）|

## 执行步骤

### 步骤一：Read（仅此一次）

读取目标文件，同时在内存中完成以下判断：
- 内容是否已存在？→ 已存在直接返回成功，不执行步骤二
- 需要替换哪一行 / 追加什么内容？→ 在内存中生成完整的新文件内容

### 步骤二：Write（仅此一次）

将步骤一生成的**完整新文件内容**整体写入目标文件，然后**立即返回**。

**target = soul**：把文件中的 XiaoPaw 名字替换为用户指定名，其余内容不动

**target = user**：追加或更新对应字段，不新增重复字段

**target = agent**：  
- checklist 勾选：将对应行的 `[ ]` 改为 `[x]`  
- 整节删除（如移除 SOP）：用 Python 字符串操作在内存中删除该节，整体写入  
- 写完立即结束，不验证

**target = topic**：直接写入 `/workspace/memory_<name>.md`（新建或覆盖），不更新 memory.md

## 返回格式

成功：
```json
{"errcode": 0, "message": "成功更新 {目标文件}", "files": ["/workspace/{目标文件}"]}
```

放弃（已存在 / 无需写入）：
```json
{"errcode": 0, "message": "无需写入：{原因}", "files": []}
```
