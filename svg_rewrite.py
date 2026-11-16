"""Independent AI SVG rewriting for Baidu OCR figure crops (including merged diagrams)."""
from __future__ import annotations

import asyncio
import base64
import mimetypes
import random
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Awaitable, Callable

from providers import Provider

_CODE_FENCE = re.compile(r"^```(?:svg|xml|html)?\s*\n(.*?)```\s*$", re.I | re.S)
_SVG_BLOCK = re.compile(r"<svg\b[^>]*>.*?</svg>", re.I | re.S)

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class InvalidSvgError(ValueError):
    """Model returned text without a well-formed <svg>...</svg> document."""


def extract_svg(raw_text: str) -> str:
    """Extract and validate a self-contained <svg>...</svg> document from model output."""
    text = raw_text.strip()
    fence = _CODE_FENCE.match(text)
    if fence:
        text = fence.group(1).strip()
    match = _SVG_BLOCK.search(text)
    if not match:
        raise InvalidSvgError("No <svg>...</svg> element found in model output")
    svg = match.group(0).strip()
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise InvalidSvgError(f"Malformed SVG XML: {exc}") from exc
    tag = root.tag.split("}", 1)[-1].lower()
    if tag != "svg":
        raise InvalidSvgError(f"Unexpected root tag: {root.tag}")
    return svg


def _mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _MIME_BY_SUFFIX:
        return _MIME_BY_SUFFIX[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "image/png"


class SvgRewriter:
    """Schedules per-figure SVG rewriting concurrently with Markdown transcription."""

    def __init__(
        self,
        *,
        call_model_fn: Callable[..., Awaitable[str]],
        is_retryable_fn: Callable[[BaseException], bool],
        client,
        key_pool,
        provider: Provider,
        model: str,
        prompt: str,
        api_base: str = "",
        thinking_level: str = "high",
        reasoning_effort: str = "",
        max_output_tokens: int = 0,
        media_resolution: str = "ultra_high",
        fallback_models: tuple[str, ...] = (),
        concurrency: int = 10,
        attempts: int = 2,
    ) -> None:
        self._call_model = call_model_fn
        self._is_retryable = is_retryable_fn
        self.client = client
        self.key_pool = key_pool
        self.provider = provider
        self.model = model
        self.prompt = prompt
        self.api_base = api_base
        self.thinking_level = thinking_level
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.media_resolution = media_resolution
        self.fallback_models = fallback_models
        self.attempts = max(1, attempts)
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._tasks: dict[str, asyncio.Task[str | None]] = {}
        self._results: dict[str, str] = {}
        self._failed: dict[str, str] = {}

    def schedule_figures(self, figures: dict[str, Path]) -> None:
        """Queue SVG rewriting for every figure crop not yet scheduled."""
        for link, image_path in figures.items():
            if link in self._tasks or link in self._results or link in self._failed:
                continue
            if not image_path.is_file():
                continue
            self._tasks[link] = asyncio.create_task(self._rewrite_one(link, image_path))

    async def _rewrite_one(self, link: str, image_path: Path) -> str | None:
        async with self._semaphore:
            started = time.monotonic()
            data = await asyncio.to_thread(image_path.read_bytes)
            encoded = base64.b64encode(data).decode("ascii")
            image_part: dict = {
                "inlineData": {
                    "mimeType": _mime_type(image_path),
                    "data": encoded,
                }
            }
            if self.media_resolution and self.media_resolution != "unspecified":
                image_part["mediaResolution"] = self.media_resolution
            parts = [{"text": self.prompt}, image_part]
            chunk_name = f"svg:{Path(link).stem}"

            last_error: Exception | None = None
            for attempt in range(1, self.attempts + 1):
                try:
                    raw = await self._call_model(
                        self.client,
                        self.key_pool,
                        self.provider,
                        self.model,
                        parts,
                        chunk_name,
                        self.thinking_level,
                        self.fallback_models,
                        self.reasoning_effort,
                        self.api_base,
                        self.max_output_tokens,
                    )
                    svg = extract_svg(raw)
                    svg_path = image_path.with_suffix(".svg")
                    await asyncio.to_thread(svg_path.write_text, svg + "\n", encoding="utf-8")
                    svg_link = Path(link).with_suffix(".svg").as_posix()
                    self._results[link] = svg_link
                    elapsed = time.monotonic() - started
                    print(f"[svg] {link} -> {svg_link} ({elapsed:.2f}s)", flush=True)
                    return svg_link
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    retryable = isinstance(exc, InvalidSvgError) or self._is_retryable(exc)
                    if not retryable or attempt >= self.attempts:
                        break
                    pause = random.uniform(1.5, 4.0) * attempt
                    print(
                        f"[svg-retry] {link}: attempt {attempt}/{self.attempts} failed "
                        f"with {type(exc).__name__}: {exc}; retrying in {pause:.1f}s",
                        flush=True,
                    )
                    await asyncio.sleep(pause)

            reason = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown error"
            self._failed[link] = reason[:300]
            print(f"[svg] {link}: failed ({reason}); keeping original image", flush=True)
            return None

    def completed_map(self) -> dict[str, str]:
        """Return a snapshot of original figure link -> generated SVG link."""
        return dict(self._results)

    async def finish(self) -> dict[str, str]:
        """Wait for all scheduled SVG rewrite tasks to complete."""
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        return dict(self._results)

    def cancel(self) -> None:
        """Cancel any still-running SVG tasks."""
        for task in self._tasks.values():
            if not task.done():
                task.cancel()

    def summary(self) -> dict:
        return {
            "provider": self.provider.name,
            "model": self.model,
            "scheduled": len(self._tasks),
            "succeeded": len(self._results),
            "failed": len(self._failed),
        }
