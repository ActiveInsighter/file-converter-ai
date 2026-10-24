# AnyWorkflow File Converter

PDF → Markdown 转换执行器。百度 PaddleOCR-VL 直接从原始链接下载 PDF 并识别每一页，GitHub Actions 同时在本地渲染页面图片；某页的 OCR 结果出来后，把 OCR 草稿和页面图片一起发给视觉模型（默认 Gemini 3.5 Flash-Lite）校对、整理成最终 Markdown。

## 处理流程

1. **下载**：支持直接 HTTP(S) PDF 地址和 Google Drive 公开分享链接。源站声明 `Accept-Ranges: bytes` 且文件不小于 32 MB 时，改用 8 条连接并行下载 16 MB 分块（本机限速源站实测单连接 8.2 秒 → 2.1 秒）；源站忽略 Range、分块反复失败或内容在下载中途变化时，自动退回单连接整体下载。
2. **OCR（百度直接拉取）**：按 25 页切段，每段以 `fileUrl`（原始链接）+ `pageRanges`（如 `26-50`）提交到 PaddleOCR-VL-1.6 异步接口，由百度自己下载 PDF，Runner 不上传。最多 5 段同时进行（服务端每个用户只接受少量排队任务，超出返回 10010 并自动退避重试；两次 400 页实测均未出现 10010）。每段完成后立即放出各页的 Markdown 草稿和图表裁图。
   - 前三段依次只有 5、10、15 页，之后才是 25 页：第一批草稿几十秒内就绪，模型阶段不必等满一个 25 页任务；三个任务的完成时间也因此错开。
   - 模型阶段按“草稿先就绪的页先处理”调度，不再按页码排队：某一段 OCR 较慢时，后面已完成的段不会被它占满的工作槽挡住。
   - 第一次提交兼作探测。百度会直接拒绝部分链接，例如 `https://arxiv.org/pdf/1706.03762` 返回 10004“格式不支持”（加 `#.pdf` 后缀也一样，已实测）。
   - 百度在每次提交请求里同步下载整个文件（实测从 Cloudflare 约 2 MB/s：28 MB 需 13–30 秒，47 MB 需 23 秒）。因此提交等待时间随文件大小增加：`30 秒 + 文件大小 ÷ 1 MB/s`，最少 60 秒、最多 300 秒（120 MB 约 150 秒）。第一次提交成功后，其余分段同时提交。
   - 大文件（超过 40 MB）经中继：每段单独裁出并放到中继，百度从中继拉取。裁剪、重编码和上传发生在任务槽之外，同时最多预备 2 段，任务槽一空出就能直接提交链接。重型扫描页（平均每页超过 256 KiB）先重编码为 150 DPI JPEG 页面（实测 25 页 82 MB → 12.7 MB，体积约缩小 6 倍），重编码在工作进程中进行（PyMuPDF 持有 GIL，线程无法并行）。需要保留原始页面图像时设 `CONVERTER_OCR_RELAY_DPI=0`。
   - 中继链接提交返回 HTTP 408/504 或无响应时，在同一条链接上重试（不重复上传）；同一段连续两次失败才只把这一段改为上传。中继任务 200 秒没出结果或失败时，同样在原链接上重新提交，连续三次才把这一段改为上传；中继对其他段始终继续使用。
   - 实测：原图分段（约 43 MB）被百度反复回 408，OCR 692 秒；150 DPI 分段（5–7 MB）OCR 303 秒、零次 408。
   - 以下情况改为旧方式：链接被拒绝；提交在上述时间内没有响应（源站对百度很慢时会这样，如 gnu.org）；链接任务失败或 5 分钟未完成；文件超过百度链接上限 200 MB；来源是 Google Drive 分享页。只有“队列已满/限流”会继续用链接重试。旧方式是：从本地 PDF 切段上传，单段超过 1 MiB 时继续对半拆分（Runner 到百度上传约 20–25 KB/s）。平均每页超过 256 KiB 的分段（扫描件）先转成 150 DPI JPEG 页面再上传，体积通常缩小 4–8 倍。
