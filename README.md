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
4. **校对**：默认每页的图片和 OCR 草稿都就绪后，一起发送给模型。`images_per_request` 可设为 1–20（默认 1）：连续 N 页的完整图片与带页码 OCR 草稿合并为一次请求，不缩小、拼成长图；末批不足 N 页照常发送。返回按页码标记严格拆分，仍逐页保存；漏页、重页、乱序或空页回答会重试，最终失败时逐页回退到各自 OCR 草稿。空白页占原始分组位置但不发送图片；相邻页参考仅附在整个分组外侧。每次额外审校同样发送整组页面。

   默认单页时，模型以图片为准、以草稿为底稿：保留正确文字，修正错字、公式与上下标，补全遗漏，删除页眉页脚，整理标题、段落、表格结构，保留草稿中的图片引用。
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
| `images_per_request` | `1` | 每次整理的连续 PDF 页数，1–20 |
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

GitHub workflow 的输入已达 25 个；模型回退链改由仓库变量 `CONVERTER_MODEL_FALLBACKS` 设置（不再接收手动 dispatch 的 `model_fallbacks` 输入）。指定自定义模型时仍默认不回退；CLI `--model-fallbacks` 保留。

CLI 支持 `--images-per-request N`（别名 `--pages-per-request N`）；另有 `--ocr none`、`--ocr-source`、`--ocr-model`、`--ocr-segment-pages`、`--ocr-url`、`--hedge-after`、`--hedge-budget`、`--adjacent-pages` 等参数，见 `python pdf2md.py --help`。

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

## Remote 持久资料库

Remote 的“资料库”保存可复用 PDF，“收藏和模板”是独立侧栏入口。
从资料库或转换页上传一次后，不同转换任务继续使用同一 `source_url`，每个任务仍有独立的
配置快照、页码范围、提示词、工作目录与输出。无需向执行器提供用户登录令牌或 R2 凭据。

持久文件链接形如 `https://<remote-host>/api/library/sources/<UUID>.pdf`，支持 Range/If-Range
并禁止缓存；上传者主动删除后返回 404/410。执行器在完整下载、分段并行下载及流式回退路径
均明确报告“资料库源文件已删除或不存在”，停止重试，避免把旧的本地文件当成新任务源文件。
现有公开 PDF/Google Drive 地址及临时 OCR 分段中转的行为保持兼容。

该链接属于持有即可访问的能力链接，应按原始文档的访问范围使用。原文件由 Remote 资料库管理，
执行器不会删除源文件；用户在 Remote 删除原文件后，既有下载产物仍保留。

## Gemini 独立文本消息

`gemini-text.yml` 和 `gemini_text.py` 从 PocketBase 领取文本任务，复用现有 Gemini KeyPool，
按任务并发数独立发送各段。请求只有系统提示词、用户提示词与该段文本，不经过 PDF/OCR 流程，
不上传 Gemini Files API；回复逐段写回 PB，截断回复也会保留并标明错误。

**普通 Gemini 聊天不启用 Google Search Grounding**：免费 API 项目无法使用该搜索工具，
因此 `gemini_text.py` 不再自动发送 `tools: [{"googleSearch": {}}]`，
不会因为搜索工具权限导致普通聊天 HTTP 400/403。
保留通用上游 HTTP 状态与错误类别日志，方便排查其他 API 问题。
PDF/OCR 转换流程不变；Antigravity 的独立 Interactions API 工具配置也不变。

不同 Gemini 对话使用各自的 GitHub Actions concurrency group（由 request_id 区分），
与 PDF 转换独立调度；Cloudflare Gemini 任务并发上限为 4。
每个 Actions Runner 的 KeyPool 仅管理当前任务，多个任务复用相同 API Key 时可能
更早触发 Google 的项目级 RPM/RPD 或其他服务限制，请合理设置任务数量。

