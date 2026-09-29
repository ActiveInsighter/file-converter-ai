from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import fitz
import gdown
import httpx

from quota_client import (
    LocalKeyPool,
    ProjectQuotaPool,
    QuotaPoolExhaustedError,
    parse_key_groups,
)
from providers import (
    GEMINI,
    Provider,
    ProviderRefusal,
    normalize_reasoning_effort,
    provider_names,
    resolve_model,
    resolve_provider,
)

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
9. 直接输出 Markdown，不要使用 Markdown 代码围栏。"""
# The prompt that actually runs is the DB-backed system prompt plus this
# conversion's own prompt, joined by a blank line. Both are optional and
# independent, and each is capped so the pair stays well inside the inline
# request budget (the same bound the PocketBase config enforces).
MAX_PROMPT_CHARS = 12000


def compose_prompt(system_prompt: str, user_prompt: str) -> str:
    """Join the shared system prompt with this conversion's own prompt.

    ``system_prompt`` is read from the saved file-conversion config and applies
    to every conversion; ``user_prompt`` is the per-task instruction. Either may
    be empty and neither overrides the other. When both are empty the built-in
    faithful Markdown prompt is used, so a manual workflow dispatch still
    produces a faithful transcription.
    """
    parts = [part.strip() for part in (system_prompt, user_prompt)]
    composed = "\n\n".join(part for part in parts if part)
    return composed or DEFAULT_PROMPT


MAX_INLINE_REQUEST_BYTES = 18 * 1024 * 1024

# A page counts as an empty scan page when it has almost no ink *and* nothing
# dark on it. Both halves of the test matter: a page with one faint line is not
# blank, while scanner speckle on an otherwise white page still is. Measured
# corpus margin: blank pages <= 0.004% ink with no pixel darker than 249, real
# pages >= 4.8% ink with a darkest pixel <= 128.
BLANK_PAGE_INK_RATIO = 0.001
BLANK_PAGE_DARK_LEVEL = 200
# Any pixel darker than this counts as ink when measuring page coverage.
INK_PIXEL_MAX = 250
# Pages are measured on a cheap downscaled grayscale pixmap instead of the full
# resolution render.
SPLIT_ANALYSIS_SCALE = 0.25
# Vertical bands (fractions of the page height) searched for a blank cut row,
# most balanced first. A cut on a row with no ink never slices a text line.
SPLIT_SEARCH_WINDOWS = ((0.35, 0.65), (0.25, 0.75), (0.15, 0.85))
# Overlap given to both halves when the page has no blank row in any band.
SPLIT_FALLBACK_OVERLAP = 0.01
# How many times a refused image may be cut again: 2 means page -> halves ->
# halves of a refused half, so a stubborn page costs at most 1 + 2 + 2 requests
# and never grows without bound.
PAGE_SPLIT_DEPTH = 2


@dataclass(frozen=True)
class Chunk:
    start_page: int
    end_page: int
    image_paths: tuple[Path, ...]
    stem: str
    blank: bool = False


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


def parse_model_list(raw: str | None) -> tuple[str, ...]:
    """Parse a comma/semicolon separated fallback chain, keeping first position."""
    if not raw:
        return ()
    return tuple(
        dict.fromkeys(
            item.strip()
            for item in re.split(r"[,;\s]+", raw)
            if item.strip()
        )
    )


def resolve_fallback_models(provider: Provider, model: str, raw: str | None) -> tuple[str, ...]:
    """Never replace an explicitly chosen model with a weaker default fallback."""
    if raw and raw.strip().lower() in {"none", "off"}:
        return ()
    explicit = parse_model_list(raw)
    if explicit:
        return explicit
    return provider.default_model_fallbacks if model == provider.default_model else ()


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
) -> tuple[list[Path], int, int, set[int]]:
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
    blank_pages: set[int] = set()
    for page_number in range(first, last + 1):
        page = document[page_number - 1]
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        path = image_dir / f"{page_number:0{width}d}.{extension}"
        if image_format == "png":
            path.write_bytes(pix.tobytes("png"))
        else:
            path.write_bytes(pix.tobytes("jpeg", jpg_quality=jpeg_quality))
        paths.append(path)
        blank, ink_ratio, darkest = is_blank_page(page)
        if blank:
            blank_pages.add(page_number)
            print(
                f"[render] {page_number}/{total_pages}: {path.name} "
                f"blank ink_ratio={ink_ratio:.6f} darkest={darkest}",
                flush=True,
            )
            continue
        print(
            f"[render] {page_number}/{total_pages}: {path.name}",
            flush=True,
        )

    document.close()
    return paths, width, total_pages, blank_pages


def page_ink_stats(page: fitz.Page) -> tuple[float, int]:
    """Ink coverage and darkest pixel of a cheap downscaled page preview."""
    pix = page.get_pixmap(
        matrix=fitz.Matrix(SPLIT_ANALYSIS_SCALE, SPLIT_ANALYSIS_SCALE),
        colorspace=fitz.csGRAY,
    )
    samples = pix.samples
    if not samples:
        return 0.0, 255
    ink = sum(1 for value in samples if value < INK_PIXEL_MAX) / len(samples)
    return ink, min(samples)


def is_blank_page(page: fitz.Page) -> tuple[bool, float, int]:
    """Is this an empty scan page, and what did the measurement see?"""
    ink, darkest = page_ink_stats(page)
    blank = ink < BLANK_PAGE_INK_RATIO and darkest >= BLANK_PAGE_DARK_LEVEL
    return blank, ink, darkest


def _page_ink_rows(page: fitz.Page) -> tuple[list[int], int]:
    """Per-row ink pixel counts of the preview pixmap."""
    pix = page.get_pixmap(
        matrix=fitz.Matrix(SPLIT_ANALYSIS_SCALE, SPLIT_ANALYSIS_SCALE),
        colorspace=fitz.csGRAY,
    )
    width, height, samples = pix.width, pix.height, pix.samples
    rows = [
        sum(1 for value in samples[y * width : (y + 1) * width] if value < INK_PIXEL_MAX)
        for y in range(height)
    ]
    return rows, height


def choose_split_row(rows: list[int], height: int) -> tuple[int, bool]:
    """Pick the cut row of a page as (row, clean).

    ``row`` is in preview coordinates and is the blank row closest to the middle
    of the page, so the two halves stay balanced. ``clean`` is True when that row
    carries no ink at all, i.e. no line of text is sliced by the cut; otherwise
    the caller has to overlap the halves a little.
    """
    center = height / 2
    for low, high in SPLIT_SEARCH_WINDOWS:
        start, stop = int(height * low), int(height * high)
        if stop <= start:
            continue
        blank_rows = [y for y in range(start, stop) if rows[y] == 0]
        if blank_rows:
            return min(blank_rows, key=lambda y: abs(y - center)), True

    start, stop = int(height * 0.35), int(height * 0.65)
    if stop <= start:
        return height // 2, False
    return min(range(start, stop), key=lambda y: rows[y]), False


def native_page_scale(page: fitz.Page) -> float:
    """Page points to image pixels, so a clipped render keeps the source detail.

    A rendered page image is an image document whose rect comes from its DPI
    metadata instead of its pixel count: rendering a clip at the identity matrix
    silently drops 25% of the resolution.
    """
    best = None
    for entry in page.get_image_info():
        width = entry.get("width") or 0
        bbox = entry.get("bbox") or (0, 0, 0, 0)
        placed_width = bbox[2] - bbox[0]
        if width and placed_width > 0:
            ratio = width / placed_width
            if best is None or ratio > best:
                best = ratio
    return best or 1.0


def split_page_image(image_path: Path, target_dir: Path) -> tuple[list[Path], bool]:
    """Cut one rendered page into two horizontal halves on a blank row.

    A content refusal is triggered by recognising a whole passage, so the same
    page usually transcribes fine once it is cropped. Halves are written as PNG
    at the source resolution, next to the page they come from.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    source = fitz.open(image_path)
    try:
        if source.page_count < 1:
            raise ValueError(f"{image_path} has no image page")
        page = source[0]
        rows, preview_height = _page_ink_rows(page)
        blank_row, clean = choose_split_row(rows, preview_height)
        rect = page.rect
        cut = rect.y0 + blank_row / SPLIT_ANALYSIS_SCALE
        overlap = 0.0 if clean else rect.height * SPLIT_FALLBACK_OVERLAP

        scale = native_page_scale(page)
        matrix = fitz.Matrix(scale, scale)
        clips = (
            fitz.Rect(rect.x0, rect.y0, rect.x1, cut + overlap),
            fitz.Rect(rect.x0, cut - overlap, rect.x1, rect.y1),
        )
        halves: list[Path] = []
        for index, clip in enumerate(clips, start=1):
            pix = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
            path = target_dir / f"{image_path.stem}-part{index}.png"
            path.write_bytes(pix.tobytes("png"))
            halves.append(path)
        return halves, clean
    finally:
        source.close()


