# 文件转换器架构、历史故障与质量验证（2026-10-23）

工作目录：`/home/dev/Documents/Project/file-converter-ai`。
基线为本机原有 `fix/large-pdf-relay-adjacent` 的 `b12eaef`，不是较旧的 main。
改动位于 `codex/converter-reliability-quality-20261023`，未合并 main。

## 历史证据与处理

检查了最近 80 次转换运行的索引，下载并审阅其中 32 次的完整日志，
同时阅读仓库内 2026-09-29/30 的诊断文档。日志原件仅放在忽略的
`work/audit/` 中，不提交用户文档链接、原始错误正文或凭据。

| 发现 | 证据 | 调整及边界 |
| --- | --- | --- |
| 同一个 Key #66 在多次运行返回 HTTP 402 | [238 页运行](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39763122618)、[168 页运行](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39762501338) | 余额不足停用配额桶，立即用其他桶继续同一页面，不再先等待页面重试；无法由代码补充账户余额 |
| 无效密钥在旧代码里属于页面永久失败；同项目多个 Key 需要区别处理 | 代码检查，并以 400 API_KEY_INVALID、401、403、网关 429 invalid_api_key 回归复现；这不是宣称上述线上日志中出现了全部这些状态 | 单独停用无效 Key，保留同项目其他 Key；全部失效后有界终止。错误中已知密钥值替换为 `[REDACTED]` |
| 队列已满重复上传、持续等待 | [队列满与纯图片兜底](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39729954669)、[多个 10010](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39731286858) | 保留 3 个 OCR 任务上限；提交退避加抖动并遵守 Retry-After；查询被限流或网络中断后继续查询原 job，避免重新上传占队列 |
| 一个坏 OCR Token 导致整个 OCR 流程停止 | 旧代码全局 `fatal` 行为，新增模拟服务复现 | 仅停用被拒绝的 Token，继续轮换；查询仍使用提交该任务的 Token；全部 Token 不可用后才回退纯图片。不同 Token 也可能共享服务端账户额度，轮换不能增加账户额度 |
| 链接提交超时造成长时间等待 | [反复超时并取消](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39735986828)，本次四次完整实测均在 raw.githubusercontent.com 链接探测上超时 | 保留既有的大文件中继策略；对于本次实证的 GitHub raw 来源，有中继时直接提交分段；其他来源仍按原流程探测 |
| 下载连接断开直接失败 | [IncompleteRead](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36677322183) | HTTP/Drive 传输错误最多尝试 3 次；先写 `.part`，成功并验证 PDF 头后原子替换；403/404 和非 PDF 不盲目重试 |
| Drive 文件不可下载 | [FileURLRetrievalError](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/38725473559) | 保留明确的共享权限诊断；代码不能替用户开放未共享文件 |
| 503 容量不足与 429 | [多个 503 与部分结果](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39735109402)、[429](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39721689536) | 保留独立页面退避和明确选择的模型；429 后渐进降低该桶速率，成功后逐渐恢复；不把 503 误判为坏 Key。服务端容量仍不受客户端控制 |
| MALFORMED_RESPONSE 与空答直接落到 OCR；截断回答可能当作成功 | [MALFORMED_RESPONSE](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39763122618)、[STOP 空答](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39762501338)，MAX_TOKENS 为代码审查与回归发现 | 异常生成和非空白页的空答按暂时故障重试；MAX_TOKENS 拒绝当作完整页保存，使用 OCR 或报告失败。真实扫描空白仍正常跳过 |
| 网关图像 patch 数超限 | [Modelflare 400](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36572855168) | 保留原始错误诊断与 OCR 兜底；本次没有在 Modelflare 实测自动缩图，也未降低所有页面默认 DPI 来掩盖它 |