3. **渲染**：同时在独立线程中逐页渲染图片（默认 240 DPI PNG）。空白扫描页直接跳过，不发请求。
4. **校对**：某页的图片和 OCR 草稿都就绪后，一起发送给模型。模型以图片为准、以草稿为底稿：保留正确文字，修正错字、公式与上下标，补全遗漏，删除页眉页脚，整理标题、段落、表格结构，保留草稿中的图片引用。
   - 开启 `adjacent_pages` 后，每页请求还附带上一页底部和下一页顶部（各占页高 40%）的截图。相邻截图用于辨认边缘字符与阅读顺序，每页只转写本页可见内容，保留本页开头的续句、续公式和续表行，避免依赖另一页是否成功而删除内容。不会自动跨页拼接正文。相邻页为空白或不在转换范围内时不附带。每个请求多两张截图，耗时和 Token 略增。
5. **兜底**：
   - 模型拒答（RECITATION/SAFETY 等）、回答被输出长度截断，或重试轮次用尽时，该页直接使用 OCR 草稿。空回答及 MALFORMED_RESPONSE 按暂时故障重试；非空白扫描页不会因为模型空答而被标成空白。
   - 某段 OCR 失败（每段最多尝试 6 次）或未配置 OCR 时，该页退回为仅凭图片转写。
   - 两者都没有结果的页面列为未完成。

每页每轮最多立即尝试 2 次；仍失败的页面独立退避后进入下一轮，最多 5 轮，期间不会阻塞其他页面。慢请求在 `max(60 秒, 2 × 近期 P95)` 后最多补发两个副本，先返回有效结果的胜出。

## 输出结构

```text
output/
├── <output_name>.md     合并文件：文档信息 + 全部页面（按页序）
├── pages/
│   ├── 001.md           每页一个文件，只含该页正文
│   └── ...
└── images/              OCR 裁出的图表，仅包含 Markdown 实际引用的图片
    └── p003-1.jpg
```

合并文件开头为“文档信息”：PDF 来源链接、转换时间（UTC）、原始页数、转换范围、完成情况、识别流程（如 `PaddleOCR-VL-1.6 → gemini / gemini-3.5-flash-lite`），以及仅用 OCR 结果、仅凭图片转写、未完成和空白的页码。正文中每页以 `<!-- 第 N 页 -->` 注释分隔，渲染时不可见。

文件名不随完成情况变化：部分完成时合并文件同名，文档信息标为“部分完成”，进程退出码为 2。运行期间每 5 秒刷新输出；取消或出错时保留已完成页面。

中间数据都在 `work/` 下（源 PDF、页面图片、`conversion/` 中的单页结果、错误、`progress.json`、`results.json` 和 `quota-usage.json`），不进入下载包。

## 密钥轮询

全部在进程内完成，不依赖外部服务：

- 每个 Key 归入一个配额桶。Gemini 免费额度按 Google Cloud Project 计算 RPM/RPD，配置 `GEMINI_KEY_GROUPS` 后同一 Project 的多个 Key 共用一个桶；未配置时每个 Key 一个桶。
- 桶内两次请求至少间隔 `60 / rpm × 1.1` 秒（默认 15 RPM，即 4.4 秒），并带少量抖动，不会突发。
- 下一个请求交给“已就绪且已用次数最少”的桶，次数相同时选空闲最久的，因此负载均匀分布到所有 Key。
- 任意两个请求之间还有一个全局最小间隔（`桶间隔 / 桶数`，最多 0.25 秒），避免任务开始时几十个请求同时打出。
- 429：该桶冷却 20 秒，连续 429 时依次翻倍（最多 300 秒）；服务端给出更长的 `retryDelay` 时以服务端为准。成功一次后清零。触发限流还会逐步放慢该桶的请求间隔（最多原间隔的 8 倍），成功后逐步恢复。
- 无效 Key（401/403、明确的 API_KEY_INVALID 等）只停用该 Key，同一 Project 中的其他 Key 保持可用；立即换 Key，不等待页面退避。
- 每日额度耗尽（RPD）或预付费额度用完（HTTP 402）：该桶在本次任务中停用，页面立即换其他桶；网关用 429 返回余额耗尽时同样处理。`rpd_per_key` 是单次任务内每个桶的请求上限。
- 近期 429/503 占比超过 20% 时不再补发副本。