def build_chunks(
    image_paths: list[Path],
    images_per_request: int,
    width: int,
    blank_pages: frozenset[int] | set[int] = frozenset(),
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
        blank = all(int(path.stem) in blank_pages for path in batch)
        chunks.append(Chunk(start_page, end_page, batch, stem, blank))
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


def _strip_markdown_fences(result: str) -> str:
    if result.startswith("~~~markdown") and result.endswith("~~~"):
        result = result[len("~~~markdown") : -3].strip()
    if result.startswith("```markdown") and result.endswith("```"):
        result = result[len("```markdown") : -3].strip()
    elif result.startswith("```md") and result.endswith("```"):
        result = result[len("```md") : -3].strip()
    return result


def _refusal_error(refusal: ProviderRefusal) -> Exception:
    """Map a provider refusal onto the retry policy's error taxonomy.

    A refusal is a property of the page image (or, for a truncated answer, of
    our own output cap), so none of these may be retried as-is.
    """
    if refusal.kind == "blocked":
        return ContentBlockedError(refusal.detail)
    if refusal.kind == "truncated":
        return PermanentProviderError(refusal.detail)
    return EmptyAnswerError(refusal.detail)


def answer_from_payload(provider: Provider, payload: dict) -> str:
    """One provider answer -> Markdown, or a typed error the retry loop knows."""
    try:
        text = provider.extract_text(payload)
    except ProviderRefusal as refusal:
        raise _refusal_error(refusal) from refusal
    return _strip_markdown_fences(text)


def extract_response_text(payload: dict) -> str:
    """Gemini-shaped response -> Markdown.

    Kept under its original name because it is the Gemini entry point used by
    the existing test-suite; multi-provider callers go through
    :func:`answer_from_payload`.
    """
    return answer_from_payload(GEMINI, payload)


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


class PermanentProviderError(RuntimeError):
    """A request rejected for a non-transient client or configuration error.

    A bad key, a bad request, no permission, no credit, or an answer that ran
    into the model's output limit: none of them is fixed by sending the very
    same request again, so this must never enter the backoff rounds.
    """


# Kept as an alias because the error predates multi-provider support and both
# the test-suite and older logs refer to it by this name.
PermanentGeminiError = PermanentProviderError


class ContentBlockedError(RuntimeError):
    """Gemini answered a page without any usable text.

    Either ``finishReason`` is a content-refusal value (``RECITATION`` and
    friends) or the prompt itself was blocked. The verdict is a property of the
    page image, not of the quota pool: re-sending the same image reproduces it
    on every attempt, so this error must never enter the backoff rounds.
    ``split_page_image`` recovers many of these pages instead.
    """


class EmptyAnswerError(ContentBlockedError):
    """The model stopped without emitting any text for a page image.

    Seen on blank scan pages (nothing to transcribe), on figure-only pages and
    on responses whose whole output landed in thinking parts. Retrying the same
    image does not change it; cutting the page in half usually does.
    """


def is_retryable_error(exc: BaseException) -> bool:
    """Only capacity, transport and formatting errors are worth another round."""
    return not isinstance(
        exc,
        (
            PermanentProviderError,
            QuotaPoolExhaustedError,
            ValueError,
            ContentBlockedError,
        ),
    )


class ModelUnavailableError(RuntimeError):
    """The model itself is not serving the request right now.

    Google's free tier answers 503 ``UNAVAILABLE`` ("This model is currently
    experiencing high demand") and sometimes 404 for a whole model while the
    API key and the Cloud project are perfectly healthy. Both must be retried
    and must be able to fall through to another model instead of failing a page.
    """


async def call_model_once(
    client: httpx.AsyncClient,
    key_pool,
    provider: Provider,
    model: str,
    parts: list[dict],
    chunk_name: str,
    thinking_level: str,
    reasoning_effort: str = "",
    api_base: str = "",
    max_output_tokens: int = 0,
) -> str:
    """Send one request through one provider and return its Markdown.

    Everything protocol-specific -- URL, auth header, request body, response
    shape and error taxonomy -- is delegated to ``provider``. This function owns
    only what the retry policy depends on: lease bookkeeping, metrics and the
    exception type raised for each class of failure.
    """
    url = provider.endpoint(api_base or provider.default_base_url, model)
    payload = provider.build_payload(
        model,
        parts,
        thinking_level=thinking_level,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
    )
    key_index, api_key, lease_id = await key_pool.acquire()
    started = time.monotonic()
    try:
        response = await client.post(
            url,
            headers=provider.build_headers(api_key),
            json=payload,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        key_pool.record_http_result(0, time.monotonic() - started)
        await key_pool.mark_error(key_index, lease_id, http_status=0)
        raise RuntimeError(
            f"{provider.label} transport failure: {type(exc).__name__}: {exc}"
        ) from exc

    key_pool.record_http_result(response.status_code, time.monotonic() - started)
    if response.status_code == 200:
        try:
            text = answer_from_payload(provider, response.json())
            text, math_warnings = repair_multiline_math_delimiters(text)
            if math_warnings:
                print(
                    f"[warning] {chunk_name}: suspicious math delimiters remain on "
                    f"lines {math_warnings}; retaining this output for review",
                    flush=True,
                )
            validate_markdown_output(text)
        except (ContentBlockedError, PermanentProviderError):
            # The request itself was fine: the model refused this page image, or
            # the answer was unusable for a reason a retry cannot fix. Release
            # the lease cleanly and keep the error type, otherwise the wrapper
            # below turns it into a retryable RuntimeError and the page burns
            # every retry round on the same verdict.
            await key_pool.mark_success(key_index, lease_id)
            raise
        except Exception as exc:
            await key_pool.mark_error(key_index, lease_id, http_status=502)
            raise RuntimeError(
                f"{provider.label} returned unusable Markdown: {exc}"
            ) from exc
        await key_pool.mark_success(key_index, lease_id)
        return text

    message = response.text[:800]
    try:
        error_payload: object = response.json()
    except ValueError:
        error_payload = {"message": message}

    kind = provider.status_error_kind(response.status_code, error_payload)
    if kind == "rate_limited":
        quota_type = provider.quota_kind(error_payload)
        retry_after = provider.retry_after(response.headers, error_payload)
        cooldown_seconds = retry_after if retry_after is not None else 30.0
        await key_pool.rate_limited(
            key_index,
            lease_id,
            cooldown_seconds,
            daily_exhausted=quota_type == "rpd",
            quota_type=quota_type,
        )
        raise RuntimeError(
            f"{provider.label} HTTP 429 ({quota_type}; "
            f"project cooldown={cooldown_seconds:.1f}s): {message}"
        )

    await key_pool.mark_error(key_index, lease_id, http_status=response.status_code)
    error = f"{provider.label} HTTP {response.status_code}: {message}"
    if kind == "permanent":
        raise PermanentProviderError(error)
    if kind == "unavailable":
        raise ModelUnavailableError(error)
    raise RuntimeError(error)


async def call_model(
    client: httpx.AsyncClient,
    key_pool,
    provider: Provider,
    model: str,
    parts: list[dict],
    chunk_name: str,
    thinking_level: str,
    fallback_models: tuple[str, ...] = (),
    reasoning_effort: str = "",
    api_base: str = "",
    max_output_tokens: int = 0,
) -> str:
    """Transcribe one chunk, walking the fallback chain when a model is saturated.

    A single model can be unavailable for hours while its siblings keep serving,
    so trying the next model is the cheapest way to turn a hard page failure
    into a slower success.
    """
    candidates = [model, *(item for item in fallback_models if item and item != model)]
    last_error: Exception | None = None
    for position, candidate in enumerate(candidates):
        try:
            return await call_model_once(
                client,
                key_pool,
                provider,
                candidate,
                parts,
                chunk_name,
                thinking_level,
                reasoning_effort,
                api_base,
                max_output_tokens,
            )
        except ModelUnavailableError as exc:
            last_error = exc
            if position + 1 < len(candidates):
                print(
                    f"[fallback] {chunk_name}: {candidate} -> "
                    f"{type(exc).__name__}; trying {candidates[position + 1]}",
                    flush=True,
                )
    if last_error is None:
        raise RuntimeError(f"No {provider.label} model configured for this request")
    raise last_error


async def call_gemini_once(
    client: httpx.AsyncClient,
    key_pool,
    model: str,
    parts: list[dict],
    chunk_name: str,
    thinking_level: str,
) -> str:
    """Gemini entry point kept for callers that predate multi-provider support."""
    return await call_model_once(
        client, key_pool, GEMINI, model, parts, chunk_name, thinking_level
    )


async def call_gemini(
    client: httpx.AsyncClient,
    key_pool,
    model: str,
    parts: list[dict],
    chunk_name: str,
    thinking_level: str,
    fallback_models: tuple[str, ...] = (),
) -> str:
    """Gemini entry point kept for callers that predate multi-provider support."""
    return await call_model(
        client,
        key_pool,
        GEMINI,
        model,
        parts,
        chunk_name,
        thinking_level,
        fallback_models,
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
    fallback_models: tuple[str, ...] = (),
    retry_rounds: int = 5,
    attempts_per_page: int = 2,
    blocked_page_split: str = "halves",
    provider: Provider | None = None,
    reasoning_effort: str = "",
    api_base: str = "",
    max_output_tokens: int = 0,
) -> list[dict]:
    if blocked_page_split not in {"none", "halves"}:
        raise ValueError("blocked_page_split must be none or halves")

    provider = provider or resolve_provider(None)

    pages_dir = output_dir / "pages"
    errors_dir = output_dir / "errors"
    split_dir = output_dir / "split"
    pages_dir.mkdir(parents=True, exist_ok=True)
    errors_dir.mkdir(parents=True, exist_ok=True)

    if provider.uses_shared_quota_pool:
        groups = parse_key_groups(
            len(keys),
            os.getenv("GEMINI_KEY_GROUPS"),
        )
        print(f"[quota] keys={len(keys)} projects={len(set(groups))}", flush=True)
    else:
        groups = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(provider.request_timeout_seconds, connect=30.0),
        limits=httpx.Limits(
            max_connections=max(100, concurrency * 2),
            max_keepalive_connections=max(50, concurrency),
            keepalive_expiry=30.0,
        ),
    ) as client:
        if provider.uses_shared_quota_pool:
            key_pool = await ProjectQuotaPool.create(
                keys,
                groups,
                rpm_per_project=rpm_per_key,
                rpd_per_project=rpd_per_key,
                client=client,
            )
        else:
            key_pool = await LocalKeyPool.create(
                keys,
                rpm_per_key=rpm_per_key,
                rpd_per_key=rpd_per_key,
                client=client,
            )

        started_at = {chunk.stem: time.monotonic() for chunk in chunks}
        chunks_by_name = {chunk.stem: chunk for chunk in chunks}
        latest_results: dict[str, dict] = {}
        completed_first_pass: set[str] = set()
        progress = {"active_chunks": 0}
        stop_metrics = asyncio.Event()

        def record_success(
            chunk: Chunk, text: str, estimated: int, note: str = "", **extra
        ) -> dict:
            md_path = pages_dir / f"{chunk.stem}.md"
            error_path = errors_dir / f"{chunk.stem}.json"
            md_path.write_text(text.rstrip() + "\n", encoding="utf-8")
            if error_path.exists():
                error_path.unlink()
            elapsed = round(time.monotonic() - started_at[chunk.stem], 2)
            print(f"[done] {chunk.stem}.md ({elapsed}s){note}", flush=True)
            return {
                "chunk": chunk.stem,
                "status": "ok",
                "path": str(md_path),
                "estimated_payload_bytes": estimated,
                "elapsed_seconds": elapsed,
                "retryable": False,
                **extra,
            }

        def record_blank(chunk: Chunk, reason: str, detail: str) -> dict:
            """An empty page contributes nothing to the Markdown: not a failure."""
            md_path = pages_dir / f"{chunk.stem}.md"
            error_path = errors_dir / f"{chunk.stem}.json"
            md_path.write_text("", encoding="utf-8")
            if error_path.exists():
                error_path.unlink()
            print(f"[blank] {chunk.stem}: {detail}; skipped", flush=True)
            return {
                "chunk": chunk.stem,
                "status": "blank",
                "path": str(md_path),
                "reason": reason,
                "retryable": False,
            }

        async def run_one(chunk: Chunk, round_number: int) -> dict:
            if chunk.blank:
                return record_blank(
                    chunk,
                    "blank_page",
                    "no visible page content "
                    f"(ink < {BLANK_PAGE_INK_RATIO} and no pixel darker than "
                    f"{BLANK_PAGE_DARK_LEVEL})",
                )

            error_path = errors_dir / f"{chunk.stem}.json"
            attempts = max(1, attempts_per_page)

            async def transcribe(target: Chunk, name: str) -> tuple[str, int]:
                parts, estimated = make_request_parts(target, prompt, media_resolution)
                text = await call_model(
                    client,
                    key_pool,
                    provider,
                    model,
                    parts,
                    name,
                    thinking_level,
                    fallback_models,
                    reasoning_effort,
                    api_base,
                    max_output_tokens,
                )

                for verify_index in range(verification_passes):
                    verify_parts, _ = make_verification_parts(
                        target, prompt, text, media_resolution
                    )
                    text = await call_model(
                        client,
                        key_pool,
                        provider,
                        model,
                        verify_parts,
                        f"{name}-verify-{verify_index + 1}",
                        thinking_level,
                        fallback_models,
                        reasoning_effort,
                        api_base,
                        max_output_tokens,
                    )
                return text, estimated

            async def transcribe_part(
                path: Path, name: str, splits_left: int
            ) -> tuple[str, bool]:
                """Transcribe one page image, cutting it up while it is refused.

                Returns the text plus whether this crop was refused (as opposed to
                simply empty). ``splits_left`` bounds the recursion, so a page that
                is refused all the way down fails in seconds instead of eating the
                whole quota pool.
                """
                part_chunk = Chunk(chunk.start_page, chunk.end_page, (path,), name)
                try:
                    text, _ = await transcribe(part_chunk, name)
                except EmptyAnswerError:
                    # An empty STOP answer means this crop has nothing to
                    # transcribe; a smaller crop would not invent text either.
                    return "", False
                except ContentBlockedError as blocked:
                    if splits_left <= 0:
                        return "", True
                    text, missing = await split_and_transcribe(
                        path, name, blocked, splits_left
                    )
                    return text, bool(missing)
                return text, False

            async def split_and_transcribe(
                path: Path, name: str, blocked: Exception, splits_left: int
            ) -> tuple[str, list[str]]:
                """Cut a refused image on blank rows and transcribe every part.

                Returns the recovered text plus the parts that stayed refused at
                their smallest size (their text is missing from the output).
                """
                parts, clean = await asyncio.to_thread(split_page_image, path, split_dir)
                print(
                    f"[split] {name}: {type(blocked).__name__} ({blocked}); "
                    f"cutting into {len(parts)} parts (clean_cut={clean})",
                    flush=True,
                )
                texts: list[str] = []
                missing: list[str] = []
                for index, part in enumerate(parts, start=1):
                    part_name = f"{name}-part{index}"
                    text, refused = await transcribe_part(part, part_name, splits_left - 1)
                    if text.strip():
                        texts.append(text)
                    elif refused:
                        # A refusal is about that one region, so the sibling parts
                        # are still worth keeping; record what got lost instead.
                        missing.append(part_name)
                return "\n\n".join(texts), missing

            async def transcribe_with_page_split(
                blocked: Exception,
            ) -> tuple[str, list[str]]:
                """Retry a refused page by cutting it on blank rows.

                Returns the recovered text and the parts that stayed refused at
                their smallest size, so the caller can report a partial recovery
                instead of silently dropping content.
                """
                text, missing = await split_and_transcribe(
                    chunk.image_paths[0], chunk.stem, blocked, PAGE_SPLIT_DEPTH
                )
                if not text.strip():
                    raise ContentBlockedError(
                        "Gemini refused this page image and every part of it "
                        f"({', '.join(missing) or 'no part returned text'})"
                    ) from blocked
                if missing:
                    print(
                        f"[warn] {chunk.stem}: Gemini refused {', '.join(missing)} "
                        "at every size; that text is missing from the Markdown",
                        flush=True,
                    )
                return text, missing

            # A saturated free tier answers 503 in a couple of seconds, so an
            # immediate retry is far cheaper than deferring the page to the next
            # round, which waits 30-480 seconds.
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    text, estimated = await transcribe(chunk, chunk.stem)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if not is_retryable_error(exc) or attempt >= attempts:
                        break
                    pause = random.uniform(3.0, 8.0) * attempt
                    print(
                        f"[retry] {chunk.stem}: attempt {attempt}/{attempts} failed "
                        f"with {type(exc).__name__}; retrying in {pause:.1f}s",
                        flush=True,
                    )
                    await asyncio.sleep(pause)
                    continue

                return record_success(chunk, text, estimated)

            exc = last_error if last_error is not None else RuntimeError(
                "transcription failed without an exception"
            )

            if isinstance(exc, EmptyAnswerError):
                # Gemini answered STOP with no text at all: there is nothing to
                # transcribe here, so this page is blank, not failed.
                return record_blank(chunk, "no_transcribable_text", str(exc))

            if (
                isinstance(exc, ContentBlockedError)
                and blocked_page_split == "halves"
                and chunk.start_page == chunk.end_page
                and chunk.image_paths
            ):
                try:
                    text, missing = await transcribe_with_page_split(exc)
                except EmptyAnswerError as empty:
                    return record_blank(chunk, "no_transcribable_text", str(empty))
                except Exception as unrecovered:  # noqa: BLE001
                    exc = unrecovered
                else:
                    note = " via page split"
                    if missing:
                        note = f" via page split ({len(missing)} part(s) missing)"
                    return record_success(
                        chunk,
                        text,
                        0,
                        note=note,
                        recovered_by="page_split",
                        missing_parts=missing,
                    )

            error = {
                "chunk": chunk.stem,
                "start_page": chunk.start_page,
                "end_page": chunk.end_page,
                "error": f"{type(exc).__name__}: {exc}",
                "round": round_number,
                "attempts": attempts,
            }
            error_path.write_text(
                json.dumps(error, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return {
                "chunk": chunk.stem,
                "status": "failed",
                "retryable": is_retryable_error(exc),
                **error,
            }

        async def run_round(round_chunks: list[Chunk], round_number: int) -> list[dict]:
            next_index = 0
            cursor_lock = asyncio.Lock()

            async def worker() -> None:
                nonlocal next_index
                while True:
                    async with cursor_lock:
                        if next_index >= len(round_chunks):
                            return
                        chunk = round_chunks[next_index]
                        next_index += 1
                    progress["active_chunks"] += 1
                    try:
                        outcome = await run_one(chunk, round_number)
                    finally:
                        progress["active_chunks"] -= 1
                    latest_results[chunk.stem] = outcome
                    completed_first_pass.add(chunk.stem)

            worker_count = min(concurrency, len(round_chunks))
            await asyncio.gather(*(worker() for _ in range(worker_count)))
            return [latest_results[chunk.stem] for chunk in round_chunks]

        async def emit_metrics() -> None:
            while True:
                try:
                    await asyncio.wait_for(stop_metrics.wait(), timeout=15)
                    return
                except asyncio.TimeoutError:
                    performance = key_pool.performance_summary()
                    successful = sum(item.get("status") == "ok" for item in latest_results.values())
                    failed = sum(item.get("status") == "failed" for item in latest_results.values())
                    blank = sum(item.get("status") == "blank" for item in latest_results.values())
                    pending = len(chunks) - len(completed_first_pass)
                    if not provider.uses_shared_quota_pool:
                        # No shared quota service to report on: keys are paced
                        # locally, so the useful signal is which provider ran and
                        # how many requests came back as an error.
                        global_summary = (
                            f"provider={provider.name} keys={len(keys)} "
                            f"http_other={performance['other_error']}"
                        )
                    else:
                        try:
                            remote = await key_pool.remote_status()
                            global_state = remote.get("global", {})
                            if not isinstance(global_state, dict):
                                global_state = {}
                            global_summary = (
                                f"global_inflight={global_state.get('activeLeases', '?')}/"
                                f"{global_state.get('maxInflight', '?')} "
                                f"global_rps={global_state.get('requestsPerSecond', '?')} "
                                f"active_projects={global_state.get('activeProjects', '?')} "
                                f"global_cooldown_ms={global_state.get('cooldownRemainingMs', '?')} "
                                f"recent_503_ratio={global_state.get('recent503Ratio', '?')} "
                                f"adaptive_stage={global_state.get('adaptiveStage', '?')}"
                            )
                        except Exception as exc:
                            global_summary = f"global_status=unavailable({type(exc).__name__})"
                    print(
                        f"[metrics] pending_chunks={pending} active_chunks={progress['active_chunks']} "
                        f"success={successful} failed={failed} blank={blank} "
                        f"http_429={performance['429']} http_503={performance['503']} "
                        f"http_p50={performance['p50_seconds']}s "
                        f"http_p95={performance['p95_seconds']}s {global_summary}",
                        flush=True,
                    )

        metrics_task = asyncio.create_task(emit_metrics())
        try:
            first_round = await run_round(chunks, 1)
            deferred = [
                chunks_by_name[result["chunk"]]
                for result in first_round
                if result["status"] == "failed" and result["retryable"]
            ]
            trivially_done = sum(
                1
                for result in first_round
                if result["status"] == "failed" and not result["retryable"]
            )
            if trivially_done:
                print(
                    f"[retry-round] {trivially_done} chunk(s) are final after round 1 "
                    "(content errors, blank pages, permanent errors); no backoff for them",
                    flush=True,
                )
            for round_number in range(2, retry_rounds + 1):
                if not deferred:
                    break
                base_delay = min(480.0, 30.0 * (2 ** (round_number - 2)))
                delay = random.uniform(base_delay, base_delay * 2.0)
                print(
                    f"[retry-round] round={round_number}/{retry_rounds} "
                    f"deferred_chunks={len(deferred)} wait={delay:.0f}s",
                    flush=True,
                )
                await asyncio.sleep(delay)
                round_results = await run_round(deferred, round_number)
                deferred = [
                    chunks_by_name[result["chunk"]]
                    for result in round_results
                    if result["status"] == "failed" and result["retryable"]
                ]
        finally:
            stop_metrics.set()
            await metrics_task
            await key_pool.close()

        results = [latest_results[chunk.stem] for chunk in chunks]
        for result in results:
            if result["status"] == "failed":
                print(
                    f"[failed] {result['chunk']}: {result['error']}",
                    file=sys.stderr,
                    flush=True,
                )
        quota_summary = key_pool.usage_summary()
        (output_dir / "quota-usage.json").write_text(
            json.dumps(quota_summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"[quota] action complete keys={len(keys)} "
            f"requests={sum(key_pool.used)}",
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
    p.add_argument(
        "--system-prompt",
        default="",
        help=(
            "Shared system prompt read from the saved file-conversion config. "
            "It is sent before --prompt; when both are empty the built-in "
            f"prompt is used. At most {MAX_PROMPT_CHARS} characters."
        ),
    )
    p.add_argument(
        "--prompt",
        default="",
        help=(
            "Per-conversion prompt. It is appended after --system-prompt and "
            "never replaces it."
        ),
    )
    p.add_argument(
        "--provider",
        default="",
        help=(
            "Model gateway to call. One of: "
            + ", ".join(provider_names())
            + ". Empty means FILE_CONVERTER_PROVIDER, then gemini. Each gateway "
            "reads its own key list and needs its own model ids."
        ),
    )
    p.add_argument(
        "--api-base",
        default="",
        help=(
            "Override the provider base URL. Empty uses the provider default "
            "(Gemini: generativelanguage.googleapis.com; Modelflare: "
            "https://modelflare.dev/v1), so pointing at a different "
            "OpenAI-compatible gateway needs no code change."
        ),
    )
    p.add_argument(
        "--model",
        default="",
        help=(
            "Model id. Empty uses the provider default, so switching provider "
            "never silently keeps another gateway's model id."
        ),
    )
    p.add_argument(
        "--reasoning-effort",
        default="",
        help=(
            "Reasoning effort for OpenAI-compatible gateways "
            "(minimal/low/medium/high/xhigh). Empty omits the field, because "
            "models without explicit reasoning reject it. --thinking-level is "
            "the Gemini equivalent and is ignored by other providers."
        ),
    )
    p.add_argument(
        "--max-output-tokens",
        type=int,
        default=0,
        help=(
            "Cap on the answer length; 0 omits the field and lets the gateway "
            "decide. A gateway default can be far below what a dense page needs, "
            "so a truncated answer is reported as a failure instead of being "
            "silently accepted as a complete page."
        ),
    )
    p.add_argument(
        "--model-fallbacks",
        default="",
        help=(
            "Comma-separated fallback model IDs tried in order when the primary "
            "model answers 503/404. Empty keeps the default model's same-tier "
            "fallback only; custom models have no automatic fallback. Use "
            "'none' to disable the default fallback."
        ),
    )
    p.add_argument(
        "--retry-rounds",
        type=int,
        default=5,
        help=(
            "Total passes over failed chunks. Each extra round waits roughly "
            "twice as long as the previous one. Default: 5."
        ),
    )
    p.add_argument(
        "--attempts-per-page",
        type=int,
        default=2,
        help=(
            "Immediate attempts per page inside a round before the page is "
            "deferred. A saturated model answers 503 within seconds, so a quick "
            "retry is much cheaper than waiting for the next round. Default: 2."
        ),
    )
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
    p.add_argument(
        "--blocked-page-split",
        choices=["none", "halves"],
        default="halves",
        help=(
            "When Gemini refuses a single page (RECITATION, safety block), retry it "
            "as two horizontal halves cut on a blank row, cutting a refused half "
            "again (at most PAGE_SPLIT_DEPTH times). 'none' disables it."
        ),
    )
    p.add_argument("--work-dir", default="work")
    p.add_argument("--output-dir", default="output")
    return p


def validate_args(args: argparse.Namespace) -> None:
    if args.conversion_type != "pdf_to_md":
        raise ValueError("conversion_type must be pdf_to_md")
    if len(args.system_prompt) > MAX_PROMPT_CHARS:
        raise ValueError(
            f"system_prompt must be at most {MAX_PROMPT_CHARS} characters"
        )
    if len(args.prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt must be at most {MAX_PROMPT_CHARS} characters")
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
    if not 1 <= args.retry_rounds <= 10:
        raise ValueError("retry_rounds must be between 1 and 10")
    if not 1 <= args.attempts_per_page <= 5:
        raise ValueError("attempts_per_page must be between 1 and 5")
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
    if args.blocked_page_split not in {"none", "halves"}:
        raise ValueError("blocked_page_split must be none or halves")
    if args.provider:
        resolve_provider(args.provider)
    if args.api_base:
        parsed = urlparse(args.api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("api_base must be an absolute http(s) URL")
    if args.reasoning_effort:
        normalize_reasoning_effort(args.reasoning_effort)
    if not 0 <= args.max_output_tokens <= 200000:
        raise ValueError("max_output_tokens must be between 0 and 200000")


async def async_main(args: argparse.Namespace) -> int:
    validate_args(args)
    provider = resolve_provider(args.provider or os.getenv("FILE_CONVERTER_PROVIDER"))
    model = resolve_model(provider, args.model)
    api_base = (args.api_base or provider.default_base_url).rstrip("/")
    reasoning_effort = normalize_reasoning_effort(args.reasoning_effort)

    system_prompt = args.system_prompt.strip()
    user_prompt = args.prompt.strip()
    prompt = compose_prompt(system_prompt, user_prompt)
    keys = parse_api_keys(os.getenv(provider.api_key_env))
    if not keys:
        raise RuntimeError(f"{provider.api_key_env} is empty. {provider.api_key_hint}")
    fallbacks_source = args.model_fallbacks
    if not fallbacks_source and provider.fallbacks_env:
        fallbacks_source = os.getenv(provider.fallbacks_env)
    fallback_models = resolve_fallback_models(provider, model, fallbacks_source)
    if provider.name != GEMINI.name and model.lower().startswith("gemini-"):
        print(
            f"[warn] model {model!r} looks like a Gemini id but provider is "
            f"{provider.name}. Switch --model to a {provider.label} id "
            f"(e.g. {provider.model_example}) or the request will 404.",
            flush=True,
        )

    work_dir = Path(args.work_dir)
    output_dir = Path(args.output_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_pdf = work_dir / "source.pdf"
    images_dir = work_dir / "images"

    print(
        f"[config] conversion_type={args.conversion_type} provider={provider.name} "
        f"model={model} keys={len(keys)} "
        f"concurrency={args.concurrency} "
        f"images_per_request={args.images_per_request} "
        f"thinking_level={args.thinking_level} "
        f"image_format={args.image_format} dpi={args.dpi} "
        f"verification_passes={args.verification_passes} "
        f"media_resolution={args.media_resolution} "
        f"rpm_per_key={args.rpm_per_key:g} rpd_per_key={args.rpd_per_key} "
        f"model_fallbacks={','.join(fallback_models) or 'none'} "
        f"retry_rounds={args.retry_rounds} "
        f"attempts_per_page={args.attempts_per_page} "
        f"blocked_page_split={args.blocked_page_split} "
        f"page_range={args.start_page or 1}-{args.end_page or 'end'}",
        flush=True,
    )
    print(
        f"[provider] {provider.name} endpoint={provider.endpoint(api_base, model)} "
        f"reasoning_effort={reasoning_effort or 'default'} "
        f"max_output_tokens={args.max_output_tokens or 'default'} "
        f"keys_env={provider.api_key_env} keys={len(keys)}",
        flush=True,
    )
    print(
        f"[prompt] system_chars={len(system_prompt)} task_chars={len(user_prompt)} "
        f"effective_chars={len(prompt)} "
        f"sha256={hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:12]}",
        flush=True,
    )
    if not system_prompt and not user_prompt:
        print(
            "[prompt] no system or task prompt supplied; using the built-in "
            "faithful transcription prompt",
            flush=True,
        )
    print(f"[download] {args.source_url}", flush=True)
    await asyncio.to_thread(download_pdf, args.source_url, source_pdf)
    with source_pdf.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise RuntimeError(
                "Downloaded source is not a PDF. Use a direct/public PDF link; ZIP and other files are not accepted."
            )

    image_paths, width, pdf_total_pages, blank_pages = await asyncio.to_thread(
        render_pdf,
        source_pdf,
        images_dir,
        args.dpi,
        args.jpeg_quality,
        args.image_format,
        args.start_page,
        args.end_page,
    )
    chunks = build_chunks(image_paths, args.images_per_request, width, blank_pages)
    print(
        f"[plan] pages={len(image_paths)} chunks={len(chunks)} "
        f"blank_pages={len(blank_pages)}",
        flush=True,
    )

    results = await process_chunks(
        chunks,
        output_dir,
        prompt,
        model,
        args.concurrency,
        keys,
        args.thinking_level,
        args.verification_passes,
        args.media_resolution,
        args.rpm_per_key,
        args.rpd_per_key,
        fallback_models=fallback_models,
        retry_rounds=args.retry_rounds,
        attempts_per_page=args.attempts_per_page,
        blocked_page_split=args.blocked_page_split,
        provider=provider,
        reasoning_effort=reasoning_effort,
        api_base=api_base,
        max_output_tokens=args.max_output_tokens,
    )
    failures = [x for x in results if x.get("status") == "failed"]

    merged_name = "merged.md" if not failures else "merged.partial.md"
    merge_markdown(chunks, output_dir / "pages", output_dir / merged_name)

    manifest = {
        "conversion_type": args.conversion_type,
        "source_url": args.source_url,
        "provider": provider.name,
        "model": model,
        "api_base": api_base,
        "model_fallbacks": list(fallback_models),
        "reasoning_effort": reasoning_effort,
        "max_output_tokens": args.max_output_tokens,
        "prompt": {
            "system_chars": len(system_prompt),
            "task_chars": len(user_prompt),
            "effective_chars": len(prompt),
            "effective_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "system": system_prompt,
            "task": user_prompt,
        },
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
        "blocked_page_split": args.blocked_page_split,
        "blank_pages": sorted(blank_pages),
        "blank_chunks": sorted(
            item["chunk"] for item in results if item.get("status") == "blank"
        ),
        "split_recovered": sorted(
            item["chunk"] for item in results if item.get("recovered_by") == "page_split"
        ),
        "partial_recovery": {
            item["chunk"]: item["missing_parts"]
            for item in results
            if item.get("missing_parts")
        },
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
