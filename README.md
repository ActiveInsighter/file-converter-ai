# AnyWorkflow File Converter

PDF → Markdown 转换执行器。GitHub Actions 下载 PDF，顺序渲染页面，页面块一就绪就发送给 AI，最终按原页序合并。支持 Gemini 和 Modelflare。

## 输出结构

下载包只包含两个 Markdown 文件：

```text
output/
├── source.md
└── <output_name>.md
```

`source.md` 记录 PDF 来源链接、转换时间（UTC）、原始页数、转换范围、完成情况及请求模型。它的全部内容也放在最终 Markdown 的最前面，用分隔线与正文隔开。

部分失败或切分恢复后仍有区域缺失时，正文文件为 `<output_name>.partial.md`，前言列出未完成页码和内容不完整的页码。空白页跳过 AI 请求，在前言标明。取消或渲染失败时保留已完成页面；运行期间每 5 秒更新部分文件。进程被强制终止时保留最近一次写入的版本。

运行中的所有中间数据保存在独立工作目录，不放进下载包：

```text
work/
├── source.pdf
├── images/
└── conversion/
    ├── pages/
    ├── errors/
    ├── split/
    ├── progress.json
    ├── results.json
    └── quota-usage.json
```

`progress.json` 是原子更新的实时进度；`results.json` 是完成页面索引；`quota-usage.json` 包含请求用量、延迟、取消数和补发统计。GitHub Actions 日志每 5 秒显示进度、活动页面、重试、ETA 和补发命中，任务结束后生成 Summary。临时 Runner 销毁后，中间数据随之删除；运行日志继续可查。

`output_name` 是不含扩展名的文件名，默认 `merged`，上限 120 字符及 240 UTF-8 字节。禁止路径、隐藏文件名、系统保留名；`source` 留给来源文件。CLI 的 `--output-dir` 是专用生成目录，每次运行会清空；它与 `--work-dir` 必须互不包含，生成目录不得包含当前工作目录。

## 调度与慢请求恢复

- 渲染使用单独线程和一个 PyMuPDF 文档。页面队列容量为 worker 数的两倍；Base64 在请求槽内构造。
- 新页面优先分配空闲 worker。失败页面独立退避重试，不必等待其他页面全部结束；退避从 5 秒起步，最多 90 秒，并带随机抖动。
- 请求真正开始 HTTP 后，等待超过 `max(60 秒, 2 × 近期成功请求 P95)`，且近期 429/503 比例低于 20%，最多补发两个副本。第一个通过解析及 Markdown 校验的结果胜出，其他请求取消并释放租约。
- 所有副本计入并发和 RPM/RPD。整组请求共享原请求截止时间：Gemini 120 秒，Modelflare 300 秒；租约清理另有有限等待。取消保留已消耗额度，状态 499 不参与过载降速。
- 默认额外请求预算为页块数的 10%（至少 2 个）。`--hedge-after 0` 或 `--hedge-budget 0` 关闭补发；`--hedge-budget -1` 使用默认预算。
- 空白扫描页直接跳过。单页内容拒答时，沿空白行切分后重试，最多两层。已恢复区域保留；缺失区域明确标记为部分结果。429/503 仍按容量错误处理。

[196 页真实测试记录](docs/streaming-tail-recovery-2026-09-30.md)包含重构前后耗时、请求成本和补发效果。记录中的旧产物路径对应当时测试版本；当前结构以本说明为准。

## 配置

Gemini 需要仓库 Secret `GEMINI_API_KEYS`（每行一个 Key）、`GEMINI_QUOTA_API_TOKEN`，以及 Variable `GEMINI_QUOTA_API_URL`。`GEMINI_KEY_GROUPS` 指定每个 Key 的 Google Cloud Project；未配置时使用 `project-1` 等顺序编号。