PaddleOCR 支持多个 Token（每行一个），按任务轮流使用；同一任务始终用提交它的 Token 查询。限流（12002）、队列已满（10010）和服务端错误会指数退避重试（每段最多 6 次，间隔最长 60 秒）；Token 无效或每日页数用尽（12001）时仅停用该 Token，其他 Token 继续工作；全部 Token 失效后剩余页面转为仅图片转写。轮询遇到 429/5xx 或连接中断时继续查询原任务，遵守 Retry-After，不重复提交。

`quota-usage.json` 和 Actions Summary 记录每个桶的请求数分布、429/503 次数、各页来源（OCR+模型 / 仅模型 / 仅 OCR）、OCR 耗时，以及按链接提交与上传的 OCR 任务数。

## 配置

| 名称 | 类型 | 用途 |
| --- | --- | --- |
| `GEMINI_API_KEYS` | Secret | Gemini Key，每行一个 |
| `GEMINI_KEY_GROUPS` | Variable，可选 | 每个 Key 所属的 Project，与 Key 一一对应；可用 `deploy/print-key-groups.py` 生成 |
| `BAIDU_AI_STUDIO` | Secret | AI Studio 访问令牌，可多行；工作流以 `PADDLEOCR_TOKENS` 传给转换器 |
| `CONVERTER_OCR` | Variable，可选 | `paddle`（默认）或 `none` |
| `CONVERTER_OCR_JOBS` | Variable，可选 | 同时进行的 OCR 任务数，默认 `5`；调大更容易收到 10010。若仓库里已把它设为 `3`，需要手动改 |
| `CONVERTER_OCR_SOURCE` | Variable，可选 | `link`（默认，百度从原始链接下载）或 `upload`（始终从 Runner 分段上传） |
| `CONVERTER_OCR_RELAY_DPI` | Variable，可选 | 中继分段中重型扫描页的 JPEG 重编码分辨率，默认 `150`；`0` 保留原始页面图像 |
| `MODELFLARE_API_KEYS` | Secret，可选 | 使用 Modelflare 时需要 |
| `CONVERTER_HEDGE_AFTER` / `CONVERTER_HEDGE_BUDGET` | Variable，可选 | 补发阈值与预算，默认 `60` / `-1`，填 `0` 关闭 |

## 运行

在 **Actions → AnyWorkflow File Converter · PDF to Markdown → Run workflow** 填写 `source_url`。

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `provider` | `gemini` | `gemini` 或 `modelflare` |
| `model` | `gemini-3.5-flash-lite` | Modelflare 必须指定视觉模型 ID |
| `output_name` | `merged` | 合并 Markdown 的文件名，不含扩展名 |
| `concurrency` | `50` | 同时进行的页面请求数，含补发 |
| `system_prompt` / `prompt` | 空 | 转录要求，按顺序拼接，各限 12000 字符；都为空时使用内置要求 |
| `dpi` / `image_format` | `240` / `png` | 发给模型的页面图片质量 |
| `thinking_level` / `media_resolution` | `high` / `ultra_high` | Gemini 思考与图像细节预算 |
| `verification_passes` | `0` | 额外对照图片审校次数 |
| `rpm_per_key` / `rpd_per_key` | `15` / `500` | 每个配额桶的 RPM 与单次任务请求上限 |
| `start_page` / `end_page` | 整份 PDF | 1-based 页面范围 |
| `retry_rounds` / `attempts_per_page` | `5` / `2` | 每页最大轮数和轮内即时尝试数 |
| `adjacent_pages` | `false` | 附带相邻页边缘作为识别参考，每页保留自己的续接内容 |

