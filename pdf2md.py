"""PDF -> Markdown: PaddleOCR drafts every page, a vision model finalises it.

1. The PDF is downloaded and handed to PaddleOCR-VL in small segments.
2. Pages are rendered to images in a background thread at the same time.
3. Each page goes to the model as soon as both its image and its OCR draft are
   ready. The model checks the draft against the image and writes the final
   Markdown for that page.
4. A page whose model request keeps failing or is refused falls back to its OCR
   draft; a page without a draft is transcribed from the image alone.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from collections.abc import AsyncIterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import fitz
import gdown
import httpx

from artifacts import prepare_directories, validate_output_name, write_result
from key_pool import KeyPool, QuotaPoolExhaustedError, parse_key_groups
from paddle_ocr import DEFAULT_MODEL as DEFAULT_OCR_MODEL
from paddle_ocr import JOB_URL, SEGMENT_PAGES, OcrPage, PaddleOcr, parse_tokens
from pipeline import Progress, schedule_chunks
from providers import (
    GEMINI,
    Provider,
    ProviderRefusal,
    normalize_reasoning_effort,
    provider_names,
    resolve_model,
    resolve_provider,
)
from render_stream import RenderPlan, render_pages
from request_policy import CURRENT_POLICY, RequestPolicy

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

#: How the model is asked to use the OCR draft. The transcription rules
#: (``DEFAULT_PROMPT`` or the configured prompts) follow it.
REFINE_INSTRUCTIONS = """下方 <ocr_draft> 是 OCR 模型（PaddleOCR-VL）对这一页的识别草稿。请以图片为准、以草稿为底稿，输出这一页最终的 Markdown：
1. 草稿的文字识别通常可靠：逐段对照图片，保留正确内容，只修正与图片不符之处（错字、漏字、多字、数字、公式符号、上下标）。
2. 草稿漏掉的正文、公式、表格或题目，按图片补全；草稿里的页眉、页脚、页码和水印删除。
3. 按图片整理结构：标题层级、段落拆分与合并、列表、阅读顺序（多栏页面按栏阅读）。
4. 表格输出为 Markdown 表格；存在合并单元格等 Markdown 无法表达的结构时，保留 HTML <table>。
5. 草稿中的图片引用 ![...](images/...) 原样保留在它在页面中的位置，不改路径，不新增图片引用。"""


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
# Pages are measured on a cheap downscaled grayscale pixmap.
BLANK_ANALYSIS_SCALE = 0.25

_FIGURE_LINK = re.compile(r"!\[[^\]\n]*\]\((images/[^)\s]+)\)")


@dataclass(frozen=True)
class Page:
    """One rendered PDF page; ``stem`` names its files and its progress entry."""

    number: int
    image_path: Path
    stem: str
    blank: bool = False

    @property
    def start_page(self) -> int:
        return self.number

    @property
    def end_page(self) -> int:
        return self.number


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


async def stream_pages(pdf_path, image_dir, plan, dpi, jpeg_quality, image_format):
    """Render pages one by one on a dedicated thread and yield them in order."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-render")
    iterator = render_pages(pdf_path, image_dir, plan, dpi, jpeg_quality,
                            image_format, is_blank_page)
    loop = asyncio.get_running_loop()
    try:
        while True:
            item = await loop.run_in_executor(executor, next, iterator, None)
            if item is None:
                return
            path, blank = item
            yield Page(int(path.stem), path, path.stem, blank)
    finally:
        # close is queued behind any in-progress render on the same thread.
        await loop.run_in_executor(executor, iterator.close)
        executor.shutdown(wait=False, cancel_futures=True)


