# Modelflare GPT-6 Sol 验证记录（2026-09-29）

测试环境：GitHub Actions 临时 Runner，`provider=modelflare`、`model=gpt-6-sol`，通过仓库 Secret `MODELFLARE_API_KEYS` 调用 Chat Completions。测试文件为公开的 [Attention Is All You Need PDF](https://arxiv.org/pdf/1706.03762)。每页一张 PNG、一页一请求，160 DPI，不做额外审校；使用内置转录提示词。每页最多请求一次，测试任务不自动重试。单把 Key，本地节流设置为 60 RPM、100 RPD。

| 任务 | 转换页数 | 并发 | 转换批次耗时 | 吞吐 | 单请求 p50 | 429 / 503 | 结果 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| [单页](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36567677347) | 1 | 1 | 108.58 秒 | 0.55 页/分钟 | 108.58 秒 | 0 / 0 | 1/1 成功 |
| [4 页](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36568054730) | 4 | 4 | 102.25 秒 | 2.35 页/分钟 | 89.92 秒 | 0 / 0 | 4/4 成功 |
| [8 页](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36568446175) | 8 | 8 | 142.47 秒 | 3.37 页/分钟 | 113.30 秒 | 0 / 0 | 8/8 成功 |

批次耗时由 `manifest.json` 中最后完成的页面耗时计算，从页面任务启动算起；不含 GitHub Runner 启动、依赖安装和 Artifact 上传。三次完整 Actions 作业分别约 2 分 7 秒、2 分 1 秒、2 分 40 秒。8 并发时 8 次请求耗时之和为 909.08 秒，批次耗时为 142.47 秒，约 6.38 个请求在时间上有效重叠。单请求 p50 从 4 并发的 89.92 秒升至 8 并发的 113.30 秒，说明提高并发增加了吞吐，也可能延长单页等待时间。

这三次运行证明该 Secret 可调用 `gpt-6-sol`，该模型能接收 PDF 页面图像并返回 Markdown。所有页面均生成了 Markdown，未观察到限流或容量错误。样本较小，不能据此推断 16 或 50 并发的上限，也不能把这次转录当作正式准确率评测。建议先以 **4–8 并发**使用，并根据真实文档的成功率、延迟和费用调整。测试中一页正常请求耗时 108.58 秒，已接近旧的 120 秒超时；因此 Modelflare 请求超时已单独提高到 300 秒，Gemini 保持 120 秒。
