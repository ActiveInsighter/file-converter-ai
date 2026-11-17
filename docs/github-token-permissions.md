# Agent GitHub 凭据发布与验证

Antigravity 读取仓库 Actions Secret `ANTIGRAVITY_GITHUB_TOKEN`，与开发机的
`gh` 登录凭据独立保存。开发机重新授权后，需要将已验证的凭据更新到该 Secret，
后续新启动的 `gemini-text.yml` 才会取得新凭据；不需要重新部署 Remote 或重新提交已有任务。

2026-11-17 本次发布使用 `ActiveInsighter` 账号的 OAuth 凭据，已由 GitHub API 确认
具有 `repo`、`workflow`、`read:org`、`gist` scopes，并具有本仓库的推送和管理员权限。
`workflow` 允许更新工作流文件；仓库写权限及分支保护仍由 GitHub 决定。
凭据值只通过标准输入写入 Secret，不进入仓库或日志。

从 Actions 手动运行 **Agent GitHub token permissions**，即可在 Runner 中检查实际部署的
Secret：读取账号、仓库权限、工作流列表和 OAuth scopes，不修改仓库、不触发 AI 调用。
OAuth/classic Token 缺少 `repo` 或 `workflow` 时检查失败；fine-grained Token 不返回这些
scope headers，因此检查会明确说明不能据此验证其工作流文件写权限。

Secret 更新供新启动的任务使用；已经创建的远端 Agent 环境不会由本次验证自动刷新凭据。