仓库 Actions Secret `POCKETBASE_SERVER_TOKEN` 支持直接使用 PB 超级用户令牌，具有整个数据库的服务端读写权限。
填入后优先使用它；未填写时回退到 `GEMINI_PB_EMAIL`、`GEMINI_PB_PASSWORD`（PB 专用
`aw_gemini_services` 账号）。variable `GEMINI_PB_URL` 默认 `https://pb.any1.tech`。
令牌不进入前端、日志或 workflow inputs；调度器只传 `request_id`，内容保持在数据库。
需要先部署 AnyWorkflow-workers 的 Gemini migration/hooks，再发布本 workflow，最后发布 Remote。
完整操作和发布说明见 Remote 的 `docs/gemini-messages.md` 与后端 `deploy/gemini-messages.md`。

PB 写入失败的回复保留在三天有效的 `gemini-text-recovery-<runId>` artifact 中。
解压后设置专用 PB 凭据及 URL，运行 `python gemini_text.py --recover-dir ./recovery`，
只重试保存，不再次生成。传输超时、失去发送许可和新的 GitHub run attempt 不自动重复发送。

## Antigravity Agent 消息

`gemini_text.py` 对 `Antigravity`（大小写不敏感）和 `antigravity-*` 使用
`gemini_agent.py` 的官方 Interactions API：`POST /v1beta/interactions`。
普通模型仍沿用 generateContent。别名解析为 `antigravity-preview-09-2026`，
默认推理模型为 Gemini 4 Hydrogen，代码执行、Google 搜索和 URL Context 全部开启，
远程沙盒自动提供文件系统；省略网络 allowlist，保留默认不限域名的出站网络。
Antigravity 请求**不发送** `agent_config.max_total_tokens`，也不再设置本地默认 Token 预算。
历史任务中保存的预算字段会被忽略，Agent 仍受 Google API 的真实配额、服务端限制与执行时限约束。
不发送 Agent 不支持的 generationConfig、thinkingLevel 或 max_output_tokens。

