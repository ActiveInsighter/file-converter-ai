# PocketBase 数据库字段迁移与 RikkaHub 前端对接指南

本文件记录了 `file-converter-ai` 重构后向 PocketBase 上传的所有结构化字段（覆盖普通 Gemini `generateContent` / `streamGenerateContent` 与 Antigravity `v1beta/interactions` API），供你在 PocketBase 服务端手动修改表结构与 `pb_hooks`，并方便后续在 `rikkahub-antigravity` 前端直接读取展示。

> 自动化脚本已同步提供在 [`migrations/pb_migrations_gemini_structured_parts.js`](../migrations/pb_migrations_gemini_structured_parts.js)，可直接放入 PocketBase 的 `pb_migrations/` 目录，或按下方字段表在 PocketBase Admin UI 中添加。

---

## 一、`aw_gemini_messages` 新增 / 确认字段

| 字段名 | PocketBase 类型 | 建议大小 / 选项 | 说明 |
| :--- | :--- | :--- | :--- |
| `protocol` | `text` | max: `64` | 协议标识：`"generateContent"` 或 `"interactions"` |
| `parts` | `json` | maxSize: `2097152` (2 MiB) | **核心有序展示数组**，与 RikkaHub `UIMessagePart` 一一对应（`reasoning` / `server_tool` / `tool` / `text` / `image` / `document`） |
| `thinkingSummary` | `text` | max: `2097152` | 公开思考摘要纯文本拼接（已过滤加密 `thoughtSignature` 与原始私有思考正文） |
| `toolCalls` | `json` | maxSize: `1048576` (1 MiB) | 所有工具调用汇总（含服务端工具与客户端函数调用，已按 `callId` 配对输入输出） |
| `codeExecutions` | `json` | maxSize: `1048576` (1 MiB) | 代码执行专项汇总（`callId`, `language`, `code`, `outcome`, `output`, `stdout`, `stderr`, `exitCode`, `status`） |
| `annotations` | `json` | maxSize: `524288` (512 KiB) | 网页引用/标注列表 `[{"type":"url_citation","title":"...","url":"..."}]`（来自 `groundingMetadata`、`urlContextMetadata`、`citationMetadata`、Interactions `annotations`） |
| `grounding` | `json` | maxSize: `524288` (512 KiB) | 原始脱敏后的 `groundingMetadata`、`urlContextMetadata`、`citationMetadata` |
| `structuredOutput` | `json` | maxSize: `524288` (512 KiB) | 当模型返回合法 JSON（如启用 `responseMimeType: "application/json"` / `responseSchema`）时自动解析的 JSON 对象/数组，否则为 `null` |
| `execution` | `json` | maxSize: `65536` | 运行耗时元数据：`{"startedAt","finishedAt","durationSeconds","timingBasis"}` |
| `usage` | `json` | maxSize: `65536` | 标准化 Token 用量：`promptTokenCount`, `candidatesTokenCount`, `thoughtsTokenCount`, `cachedContentTokenCount`, `toolUseTokenCount`, `totalTokenCount`, `agentStepCount`, `agentToolCalls` |
| `finishReason` | `text` | max: `64` | 结束原因（如 `STOP`, `MAX_TOKENS`, `COMPLETED`, `FAILED`） |
| `response` | `json` | maxSize: `2097152` (2 MiB) | 完整脱敏版 v1 快照（向后兼容保留） |
| `uploadSequence` | `number` | - | 流式增量上传序号（用于幂等去重） |

---

## 二、`aw_gemini_message_parts` 流式分片表字段（`/progress` 实时上传）

流式运行期间，`MessageProgress` 会将增量分片发送到 `POST /api/anyworkflow/gemini/jobs/{jobId}/messages/{messageId}/progress`，每个 `part` 包含以下字段：