外部调用统一使用 `workflow_dispatch`：

```bash
gh workflow run pdf-to-md.yml --repo ActiveInsighter/file-converter-ai --ref main \
  -f conversion_type=pdf_to_md \
  -f request_id=your-task-id \
  -f source_url='https://example.com/document.pdf' \
  -f output_name='文档笔记'
```

运行标题为 `file-converter-pdf-to-md-<request_id>`，Artifact 名为 `file-converter-pdf-to-md-<run_id>`，内容即上面的 `output/` 目录。

本地运行：

```bash
pip install -r requirements.txt
export GEMINI_API_KEYS='你的密钥'
export PADDLEOCR_TOKENS='AI Studio 令牌'
python pdf2md.py --source-url 'https://example.com/document.pdf' --output-name '文档笔记'
```

CLI 另有 `--ocr none`、`--ocr-source`、`--ocr-model`、`--ocr-segment-pages`、`--ocr-url`、`--hedge-after`、`--hedge-budget`、`--adjacent-pages` 等参数，见 `python pdf2md.py --help`。

## 耗时

实测（GitHub Runner，Gemini 3.5 Flash-Lite，66 个 Key）：75 页论文约 7 分钟完成，全部页面为“OCR + 模型校对”，零 429/503。瓶颈在 OCR：Runner 到百度的上传约 20–25 KB/s，单个 OCR 任务在服务端排队 30–150 秒。模型请求本身 P50 约 13 秒。需要更快时可调大 `CONVERTER_OCR_JOBS`，或设 `CONVERTER_OCR=none` 回到纯图片转写。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `pdf2md.py` | 命令行入口、请求构造、单页流程与兜底 |
| `prompts.py` | 与前端配置一致的默认提示词，以及系统/任务提示词拼接 |
| `downloads.py` | 原子下载、下载中断恢复和 PDF 格式检查 |
| `paddle_ocr.py` | PaddleOCR 异步任务：按链接或上传提交分段、轮询、解析、下载图表 |
| `key_pool.py` | 本地密钥轮询与 429 处理 |
| `providers.py` | Gemini / OpenAI 兼容网关的协议适配 |
| `pipeline.py` | 有界流式队列、独立重试、进度文件 |
| `request_policy.py` | 慢请求补发 |
| `render_stream.py` | 单线程逐页渲染 |
| `artifacts.py` | 输出目录 |

## 测试

```bash
pip install -r requirements.txt
python -m unittest discover -s tests
```

本地单元和端到端测试不访问外网：OCR 接口、模型网关和 PDF 下载都由本地假服务模拟。

2026-10-23 的历史日志诊断、真实 API 对照、可重跑的性能评测及结果见 [架构与质量验证记录](docs/reliability-quality-2026-10-23.md)。提示词现统一由 Remote「转换配置 → 系统提示词」管理，OCR 校对、保真、相邻页和额外审校规则都放在这段可编辑文本中。转换器只按顺序发送已保存的 `system_prompt` 和本次 `prompt`，再附加页码、`<ocr_draft>`、`<verification_draft>` 与图片等输入，不额外注入隐藏规则；未传两段提示词的直接 CLI/Actions 调用才使用相同内容的内置默认值。额外审校沿用同一份已保存提示词。后端升级迁移仅更新精确匹配旧默认词的配置并递增版本，不修改自定义词或已有任务快照。

普通 HTTP 下载与 Google Drive 的连接中断最多尝试 3 次，完整下载并通过 PDF 头检查后才替换目标文件。未共享、404 和非 PDF 等不可恢复错误不会盲目重试。配置了中继时，已实测百度拉取超时的 raw.githubusercontent.com 来源直接通过中继提交，省去重复的 60 秒链接探测。
