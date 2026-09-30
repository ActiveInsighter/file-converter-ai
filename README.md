# AnyWorkflow File Converter

这是 AnyWorkflow 的文件转换执行仓库。当前首个转换器是 PDF → Markdown：用 GitHub Actions 临时 Runner 将 PDF 按页渲染为图片，并并发调用所选模型接口，把连续页面转换成 Markdown，最后按原页序合并。后续转换类型通过 `conversion_type` 扩展。

## 功能

- 支持普通 HTTP(S) PDF 下载地址。
- 支持 Google Drive 公开分享链接，例如：
  `https://drive.google.com/file/d/FILE_ID/view?usp=drivesdk`
- PDF 按页渲染为 PNG（默认无损）；也可显式选择 JPEG。每个页面块渲染后立即入队发送，解析与 AI 请求重叠执行。
- `images_per_request` 控制一次请求发送多少张连续页面图片。
- `concurrency` 控制同时进行的模型请求数。
- 支持自定义提示词、模型、DPI 和 JPEG 质量。
- 支持多个 Gemini API Key 轮询，重试时自动换下一个 Key。
- 支持 `gemini` 和 `modelflare` 提供方；Modelflare 使用 OpenAI 兼容的 Chat Completions 图像输入。
- 通过稳定的 `conversion_type` 输入选择转换器；当前值为 `pdf_to_md`。
- 输出按原页码命名：`001.md` 或 `001-003.md`。
- 全部成功生成 `merged.md`；有失败时生成 `merged.partial.md` 和错误详情。
- Action Artifact 保存 Markdown 分块、合并文件和 `manifest.json`。

## 配置 Gemini API Key

进入仓库：

**Settings → Secrets and variables → Actions → New repository secret**

创建：

`GEMINI_API_KEYS`

推荐把 10 个 Google AI Studio API Key 放进同一个 Secret，**每行一个**：

```text
AIza...key1
AIza...key2
AIza...key3
...
AIza...key10
```

脚本也兼容逗号或分号分隔，但多行最清晰。代码不会打印 Key。

使用一个多行 Secret 比维护 `GEMINI_API_KEY_1` 到 `GEMINI_API_KEY_10` 更方便；增删 Key 不需要修改 Workflow。

## 配置 Modelflare

后续要使用时，在同一个 GitHub Actions Secrets 页面创建 `MODELFLARE_API_KEYS`，填入一个或多个 Modelflare API Key，每行一个。密钥不写入仓库、任务参数或前端。创建密钥时选择能访问目标模型的路由分组；模型 ID 与分组必须匹配。可先用该密钥调用 `GET https://modelflare.dev/v1/models` 查看可用模型，再确认所选模型支持**图像输入**和 **Chat Completions**。图片中的 `gpt-6-sol` 等名称仅供识别接口，项目不会预选它们。

运行时选择 `provider=modelflare`，并填写确切的视觉模型 ID。默认接口为 `https://modelflare.dev/v1/chat/completions`，页面图片作为 Base64 `image_url` 发送。`thinking_level` 只用于 Gemini；如目标模型支持，可另外填写 `reasoning_effort`。`max_output_tokens=0` 表示由接口决定输出上限。Modelflare 使用本次任务内的请求节流，不要求 Gemini 的共享配额服务。

本地示例（将密钥放入环境变量，不要写入命令行参数）：

```bash
export MODELFLARE_API_KEYS='你的密钥'
python pdf2md.py \
  --source-url 'https://example.com/document.pdf' \
  --provider modelflare \
  --model '你选定的视觉模型ID' \
  --concurrency 5
```

