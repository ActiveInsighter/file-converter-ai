# PocketBase 数据库迁移说明 (Agent 快速模型 · 总结与时间节点)

请在 PocketBase 管理后台 (`_/` -> Collections) 按以下说明添加字段，无需修改现有字段或删除数据：

---

## 1. `aw_gemini_jobs` 集合

新增字段：

| 字段名 (Name) | 类型 (Type) | 选项 / 说明 |
|---|---|---|
| `titleSource` | `Text` (`text`) | 可选（Nonempty = false），默认空。用于标记标题是否已由 AI 生成（值为 `"ai"`），避免连续对话或已有 AI 标题时重复生成覆盖。 |

> 权限说明：确保 `aw_gemini_services`（或 Action 使用的 `POCKETBASE_SERVER_TOKEN`）对 `aw_gemini_jobs` 具有 `View` 和 `Update` 权限（若使用 `POCKETBASE_SERVER_TOKEN` 超级管理员令牌则自动拥有全部权限）。

---

## 2. `aw_gemini_messages` 集合

新增以下 2 个 `JSON` 字段：

| 字段名 (Name) | 类型 (Type) | 选项 / 说明 |
|---|---|---|
| `agentSummary` | `JSON` (`json`) | 可选，Max size 建议 `131072` (128 KiB)。保存快速模型（`gemini-3.5-flash-lite`）定期总结及最终总结的结构化 JSON（包含 `sentWhat`, `actionsDone`, `completed`, `issues`, `summary`, `updatedAt`, `stepCount`, `model`）。 |
| `agentTimings` | `JSON` (`json`) | 可选，Max size 建议 `131072` (128 KiB)。保存执行关键时间节点与每个步骤的到达耗时（包含 `submittedAt`, `dispatchedAt`, `dispatchSeconds`, `firstReplyAt`, `firstReplySeconds`, `completedAt`, `totalSeconds`, `status`, `steps`）。 |

> 客户端说明：RikkaHub Android 客户端已内置兼容处理——即使尚未迁移上述字段，也不会影响正常对话与构建；迁移完成后，Action 会自动写入并实时同步到客户端弹窗中。

---

## 3. RikkaHub 本地数据库 (Room)

RikkaHub Android 客户端的 `AgentDatabase`（本地草稿 `agent_draft` 表）已在 v4 完成建表，本次在 `UIMessage` 中新增了可选默认值为 `null` 的 `agentSummaryJson` 和 `agentTimingsJson` 字段（随 `agent_message_node.messages` JSON 序列化），**无需额外手动迁移**。
