# 无游标 Agent 流恢复及逐步写入

## 线上证据

任务 `5f0b31a900ee485` 的消息 `qgpnwas4klzuug4` 长时间只有 7 个内容分段，
尽管执行器心跳持续更新。10:15 UTC 只读事件流已重放出 19 步，证明存在过程同步丢失。

[重连诊断 44063828401](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/44063828401)
确认该响应没有事件游标（`cursor_chars=0`）。主动断开只读观察后重新 GET，返回
`HTTP 400 / invalid_request / Precondition check failed.`。原代码遇到无游标断流直接退出，
并将该 400 一律视为永久失败；主线程继续轮询及发心跳，造成“执行器在线但没有新步骤”。
这些证据不能证明每次长间隔都是同步问题，也不能证明 Google 内部排队或推理的具体耗时。

## 修复与验证

- 有游标时按游标续读；无游标时清理接收缓冲，从头按步骤编号重建。
  重放过程中保留已完成步骤，避免正文重复追加和状态退回 running。
- 仅对上述精确错误重试 400；401/403 等明确错误不无限重试。
- 每个 `step.stop` 立即将当前批次和恢复文件落盘，等待 PB ACK；失败使用原批次重试，
  不在旧数据尚未确认时继续消费后续完成步骤。任务关闭时保留未确认批次。
- 修复取消路径为官方 `/interactions/{id}/cancel`，检查 HTTP 结果，拒绝把 404 当作取消成功。
- `sync-` 修复观察器仅 GET 原 Google interaction；写入后回读确认每个字段。
  处理旧心跳和修复观察器的批次竞争，不把其他写入者的重复序号回复当作内容已保存。
  不重启原 Agent，不改写原 Runner 的累计用量，不完成或取消原任务。

353 项 Python 回归通过，包括无游标重放、短暂 precondition 拒绝、步骤立即 ACK、
数据库故障、丢失 ACK、身份变化、批次竞争、恢复文件和取消接口校验。

[在线补同步 44065983241](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/44065983241)
于 10:34 UTC 将数据库从 7 个内容分段补到 40 个，每个批次经过 API 回读核对。
SQLite `mode=ro`、`query_only=on` 的独立查询确认 `step:0`–`step:39` 已保存。
原 interaction 与原 Runner 未被重跑或取消；补同步继续跟随当前执行。
