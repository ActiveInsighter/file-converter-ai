# Agent 执行详情与定时任务

后端迁移、发布顺序与回退步骤集中在 AnyWorkflow-workers 的 [Agent 工作流生产部署](https://github.com/ActiveInsighter/AnyWorkflow-workers/blob/main/deploy/agent-workflows.md)。

GitHub Actions 通过已领取的任务和分段写入标题、总结及时间节点。普通 Gemini 使用 `generateContent`，Antigravity 使用 Interactions，推理程度由 Antigravity 管理。
