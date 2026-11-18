# PocketBase 数据库迁移说明（快速模型标题生成、运行总结与关键时间节点）

本文件记录本次新增功能所需的 PocketBase 表字段与 `pb_hooks` 放行片段，方便你在服务端手动迁移修改。

> 对应的可复用 PocketBase 迁移脚本已保存在 [`migrations/pb_migrations_agent_fast_model_summary.js`](../migrations/pb_migrations_agent_fast_model_summary.js)。

---

## 一、`aw_gemini_jobs` 新增字段

| 字段名 | PocketBase 类型 | 选项 | 说明 |
| :--- | :--- | :--- | :--- |
| `titleGenerated` | `bool` | 默认 `false` | 标记该会话是否已由快速模型（默认 `gemini-3.5-flash-lite`）生成过标题；为 `true` 或为续聊时不再重复生成 |

---

## 二、`aw_gemini_messages` 新增字段

| 字段名 | PocketBase 类型 | 建议大小 | 说明 |
| :--- | :--- | :--- | :--- |
| `summary` | `json` | `maxSize: 131072` (128 KiB) | 快速模型定期总结的运行进展：`{"updatedAt","stepCount","model","status","overview","sentSummary","actions":[],"completed":[],"issues":[]}` |
| `timeline` | `json` | `maxSize: 131072` (128 KiB) | 关键时间节点数据：`{"startedAt","submittedAt","submittedOffsetMs","firstReplyAt","firstReplyOffsetMs","finishedAt","totalDurationMs","steps":[{"index","key","type","label","receivedAt","offsetMs","deltaMs"}]}` |

---

## 三、`pb_hooks` 持久化参考

1. **`POST /api/anyworkflow/gemini/jobs/:jobId/messages/:messageId/progress`**（流式进度写入 `aw_gemini_messages`）：
   ```js
   if (body.summary && typeof body.summary === "object") {
     message.set("summary", body.summary);
   }
   if (body.timeline && typeof body.timeline === "object") {
     message.set("timeline", body.timeline);
   }
   ```

2. **`POST /api/anyworkflow/gemini/jobs/:jobId/messages/:messageId/complete`**（完成态持久化写入 `aw_gemini_messages`）：
   ```js
   if (body.summary && typeof body.summary === "object") {
     message.set("summary", body.summary);
   }
   if (body.timeline && typeof body.timeline === "object") {
     message.set("timeline", body.timeline);
   }
   ```

3. **`PATCH /api/anyworkflow/gemini/jobs/:jobId/metadata`**（允许写入 `titleGenerated`）：
   ```js
   if (typeof body.titleGenerated === "boolean") {
     job.set("titleGenerated", body.titleGenerated);
   }
   ```