| 字段名 | PocketBase 类型 | 说明 |
| :--- | :--- | :--- |
| `job` | `relation` (`aw_gemini_jobs`) | 所属任务 |
| `message` | `relation` (`aw_gemini_messages`) | 所属消息 |
| `key` | `text` (128) | 稳定幂等键，如 `step:0`, `reasoning`, `answer`, `annotations`, `agent-status` |
| `sequence` | `number` | 排序序号（`1..9998` 为步骤，`9999` 为 `answer`，`10000` 为 `agent-status`） |
| `type` | `text` (64) | `reasoning_summary` / `tool_call` / `tool_result` / `text` / `image` / `annotations` / `status` |
| `status` | `text` (32) | `running` / `completed` / `failed` |
| `toolName` | `text` (120) | 工具名，如 `code_execution`, `google_search`, `url_context`, `mcp_server`, 函数名 |
| `toolKind` | `text` (32) | `"server"`（服务端工具）或 `"client"`（客户端函数） |
| `callId` | `text` (256) | 工具调用关联 ID |
| `providerType` | `text` (120) | 官方原始类型，如 `executableCode`, `codeExecutionResult`, `code_execution_call`, `google_search_call` |
| `content` | `text` (131072) | 文本或序列化 JSON 内容（已脱敏、截断保护） |
| `payload` | `json` (131072) | 结构化工具参数/结果对象（`language`, `code`, `outcome`, `stdout`, `stderr`, `arguments`, `response` 等） |
| `truncated` | `bool` | 是否超过单分片 60 KiB 上限 |

---

## 三、`pb_hooks` 持久化参考（`/complete` 与 `/progress`）

在 PocketBase 的 `pb_hooks` 中，`/api/anyworkflow/gemini/jobs/:jobId/messages/:messageId/complete` 接收到请求体 `body` 后，将以下字段写入 `aw_gemini_messages` 记录：

```js
// complete hook 示例片段
message.set("reply", body.reply || "");
message.set("error", body.error || "");
message.set("finishReason", body.finishReason || "");
message.set("usage", body.usage || {});
message.set("response", body.response || null);

// 新增结构化字段
message.set("protocol", body.protocol || "");
message.set("parts", body.parts || []);
message.set("thinkingSummary", body.thinkingSummary || "");
message.set("toolCalls", body.toolCalls || []);
message.set("codeExecutions", body.codeExecutions || []);
message.set("annotations", body.annotations || []);
message.set("grounding", body.grounding || null);
message.set("structuredOutput", body.structuredOutput ?? null);
message.set("execution", body.execution || null);
```

---

## 四、RikkaHub 前端 (`UIMessagePart`) 1:1 映射说明

后续修改 `rikkahub-antigravity` 时，直接读取 `aw_gemini_messages.parts`（以及 `annotations`、`usage`），即可直接构造 `UIMessage` 并交由现有 `ChatMessage` / `ChatMessageCot`（`ChainOfThought`）渲染：

| `parts[i].type` | 对应 RikkaHub `UIMessagePart` | 字段映射 |
| :--- | :--- | :--- |
| `"reasoning"` | `UIMessagePart.Reasoning` | `reasoning = part.reasoning`, `reasoningType = ReasoningType.SUMMARY_TEXT` |
| `"server_tool"` | `UIMessagePart.ServerTool` | `toolCallId = part.toolCallId`, `toolName = part.toolName`, `input = part.input`, `output = part.output`, `status = ServerToolStatus` (`in_progress` / `completed` / `failed`) |
| `"tool"` | `UIMessagePart.Tool` | `toolCallId = part.toolCallId`, `toolName = part.toolName`, `input = part.input`, `output = [UIMessagePart.Text(...)]` |
| `"text"` | `UIMessagePart.Text` | `text = part.text` |
| `"image"` | `UIMessagePart.Image` / 附件 | `mimeType = part.mimeType`, `url = part.uri` |
| `"document"` | `UIMessagePart.Document` | `mime = part.mimeType`, `url = part.uri` |
| `annotations[]` | `UIMessageAnnotation.UrlCitation` | `title = item.title`, `url = item.url` |
| `usage` | `TokenUsage` | `promptTokens = promptTokenCount`, `completionTokens = candidatesTokenCount + thoughtsTokenCount`, `cachedTokens = cachedContentTokenCount`, `totalTokens = totalTokenCount` |