def page_ink_stats(page: fitz.Page) -> tuple[float, int]:
    """Ink coverage and darkest pixel of a cheap downscaled page preview."""
    pix = page.get_pixmap(
        matrix=fitz.Matrix(BLANK_ANALYSIS_SCALE, BLANK_ANALYSIS_SCALE),
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


def media_resolution_payload(level: str) -> dict | None:
    if level == "unspecified":
        return None
    return {
        "level": f"MEDIA_RESOLUTION_{level.upper()}",
    }


def image_part(path: Path, media_resolution: str) -> tuple[dict, int]:
    data = path.read_bytes()
    mime_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    part = {
        "inlineData": {
            "mimeType": mime_type,
            "data": base64.b64encode(data).decode("ascii"),
        }
    }
    resolution = media_resolution_payload(media_resolution)
    if resolution is not None:
        part["mediaResolution"] = resolution
    return part, len(data)


def _check_request_size(name: str, raw_bytes: int, text: str) -> int:
    estimated = int(raw_bytes * 4 / 3) + len(text.encode("utf-8")) + 64 * 1024
    if estimated > MAX_INLINE_REQUEST_BYTES:
        raise ValueError(
            f"Page {name} is about {estimated / 1024 / 1024:.1f} MiB after base64, "
            "too large for inline image input. Lower dpi; for JPEG you can also lower jpeg_quality."
        )
    return estimated


def make_request_parts(
    page: Page, prompt: str, media_resolution: str, draft: str | None = None
) -> tuple[list[dict], int]:
    """The page image plus, when OCR produced one, the draft to refine."""
    text = f"以下图片是 PDF 第 {page.number} 页。\n"
    if draft is None:
        text += f"请严格按图片内容处理。\n\n{prompt.strip()}"
    else:
        text += (
            f"{REFINE_INSTRUCTIONS}\n\n转录要求：\n{prompt.strip()}\n\n"
            f"<ocr_draft>\n{draft.strip() or '（OCR 未识别到内容）'}\n</ocr_draft>"
        )
    image, raw_bytes = image_part(page.image_path, media_resolution)
    return [{"text": text}, image], _check_request_size(page.stem, raw_bytes, text)


def make_verification_parts(
    page: Page, prompt: str, draft: str, media_resolution: str
) -> tuple[list[dict], int]:
    verify_prompt = f"""请对照图片，审校下面这份 Markdown 草稿并直接返回修正后的完整 Markdown。

审校重点：
1. 逐字符核对所有数学公式：正负号、分数、根号、上下标、积分/求和上下限、矩阵、转置、希腊字母、括号和数字。
2. 检查是否有漏行、错行、重复，或把页眉页脚、扫描水印误当正文。
3. 检查 Markdown/LaTeX 格式，行内公式使用成对 $...$，多行公式使用成对 $$...$$。
4. 只依据图片纠错，不要自行改写原文、推导新内容或加入解释。
5. 若草稿与图片冲突，以图片为准。看不清时写 [无法辨认]，不要猜。
6. 图片引用 ![...](images/...) 原样保留。只输出最终修正后的 Markdown。

原始转录要求：
{prompt.strip()}

待审校草稿：
---BEGIN DRAFT---
{draft}
---END DRAFT---
"""
    image, raw_bytes = image_part(page.image_path, media_resolution)
    return [{"text": verify_prompt}, image], _check_request_size(page.stem, raw_bytes, verify_prompt)


def keep_known_figures(text: str, figures: dict[str, Path]) -> str:
    """Drop figure links the model invented; only OCR crops exist on disk."""
    return _FIGURE_LINK.sub(lambda m: m.group(0) if m.group(1) in figures else "", text)


def figures_used(text: str) -> list[str]:
    return list(dict.fromkeys(_FIGURE_LINK.findall(text)))


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


class ContentBlockedError(RuntimeError):
    """Gemini answered a page without any usable text.

    Either ``finishReason`` is a content-refusal value (``RECITATION`` and
    friends) or the prompt itself was blocked. The verdict is a property of the
    page image, not of the quota pool: re-sending the same image reproduces it
    on every attempt, so this error must never enter the backoff rounds.
    The page falls back to its OCR draft instead.
    """


class EmptyAnswerError(ContentBlockedError):
    """The model stopped without emitting any text for a page image.

    Seen on blank scan pages (nothing to transcribe), on figure-only pages and
    on responses whose whole output landed in thinking parts. Retrying the same
    image does not change it.
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
    policy = CURRENT_POLICY.get()
    async def attempt(on_started=lambda: None):
        return await _call_model_attempt(
            client, key_pool, provider, model, parts, chunk_name, thinking_level,
            reasoning_effort, api_base, max_output_tokens, on_started,
        )
    if policy is None:
        return await attempt()
    return await policy.run(attempt, chunk_name, max_duration=provider.request_timeout_seconds)


async def finish_report(report) -> None:
    """Release a lease even during cancellation; reporting cannot discard text."""
    async def bounded():
        try:
            async with asyncio.timeout(8):
                await report
        except Exception as exc:
            print(f"[quota-warning] report failed: {type(exc).__name__}; lease expiry is the fallback", flush=True)
    task = asyncio.create_task(bounded())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _call_model_attempt(
    client, key_pool, provider, model, parts, chunk_name, thinking_level,
    reasoning_effort, api_base, max_output_tokens, on_started,
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
        on_started()
        print(f"[request] {chunk_name} key#{key_index + 1} deadline={provider.request_timeout_seconds:g}s", flush=True)
        # HTTPX bounds inactivity between chunks, not total wall time.
        async with asyncio.timeout(provider.request_timeout_seconds):
            response = await client.post(
                url,
                headers=provider.build_headers(api_key),
                json=payload,
            )
    except asyncio.CancelledError:
        await finish_report(key_pool.mark_cancelled(key_index, lease_id))
        raise
    except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
        key_pool.record_http_result(0, time.monotonic() - started)
        await finish_report(key_pool.mark_error(key_index, lease_id, http_status=0))
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
            await finish_report(key_pool.mark_success(key_index, lease_id))
            raise
        except Exception as exc:
            await finish_report(key_pool.mark_error(key_index, lease_id, http_status=502))
            raise RuntimeError(
                f"{provider.label} returned unusable Markdown: {exc}"
            ) from exc
        policy = CURRENT_POLICY.get()
        if policy is not None:
            policy.mark_validated()
        await finish_report(key_pool.mark_success(key_index, lease_id))
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
        await finish_report(key_pool.rate_limited(
            key_index,
            lease_id,
            cooldown_seconds,
            daily_exhausted=quota_type == "rpd",
            quota_type=quota_type,
        ))
        raise RuntimeError(
            f"{provider.label} HTTP 429 ({quota_type}; "
            f"project cooldown={cooldown_seconds:.1f}s): {message}"
        )

    await finish_report(key_pool.mark_error(key_index, lease_id, http_status=response.status_code))
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
    """Send one page request, walking the fallback chain when a model is saturated.

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


async def process_pages(
    pages: list[Page] | AsyncIterable[Page],
    state_dir: Path,
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
    provider: Provider | None = None,
    reasoning_effort: str = "",
    api_base: str = "",
    max_output_tokens: int = 0,
    total_pages: int | None = None,
    hedge_after: float = 60.0,
    hedge_budget: int | None = None,
    publish_progress=None,
    ocr: PaddleOcr | None = None,
) -> list[dict]:
    """Finalise every page with the model, using its OCR draft when there is one."""
    provider = provider or resolve_provider(None)

    pages_dir = state_dir / "pages"
    errors_dir = state_dir / "errors"
    pages_dir.mkdir(parents=True, exist_ok=True)
    errors_dir.mkdir(parents=True, exist_ok=True)

    groups = (
        parse_key_groups(len(keys), os.getenv(provider.key_groups_env))
        if provider.key_groups_env else None
    )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(provider.request_timeout_seconds, connect=30.0),
        limits=httpx.Limits(
            max_connections=max(100, concurrency * 2),
            max_keepalive_connections=max(50, concurrency),
            keepalive_expiry=30.0,
        ),
    ) as client:
        key_pool = await KeyPool.create(
            keys, groups, rpm_per_key=rpm_per_key, rpd_per_key=rpd_per_key
        )

        if isinstance(pages, list):
            total_pages = len(pages)
        if total_pages is None:
            raise ValueError("Streaming input requires total_pages")
        tracker = Progress(state_dir, total_chunks=total_pages, total_pages=total_pages)
        started_at: dict[str, float] = {}
        stop_metrics = asyncio.Event()

        def can_hedge():
            recent = key_pool.recent_statuses[-20:]
            return not recent or sum(s in (429, 503) for s in recent) / len(recent) < 0.2

        policy = RequestPolicy(
            concurrency=concurrency, hedge_after=hedge_after,
            hedge_budget=hedge_budget if hedge_budget is not None else max(2, math.ceil(total_pages * 0.1)),
            can_hedge=can_hedge,
            latency_p95=lambda: key_pool.performance_summary().get("success_p95_seconds"),
        )
        tracker.request_summary = policy.summary
        policy_token = CURRENT_POLICY.set(policy)

        async def source():
            if isinstance(pages, list):
                for page in pages:
                    started_at.setdefault(page.stem, time.monotonic())
                    yield page
            else:
                async for page in pages:
                    started_at.setdefault(page.stem, time.monotonic())
                    yield page

        def record_success(page: Page, text: str, estimated: int, source: str, **extra) -> dict:
            md_path = pages_dir / f"{page.stem}.md"
            md_path.write_text(text.strip() + "\n", encoding="utf-8")
            (errors_dir / f"{page.stem}.json").unlink(missing_ok=True)
            elapsed = round(time.monotonic() - started_at[page.stem], 2)
            print(f"[done] {page.stem}.md ({elapsed}s, {source})", flush=True)
            return {
                "chunk": page.stem,
                "page": page.number,
                "status": "ok",
                "source": source,
                "path": str(md_path),
                "figures": figures_used(text),
                "estimated_payload_bytes": estimated,
                "elapsed_seconds": elapsed,
                "retryable": False,
                **extra,
            }

        def record_blank(page: Page, reason: str, detail: str) -> dict:
            """An empty page contributes nothing to the Markdown: not a failure."""
            md_path = pages_dir / f"{page.stem}.md"
            md_path.write_text("", encoding="utf-8")
            (errors_dir / f"{page.stem}.json").unlink(missing_ok=True)
            print(f"[blank] {page.stem}: {detail}; skipped", flush=True)
            return {
                "chunk": page.stem,
                "page": page.number,
                "status": "blank",
                "path": str(md_path),
                "reason": reason,
                "retryable": False,
            }

        async def run_one(page: Page, round_number: int) -> dict:
            if round_number == 1:
                (pages_dir / f"{page.stem}.md").unlink(missing_ok=True)
            if page.blank:
                return record_blank(
                    page,
                    "blank_page",
                    "no visible page content "
                    f"(ink < {BLANK_PAGE_INK_RATIO} and no pixel darker than "
                    f"{BLANK_PAGE_DARK_LEVEL})",
                )

            ocr_page: OcrPage | None = await ocr.page(page.number) if ocr else None
            draft = ocr_page.markdown if ocr_page else None
            figures = ocr_page.figures if ocr_page else {}
            attempts = max(1, attempts_per_page)

            async def transcribe() -> tuple[str, int]:
                parts, estimated = make_request_parts(page, prompt, media_resolution, draft)
                text = await call_model(
                    client, key_pool, provider, model, parts, page.stem, thinking_level,
                    fallback_models, reasoning_effort, api_base, max_output_tokens,
                )
                for verify_index in range(verification_passes):
                    verify_parts, _ = make_verification_parts(page, prompt, text, media_resolution)
                    text = await call_model(
                        client, key_pool, provider, model, verify_parts,
                        f"{page.stem}-verify-{verify_index + 1}", thinking_level,
                        fallback_models, reasoning_effort, api_base, max_output_tokens,
                    )
                return text, estimated

            # A saturated free tier answers 503 in a couple of seconds, so an
            # immediate retry is far cheaper than deferring the page to the next
            # round, which waits 5-90 seconds.
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    text, estimated = await transcribe()
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if not is_retryable_error(exc) or attempt >= attempts:
                        break
                    pause = random.uniform(3.0, 8.0) * attempt
                    print(
                        f"[retry] {page.stem}: attempt {attempt}/{attempts} failed "
                        f"with {type(exc).__name__}: {exc}; retrying in {pause:.1f}s",
                        flush=True,
                    )
                    await asyncio.sleep(pause)
                    continue
                text = keep_known_figures(text, figures)
                return record_success(
                    page, text, estimated, "ocr+model" if draft is not None else "model"
                )

            exc = last_error or RuntimeError("transcription failed without an exception")
            has_draft = bool(draft and draft.strip())
            last_round = round_number >= retry_rounds or not is_retryable_error(exc)
            if has_draft and last_round:
                print(
                    f"[fallback] {page.stem}: model gave up with {type(exc).__name__}: "
                    f"{exc}; using the OCR draft",
                    flush=True,
                )
                return record_success(
                    page, draft, 0, "ocr", fallback_reason=f"{type(exc).__name__}: {exc}"[:500]
                )
            if isinstance(exc, EmptyAnswerError):
                # No OCR text and no model text: nothing to transcribe here.
                return record_blank(page, "no_transcribable_text", str(exc))

            error = {
                "chunk": page.stem,
                "page": page.number,
                "start_page": page.number,
                "end_page": page.number,
                "error": f"{type(exc).__name__}: {exc}",
                "round": round_number,
                "attempts": attempts,
            }
            (errors_dir / f"{page.stem}.json").write_text(
                json.dumps(error, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return {
                "status": "failed",
                "retryable": is_retryable_error(exc),
                **error,
            }

        async def emit_metrics() -> None:
            while True:
                try:
                    await asyncio.wait_for(stop_metrics.wait(), timeout=5)
                    return
                except TimeoutError:
                    if publish_progress is not None:
                        try:
                            publish_progress(list(tracker.results.values()))
                        except Exception as exc:
                            print(f"[warn] Cannot refresh partial Markdown: {type(exc).__name__}: {exc}",
                                  file=sys.stderr, flush=True)
                    counts = key_pool.performance_summary()
                    snapshot = tracker.write(log=True)
                    ocr_note = f"ocr_pages={ocr.stats['pages']} " if ocr and ocr.enabled else ""
                    print(
                        f"[metrics] pending={snapshot['pending_chunks']} "
                        f"active={snapshot['active_chunks']} "
                        f"success={snapshot['success_chunks']} failed={snapshot['failed_chunks']} "
                        f"retrying={snapshot['retrying_chunks']} {ocr_note}"
                        f"http_429={counts['429']} http_503={counts['503']} "
                        f"http_p50={counts['p50_seconds']}s http_p95={counts['p95_seconds']}s "
                        f"hedges={policy.hedges} hedge_wins={policy.hedge_wins}", flush=True,
                    )

        metrics_task = asyncio.create_task(emit_metrics())
        try:
            results = await schedule_chunks(source(), run_one, concurrency, retry_rounds, tracker)
        finally:
            CURRENT_POLICY.reset(policy_token)
            stop_metrics.set()
            try:
                await metrics_task
            finally:
                await key_pool.close()

        for result in results:
            if result["status"] == "failed":
                print(f"[failed] {result['chunk']}: {result['error']}", file=sys.stderr, flush=True)
        usage = key_pool.usage_summary()
        usage["speculation"] = policy.summary()
        usage["ocr"] = ocr.stats if ocr and ocr.enabled else None
        usage["page_sources"] = {
            source: sum(r.get("source") == source for r in results)
            for source in ("ocr+model", "model", "ocr")
        }
        (state_dir / "quota-usage.json").write_text(
            json.dumps(usage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        spread = usage["spread"]
        print(
            f"[quota] action complete keys={len(keys)} requests={sum(key_pool.used)} "
            f"per_bucket={spread['min_requests']}-{spread['max_requests']} "
            f"sources={json.dumps(usage['page_sources'])}",
            flush=True,
        )
        return results


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--conversion-type",
        choices=["pdf_to_md"],
        default="pdf_to_md",
        help="Conversion handler selected by the generic file-converter workflow.",
    )
    p.add_argument("--source-url", required=True)
    p.add_argument("--output-name", default="merged",
                   help="Merged Markdown basename without extension.")
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
        "--ocr",
        choices=["paddle", "none"],
        default="paddle",
        help=(
            "Draft every page with PaddleOCR-VL first (tokens in PADDLEOCR_TOKENS) "
            "and let the model refine it. 'none' sends only the page image."
        ),
    )
    p.add_argument("--ocr-model", default=DEFAULT_OCR_MODEL)
    p.add_argument("--ocr-url", default=JOB_URL, help="PaddleOCR async job endpoint.")
    p.add_argument("--ocr-segment-pages", type=int, default=SEGMENT_PAGES,
                   help="Pages per PaddleOCR job.")
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
            "decide. A truncated answer is reported as a failure instead of "
            "being silently accepted as a complete page."
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
        help="Maximum passes per failed page; the last one falls back to the OCR draft.",
    )
    p.add_argument(
        "--attempts-per-page",
        type=int,
        default=2,
        help="Immediate attempts per page inside a round before the page is deferred.",
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
        help="Extra image-vs-Markdown verification passes after the first answer.",
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
        help="Requests/minute per Gemini project or other provider key.",
    )
    p.add_argument(
        "--rpd-per-key",
        type=int,
        default=500,
        help="Requests per run per Gemini project or other provider key.",
    )
    p.add_argument("--start-page", type=int, default=None,
                   help="1-based first PDF page to process. Default: first page.")
    p.add_argument("--end-page", type=int, default=None,
                   help="1-based last PDF page to process. Default: last page.")
    p.add_argument("--hedge-after", type=float, default=60.0,
                   help="Minimum HTTP age for speculative copies; also >= twice successful p95. 0 disables.")
    p.add_argument("--hedge-budget", type=int, default=-1,
                   help="Max extra requests per conversion. -1 uses 10%% of pages (minimum 2); 0 disables.")
    p.add_argument("--work-dir", default="work")
    p.add_argument("--output-dir", default="output",
                   help="Dedicated generated artifact directory; cleared on each run.")
    return p


def validate_args(args: argparse.Namespace) -> None:
    validate_output_name(args.output_name)
    source = urlparse(args.source_url)
    if source.scheme not in {"http", "https"} or not source.hostname:
        raise ValueError("source_url must be an absolute http(s) URL")
    if not math.isfinite(args.hedge_after) or not 0 <= args.hedge_after <= 600:
        raise ValueError("hedge_after must be between 0 and 600 seconds")
    if not -1 <= args.hedge_budget <= 10000:
        raise ValueError("hedge_budget must be between -1 and 10000")
    if args.conversion_type != "pdf_to_md":
        raise ValueError("conversion_type must be pdf_to_md")
    if len(args.system_prompt) > MAX_PROMPT_CHARS:
        raise ValueError(
            f"system_prompt must be at most {MAX_PROMPT_CHARS} characters"
        )
    if len(args.prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt must be at most {MAX_PROMPT_CHARS} characters")
    if not 1 <= args.concurrency <= 100:
        raise ValueError("concurrency must be between 1 and 100")
    ocr_url = urlparse(args.ocr_url)
    if ocr_url.scheme not in {"http", "https"} or not ocr_url.hostname:
        raise ValueError("ocr_url must be an absolute http(s) URL")
    if not 1 <= args.ocr_segment_pages <= 100:
        raise ValueError("ocr_segment_pages must be between 1 and 100")
    if not 72 <= args.dpi <= 300:
        raise ValueError("dpi must be between 72 and 300")
    if not 50 <= args.jpeg_quality <= 100:
        raise ValueError("jpeg_quality must be between 50 and 100")
    if not 0 <= args.verification_passes <= 3:
        raise ValueError("verification_passes must be between 0 and 3")
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
    work_dir = Path(args.work_dir)
    output_dir = Path(args.output_dir)
    state_dir = prepare_directories(work_dir, output_dir)
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
    ocr_tokens = parse_tokens(os.getenv("PADDLEOCR_TOKENS")) if args.ocr == "paddle" else []
    if args.ocr == "paddle" and not ocr_tokens:
        print("[warn] PADDLEOCR_TOKENS is empty; pages are transcribed from images only",
              flush=True)

    source_pdf = work_dir / "source.pdf"
    images_dir = work_dir / "images"

    print(
        f"[config] conversion_type={args.conversion_type} provider={provider.name} "
        f"model={model} keys={len(keys)} "
        f"ocr={args.ocr_model if ocr_tokens else 'off'} ocr_tokens={len(ocr_tokens)} "
        f"concurrency={args.concurrency} "
        f"thinking_level={args.thinking_level} "
        f"image_format={args.image_format} dpi={args.dpi} "
        f"verification_passes={args.verification_passes} "
        f"media_resolution={args.media_resolution} "
        f"rpm_per_key={args.rpm_per_key:g} rpd_per_key={args.rpd_per_key} "
        f"model_fallbacks={','.join(fallback_models) or 'none'} "
        f"retry_rounds={args.retry_rounds} "
        f"attempts_per_page={args.attempts_per_page} "
        f"hedge_after={args.hedge_after:g}s hedge_budget={args.hedge_budget} "
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

    plan = await asyncio.to_thread(RenderPlan.from_pdf, source_pdf, args.start_page, args.end_page)
    print(f"[plan] pages={plan.page_count} streaming=true", flush=True)
    pages: list[Page] = []
    rendered = stream_pages(source_pdf, images_dir, plan, args.dpi,
                            args.jpeg_quality, args.image_format)

    async def page_source():
        async with contextlib.aclosing(rendered):
            async for page in rendered:
                pages.append(page)
                yield page

    pipeline_error = None

    def publish(results):
        return write_result(
            output_dir=output_dir, state_dir=state_dir, name=args.output_name,
            source_url=args.source_url, plan=plan, provider=provider.name, model=model,
            ocr_model=args.ocr_model if ocr_tokens else None,
            converted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            pages=pages, results=results, pipeline_error=pipeline_error,
        )

    # Always keep a compact partial package, including during cancellation.
    publish([])
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0)) as ocr_client:
        ocr = PaddleOcr(ocr_tokens, client=ocr_client, work_dir=state_dir,
                        model=args.ocr_model, job_url=args.ocr_url,
                        segment_pages=args.ocr_segment_pages)
        ocr_task = ocr.start(source_pdf, plan.first, plan.last, plan.width)
        try:
            results = await process_pages(
                page_source(),
                state_dir,
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
                provider=provider,
                reasoning_effort=reasoning_effort,
                api_base=api_base,
                max_output_tokens=args.max_output_tokens,
                total_pages=plan.page_count,
                hedge_after=args.hedge_after,
                hedge_budget=None if args.hedge_budget < 0 else args.hedge_budget,
                publish_progress=publish,
                ocr=ocr,
            )
        except (Exception, asyncio.CancelledError) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError) or asyncio.current_task().cancelling() > 0
            pipeline_error = f"{type(exc).__name__}: {exc}"
            print(f"[pipeline-error] {pipeline_error}; preserving completed pages", file=sys.stderr, flush=True)
            try:
                result_index = state_dir / "results.json"
                results = json.loads(result_index.read_text()) if result_index.exists() else []
                if not (state_dir / "progress.json").exists():
                    failed_progress = Progress(state_dir, total_chunks=plan.page_count,
                                               total_pages=plan.page_count)
                    failed_progress.state = "failed"
                    failed_progress.write(log=True)
                files = publish(results)
            except Exception as save_error:
                print(f"[warn] Cannot save partial result: {type(save_error).__name__}: {save_error}",
                      file=sys.stderr, flush=True)
                raise
            finally:
                if cancelled:
                    raise asyncio.CancelledError
        else:
            files = publish(results)
        finally:
            if ocr_task is not None:
                ocr_task.cancel()
                await asyncio.gather(ocr_task, return_exceptions=True)
    if files.partial:
        print(f"[result] partial: {files.markdown}; missing pages are listed in its header",
              file=sys.stderr, flush=True)
        return 2
    print(f"[result] success: {files.markdown}", flush=True)
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
