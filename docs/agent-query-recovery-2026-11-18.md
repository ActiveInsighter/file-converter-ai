# Agent 查询故障与接口核对

## 对应故障

执行器基线 `eaf3aa7f5d57c3b7d6ac505d9de7d94d612e5af2`。

- [7238 秒运行](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/44132115361)：
  原 interaction 已产生 119 步，状态 GET 第一次返回 `400 invalid_request / Precondition check failed.` 后，
  执行器直接抛错，尝试取消上游也失败，随后保存本地失败。该 HTTP 错误不能证明上游执行失败。
- [2852 秒运行](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/44132190346)：
  上游返回 `completed`，60 步只有 thought、工具调用和结果，没有 `model_output`。
  累计 172170 Token，最终正文为空。与查询故障分开处理；不虚构最终答案或重发收费任务。

## 接口核对

对照 [Google API 参考](https://aistudio.google.com/api-reference/interactions-api/get-interaction-by-id)
及 [官方 Python SDK](https://github.com/googleapis/python-genai/blob/main/google/genai/_gaos/interactions.py)：

- 创建：`POST https://generativelanguage.googleapis.com/v1beta/interactions`，后台执行使用 `background=true`、`store=true`。
- 状态：`GET /v1beta/interactions/{id}`；`api_version` 对应路径中的 `v1beta`，不是遗漏的额外请求头。
- 事件：同一 GET 加 `stream=true`，有游标时加 `last_event_id`，使用 `Accept: text/event-stream`。
  普通状态 GET 不携带事件游标，显式使用 `Accept: application/json`。
- 取消：`POST /v1beta/interactions/{id}/cancel`。API 参考和当前 SDK 使用 `/cancel`；部分旧指南曾出现 `:cancel`，不据此改回旧路径。
- REST 正文读取 `steps[type=model_output].content[type=text].text`；同时兼容 SDK 的 `output_text` 和旧 `outputs`。
  空 `output_text` 不再掩盖非空的 REST 正文。

现有路径与认证方式一致，没有证据把本次 GET 400 归因于地址写错。未对线上收费任务执行新的 POST 或取消。

## 修复行为

状态 GET、事件流和 PB checkpoint 查询发生 HTTP 拒绝、网络异常或无效 JSON 时，
保留原执行身份和已有内容，按 2、4、8、16、30 秒退避持续重试，不再因次数耗尽结束任务。
状态响应身份不匹配时也不能拿其他 interaction 的终态结束当前任务。
PB checkpoint 使用同一 executionId 和状态重复写入，读取不到控制状态时不猜测取消意图。

只有明确用户取消才主动请求上游取消；本地观察协程退出不触发远端取消。
创建请求被拒绝、创建结果无法确认和上游已确认终态仍保留各自原有处理，不自动提交第二个 interaction。
已确认 `completed` 但没有正文的情况仍如实显示无最终回复。

Remote 保留查询失败前的消息与运行状态，继续轮询。补齐消息详情读取故障的连接提示。
浏览器故障注入检查状态、详情和过程查询失败时，不发送完成、失败、取消或重新提交请求，
保留工具消息及停止按钮，连接恢复后继续原会话。

## 验证

- Python 完整离线回归，涵盖超过原重试次数的连续 HTTP/传输失败、无效响应、身份不符、显式取消和本地观察器停止。
- Remote `npm run preflight`；浏览器覆盖进度、分页、断线自动恢复、桌面和手机尺寸。
- 不改变已经运行的旧 Runner，也不把旧失败记录改成成功；运行中的进程需要加载新代码才具有新行为。
