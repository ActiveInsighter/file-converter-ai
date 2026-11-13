# 每次整理页数：实现与 Gemini 4 Hydrogen 实测

2026-11-13。默认仍为 1 页；Remote「转换配置 → 吞吐与配额 → 每次整理页数」可填写 1–20 的整数。设置 5 时，连续 5 页的原尺寸完整图片和各自 OCR 草稿进入同一模型请求；末组不足 5 页也会发送。没有缩成一张长图。结果严格按 PDF 页码拆回单页文件，再按原顺序合并。

本样本中 **3 页较均衡**：密集公式 15 个抽检细节保留 14 个，模型请求 2 次；1 页与 5 页各保留 12 个。这是人工对照原图选择的局部保真检查，不能当作全文准确率或普遍排名。三档都有同一处负号遗漏，复杂扫描公式仍需要核对。

## 数据与固定条件

[香港科技大学 Kin Y. Li 的 Math 4023 公开复分析扫描讲义](https://www.math.hkust.edu.hk/~makyli/4023/2017-2018Sp/Math4023Transparenices_20180515.pdf)，55 个 PDF 页面，每页通常是两页手写笔记。采用原始模糊扫描，没有人为模糊或锐化。原文件 SHA256：

`808cd09bb3994a6b494ad238f06b5b5d59b47cd2132b24fa6dc2a03398ec0748`

先测第 1–5 页，再用公式更密集的第 31–35 页做重点比较：Laurent 级数、奇点分类、留数定理、高阶 Cauchy 积分公式、辐角原理、Rouché 定理及 Blaschke 乘积。注意页码指 PDF 页码，非扫描件内部的手写页码。

三个批大小都使用 `provider=gemini`、`model=gemini-4-hydrogen`、PaddleOCR-VL-1.6 草稿、PNG / 240 DPI、`thinking_level=high`、`media_resolution=ultra_high`、并发 5、每 Key RPM 5 / 任务上限 20、额外审校 0、相邻页关闭、相同内置提示词。无模型回退；基准限制为一轮一次尝试，保留生产默认的慢请求补发。日志确认六个任务的所有页面均为 `ocr+model`，没有用纯 OCR 冒充模型识别。

各档的 OCR 草稿分别由真实服务生成，没有冻结草稿或模型随机性；每段每档运行一次。下面比较的是现有完整流程的这次输出，不能隔离全部随机因素。任务同时运行，OCR 排队时长明显不同，因此总耗时不可当作批大小的纯性能结论。

## 第 31–35 页结果

| 每次整理页数 | 完成页数 | 15 个抽检细节保留数 | 逻辑请求组 | 实际发送请求（含补发） | 成功模型请求 P50 / P95 |
| --- | --- | --- | --- | --- | --- |
| 1 | 5/5 | 12/15 | 5 | 5 | 32.81 / 52.63 秒 |
| 3 | 5/5 | 14/15 | 2 | 2 | 42.66 / 44.68 秒 |
| 5 | 5/5 | 12/15 | 1 | 3 | 85.21 / 85.21 秒 |

5 页组在主请求开始 60 秒后触发两个慢请求副本，主请求约 85 秒完成后副本被取消。`performance.requests=1` 只记录完成请求，Key 配额计数 `requests=3` 才是实际已发送次数；不能据完成请求数宣称只消耗 1 次。三档都没有 429/503、漏页、重页、模型回退或纯 OCR 回退。

完整转换观察耗时约 150、149、307 秒，其中 OCR 约 95、100、218 秒。5 页组的总耗时既包含更长模型回答，也包含更久的 OCR 排队。

明确的差异：

- PDF 第 31 页内圈核函数展开原图为 `-\sum_{k=-\infty}^{-1}`，三档都漏了求和前的负号。原图已放大复核，不能把模型输出正常结束当成公式正确。
- 同页 `A_3` 的定义应含 `0 < |z-1| < 1`。1 页和 5 页在定义行漏掉 `< 1`；后面的推导仍写出该条件。3 页完整保留定义行。
- 第 34 页 Remark 重述辐角原理时应为 `f'(z)/f(z)`，1 页和 5 页写成 `f(z)/f(z)`；3 页保留导数撇号。三档在本页其他位置都有正确版本，因此评分只检查该处，避免“全文搜索找到一次正确公式”掩盖重复位置的错误。

其余 12 个抽检点（三档均保留）：余割展开的 `7z^3/360`、`sin(1/z)` 的三阶和五阶阶乘、极点判据 `k+1` 阶极限、`g(z)=(z-z0)^k h(z)` 及非零条件、留数系数 `a_{-1}`、留数定理绕数权重、主部的负下标求和范围、Cauchy 高阶公式阶乘和绕数分母、`f(iy)` 的符号与次数、Rouché 严格不等式、半径例题关键数值与结论、Blaschke 分母中的共轭。

没有逐字符标注全文、完整评价图形转录或对所有合法等价 LaTeX 做符号验证。标题粗体、换行和框线的不同不视为识别错误。15 点检查定义与每项结果保存在 [评分脚本](../benchmarks/score_page_batches.py) 和 [机器可读记录](../benchmarks/results/2026-11-13/page-batching-summary.json)。

## 第 1–5 页补充测试

三档均完成 5/5 页，实际请求分别 5、2、1 次；成功模型请求 P50 分别 32.31、37.00、52.15 秒，无补发、限流或回退。两段合计每档检查 10 页，共 30 页输出。此段用于验证手写双栏和基本公式的完整运行，没有计入上面的 15 个密集公式抽检点。

## 运行与复现

首轮任务：[1 页](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/43346939376)、[3 页](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/43347009450)、[5 页](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/43347038295)。

密集公式任务：[1 页](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/43347290024)、[3 页](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/43347293810)、[5 页](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/43347297140)。各任务的 Artifact 含合并 Markdown、逐页结果和图形文件，默认仅保留 3 天；本地已保留完整下载。

由于当前 GitHub OAuth token 缺少 `workflow` scope，完整功能分支的 workflow 文件无法推送。真实测试使用 `bench/page-batching-1`、`bench/page-batching-3`、`bench/page-batching-5` 三个分支，保留原 workflow；除 CLI 默认批大小分别为 1、3、5 外，请求构造与处理代码相同。正式分支默认始终为 1。后续仅修正了进度统计及结果索引，未改模型请求或转录提示词。

本地原始文件位于 `work/batch-benchmark/{intro,formulas}-{1,3,5}/`。重新评分：

```sh
python benchmarks/score_page_batches.py work/batch-benchmark > work/batch-benchmark/scores.json
```

真实运行可用 CLI `--images-per-request N`（别名 `--pages-per-request N`），固定上述来源与范围、配置和模型；需要已有环境中的 `GEMINI_API_KEYS`、`PADDLEOCR_TOKENS`。不要把密钥写进命令记录或提交 Git。

## 验证与交付

- 转换器：全部本地测试通过，覆盖默认单页、1/3/5 页请求数、尾组、空白页、OCR 混合失败、拒答逐页回退、缺页/重页/乱序/空答、整组额外审校、外侧相邻页、请求体大小限制、逐页进度和故障恢复索引。
- Remote：`npm run preflight` 通过；PDF 浏览器回归通过，检查 1–20 整数、非法值、保存与任务快照、详情展示；320/1440 的配置面板和四种页面宽度通过，明暗主题截图已保存。
- Workers：本地 341 项中 336 通过、5 个依赖外部测试程序的用例跳过；GitHub CI 成功，覆盖额外的真实 PocketBase 测试与构建。

已修改 Remote 展示、配置读写链路、配置快照与 Cloudflare / Node 共用的工作流参数白名单。沿用已存在的 `images_per_request` 数据字段，无需数据库迁移；旧任务快照不会被新配置覆盖。

完整转换器改动保存在 `/home/dev/Documents/Project/file-converter-ai/.worktrees/page-batching` 的 `feat/page-batching`。可推送的代码交付分支 `feat/page-batching-code` 附带 `deploy/page-batching-workflow.patch`，包含 workflow 与配套契约测试的补丁。授权 workflow 写入后在代码分支应用补丁，重新验证并发布转换器、Remote 与 converter coordinator。尚未合并主线或部署生产。

GitHub workflow 输入已有 25 个，新增批大小输入后，把仅供手动调用的 `model_fallbacks` 移到仓库变量 `CONVERTER_MODEL_FALLBACKS`；CLI `--model-fallbacks` 保留，显式模型仍默认不回退。这是 workflow 手动调用契约的唯一调整。

草稿 PR：[转换器 #34](https://github.com/ActiveInsighter/file-converter-ai/pull/34)、[Remote #47](https://github.com/ActiveInsighter/Anyworkflow-remote/pull/47)、[Workers #26](https://github.com/ActiveInsighter/AnyWorkflow-workers/pull/26)。三者均保留草稿状态，等待配套 workflow 变更后再合并发布。代码交付分支 CI 亦已通过。