Gemini 使用 `quota_api/` 共享调度，跨 Actions 控制 Project 配额及全局容量。当前服务固定 66 个独立 Project，全局最多 24 个在途请求、持续 2 req/s、突发 8 个。503 增多时降低容量并冷却，持续成功后恢复。429 按错误详情区分分钟限速、每日耗尽及容量问题，并尊重服务端重试时间。Valkey 不保存 API Key。

Modelflare 使用 Secret `MODELFLARE_API_KEYS` 和所选视觉模型 ID，通过 Chat Completions 接口发送图像；每个 Key 在本任务内单独限速，不使用 Gemini 配额服务。部署配置见 [deploy/README.md](deploy/README.md)。

## 运行

在 **Actions → AnyWorkflow File Converter · PDF to Markdown → Run workflow** 填写 `source_url`。支持直接 HTTP(S) PDF 地址及 Google Drive 公开分享链接。

主要参数：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `provider` | `gemini` | `gemini` 或 `modelflare` |
| `model` | Gemini 默认模型 | Modelflare 必须指定视觉模型 ID |
| `output_name` | `merged` | 最终 Markdown 文件名，不含扩展名 |
| `images_per_request` | `1` | 每个请求的连续页面数 |
| `concurrency` | `50` | 本任务请求并发上限，含补发；Gemini 还受共享配额限制 |
| `system_prompt` / `prompt` | 空 | 系统提示词与本次指令，按顺序拼接；各限 12000 字符 |
| `dpi` / `image_format` | `240` / `png` | 渲染质量；PNG 无损 |
| `thinking_level` / `media_resolution` | `high` / `ultra_high` | Gemini 思考与图像细节预算 |
| `verification_passes` | `0` | 额外对照图片审校次数 |
| `rpm_per_key` / `rpd_per_key` | `15` / `500` | Gemini 按 Project 限制，其他提供方按 Key 限制 |
| `start_page` / `end_page` | 整份 PDF | 1-based 页面范围 |
| `retry_rounds` / `attempts_per_page` | `5` / `2` | 每页最大轮数和轮内即时尝试数 |

默认提示词要求忠实转写正文、表格和公式。只有两段自定义提示词都为空时才使用它。自定义模型只请求所选模型；显式填写 `model_fallbacks` 才启用替补，默认 Gemini 模型有同档替补。`model_fallbacks=none` 禁用替补。

Actions 的补发阈值和预算通过仓库 Variables `CONVERTER_HEDGE_AFTER` / `CONVERTER_HEDGE_BUDGET` 设置，默认 `60` / `-1`；填 `0` 可关闭。CLI 提供相应参数。

外部调用统一使用 `workflow_dispatch`，输入与手动运行相同：

```bash
gh workflow run pdf-to-md.yml --repo ActiveInsighter/file-converter-ai --ref main \
  -f conversion_type=pdf_to_md \
  -f request_id=your-task-id \
  -f source_url='https://example.com/document.pdf' \
  -f output_name='文档笔记'
```

运行标题为 `file-converter-pdf-to-md-<request_id>`，Artifact 名为 `file-converter-pdf-to-md-<run_id>`。AnyWorkflow 的私有 worker 派发任务、同步 GitHub 状态，将这两个 Markdown 文件的 ZIP 上传到受保护的任务文件字段。

本地运行：

```bash
pip install -r requirements.txt
export GEMINI_API_KEYS='你的密钥'
export GEMINI_QUOTA_API_URL='配额服务地址'
export GEMINI_QUOTA_API_TOKEN='配额服务令牌'
python pdf2md.py --source-url 'https://example.com/document.pdf' --output-name '文档笔记'
```

## 测试

```bash
pip install -r requirements.txt -r quota_api/requirements.txt
VALKEY_TEST_URL=redis://127.0.0.1:6379/15 python -m unittest discover -s tests
```

集成测试使用隔离的随机 Valkey namespace。CI 使用 Valkey service，执行全部测试及 Python 编译检查。