Agent 后台执行需要 `background=true`、`store=true`。执行器在 PB 保存 interaction ID
和凭据的 SHA-256（不保存 API Key），之后只轮询同一 ID；中断的创建不盲目重发。
取消会调用远程 `/cancel`；读取失败重试 GET。不再设置应用层任务时限，旧任务中的
`timeoutMinutes` 同样忽略；workflow 不再自设 180 分钟超时。GitHub 托管 Runner 仍有
[每个 job 最长 6 小时的平台限制](https://docs.github.com/en/actions/reference/limits)，不能通过仓库配置取消。
PB 必须先部署 `1794740000_gemini_agent_state.js`
与 checkpoint 路由。使用现有 `GEMINI_API_KEYS`，无需把任何服务器凭据挂入沙盒。

### Antigravity GitHub 仓库集成（仅需 Actions Secret）

在本仓库的 `Settings → Secrets and variables → Actions → Secrets` 中添加
`ANTIGRAVITY_GITHUB_TOKEN`（GitHub Fine-grained PAT）。Token 根据实际需要授权目标仓库；若要让 Agent 访问你有权限的**所有仓库**，
在 GitHub Token 配置中选择对应范围（例如所有个人仓库），授予 Contents: Read/Write；
需要创建 PR 时再授予 Pull requests: Read/Write。仓库范围由 GitHub Token 权限决定，
不再由本项目代码限制。

**只需配置这一个 Secret**，无需 `ANTIGRAVITY_GITHUB_ALLOWED_OWNER`、
PocketBase 用户 ID、额外后端迁移或 Google Credentials API。
`gemini-text.yml` 将密钥仅传给服务端执行器；`gemini_agent.py` 在发起
Antigravity Interactions API 请求时检测密钥；只要已配置，GitHub Git/REST 认证就始终可用，
不再依赖“仓库”“分支”等提示词关键词。默认使用 REST/Git 和精简的工作区 helper，
不加载完整 GitHub MCP 工具目录；特殊调用可显式设置 `options.agent.githubMcpEnabled=true`。
项目任务在创建 Google interaction 前解析目标仓库，并在需要推送代码时用只读 Git
receive-pack 握手检查实际写权限。检查不修改仓库；拒绝时立即说明目标和所需权限，
不先花几十分钟改代码。它检查普通 Git 写权限，不替代分支保护、workflow 文件权限或 PR 权限。
执行器优先明确的 owner/repository，其次匹配当前 Token 可访问仓库的精简清单，
不在全 GitHub 搜索 `remote`、`exam` 等泛词。仓库歧义明确报出候选，不盲选。
只有需要源码的任务才克隆指定仓库到 `/workspace`，不预先克隆所有仓库。
单纯重新部署使用 workflow API，不先克隆、安装依赖或本地构建。
为空则保持原来的普通 Agent 模式。
普通 Gemini 模型和 PDF 转换流程不受影响。

**安全边界：** 启用后，任何能够通过该执行器提交 Antigravity 任务的用户，
都可能间接获得这份 PAT 的仓库写权限。仓库是私有执行器还是公开仓库，
并不能代替调用入口的身份验证。请确认 Remote 和 PocketBase 任务创建权限只开放给可信用户；
否则请勿配置写权限 PAT。Token 会出现在发送给 Google 的 API 请求体中，
但不应进入模型提示词、代码仓库文件、生成日志或用户可见消息。
建议使用最小范围、短有效期的 PAT，定期轮换。

配置后先在 Remote 的「Gemini 消息」选择 Antigravity，要求它只读取目标仓库并测试
GitHub REST 访问；需要修改源码时，执行器还会在开始前验证 Git 写权限。
**不要直接推送 `main`**：Anyworkflow-remote 的 `main` 推送会触发正式 Cloudflare 部署。
真实凭据能力检查、工作区恢复和运行时探针记录在本次修复说明中。

### 部署防重与故障恢复

项目任务挂载 `/workspace/.anyworkflow/github.py` 与部署 hook。Agent 通过
`deploy --repo OWNER/REPO --workflow FILE.yml --ref BRANCH --inputs-json '{...}'` 提交：

- 先写入任务工作区日志，再发出一次 POST；布尔输入统一转为字符串。
- HTTP 204 视为已接受。通过限定 workflow、分支、SHA 和提交前运行集合查找新增运行，
  绑定 run ID 后只查询该运行，不使用整个仓库列表的“第一条”。
- 同一工作区重复调用复用原 run；响应丢失、暂不可见或结果有歧义都不再次 POST。
- `watch --repo OWNER/REPO --run-id ID` 等待指定运行；success 立即结束。
- pre-tool hook 对常见直接 curl dispatch / gh workflow run 返回 helper 使用说明。
  这是防止误操作的执行约定，不是针对恶意代码的网络安全隔离。

维护入口沿用既有 workflow，`request_id` 使用以下显式前缀，与原任务队列隔离：

- `check-<原任务ID>`：仅 GET，检查原任务目标及当前密钥的写权限。
- `rescue-<原任务ID>`：仅 GET，从原 Google 环境下载目标仓库的 `.git`，保存为
  `anyworkflow-git-recovery-v1` JSON artifact，供恢复提交；不包含 node_modules。
- `probe-<原任务ID>`：创建一个最长 3 分钟的只读 Google Agent 探针，验证挂载工具、hook
  配置和 GitHub identity；不修改原任务、不推送代码、不部署网站。它会产生真实 API 用量。

Git 恢复 JSON 不是回复恢复 JSON，不能交给 `--recover-dir`。恢复时校验 SHA-256，
将 Git objects/refs 导入干净仓库，丢弃沙盒的 config 和 hooks，再检查和验证提交。

### Actions Agent 诊断日志

Gemini 消息 workflow 的 `Send segments and save replies to PocketBase` 步骤默认只输出有用信息：

- `[gemini]` JSON 行的 `segment_result`：完成状态、finish reason、错误原因、生成耗时，
  以及输入、输出、思考、工具、缓存和总 Token；未返回的计数为 `null`，不估算。
- `reply`：完整 AI 回复，经凭据脱敏后每 4000 字符分块，使用 `part` / `parts` 标明顺序。
  不截断长回复；JSON 转义换行及控制字符，避免回复文本被当作 Actions 命令。
- `segment_saved`：数据库已确认保存。结果及回复在保存前打印；保存失败会输出
  `segment_error` 的阶段、错误类型、原因与 `recovery_available`，区分模型问题和 PB 问题。
- `job_summary`：总耗时、生成/保存/失败段数，各段最后一次返回用量的合计，
  以及 `usage_reported_segments`。没有用量的段不补零；缓存属于输入的子集，不重复计入总计。
- 每行包含 UTC 时间、任务 ID、分段序号（任务汇总不指定分段）和耗时，支持并发时定位。

Agent 的 `[agent.telemetry]` 保留创建/完成信息及累计用量；同一状态的进度最多每分钟一次，
终态用量只记录一次，不把每轮轮询的累计值相加。普通任务不订阅诊断 SSE，不打印工具参数、
工具输出、思考摘要、20 秒心跳、checkpoint 耗时或结束时的整份步骤回放。
HTTP 拒绝、连接中断等关键错误仍会保留并脱敏。数据库中的原始回复不受日志脱敏影响。
AI 回复会进入 Actions 日志，日志可见性沿用仓库设置。

详细 `[agent.trace]` 仅用于显式的只读诊断，包含有界工具预览、事件重连及阶段汇总。
工具预览每项最多 1800 字符、每次观察合计约 1 MiB；历史事件到达间隔不代表原始执行耗时。
诊断与普通日志均会脱敏 API Key、PAT、PB 凭据、认证头和令牌字段。

对正在运行或已结束的任务，可以用现有 workflow 启动只读诊断：

```sh
gh workflow run gemini-text.yml --repo ActiveInsighter/file-converter-ai \
  --ref main -f request_id=trace-<15位原任务ID>
```

`trace-` 前缀与正常任务使用不同的 Actions concurrency group，执行器只读取 PB 中保存的
interaction 身份并使用匹配 Key 获取现有事件，不领取、生成、保存或取消原任务。
默认观察 180 秒。也可本地设置相同服务端凭据后运行 `python agent_diagnostics.py`，
此时 `REQUEST_ID` 使用原任务 ID；`AGENT_TRACE_SECONDS` 可设为 10–900 秒。
该操作需要执行器拥有读取对应 PB 消息的权限；缺少权限或匹配 Key 时明确失败。

**说明：** `total_cached_tokens` 通常是输入 Token 的子集，不能简单把所有字段相加当作总计；
页面显示的 Total Token 直接来自 Google `usage.total_tokens`。更新前的任务没有这些明细日志，
无法从旧日志反推为什么消耗了多少工具 Token 或思考 Token。
GitHub MCP 可显式选择启用，默认仓库任务通过已认证的 REST/Git 执行；
普通问题仍然经过 Antigravity Agent，因此耗时不会完全等同于简单模型的 generateContent。

官方参考：https://aistudio.google.com/docs/antigravity-agent
验证：`python -m unittest discover -s tests`。

## 图片拼接与 SVG 重绘

Remote 新任务默认启用图片 SVG 重绘，可分别选择正文和 SVG 的模型接口、模型与提示词。
OCR 完成后先按原始 PDF 坐标恢复同一图中的相邻碎片，保留原来的相对位置、连线和图内标注；
正文、完整图题和独立插图阻断合并。恢复后的完整裁图与独立插图分别交给 SVG 模型，
与正文转录并行处理，不把一页中的所有图片拼成一张。

成功时 Markdown 和 `pages/` 中的引用改为 `.svg`，`images/` 同时保留原始裁图供核对。
无效 XML、缺失标准命名空间/有效 viewBox、空图、嵌入位图、脚本或外部资源引用均视为失败；
按配置重试后仍失败时继续引用原图，不影响正文完成。没有 OCR 图块时不发送 SVG 请求。
同接口、同地址、同密钥的正文与 SVG 请求共用配额桶，独立接口则分别记录配额；
`quota-usage.json` 与 Actions Summary 包含 SVG 成功/失败计数。

Actions 的 25 个输入已用满，图片设置通过一个 `image_config` JSON 传入：

```sh
gh workflow run pdf-to-md.yml --repo ActiveInsighter/file-converter-ai --ref <已发布分支> \
  -f source_url='https://example.com/document.pdf' \
  -f image_config='{"jpeg_quality":95,"svg":{"enabled":true,"provider":"gemini","model":"gemini-3.5-flash-lite","prompt":""}}'
```

原先直接通过 `workflow_dispatch` 传递的 `jpeg_quality` 须改为 `image_config.jpeg_quality`；
Remote 和后端派发端已同步适配。CLI 的 `--jpeg-quality` 保持可用。直接在 Actions 运行而不传
`image_config.svg.enabled=true` 时沿用原图。独立 SVG 接口需要对应的 `GEMINI_API_KEYS` 或
`MODELFLARE_API_KEYS` Secret；无需另外添加一组密钥。

CLI 也支持 `--svg-enabled --svg-provider gemini --svg-model <模型>`，以及
`--svg-config '{"enabled":true,...}'`、`--image-config '{"jpeg_quality":95,"svg":{...}}'`。
完整参数见 `python pdf2md.py --help`。

## Agent / Chat 过程同步

Gemini 消息执行器在每个 Antigravity `step.stop` 后立即写入 PocketBase 并等待确认，
收到确认前不继续消费后续步骤；正文增量和累计 Token 仍由每 5 秒的刷新兜底。Antigravity 的工具调用、
工具结果和公开思考摘要按序保存；普通 Gemini 使用流式文本写相同内容分段。Remote 从数据库
增量读取，刷新页面可恢复已保存过程。SSE 断开后，在任务整个生命周期内使用事件游标继续重连，
并持续轮询 interaction 状态，不重新创建执行。非终态快照可能只返回状态、不含步骤或用量，
不能靠快照补齐事件流；重连失败会记录原因与退避时间。暂时故障在收到首个事件前也会重试。
部分实际响应没有事件游标，此时从头重放并按步骤编号重建增量，已经完成且内容相同的步骤不重复写入。
已确认的 `400 invalid_request / Precondition check failed.` 会退避重试；其他明确的参数或权限错误保留失败日志。
Actions 每分钟的状态日志同时记录事件观察器是否仍运行及已观察步骤数，区分快照步骤数与事件步骤数。
上传失败保留恢复文件，最终完成前补传；Actions recovery artifact 同时包含 `*.progress.json` 过程恢复文件。
无新内容时执行器每 30 秒发送一次无内容心跳，刷新 `progressAt`，不重复写入步骤。
Remote 以此显示执行器在线状态，以最后一个非状态分段的时间显示最近步骤；任务行的
`updated` 由调度器也会刷新，不能用来推断执行器仍在线。
checkpoint 使用同一 executionId 和 interaction 身份重试短暂的 PB 5xx/连接故障，
指数退避最多 16 秒、最多重试 12 次，为短暂发布提供比原先 7 秒更长的恢复窗口。
权限等明确 4xx 错误仍立即报告，不重发模型创建请求。
先发布 AnyWorkflow-workers 的 `1794870000_agent_message_parts.js` 迁移及 hooks，再更新执行器和 Remote。
完整接口及发布设计见后端 `docs/agent-message-stream.md`。

对仍在执行、过程同步已停止的单段任务，可显式运行 `sync-<原任务ID>`。
它只 GET 原 interaction 并写进度，不领取任务、不创建模型请求、不完成或取消原执行。
每个写入批次都回读数据库核对内容，防止旧 Runner 的心跳抢占批次号后被误当作已保存；
执行身份变化或任务结束后停止。原 Runner 继续负责最终回复和用量。
只读 trace 可通过 workflow 的 `diagnostic_seconds` 选择 10–900 秒窗口（默认 180）。
