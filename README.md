# md-to-pdf-ai

用 GitHub Actions 临时 Runner 将 PDF 按页渲染为图片，并并发调用 Google AI Studio / Gemini API，把连续页面转换成 Markdown，最后按原页序合并。

## 功能

- 支持普通 HTTP(S) PDF 下载地址。
- 支持 Google Drive 公开分享链接，例如：
  `https://drive.google.com/file/d/FILE_ID/view?usp=drivesdk`
- PDF 按页渲染为 JPEG。
- `images_per_request` 控制一次请求发送多少张连续页面图片。
- `concurrency` 控制同时进行的 Gemini 请求数。
- 支持自定义提示词、模型、DPI 和 JPEG 质量。
- 支持多个 Gemini API Key 轮询，重试时自动换下一个 Key。
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

**Actions → PDF to Markdown with Gemini → Run workflow**

参数：

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `source_url` | PDF 下载地址，支持 Google Drive 分享链接 | 必填 |
| `images_per_request` | 每次请求发送几张连续页面图片 | `1` |
| `concurrency` | 最大 Gemini 并发请求数 | `5` |
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
POST https://api.github.com/repos/ActiveInsighter/md-to-pdf-ai/dispatches
Authorization: Bearer <GITHUB_TOKEN>
Accept: application/vnd.github+json
Content-Type: application/json
```

请求体：

```json
{
  "event_type": "pdf_to_md",
  "client_payload": {
    "source_url": "https://drive.google.com/file/d/12DMkT6QkZSad5_SsxvcsHgFKsQrxf9JN/view?usp=drivesdk",
    "images_per_request": 3,
    "concurrency": 10,
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

建议先用：

```text
images_per_request = 2~4
concurrency = 5~10
dpi = 220
image_format = png
jpeg_quality = 95
verification_passes = 0
```

确认你的 Google AI Studio 项目实际限流后，再逐步增加并发。

## 本地运行

```bash
pip install -r requirements.txt
export GEMINI_API_KEYS=$'key1\nkey2\nkey3'
python pdf2md.py \
  --source-url 'https://drive.google.com/file/d/FILE_ID/view?usp=sharing' \
  --images-per-request 3 \
  --concurrency 10 \
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

当前默认使用 **240 DPI + PNG 无损**。PNG 仍会进行无损压缩，但不会损失像素信息；相比真正的未压缩位图，体积小很多而视觉内容完全一致。对于本次 110 页数学 PDF，抽样页约 0.68 MB/页，3 页一组经过 Base64 后仍远低于 Gemini 内联请求大小限制。

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


### Project 级配额与跨 Action 持久化

Gemini API 的 RPM/RPD 按 **Google Cloud Project** 计算，而不是按 API Key。仓库现在使用 Project 配额池调度，并把使用状态持久化到专用的 `quota-state` 分支：

```text
quota-state
└── quota-state.json
```

状态文件只保存 `key#1`、`key#2` 这样的编号和统计信息，**不会保存真实 API Key**。每次 Action 启动都会读取上一次状态；运行中每 10 次请求或约 30 秒 checkpoint，一旦遇到 429 会立即 checkpoint，Action 结束时再强制保存一次。

需要在：

**Settings → Secrets and variables → Actions → Variables**

新增 Repository Variable：

```text
GEMINI_KEY_GROUPS
```

它与 `GEMINI_API_KEYS` 按行一一对应。例如：

```text
GEMINI_API_KEYS              GEMINI_KEY_GROUPS
key1                         p1
key2                         p1
key3                         p2
key4                         p3
...
```

表示 `key1` 和 `key2` 属于同一个 Project，共享同一份 RPM/RPD；`key3` 属于另一 Project。

实际 Variable 只填写右侧组名，每行一个：

```text
p1
p1
p2
p3
p4
p5
p6
p7
p8
p9
```

如果不设置 `GEMINI_KEY_GROUPS`，为了兼容旧配置，程序会暂时把每个 Key 当成独立 Project。

现有 workflow input 名 `rpm_per_key` / `rpd_per_key` 为了兼容 n8n 调用暂时保留，但**现在语义是每个 Project 配额池的 RPM/RPD**：

```text
rpm_per_key = 15   # 实际：每个 Project 15 RPM
rpd_per_key = 500  # 实际：每个 Project 500 RPD
```

调度器会：

- 在同一 Project 内轮换多个 Key，但共享同一个 RPM/RPD 计数；
- 跨 Action 继承当天已经消耗的请求数和最近一分钟请求时间；
- Google 返回项目级 429 时，立即更新该 Project 的 cooldown；
- 识别到 `limit: 500` 等日额度耗尽信息时，把整个 Project 标记为当天 exhausted，跳过其所有 Key；
- 到 **Pacific Time 新的一天**时自动重置 RPD 状态；
- 每次 Artifact 额外输出 `quota-usage.json`，便于查看本次/累计 Project 与 Key 使用情况。

GitHub workflow 本身使用同一个 concurrency group 排队，因此多个 PDF 任务可以同时提交，但 Gemini 阶段一次只运行一个 Action，避免跨 Action 抢同一组 Project 配额。

## AnyWorkflow Remote integration

`workflow_dispatch` accepts `request_id` (an external job identity) and
`output_name` (the merged Markdown basename, default `merged`). The run title is
`pdf-to-md-<request_id>` so a caller can recover the run after a lost dispatch
response without dispatching twice. The artifact name stays
`pdf-to-md-<github.run_id>`; its ZIP contains `<output_name>.md` (or
`<output_name>.partial.md`), page Markdown and conversion metadata. Only
`output/` is uploaded; the source PDF and rendered images stay in the temporary
runner's `work/` directory.

The Remote frontend creates owner-scoped `aw_pdf_to_md_jobs` records in
PocketBase. The n8n PDF workflow invokes the private PDF worker every minute;
it dispatches queued jobs, reconciles GitHub status, downloads the artifact ZIP,
and uploads it to the protected PocketBase `file` field. The final download
filename is configurable independently of the task title.

The GitHub REST API version `2026-03-10` returns `workflow_run_id` from
[workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event).
The worker credential needs repository **Actions: write** permission.
