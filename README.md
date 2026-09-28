# AnyWorkflow File Converter

这是 AnyWorkflow 的文件转换执行仓库。当前首个转换器是 PDF → Markdown：用 GitHub Actions 临时 Runner 将 PDF 按页渲染为图片，并并发调用 Google AI Studio / Gemini API，把连续页面转换成 Markdown，最后按原页序合并。后续转换类型通过 `conversion_type` 扩展。

## 功能

- 支持普通 HTTP(S) PDF 下载地址。
- 支持 Google Drive 公开分享链接，例如：
  `https://drive.google.com/file/d/FILE_ID/view?usp=drivesdk`
- PDF 按页渲染为 PNG（默认无损）；也可显式选择 JPEG。
- `images_per_request` 控制一次请求发送多少张连续页面图片。
- `concurrency` 控制同时进行的 Gemini 请求数。
- 支持自定义提示词、模型、DPI 和 JPEG 质量。
- 支持多个 Gemini API Key 轮询，重试时自动换下一个 Key。
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

## 手动运行

进入：

**Actions → AnyWorkflow File Converter · PDF to Markdown → Run workflow**

参数：

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `conversion_type` | 文件转换处理器 | `pdf_to_md` |
| `source_url` | PDF 下载地址，支持 Google Drive 分享链接 | 必填 |
| `images_per_request` | 每次请求发送几张连续页面图片 | `1` |
| `concurrency` | 最大 Gemini 并发请求数 | `50` |
| `prompt` | 自定义提示词；留空使用内置 Markdown 转换提示词 | 空 |
| `model` | Gemini 模型 ID | `gemini-3.5-flash-lite` |
| `thinking_level` | Gemini 思考深度 | `high` |
| `dpi` | PDF 渲染 DPI | `240` |
| `image_format` | 页面图像格式，PNG 为无损 | `png` |
| `jpeg_quality` | JPEG 质量（PNG 时忽略） | `95` |
| `verification_passes` | 初次转录后再对照图片审校的次数 | `0` |
| `media_resolution` | Gemini 每张图片的视觉分辨率预算 | `ultra_high` |

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
    "source_url": "https://drive.google.com/file/d/12DMkT6QkZSad5_SsxvcsHgFKsQrxf9JN/view?usp=drivesdk",
    "images_per_request": 1,
    "concurrency": 50,
    "prompt": "请准确识别页面内容并转换为 Markdown，公式使用 LaTeX。",
    "model": "gemini-3.5-flash-lite",
    "thinking_level": "high",
    "dpi": 240,
    "image_format": "png",
    "jpeg_quality": 95,
    "verification_passes": 0
  }
}
```

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

其中 `concurrency = 50` 是客户端同时在途请求的上限；实际发起速率仍由 Project 级 RPM/RPD 调度器约束，因此不会为了凑满 50 并发而绕过配额。

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

Gemini RPM/RPD 按 **Google Cloud Project** 计算。全局配额池运行在 `worker/` 下的 Cloudflare Worker + SQLite Durable Object 中；DO 为所有 GitHub Actions 原子发放 lease、计数并记录 cooldown。Worker 只保存项目编号、配额状态和 lease，**不接收或保存 Gemini API Key**。

此仓库配置为 66 个 key 对应 66 个独立 Project。Worker 会拒绝 key 数或唯一 Project 数不等于 66 的配置。`GEMINI_API_KEYS` 继续只放在 GitHub Actions Secret 中；`GEMINI_KEY_GROUPS` 可选，与 key 按行对应。若不设置，它会按 `project-1` 至 `project-66` 生成映射。更换或重排 key 时，必须保持对应的项目组顺序不变。

Actions 需要以下仓库设置：

| 类型 | 名称 | 用途 |
| --- | --- | --- |
| Variable | `GEMINI_QUOTA_WORKER_URL` | `https://file-converter-gemini-quota.2212148739lbw.workers.dev` |
| Secret | `GEMINI_QUOTA_WORKER_TOKEN` | Worker 的 Bearer 认证令牌 |
| Secret | `GEMINI_API_KEYS` | 66 个 Gemini API Key，每行一个 |
| Variable（可选） | `GEMINI_KEY_GROUPS` | 每个 key 对应的 Project 组名，每行一个 |

现有 `rpm_per_key` / `rpd_per_key` workflow 参数保留以兼容 n8n；它们实际配置每个 **Project** 的限额，默认分别为 15 RPM 和 500 RPD。所有并发 Action 使用同一个额度池和配置。

除 Project 配额外，DO 还对所有 Action 统一整形：最多 24 个 Gemini 请求在途，持续启动速率 2 req/s，空闲时最多突发 8 个。发现最近 20 个结果中 503 占比达到 20%（至少 5 个结果）时，会分阶段把在途上限降至 12、再降至 8，并分别增加 5–15 秒、15–30 秒、30–60 秒的随机全局冷却；连续稳定成功后再逐步恢复。lease 超时为 180 秒，Gemini HTTP 超时为 120 秒。

每页每轮只请求一次：先完成所有页面的首轮，再把可重试失败页放入两轮 deferred retry；两轮分别随机等待 30–60 秒和 60–120 秒。400/401/403/404、超过请求体限制和全局 RPD 耗尽会直接记为最终失败。日志每 15 秒输出一次聚合请求数、429/503、p50/p95 延迟和 DO 状态，避免逐次重试刷屏。

调度过程：

- `/v1/lease` 由 DO 按可用时间、当日用量和 round-robin 顺序原子挑选 Project，并在发放 lease 时增加 RPD 计数；
- `/v1/lease` 同时检查共享在途上限、全局 token bucket 和全局冷却，只有真正取得 lease 的调用会占用在途名额；
- Gemini 返回后，Action 通过 `/v1/report` 回报状态；429 按 error details 分类，尊重 `Retry-After` / `RetryInfo`，泛化 429 暂停对应 Project 并触发短暂全局退避；明确的每日请求额度错误会停用该 Project 至 Pacific Time 次日；
- 503 不会通过换 key 反复冲击同一模型后端；DO 根据近期 503 占比启动全局冷却和动态降并发，失败页面之后再进入 deferred retry；
- 每个 Pacific Time 新日首次访问时自动重置每日计数和 cooldown；
- GitHub Actions 不再读取或提交 `quota-state` 分支；`quota-usage.json` 记录本次 Action 的用量，`/v1/status` 返回 Project 和全局控制状态。

部署 Worker：

```bash
cd worker
wrangler deploy
wrangler secret put QUOTA_API_TOKEN
```

把 Worker URL 保存到 Repository Variable `GEMINI_QUOTA_WORKER_URL`，并把同一个 `QUOTA_API_TOKEN` 保存为 Repository Secret `GEMINI_QUOTA_WORKER_TOKEN`。

当前已部署的 Worker 地址为 `https://file-converter-gemini-quota.2212148739lbw.workers.dev`。旧 `quota-state` 分支中 2026-09-28 Pacific 日的 66 个项目用量已导入 Durable Object；该分支仅保留作历史备份。

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
