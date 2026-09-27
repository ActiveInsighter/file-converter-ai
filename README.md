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
| `model` | Gemini 模型 ID | `gemini-3.8-flash` |
| `dpi` | PDF 渲染 DPI | `180` |
| `jpeg_quality` | JPEG 质量 | `88` |

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
    "model": "gemini-3.8-flash",
    "dpi": 180,
    "jpeg_quality": 88
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
dpi = 180
jpeg_quality = 88
```

确认你的 Google AI Studio 项目实际限流后，再逐步增加并发。

## 本地运行

```bash
pip install -r requirements.txt
export GEMINI_API_KEYS=$'key1\nkey2\nkey3'
python pdf2md.py \
  --source-url 'https://drive.google.com/file/d/FILE_ID/view?usp=sharing' \
  --images-per-request 3 \
  --concurrency 10
```
