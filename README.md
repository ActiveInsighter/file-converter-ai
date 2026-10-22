# AnyWorkflow File Converter

PDF → Markdown 转换执行器。GitHub Actions 下载 PDF，先用百度 PaddleOCR-VL 识别每一页，再把每页的 OCR 草稿和页面图片一起发给视觉模型（默认 Gemini 3.5 Flash-Lite）校对、整理成最终 Markdown。

## 处理流程

1. **下载**：支持直接 HTTP(S) PDF 地址和 Google Drive 公开分享链接。
2. **OCR**：PDF 按 25 页切段，单段超过 1 MiB 时继续对半拆分（从 GitHub Runner 上传大文件到百度会卡顿：375 KiB 约 3 秒，1.8 MB 约 90 秒），最多 3 段同时提交到 PaddleOCR-VL-1.6 异步接口（服务端每个用户只接受少量排队任务，超出会返回 10010）。每段完成后立即放出各页的 Markdown 草稿和图表裁图。
3. **渲染**：同时在独立线程中逐页渲染图片（默认 240 DPI PNG）。空白扫描页直接跳过，不发请求。
4. **校对**：某页的图片和草稿都就绪后，发送给模型。模型以图片为准、以草稿为底稿：保留正确文字，修正错字、公式与上下标，补全遗漏，删除页眉页脚，整理标题、段落、表格结构，保留草稿中的图片引用。
5. **兜底**：
   - 模型拒答（RECITATION/SAFETY 等）、空回答，或重试轮次用尽时，该页直接使用 OCR 草稿。
   - 某段 OCR 失败（重试 3 次）或未配置 OCR 时，该页退回为仅凭图片转写。
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
- 429：该桶冷却 20 秒，连续 429 时依次翻倍（最多 300 秒）；服务端给出更长的 `retryDelay` 时以服务端为准。成功一次后清零。
- 每日额度耗尽（RPD）或预付费额度用完（HTTP 402）：该桶在本次任务中停用，页面换其他 Key 重试。`rpd_per_key` 是单次任务内每个桶的请求上限。
- 近期 429/503 占比超过 20% 时不再补发副本。

PaddleOCR 支持多个 Token（每行一个），按任务轮流使用；同一任务始终用提交它的 Token 查询。限流（12002）、队列已满（10010）和服务端错误会指数退避重试（每段最多 6 次，间隔最长 60 秒）；Token 无效或每日页数用尽（12001）时停止提交，剩余页面转为仅图片转写。

`quota-usage.json` 和 Actions Summary 记录每个桶的请求数分布、429/503 次数、各页来源（OCR+模型 / 仅模型 / 仅 OCR）和 OCR 耗时。

## 配置

| 名称 | 类型 | 用途 |
| --- | --- | --- |
| `GEMINI_API_KEYS` | Secret | Gemini Key，每行一个 |
| `GEMINI_KEY_GROUPS` | Variable，可选 | 每个 Key 所属的 Project，与 Key 一一对应；可用 `deploy/print-key-groups.py` 生成 |
| `BAIDU_AI_STUDIO` | Secret | AI Studio 访问令牌，可多行；工作流以 `PADDLEOCR_TOKENS` 传给转换器 |
| `CONVERTER_OCR` | Variable，可选 | `paddle`（默认）或 `none` |
| `CONVERTER_OCR_JOBS` | Variable，可选 | 同时进行的 OCR 任务数，默认 `3`；调大上传更快，但更容易收到 10010 |
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
| `images_per_request` | — | 已弃用，保留只为兼容旧调用方；现在固定每页一个请求 |

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

CLI 另有 `--ocr none`、`--ocr-model`、`--ocr-segment-pages`、`--ocr-url`、`--hedge-after`、`--hedge-budget` 等参数，见 `python pdf2md.py --help`。

## 耗时

实测（GitHub Runner，Gemini 3.5 Flash-Lite，66 个 Key）：75 页论文约 7 分钟完成，全部页面为“OCR + 模型校对”，零 429/503。瓶颈在 OCR：Runner 到百度的上传约 20–25 KB/s，单个 OCR 任务在服务端排队 30–150 秒。模型请求本身 P50 约 13 秒。需要更快时可调大 `CONVERTER_OCR_JOBS`，或设 `CONVERTER_OCR=none` 回到纯图片转写。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `pdf2md.py` | 命令行入口、请求构造、单页流程与兜底 |
| `paddle_ocr.py` | PaddleOCR 异步任务：切段、提交、轮询、解析、下载图表 |
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

测试不访问外网：OCR 接口、模型网关和 PDF 下载都由本地假服务模拟。
