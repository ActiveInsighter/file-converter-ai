# Agent 执行可靠性修复与验证

## 修复内容

1. 配置了 GitHub 密钥时，Git/REST 认证始终可用，不由提示词关键词决定。
   默认不注入完整 GitHub MCP 工具目录，减少无关工具初始化与多步上下文负担。
2. 创建 Google interaction 前解析目标仓库。优先明确的 owner/repository，再匹配
   Token 可访问的精简仓库清单；不以 `remote`、`exam` 搜索整个 GitHub。
3. 需要源码推送时，先用 GET receive-pack 握手检查实际 Git 写权限。拒绝时立即结束，
   不先执行昂贵的改代码、测试和截图。单纯重新部署不强求 Contents 写权限。
4. 单纯重新部署通过 workflow API 执行，不克隆仓库或本地构建。部署 helper 在 POST
   前保存日志，以 workflow/ref 为单位只提交一次；204、丢失响应、运行暂不可见等情况
   都通过查询核对，不盲目重发。绑定 run ID 后只等待指定运行，success 即结束。
5. 运行时指导要求搜索超时后切换到针对性的 rg、保留测试退出码、修复具体失败后再重跑。
   对常见直接 shell dispatch 提供 hook，指向记录了提交身份的 helper。

这些改变消除已确认的绕路、晚发现权限不足和重复部署。它们不能保证模型永不产生
代码错误，也不能消除 Google 托管服务的准备和推理延迟。

## 真实验证

- [原密钥检查 43734793650](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/43734793650)：
  解析到 `ActiveInsighter/Anyworkflow-remote`，约 0.94 秒确认 Git 写权限被拒绝；
  未创建 Google interaction、未修改 GitHub ref。运行返回失败是预期的权限检测结果。
- [原工作区恢复 43734795866](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/43734795866)：
  通过环境 files API 只读恢复约 2.79 MiB Git 数据。校验 SHA-256 后，仅导入 objects、refs、
  HEAD 等 Git 元数据到干净仓库，未执行或导入沙盒 config/hooks。
  恢复到 `570f8a8`，父提交为 Remote main 的 `1feeedc`。
- [真实运行时探针 43735689415](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/43735689415)：
  Google Agent 成功读取挂载的 helper 与 hook 配置，执行只读 identity，返回
  `AW_RUNTIME_OK` 和 `ActiveInsighter`。8 条步骤，总耗时约 119 秒，累计 16643 Token。
  没有仓库修改、推送或部署。这个小任务仍花约两分钟，说明上游基础延迟依然存在。

本地故障注入覆盖：权限拒绝时零 Google 请求、首次 204 成功后复用、响应丢失不重发、
暂不可见后恢复、多个候选停止而不猜测、布尔/字符串输入统一、仅等待准确 run ID。

## 补齐之前失败的 UI 交付

原任务 `aa443a88d77148f` 最终耗时约 82 分钟，保存回复时 status 为 succeeded，
但回复明确报告 Git push 和创建远端 ref 均为 403，实际没有预览交付。

恢复原提交后，保留重构内容，修正浏览器用例对已经移除的 Token 预算控件的旧断言。
构建与预检通过，Gemini 浏览器用例通过（含 Blob 中文下载名）。恢复分支已补推：

`feat/agent-gemini-recovered`，提交 `9e2968b`。

[预览部署 43735857658](https://github.com/ActiveInsighter/Anyworkflow-remote/actions/runs/43735857658)
已成功，版本 `b8013d90-b855-46c6-a173-b44526b2b461`。

预览：https://anyworkflow-remote-preview.2212148739lbw.workers.dev/gemini

补推使用本机已经登录且对该仓库有写权限的连接。Agent Actions Secret 的替换是另一项
配置动作；使用范围更广的本机凭据替换现有密钥，需要用户确认，不能将补推成功当作
原 Agent 密钥的权限已修复。