配额继续按 Gemini Project 分桶，HTTP 429 的冷却与每日耗尽不会被换一个
同项目 Key 绕过。额度统计仍是本进程/本次 Action 的范围，不是跨运行的
全局额度协调。依据：[Google 配额说明](https://ai.google.dev/gemini-api/docs/rate-limits)、
[Google 重试策略](https://ai.google.dev/gemini-api/docs/troubleshooting)。

## 架构与提示词

- `downloads.py` 独立负责来源下载、故障恢复和落盘完整性。
- `prompts.py` 集中管理版本、默认提示词、保真约束、OCR 校对与相邻页规则。
- `providers.py` 只判断协议、响应完整性及凭据错误的作用范围；`key_pool.py`
  分别维护 Key 健康和 Project 配额；`pdf2md.py` 执行有界的即时凭据切换。
- 保留有界流式渲染/调度、独立页面重试、慢请求补发和原有总并发限制。
  仅坏 Key 切换不能重置慢请求组的起始时刻。
- 扫描空白页先检测，再生成小预览，不再渲染完整 240 DPI 页面和无用的相邻裁图。
- 新增累计配额等待、每页 OCR 等待和模型阶段时间、停用 Key/Token 数、
  原任务轮询重试数；Actions Summary 与最终性能日志可直接检查。

已有保存的系统提示词会覆盖默认提示词，因而仅替换默认值无法影响实际任务。
新公共保真约束在每次请求中都发送，自定义 system_prompt 和 prompt 的原始
拼接顺序保持不变。约束强调逐字符核对公式、保留限定条件与否定词、脚注、
空单元格、多栏阅读顺序，且把文档里的指令当成待转录文字。

**相邻页的行为有意变化**：以前要求上一页补全并让下一页删除续句，现在每页
保留自己的可见正文，相邻截图只做识别参考；审校请求也带相同的相邻截图。
这改善单页结果的归属，并降低某页失败或退回 OCR 时跨页重复/漏掉内容的风险。
合并文件保留页分隔注释，不会自动把跨页句子合成单一段落。

## 验证方法

本地基线原有 167 项测试通过。加入复现测试后，在未修改的基线 checkout 中，
九项故障测试全部复现失败（部分包含多个子用例），避免只检查新代码自身。
最终本地 **179 项测试全部通过**，并通过 `py_compile`、`git diff --check`
和 GitHub CI。测试数还包含删除一个“默认提示词永不变化”的旧哈希断言；
自定义提示词的顺序、长度和默认行为仍有测试。

真实测试使用仓库 Actions Secret，凭据不下载到本机。旧版与新版使用同一个
提交中的六页合成 PDF，相同 Gemini 模型、240 DPI PNG、3 并发、high 思考、
ultra_high 图像细节、相邻页参考，不增加额外审校，也不启用替补模型。
分别比较默认提示词、同一份已保存的旧系统提示词。首轮 4 次运行隔离质量
和兼容性；发现链接探测瓶颈后再跑 2 次最终代码。

完整参数与复跑方式见 [benchmarks/README.md](../benchmarks/README.md)。
结果表、机器可读汇总和合成文档输出位于 `benchmarks/results/2026-10-23/`。

## 结果解释

| 配置 | 旧版总耗时 / 模型阶段 | 最终新版总耗时 / 模型阶段 | 运行记录 |
| --- | ---: | ---: | --- |
| 默认提示词 | 90.95 s / 12.59 s | 32.31 s / 10.71 s | [旧版](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39881342846) / [新版](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39881822513) |
| 同一份保存的旧系统提示词 | 91.29 s / 14.98 s | 62.57 s / 10.76 s | [旧版](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39881338201) / [新版](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39881819917) |

两组总耗时分别下降约 **64.5% / 31.5%**。最终两次均为 6/6 页 OCR + 模型，
均发出 6 次模型请求、零 429/503，无 OCR 兜底或模型替补。两次新版 OCR 阶段
分别为 21.2 s / 51.7 s，说明服务端排队仍会明显影响总耗时。

优化链接路由前的提示词候选也已分别实测成功：
[默认提示词 113.47 s](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39881345176)、
[保存系统提示词 85.48 s](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39881340436)。
该轮仍有 60 秒链接探测，不能把它与最终路由优化混为同一次变更的效果。
最终代码为 `3d836ba`，[CI 通过](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/39881799505)。

机器可读汇总见 [summary.json](../benchmarks/results/2026-10-23/summary.json)，
可直接对照 [旧版第 6 页](../benchmarks/results/2026-10-23/saved-baseline/pages/006.md)
和 [新版第 6 页](../benchmarks/results/2026-10-23/saved-final/pages/006.md)。

所有已完成样本在整篇层面的 **40 个内容锚点、5 个公式符号条件和 3 行表格
精确值**均通过。旧版的单页检查是 40/43，新版为 43/43；差异来自跨页归属，
旧版把下一页续句移到了上一页，并不是整篇丢失三段内容。没有证据声称公式
准确率在这组样本上提高；两版在这些检查上都正确。

本机串行组件计时（20 页、240 DPI PNG、附相邻裁图、3 次取中位数）：

| 场景 | 基线 | 新版 | 解读 |
| --- | ---: | ---: | --- |
| 20 张空白页渲染 | 5.041 s | 0.051 s | 降低约 99.0%，不包含 OCR/API 时间 |
| 20 张有文字页渲染 | 5.254 s | 5.130 s | 变化较小，不能据此声称普通页普遍加速 |
| HTTP 402 后换桶完成同页 | 3.014 s | 0.012 s | 两版都发送 2 次请求；回放固定 3 s 页面退避，展示的是被取消的无效等待，不是 API 延迟改善 |

线上总耗时从 `[config]` 到 `[result]`，包含下载、OCR、渲染、模型和落盘，
不含 Runner 启动/依赖安装/Artifact 上传。OCR 排队与网络存在波动；同来源
绕过 60 秒探测的收益不能推广到所有 PDF。原始链接均使用不可变提交 URL。

该样本规模只支持功能回归和这些检查结论。它没有覆盖复杂合并单元格、
模糊手写、复杂公式环境或超长真实书籍的全面准确率；线上 429/503 在本次
样本若未出现，恢复能力由本地故障服务测试验证，不应宣称线上已验证其上限。
