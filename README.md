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
| `system_prompt` | 系统提示词（来自已保存的转换配置，所有任务共用） | 空 |
| `prompt` | 本次转换的专有提示词，追加在 `system_prompt` 之后 | 空 |
| `model` | Gemini 模型 ID | `gemini-3.5-flash-lite` |
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

每页每轮只请求一次：先完成全部页面首轮，再分两轮重试可恢复的失败页。429 按 error details 分类，尊重 `Retry-After` / `RetryInfo`；只有明确的每日请求额度错误才停用该 Project 至 Pacific Time 次日。API 的 `/v1/lease` 支持 `requestId`，请求超时后以相同 ID 重试会返回原 lease；`/v1/report` 重复提交不会重复计数。`/v1/status` 提供 Project 与全局状态；日志每 15 秒汇总进度、429/503 和 p50/p95 延迟。

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
