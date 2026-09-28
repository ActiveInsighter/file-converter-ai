from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import fitz
import gdown
import httpx

from quota_state import ProjectQuotaPool, parse_key_groups

DEFAULT_PROMPT = """请按图片原始顺序逐页、忠实地把这些 PDF 页面转写为 Markdown。这是高精度文档转录任务，不是总结、改写或解题任务。

要求：
1. 不要遗漏正文、标题、列表、表格、公式、题目、选项、注释、评分等有效内容，并严格保持原始顺序。
2. 数学内容必须逐字符核对图片：重点检查正负号、等号/不等号、上下标、根号、分数、积分/求和上下限、导数阶数、矩阵元素、向量转置、希腊字母和括号。不要仅凭上下文猜公式。
3. 不要自行修正原文、补全推导或加入解释。看不清时写 [无法辨认]，不要编造。
4. 数学公式使用标准 LaTeX。行内公式只用成对的 $...$；多行公式、aligned、cases、matrix 等必须使用成对的 $$...$$，禁止用单个 $ 跨多行包裹。
5. 不要输出处理备注、模型解释、占位说明或类似“此行视具体排版”的元信息。
6. 忽略页眉、页脚、纯页码、扫描水印、装饰性字符和无意义的重复数字/编码；除非它们明显属于正文。
7. 表格尽量转换为 Markdown 表格；无法可靠转换时按原有阅读顺序保留文本，不要虚构表格结构。
8. 输出前自行复核一遍图片与 Markdown，尤其复核所有数学公式和数字。
9. 直接输出 Markdown，不要使用 Markdown 代码围栏。
"""
GOOGLE_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_INLINE_REQUEST_BYTES = 18 * 1024 * 1024


@dataclass(frozen=True)
class Chunk:
    start_page: int
    end_page: int
    image_paths: tuple[Path, ...]
    stem: str


class KeyPool:
    """Quota-aware scheduler for independent Gemini API keys.

    Each key is paced independently. With the default 15 RPM, a key is never
    assigned more often than once every 4 seconds. 429 responses can temporarily
    cool down or permanently exhaust one key for the current run.
    """

    def __init__(
        self,
        keys: list[str],
        rpm_per_key: float,
        rpd_per_key: int,
    ) -> None:
        if not keys:
            raise ValueError("No Gemini API keys configured")
        if rpm_per_key <= 0:
            raise ValueError("rpm_per_key must be > 0")
        if rpd_per_key <= 0:
            raise ValueError("rpd_per_key must be > 0")

        self.keys = keys
        # Keep a small safety margin for provider sliding-window accounting.
        self.interval = (60.0 / rpm_per_key) * 1.08
        self.rpd_per_key = rpd_per_key
        self.next_allowed = [0.0 for _ in keys]
        self.cooldown_until = [0.0 for _ in keys]
        self.used = [0 for _ in keys]
        self.disabled = [False for _ in keys]
        self.lock = asyncio.Lock()

    async def acquire(self) -> tuple[int, str]:
        while True:
            wait_for = 0.0
            async with self.lock:
                now = time.monotonic()
                candidates: list[tuple[float, int]] = []
                for index in range(len(self.keys)):
                    if self.disabled[index] or self.used[index] >= self.rpd_per_key:
                        continue
                    ready_at = max(
                        self.next_allowed[index],
                        self.cooldown_until[index],
                    )
                    candidates.append((ready_at, index))

                if not candidates:
                    raise RuntimeError(
                        "All Gemini API keys reached their configured per-run "
                        "daily limit or were disabled by quota errors."
                    )

                ready_at, index = min(candidates)
                if ready_at <= now:
                    self.used[index] += 1
                    self.next_allowed[index] = now + self.interval
                    return index, self.keys[index]

                wait_for = max(0.05, ready_at - now)

            await asyncio.sleep(wait_for)

    async def rate_limited(
        self,
        index: int,
        cooldown_seconds: float,
        daily_exhausted: bool = False,
    ) -> None:
        async with self.lock:
            if daily_exhausted:
                self.disabled[index] = True
                return
            self.cooldown_until[index] = max(
                self.cooldown_until[index],
                time.monotonic() + cooldown_seconds,
            )

    def usage_summary(self) -> list[int]:
        return list(self.used)