接口路径与密钥分组规则以 [Modelflare 官方接口文档](https://docs.modelflare.dev/guides/endpoints/) 和 [模型与分组文档](https://docs.modelflare.dev/guides/models-and-groups/) 为准。当前适配的是 Chat Completions；若模型只提供 Responses 接口，需先增加对应的请求与响应适配器。

`gpt-6-sol` 的单页、4 并发和 8 并发实测数据见 [Modelflare 验证记录](docs/modelflare-validation-2026-09-29.md)。

## 手动运行

进入：

**Actions → AnyWorkflow File Converter · PDF to Markdown → Run workflow**

参数：

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `conversion_type` | 文件转换处理器 | `pdf_to_md` |
| `provider` | 模型提供方：`gemini` / `modelflare` | `gemini` |
| `source_url` | PDF 下载地址，支持 Google Drive 分享链接 | 必填 |
| `images_per_request` | 每次请求发送几张连续页面图片 | `1` |
| `concurrency` | 本任务最大模型并发请求数（含补发副本） | `50` |
| `system_prompt` | 系统提示词（来自已保存的转换配置，所有任务共用） | 空 |
| `prompt` | 本次转换的专有提示词，追加在 `system_prompt` 之后 | 空 |
| `model` | 模型 ID；Modelflare 必填 | Gemini 默认 `gemini-3.5-flash-lite` |
| `reasoning_effort` | Modelflare 推理强度，目标模型支持时填写 | 空 |
| `max_output_tokens` | 输出 token 上限，0 表示接口默认 | `0` |
| `thinking_level` | Gemini 思考深度 | `high` |
| `dpi` | PDF 渲染 DPI | `240` |
| `image_format` | 页面图像格式，PNG 为无损 | `png` |
| `jpeg_quality` | JPEG 质量（PNG 时忽略） | `95` |
| `verification_passes` | 初次转录后再对照图片审校的次数 | `0` |
| `media_resolution` | Gemini 每张图片的视觉分辨率预算 | `ultra_high` |

### 提示词如何拼接

真正发给模型的提示词是**系统提示词**与**本次转换的专有提示词**按顺序拼接（中间空一行）：

```text
system_prompt（存在 PocketBase 转换配置里，前端可编辑）
+ "\n\n" +
prompt（每次转换单独填写）
```

两者互不覆盖、各自可选，各限 12000 字符。只有**两者都为空**时才回退到内置的忠实转写提示词，
所以从 GitHub 页面手动派发仍然可用。`manifest.json` 的 `prompt` 字段记录两段原文、长度与
拼接结果的 sha256，便于事后核对这一次究竟用了什么提示词。

例如 10 页 PDF 且：

`images_per_request = 3`

会得到：

```text
output/pages/
├── 001-003.md
├── 004-006.md
├── 007-009.md
└── 010.md

output/merged.md
output/manifest.json
```

## 从 n8n / 外部程序触发

Workflow 暴露 GitHub `repository_dispatch` 事件：

`pdf_to_md`

调用 GitHub REST API：

```http
POST https://api.github.com/repos/ActiveInsighter/file-converter-ai/dispatches
Authorization: Bearer <GITHUB_TOKEN>
Accept: application/vnd.github+json
Content-Type: application/json
```

请求体：

```json
{
  "event_type": "pdf_to_md",
  "client_payload": {
    "conversion_type": "pdf_to_md",
    "provider": "gemini",
    "source_url": "https://drive.google.com/file/d/12DMkT6QkZSad5_SsxvcsHgFKsQrxf9JN/view?usp=drivesdk",
    "images_per_request": 1,
    "concurrency": 50,
    "system_prompt": "请按图片原始顺序忠实转写，公式使用 LaTeX。",
    "prompt": "只处理第 3-7 页，保留题号与分数。",
    "model": "gemini-3.5-flash-lite",
    "thinking_level": "high",
    "dpi": 240,
    "image_format": "png",
    "jpeg_quality": 95,
    "verification_passes": 0
  }
}
```

通过 `repository_dispatch` 使用 Modelflare 时，把 `provider` 改为 `modelflare`，并将 `model` 改为你选定的视觉模型 ID；不要沿用 Gemini 的模型 ID 或 `model_fallbacks`。仓库中即使仍有 Gemini 配额变量，Modelflare 任务也不会使用它们。

Gemini 自定义模型（例如 `gemini-3.8-flash`、`gemini-3.7-flash`）默认只用所选模型。Google 返回 503 时会按页面重试，不会自动改用 Flash-Lite。只有显式传入 `model_fallbacks` 才会切换模型；默认 `gemini-3.5-flash-lite` 仍保留其同档替补。传入 `model_fallbacks=none` 可禁用默认替补。关于 3.8/3.7 Flash 的 503 实测见 [诊断记录](docs/gemini-flash-503-2026-09-29.md)。

因为仓库是私有仓库，调用方需要一个有权限访问该仓库的 GitHub Token。n8n 的 HTTP Request 节点可以直接调用这个地址。

## 输出和失败处理

每个页面块完成后立即写入独立 Markdown。

全部成功：

```text
output/merged.md
```

有失败：

```text
output/merged.partial.md
output/errors/<页码范围>.json
```

`manifest.json` 会记录总页数、分块、配置以及每块状态。Action 即使失败也会尝试上传 `output/` Artifact，因此已成功的结果不会白跑。

### 空白页与模型拒答：自动恢复，不再算失败

两类页面级问题不再拖垮整单，也不再白等退避轮次：

- **空白页**：渲染时测量每页的墨迹覆盖率，整页几乎没有墨迹且没有深色像素（扫描空白页）时，
  该块**不发送任何请求**，日志记 `[blank]`，`manifest.json` 里 `status` 为 `blank`。
  空白页本来就无内容可转写，因此不计数为失败，`merged.md` 照常完整生成。
- **模型拒答**：`finishReason` 为 `RECITATION` / 安全拦截，或模型空回答（`STOP` 无文本）。
  这类判定对同一张图是可复现的，因此**不再进入 30-480 秒退避轮次**（原来会把整单拖 18 分钟以上）。
  对单页块会自动把页面**沿空白行切成上下两半**分别转换再拼接，切分保持原始像素分辨率
  （产物在 `output/split/<页码>-part1.png`）。**若切出来的半页仍被拒答，会把该半页再切一次**
  （最多 `PAGE_SPLIT_DEPTH=2` 层：整页 → 半页 → 四分之一页），实测确有页面需要这一层才拿到正文。
  被拒答的区域**不会被静默丢弃**：只要有一块成功，该页仍记为 `ok`，日志打 `[warn]` 说明少了哪一块，
  并记入 `manifest.json` 的 `partial_recovery`。
  整页与所有切块都无文本时（图形页等）按空白页处理并记 `no_transcribable_text`；
  只有整页彻底拒答才计入 `failures`，且**立即失败**，不再空等退避轮次。
  切块时若遇到 503/429（容量问题，与拒答无关）会重新抛出让正常轮次重试，不会被误报成最终失败。

`--blocked-page-split halves|none` 控制切分恢复（默认 `halves`，`none` 关闭）。
`manifest.json` 新增 `blank_pages`、`blank_chunks`、`split_recovered`、`partial_recovery` 四个字段便于审计。

## 建议起始参数

Gemini 内联图片请求存在总请求大小限制，脚本会估算 Base64 后体积，过大时要求降低图片数或图片质量。

当前默认吞吐配置：

```text
images_per_request = 1
concurrency = 50
dpi = 240
image_format = png
jpeg_quality = 95
media_resolution = ultra_high
verification_passes = 0
```

其中 `concurrency = 50` 是客户端同时处理的页面任务数；真正的 Gemini 请求由 quota API 统一限制为最多 24 个在途、持续 2 req/s。

## 本地运行

```bash
pip install -r requirements.txt
export GEMINI_API_KEYS=$'key1\nkey2\nkey3'
python pdf2md.py \
  --source-url 'https://drive.google.com/file/d/FILE_ID/view?usp=sharing' \
  --images-per-request 1 \
  --concurrency 50 \
  --thinking-level high
```


## 高精度模式

默认使用 `gemini-3.5-flash-lite` 并将 Gemini 3 的 `thinkingLevel` 设置为 `high`。对于数学 PDF，建议优先使用：

```text
thinking_level = high
images_per_request = 1~2
dpi = 200~220
jpeg_quality = 90
```

这会牺牲一些速度和额度，但更适合公式密集文档。默认提示词也要求逐符号核对公式、禁止猜测、禁止跨行单美元符号数学环境，并忽略无意义的扫描水印或重复编码。


### 默认图像质量

当前默认使用 **240 DPI + PNG 无损**。PNG 仍会进行无损压缩，但不会损失像素信息；相比真正的未压缩位图，体积小很多而视觉内容完全一致。对于本次 110 页数学 PDF，抽样页约 0.68 MB/页，1 页一组经过 Base64 后仍远低于 Gemini 内联请求大小限制。

默认不会执行额外图片对照审校（`verification_passes = 0`）；如手动开启，则会在初次转录后执行额外审校，也就是同一页组会经过“转录 → 再对照原图修正”的两阶段处理。可将 `verification_passes` 设为 0 关闭，或提高到 2~3（会增加耗时和 API 用量）。


### 数学 PDF 推荐精度策略

对公式密集型 PDF，默认配置现在是：

```text
images_per_request = 1
dpi = 240
image_format = png
thinking_level = high
media_resolution = ultra_high
verification_passes = 0
```

原因：同一模型进行第二次“整段重写式审校”有时会修正错误，也可能把原本正确的公式改错，所以默认关闭；需要时仍可手动开启。相比单纯继续增加 DPI，Gemini 3 的 `MEDIA_RESOLUTION_ULTRA_HIGH` 会给每张图片分配更高的视觉 token 预算，更适合小字号公式、上下标和矩阵。


### Project 级配额与跨 Action 调度

Gemini RPM/RPD 按 **Google Cloud Project** 计算。66 个 key 分属 66 个独立 Project。所有 GitHub Actions 通过 HTTPS 调用 `quota_api/`，服务在本机访问 Valkey。`quota_api/scheduler.lua` 原子发放 lease、增加当日计数、控制 RPM 和全局速率，并处理 429/503。Valkey 只保存 Project 编号、配额状态和 lease；**Gemini API Key 仍只存在 GitHub Secret 中**。

Actions 需要以下仓库设置：

| 类型 | 名称 | 用途 |
| --- | --- | --- |
| Variable | `GEMINI_QUOTA_API_URL` | quota API 的 HTTPS 根地址 |
| Secret | `GEMINI_QUOTA_API_TOKEN` | quota API 的 Bearer 认证令牌 |
| Secret | `GEMINI_API_KEYS` | 66 个 Gemini API Key，每行一个 |
| Variable（可选） | `GEMINI_KEY_GROUPS` | 每个 key 对应的 Project 组名，每行一个 |

当前 HTTPS 入口为 `https://n8n.any1.tech/file-converter-quota`；quota API 和 Valkey 实际运行在本机回环地址，经现有网关的 SSH 反向隧道转发。

如果不设置 `GEMINI_KEY_GROUPS`，会按 `project-1` 至 `project-66` 生成映射。服务拒绝不等于 66 个或不唯一的 Project 映射，初始化后也拒绝重排。`rpm_per_key` / `rpd_per_key` workflow 参数保留以兼容 n8n；它们实际配置每个 Project 的限额，默认 15 RPM 和 500 RPD。

全局调度最多允许 24 个 Gemini 请求在途，持续启动速率 2 req/s，空闲时突发上限 8 个。近 20 个结果中 503 占比达到 20%（至少 5 个结果）时，自动将上限降至 12、再降至 8，并加入随机全局冷却；持续成功后恢复。lease 超时 180 秒，Gemini HTTP 超时 120 秒。Valkey Sorted Set 维护每个 Project 的下次可用时间，正常发放 lease 时无需扫描 66 个 Project。

失败页面独立调度重试，不再等待全部页面首轮结束。`attempts_per_page` 保留即时尝试次数，`retry_rounds` 保留每页最大轮数；轮间退避由 5 秒起步、最多 90 秒（含随机抖动），等待重试不占页面 worker。新页面优先取得空闲 worker。429 按 error details 分类，尊重服务端 `Retry-After` / `RetryInfo`；每日额度错误才停用该 Project 至 Pacific Time 次日。`/v1/lease` 的同一 `requestId` 在响应超时后可重放，`/v1/report` 重复提交不会重复计数。

[196 页重构前后实测与部署记录](docs/streaming-tail-recovery-2026-09-30.md)（包括真实补发、请求消耗和时间对比）。

### 慢请求补发、超时与进度

本地 CLI 的 `--hedge-after` / `--hedge-budget`，以及 `repository_dispatch.client_payload` 中的 `hedge_after` / `hedge_budget` 可按任务调整补发。手动 Actions 保留原有 25 个输入（GitHub 上限）；其高级默认值可用仓库 Variables `CONVERTER_HEDGE_AFTER` / `CONVERTER_HEDGE_BUDGET` 调整。分别默认 `60` / `-1`，任一设为 `0` 即禁用补发。

当请求真正取得配额并开始 HTTP 后，等待超过 `max(hedge_after, 2 × 近期成功请求 P95)`，且近期 429/503 占比低于 20%，会尝试增加最多两个相同请求。原请求继续执行；第一个通过响应解析及 Markdown 校验的结果胜出，其余请求取消并释放租约。配置的并发上限包含所有副本，副本仍通过全局配额 API，并且会消耗真实 RPM/RPD；取消不会返还额度。并发已满时，副本也需要等待空闲容量。全任务默认补发预算约为页块数的 10%（小任务至少 2 个）；可设置 `hedge_budget=0` 禁用，或设固定预算。

HTTP 除原来的连接/读取超时外，新增总时间上限：Gemini 为 120 秒，Modelflare 为 300 秒。整组补发共享原请求的截止时间，副本同时卡住时不会另等一轮完整超时（之后仍需有限的租约清理时间）。成功的文本不会因为配额上报临时失败而被丢弃；上报清理最多等待 8 秒，服务不可达时由租约到期兜底。取消的副本向服务上报 499，保留已消耗的额度，但不参与 503 降速或成功恢复统计。

日志每 5 秒显示完成百分比、渲染进度、活动页块、待重试页块、最终失败数、预计剩余时间与补发命中数。`[request]` 标记真实 HTTP 开始；`[hedge]` / `[hedge-win]` 标记慢请求恢复；错误日志包含具体原因。

- `output/progress.json`：原子更新的实时进度，包含当前处理的页码及耗时；ETA 是基于已完成吞吐的估计。
- `output/results.json`：每页完成后更新的结果索引。
- `output/quota-usage.json`：HTTP 延迟、真实请求消耗、取消数和补发预算/命中。
- GitHub Actions 完成后在 Summary 显示结果概览并上传这些文件。

进度展示当前覆盖转换器日志、进度文件及 Actions Summary；AnyWorkflow Remote 的网页仍使用原有任务状态接口，尚未消费逐页实时进度。

渲染采用单独线程顺序访问同一个 PyMuPDF 文档，队列上限是页面 worker 数的两倍；仅在请求槽内构造 Base64，避免整个长文档载荷同时驻留内存。已成功页面立即保存；渲染失败时保留已完成页、部分合并结果和 `manifest.json` 中的 `pipeline_error`。合并只使用本次成功页面，避免复用输出目录时混入旧任务同名页。

`deploy/valkey/docker-compose.yml` 使用官方 Valkey 9.1.2 镜像、AOF `everysec` 与 RDB 快照，6379 仅映射到 `127.0.0.1`。没有 Docker 的 Ubuntu 主机也可使用发行版 `valkey-server` 包，启用相同的持久化配置。`deploy/quota-api.service` 将 API 绑定到本机 `127.0.0.1:8788`；由现有 HTTPS 入口反向代理，不向公网开放 Valkey 或 Uvicorn 端口。具体部署步骤见 `deploy/README.md`。

## AnyWorkflow Remote integration

`workflow_dispatch` accepts `conversion_type` and `request_id` (an external job identity) and
`output_name` (the merged Markdown basename, default `merged`). The run title is
`file-converter-pdf-to-md-<request_id>` so a caller can recover the run after a lost dispatch
response without dispatching twice. The artifact name is
`file-converter-pdf-to-md-<github.run_id>`; its ZIP contains `<output_name>.md` (or
`<output_name>.partial.md`), page Markdown and conversion metadata. Only
`output/` is uploaded; the source PDF and rendered images stay in the temporary
runner's `work/` directory.

The Remote frontend stores owner-scoped reusable profiles in
`aw_file_conversion_configs`, then creates `aw_pdf_to_md_jobs` with only the
selected `configId`. The PocketBase hook snapshots the database profile into
the job; the private worker reads that snapshot and passes flat inputs to this
workflow. The n8n PDF workflow invokes the worker every minute, reconciles
GitHub status, downloads the artifact ZIP, and uploads it to the protected
PocketBase `file` field. The final download filename is configurable
independently of the task title.

The GitHub REST API version `2026-03-10` returns `workflow_run_id` from
[workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event).
The worker credential needs repository **Actions: write** permission.


### 高吞吐默认值

默认值已调整为 `images_per_request = 1` 与 `concurrency = 50`。HTTP 客户端同时扩大连接池与 keep-alive 池；请求图片的 Base64 构造也放入并发槽内，避免长 PDF 在排队阶段提前把所有图片载荷常驻内存。图像质量保持 `240 DPI + PNG + ultra_high`，没有通过降低图片质量换取吞吐。