def parse_api_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    result = []
    seen = set()
    for item in re.split(r"[\r\n,;]+", raw):
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def is_google_drive_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "drive.google.com" or host.endswith(".drive.google.com")


def download_pdf(source_url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if is_google_drive_url(source_url):
        result = gdown.download(url=source_url, output=str(destination), quiet=False, fuzzy=True)
        if not result or not destination.exists():
            raise RuntimeError(
                "Google Drive download failed. Make sure the file is shared as Anyone with the link."
            )
        return

    with httpx.stream("GET", source_url, follow_redirects=True, timeout=120.0) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for data in response.iter_bytes(chunk_size=1024 * 1024):
                handle.write(data)


def render_pdf(
    pdf_path: Path,
    image_dir: Path,
    dpi: int,
    jpeg_quality: int,
    image_format: str,
    start_page: int | None,
    end_page: int | None,
) -> tuple[list[Path], int, int]:
    image_dir.mkdir(parents=True, exist_ok=True)
    document = fitz.open(pdf_path)
    total_pages = document.page_count
    if total_pages < 1:
        raise ValueError("PDF has no pages")

    width = max(3, len(str(total_pages)))
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    paths: list[Path] = []

    first = 1 if start_page is None else start_page
    last = total_pages if end_page is None else end_page
    if first < 1 or last > total_pages or first > last:
        document.close()
        raise ValueError(
            f"Invalid page range {first}-{last}; PDF has {total_pages} pages"
        )

    extension = "png" if image_format == "png" else "jpg"
    for page_number in range(first, last + 1):
        page = document[page_number - 1]
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        path = image_dir / f"{page_number:0{width}d}.{extension}"
        if image_format == "png":
            path.write_bytes(pix.tobytes("png"))
        else:
            path.write_bytes(pix.tobytes("jpeg", jpg_quality=jpeg_quality))
        paths.append(path)
        print(
            f"[render] {page_number}/{total_pages}: {path.name}",
            flush=True,
        )

    document.close()
    return paths, width, total_pages


def build_chunks(
    image_paths: list[Path], images_per_request: int, width: int
) -> list[Chunk]:
    chunks = []
    for offset in range(0, len(image_paths), images_per_request):
        batch = tuple(image_paths[offset : offset + images_per_request])
        start_page = int(batch[0].stem)
        end_page = int(batch[-1].stem)
        stem = (
            f"{start_page:0{width}d}"
            if start_page == end_page
            else f"{start_page:0{width}d}-{end_page:0{width}d}"
        )
        chunks.append(Chunk(start_page, end_page, batch, stem))
    return chunks


def media_resolution_payload(level: str) -> dict | None:
    if level == "unspecified":
        return None
    return {
        "level": f"MEDIA_RESOLUTION_{level.upper()}",
    }


def make_request_parts(
    chunk: Chunk, prompt: str, media_resolution: str
) -> tuple[list[dict], int]:
    page_label = (
        f"第 {chunk.start_page} 页"
        if chunk.start_page == chunk.end_page
        else f"第 {chunk.start_page}-{chunk.end_page} 页"
    )
    parts = [
        {
            "text": (
                f"以下图片按顺序对应 PDF {page_label}。\n"
                f"请严格按图片顺序处理。\n\n{prompt.strip()}"
            )
        }
    ]
    raw_bytes = 0

    for path in chunk.image_paths:
        data = path.read_bytes()
        raw_bytes += len(data)
        mime_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        image_part = {
            "inlineData": {
                "mimeType": mime_type,
                "data": base64.b64encode(data).decode("ascii"),
            }
        }
        resolution = media_resolution_payload(media_resolution)
        if resolution is not None:
            image_part["mediaResolution"] = resolution
        parts.append(image_part)

    estimated = int(raw_bytes * 4 / 3) + len(prompt.encode("utf-8")) + 64 * 1024
    if estimated > MAX_INLINE_REQUEST_BYTES:
        raise ValueError(
            f"Chunk {chunk.stem} is about {estimated / 1024 / 1024:.1f} MiB after base64, "
            "too large for inline image input. Lower images_per_request or dpi; for JPEG you can also lower jpeg_quality."
        )
    return parts, estimated


def make_verification_parts(
    chunk: Chunk, prompt: str, draft: str, media_resolution: str
) -> tuple[list[dict], int]:
    verify_prompt = f"""请逐页对照图片，审校下面这份 Markdown 草稿并直接返回修正后的完整 Markdown。

审校重点：
1. 逐字符核对所有数学公式：正负号、分数、根号、上下标、积分/求和上下限、矩阵、转置、希腊字母、括号和数字。
2. 检查是否有漏行、错行、重复、跨页错位、把页眉页脚或扫描水印误当正文。
3. 检查 Markdown/LaTeX 格式，行内公式使用成对 $...$，多行公式使用成对 $...$。
4. 只依据图片纠错，不要自行改写原文、推导新内容或加入解释。
5. 若草稿与图片冲突，以图片为准。看不清时写 [无法辨认]，不要猜。
6. 只输出最终修正后的 Markdown。

原始转录要求：
{prompt.strip()}

待审校草稿：
---BEGIN DRAFT---
{draft}
---END DRAFT---
"""
    parts = [{"text": verify_prompt}]
    raw_bytes = 0
    for path in chunk.image_paths:
        data = path.read_bytes()
        raw_bytes += len(data)
        mime_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        image_part = {
            "inlineData": {
                "mimeType": mime_type,
                "data": base64.b64encode(data).decode("ascii"),
            }
        }
        resolution = media_resolution_payload(media_resolution)
        if resolution is not None:
            image_part["mediaResolution"] = resolution
        parts.append(image_part)
    estimated = (
        int(raw_bytes * 4 / 3)
        + len(verify_prompt.encode("utf-8"))
        + 64 * 1024
    )
    if estimated > MAX_INLINE_REQUEST_BYTES:
        raise ValueError(
            f"Verification chunk {chunk.stem} is about "
            f"{estimated / 1024 / 1024:.1f} MiB after base64, too large. "
            "Lower images_per_request or dpi."
        )
    return parts, estimated


def extract_response_text(payload: dict) -> str:
    texts = []
    for candidate in payload.get("candidates", []):
        for part in (candidate.get("content") or {}).get("parts", []):
            if part.get("thought"):
                continue
            if part.get("text"):
                texts.append(part["text"])

    result = "\n".join(texts).strip()
    if not result:
        finish_reasons = [
            candidate.get("finishReason")
            for candidate in payload.get("candidates", [])
            if candidate.get("finishReason")
        ]
        raise RuntimeError(
            "Gemini returned no answer text. "
            f"finishReasons={finish_reasons}, "
            f"promptFeedback={payload.get('promptFeedback') or {}}"
        )

    if result.startswith("~~~markdown") and result.endswith("~~~"):
        result = result[len("~~~markdown") : -3].strip()
    if result.startswith("```markdown") and result.endswith("```"):
        result = result[len("```markdown") : -3].strip()
    elif result.startswith("```md") and result.endswith("```"):
        result = result[len("```md") : -3].strip()
    return result


def _single_dollar_positions(line: str) -> list[int]:
    positions = []
    i = 0
    while i < len(line):
        if line[i] != "$":
            i += 1
            continue
        if i > 0 and line[i - 1] == "\\":
            i += 1
            continue
        if i + 1 < len(line) and line[i + 1] == "$":
            i += 2
            continue
        if i > 0 and line[i - 1] == "$":
            i += 1
            continue
        positions.append(i)
        i += 1
    return positions


def repair_multiline_math_delimiters(text: str) -> tuple[str, list[int]]:
    """Join lines when an inline $...$ formula was split by a newline.

    This preserves the model's delimiters and only removes the newline inside
    an already-open inline-math span. It is safer than re-generating the page
    or changing the span to display math.
    """
    source_lines = text.splitlines()
    if not source_lines:
        return text, []

    output: list[str] = []
    inline_open = False

    for line in source_lines:
        if inline_open and output:
            output[-1] = output[-1].rstrip() + " " + line.lstrip()
        else:
            output.append(line)

        # Recompute state from the newly appended/current logical line.
        single_count = len(_single_dollar_positions(output[-1]))
        inline_open = single_count % 2 == 1

    result = "\n".join(output)
    remaining = [
        i + 1
        for i, line in enumerate(result.splitlines())
        if len(_single_dollar_positions(line)) % 2 == 1
    ]
    return result, remaining


def validate_markdown_output(text: str) -> None:
    """Reject clear formatting failures so the request can be retried."""
    if "\ufffd" in text:
        raise RuntimeError("Markdown contains Unicode replacement characters")

    if "此行视具体排版" in text:
        raise RuntimeError("Markdown contains model-side layout commentary")

    # Catch OCR/model artifacts such as a four-digit code repeated as its own
    # line many times in one response.
    numeric_lines = [
        line.strip()
        for line in text.splitlines()
        if re.fullmatch(r"\d{4,8}", line.strip())
    ]
    if numeric_lines:
        counts = {value: numeric_lines.count(value) for value in set(numeric_lines)}
        repeated = {value: count for value, count in counts.items() if count >= 3}
        if repeated:
            raise RuntimeError(
                f"Markdown contains repeated standalone numeric artifacts: {repeated}"
            )


async def call_gemini(
    client: httpx.AsyncClient,
    key_pool: ProjectQuotaPool,
    key_count: int,
    model: str,
    parts: list[dict],
    chunk_name: str,
    thinking_level: str,
) -> str:
    url = f"{GOOGLE_API_BASE}/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "thinkingConfig": {
                "thinkingLevel": thinking_level,
            }
        },
    }
    max_attempts = max(4, min(12, key_count * 2))
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        key_index, api_key = await key_pool.acquire()
        try:
            response = await client.post(
                url,
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if response.status_code == 200:
                try:
                    text = extract_response_text(response.json())
                    text, math_warnings = repair_multiline_math_delimiters(text)
                    if math_warnings:
                        print(
                            f"[warning] {chunk_name}: suspicious math delimiters "
                            f"remain on lines {math_warnings}; keeping content "
                            "instead of re-running the full page",
                            flush=True,
                        )
                    validate_markdown_output(text)
                    await key_pool.mark_success(key_index)
                    return text
                except RuntimeError as exc:
                    await key_pool.mark_error(key_index)
                    last_error = exc
                    print(
                        f"[retry] {chunk_name} attempt {attempt}/{max_attempts}: "
                        f"{exc}; rotating key",
                        flush=True,
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(min(2 ** (attempt - 1), 20))
                    continue

            message = response.text[:2000]
            if response.status_code in {400, 404}:
                await key_pool.mark_error(key_index)
                raise RuntimeError(
                    f"Gemini request rejected ({response.status_code}): {message}"
                )

            last_error = RuntimeError(
                f"Gemini HTTP {response.status_code}: {message}"
            )

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                cooldown_seconds = 60.0
                if retry_after:
                    try:
                        cooldown_seconds = max(
                            cooldown_seconds,
                            float(retry_after),
                        )
                    except ValueError:
                        pass

                retry_match = re.search(
                    r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"',
                    message,
                )
                if retry_match:
                    cooldown_seconds = max(
                        cooldown_seconds,
                        float(retry_match.group(1)),
                    )

                daily_exhausted = key_pool.is_daily_quota_message(
                    message
                )
                await key_pool.rate_limited(
                    key_index,
                    cooldown_seconds,
                    daily_exhausted=daily_exhausted,
                )
                quota_kind = (
                    "daily quota"
                    if daily_exhausted
                    else f"rate quota; cooldown={cooldown_seconds:.0f}s"
                )
                print(
                    f"[retry] {chunk_name} attempt {attempt}/{max_attempts}: "
                    f"HTTP 429 on key#{key_index + 1} ({quota_kind}); "
                    f"detail={message[:700]}",
                    flush=True,
                )
            else:
                await key_pool.mark_error(key_index)
                print(
                    f"[retry] {chunk_name} attempt {attempt}/{max_attempts}: "
                    f"HTTP {response.status_code}; rotating key",
                    flush=True,
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            await key_pool.mark_error(key_index)
            last_error = exc
            print(
                f"[retry] {chunk_name} attempt {attempt}/{max_attempts}: "
                f"{type(exc).__name__}; rotating key",
                flush=True,
            )

        if attempt < max_attempts:
            await asyncio.sleep(min(2 ** (attempt - 1), 20))

    raise RuntimeError(
        f"Chunk {chunk_name} failed after {max_attempts} attempts: {last_error}"
    )


async def process_chunks(
    chunks: list[Chunk],
    output_dir: Path,
    prompt: str,
    model: str,
    concurrency: int,
    keys: list[str],
    thinking_level: str,
    verification_passes: int,
    media_resolution: str,
    rpm_per_key: float,
    rpd_per_key: int,
) -> list[dict]:
    pages_dir = output_dir / "pages"
    errors_dir = output_dir / "errors"
    pages_dir.mkdir(parents=True, exist_ok=True)
    errors_dir.mkdir(parents=True, exist_ok=True)

    groups = parse_key_groups(
        len(keys),
        os.getenv("GEMINI_KEY_GROUPS"),
    )
    print(
        f"[quota] keys={len(keys)} projects={len(set(groups))} "
        f"mapping={groups}",
        flush=True,
    )
    key_pool = await ProjectQuotaPool.create(
        keys,
        groups,
        rpm_per_project=rpm_per_key,
        rpd_per_project=rpd_per_key,
    )
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(240.0, connect=30.0),
        limits=httpx.Limits(
            max_connections=max(100, concurrency * 2),
            max_keepalive_connections=max(50, concurrency),
            keepalive_expiry=30.0,
        ),
    ) as client:

        async def run_one(chunk: Chunk) -> dict:
            md_path = pages_dir / f"{chunk.stem}.md"
            error_path = errors_dir / f"{chunk.stem}.json"
            started = time.time()

            try:
                async with semaphore:
                    parts, estimated = make_request_parts(
                        chunk, prompt, media_resolution
                    )
                    text = await call_gemini(
                        client,
                        key_pool,
                        len(keys),
                        model,
                        parts,
                        chunk.stem,
                        thinking_level,
                    )

                for verify_index in range(verification_passes):
                    async with semaphore:
                        verify_parts, _ = make_verification_parts(
                            chunk, prompt, text, media_resolution
                        )
                        text = await call_gemini(
                            client,
                            key_pool,
                            len(keys),
                            model,
                            verify_parts,
                            f"{chunk.stem}-verify-{verify_index + 1}",
                            thinking_level,
                        )

                md_path.write_text(text.rstrip() + "\n", encoding="utf-8")
                if error_path.exists():
                    error_path.unlink()

                elapsed = round(time.time() - started, 2)
                print(f"[done] {chunk.stem}.md ({elapsed}s)", flush=True)
                return {
                    "chunk": chunk.stem,
                    "status": "ok",
                    "path": str(md_path),
                    "estimated_payload_bytes": estimated,
                    "elapsed_seconds": elapsed,
                }
            except Exception as exc:
                error = {
                    "chunk": chunk.stem,
                    "start_page": chunk.start_page,
                    "end_page": chunk.end_page,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                error_path.write_text(
                    json.dumps(error, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(
                    f"[failed] {chunk.stem}: {error['error']}",
                    file=sys.stderr,
                    flush=True,
                )
                return {"chunk": chunk.stem, "status": "failed", **error}

        tasks = [asyncio.create_task(run_one(chunk)) for chunk in chunks]
        results = []
        for future in asyncio.as_completed(tasks):
            results.append(await future)
        await key_pool.close()
        quota_summary = key_pool.usage_summary()
        (output_dir / "quota-usage.json").write_text(
            json.dumps(quota_summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            "[quota] " + json.dumps(
                quota_summary,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        return results


def merge_markdown(
    chunks: list[Chunk], pages_dir: Path, destination: Path
) -> None:
    def sort_key(path: Path) -> tuple[int, str]:
        match = re.match(r"(\\d+)", path.stem)
        return (int(match.group(1)) if match else 10**9, path.name)

    page_files = sorted(pages_dir.glob("*.md"), key=sort_key)
    blocks = [
        path.read_text(encoding="utf-8").strip()
        for path in page_files
        if path.stat().st_size > 0
    ]
    destination.write_text(
        "\n\n".join(blocks).rstrip() + "\n", encoding="utf-8"
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--conversion-type",
        choices=["pdf_to_md"],
        default="pdf_to_md",
        help="Conversion handler selected by the generic file-converter workflow.",
    )
    p.add_argument("--source-url", required=True)
    p.add_argument("--images-per-request", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=50)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--model", default="gemini-3.5-flash-lite")
    p.add_argument(
        "--thinking-level",
        choices=["minimal", "low", "medium", "high"],
        default="high",
        help="Gemini 3 thinking level. Default: high.",
    )
    p.add_argument("--dpi", type=int, default=240)
    p.add_argument(
        "--image-format",
        choices=["png", "jpeg"],
        default="png",
        help="Rendered page image format. PNG is lossless and is the default.",
    )
    p.add_argument("--jpeg-quality", type=int, default=95)
    p.add_argument(
        "--verification-passes",
        type=int,
        default=0,
        help="Extra image-vs-Markdown verification passes after initial transcription.",
    )
    p.add_argument(
        "--media-resolution",
        choices=["unspecified", "low", "medium", "high", "ultra_high"],
        default="ultra_high",
        help="Gemini per-image media resolution. ultra_high allocates the most vision detail.",
    )
    p.add_argument(
        "--rpm-per-key",
        type=float,
        default=15.0,
        help="Compatibility name: maximum requests/minute for each project quota pool.",
    )
    p.add_argument(
        "--rpd-per-key",
        type=int,
        default=500,
        help="Compatibility name: requests/day guard for each project quota pool.",
    )
    p.add_argument(
        "--start-page",
        type=int,
        default=None,
        help="1-based first PDF page to process. Default: first page.",
    )
    p.add_argument(
        "--end-page",
        type=int,
        default=None,
        help="1-based last PDF page to process. Default: last page.",
    )
    p.add_argument("--work-dir", default="work")
    p.add_argument("--output-dir", default="output")
    return p


def validate_args(args: argparse.Namespace) -> None:
    if args.conversion_type != "pdf_to_md":
        raise ValueError("conversion_type must be pdf_to_md")
    if not 1 <= args.images_per_request <= 20:
        raise ValueError("images_per_request must be between 1 and 20")
    if not 1 <= args.concurrency <= 100:
        raise ValueError("concurrency must be between 1 and 100")
    if not 72 <= args.dpi <= 300:
        raise ValueError("dpi must be between 72 and 300")
    if not 50 <= args.jpeg_quality <= 100:
        raise ValueError("jpeg_quality must be between 50 and 100")
    if args.image_format not in {"png", "jpeg"}:
        raise ValueError("image_format must be png or jpeg")
    if not 0 <= args.verification_passes <= 3:
        raise ValueError("verification_passes must be between 0 and 3")
    if args.thinking_level not in {"minimal", "low", "medium", "high"}:
        raise ValueError("thinking_level must be minimal, low, medium, or high")
    if args.start_page is not None and args.start_page < 1:
        raise ValueError("start_page must be >= 1")
    if args.end_page is not None and args.end_page < 1:
        raise ValueError("end_page must be >= 1")
    if (
        args.start_page is not None
        and args.end_page is not None
        and args.start_page > args.end_page
    ):
        raise ValueError("start_page must be <= end_page")
    if args.rpm_per_key <= 0:
        raise ValueError("rpm_per_key must be > 0")
    if args.rpd_per_key <= 0:
        raise ValueError("rpd_per_key must be > 0")
    if args.media_resolution not in {
        "unspecified",
        "low",
        "medium",
        "high",
        "ultra_high",
    }:
        raise ValueError(
            "media_resolution must be unspecified, low, medium, high, or ultra_high"
        )


async def async_main(args: argparse.Namespace) -> int:
    validate_args(args)
    keys = parse_api_keys(os.getenv("GEMINI_API_KEYS"))
    if not keys:
        raise RuntimeError(
            "GEMINI_API_KEYS is empty. Add a repository Actions secret with one Gemini API key per line."
        )

    work_dir = Path(args.work_dir)
    output_dir = Path(args.output_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_pdf = work_dir / "source.pdf"
    images_dir = work_dir / "images"

    print(
        f"[config] conversion_type={args.conversion_type} model={args.model} keys={len(keys)} "
        f"concurrency={args.concurrency} "
        f"images_per_request={args.images_per_request} "
        f"thinking_level={args.thinking_level} "
        f"image_format={args.image_format} dpi={args.dpi} "
        f"verification_passes={args.verification_passes} "
        f"media_resolution={args.media_resolution} "
        f"rpm_per_key={args.rpm_per_key:g} rpd_per_key={args.rpd_per_key} "
        f"page_range={args.start_page or 1}-{args.end_page or 'end'}",
        flush=True,
    )
    print(f"[download] {args.source_url}", flush=True)
    await asyncio.to_thread(download_pdf, args.source_url, source_pdf)
    with source_pdf.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise RuntimeError(
                "Downloaded source is not a PDF. Use a direct/public PDF link; ZIP and other files are not accepted."
            )

    image_paths, width, pdf_total_pages = await asyncio.to_thread(
        render_pdf,
        source_pdf,
        images_dir,
        args.dpi,
        args.jpeg_quality,
        args.image_format,
        args.start_page,
        args.end_page,
    )
    chunks = build_chunks(image_paths, args.images_per_request, width)
    print(
        f"[plan] pages={len(image_paths)} chunks={len(chunks)}",
        flush=True,
    )

    results = await process_chunks(
        chunks,
        output_dir,
        args.prompt,
        args.model,
        args.concurrency,
        keys,
        args.thinking_level,
        args.verification_passes,
        args.media_resolution,
        args.rpm_per_key,
        args.rpd_per_key,
    )
    failures = [x for x in results if x.get("status") == "failed"]

    merged_name = "merged.md" if not failures else "merged.partial.md"
    merge_markdown(chunks, output_dir / "pages", output_dir / merged_name)

    manifest = {
        "conversion_type": args.conversion_type,
        "source_url": args.source_url,
        "model": args.model,
        "total_pages": pdf_total_pages,
        "processed_pages": len(image_paths),
        "start_page": args.start_page,
        "end_page": args.end_page,
        "images_per_request": args.images_per_request,
        "concurrency": args.concurrency,
        "thinking_level": args.thinking_level,
        "dpi": args.dpi,
        "image_format": args.image_format,
        "jpeg_quality": args.jpeg_quality,
        "verification_passes": args.verification_passes,
        "media_resolution": args.media_resolution,
        "rpm_per_key": args.rpm_per_key,
        "rpd_per_key": args.rpd_per_key,
        "chunks": [
            {
                **asdict(chunk),
                "image_paths": [str(p) for p in chunk.image_paths],
            }
            for chunk in chunks
        ],
        "results": sorted(results, key=lambda item: item["chunk"]),
        "failures": len(failures),
        "merged_file": merged_name,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if failures:
        print(
            f"[result] {len(failures)} chunk(s) failed; "
            f"partial output: {merged_name}",
            file=sys.stderr,
        )
        return 2

    print(f"[result] success: {output_dir / 'merged.md'}", flush=True)
    return 0


def main() -> int:
    args = parser().parse_args()
    try:
        return asyncio.run(async_main(args))
    except Exception as exc:
        print(f"[fatal] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
