"""PaddleOCR-VL document parsing through Baidu AI Studio's async job API.

The whole page range is cut into small PDF segments that are uploaded as
separate jobs, a few at a time. Each finished job yields one Markdown draft per
page plus the figure crops PaddleOCR found on it. Pages are published as soon
as their segment finishes, so the model stage can start on the first segment
while later ones are still parsing.

OCR is an accelerator, never a single point of failure: a segment that keeps
failing resolves its pages to ``None`` and the converter transcribes those pages
from the image alone.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import fitz
import httpx

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
DEFAULT_MODEL = "PaddleOCR-VL-1.6"
OPTIONAL_PAYLOAD = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
}
#: Pages per job. Small jobs finish quickly and let the model stage start early.
SEGMENT_PAGES = 25
#: Local uploads are capped at 50 MB by the service; leave headroom.
MAX_UPLOAD_BYTES = 45 * 1024 * 1024
PARALLEL_JOBS = 4
POLL_SECONDS = 3.0
JOB_TIMEOUT_SECONDS = 900.0
SEGMENT_ATTEMPTS = 3
FIGURE_DOWNLOADS = 8

#: Service codes that mean "slow down and try again".
RETRYABLE_CODES = {10010, 12002, 500}
#: Service codes that mean OCR cannot work for the rest of this run.
FATAL_CODES = {401, 12001}


class OcrFatalError(RuntimeError):
    """Bad token or daily page quota used up: stop submitting jobs."""


class OcrRetryableError(RuntimeError):
    """Throttled, queue full, or a transient service failure."""


@dataclass(frozen=True)
class OcrPage:
    number: int
    markdown: str
    #: Figure links used in ``markdown`` ("images/p003-1.jpg") -> local file.
    figures: dict[str, Path] = field(default_factory=dict)


def parse_tokens(raw: str | None) -> list[str]:
    return list(dict.fromkeys(t.strip() for t in re.split(r"[\r\n,;]+", raw or "") if t.strip()))


def plan_segments(first: int, last: int, size: int = SEGMENT_PAGES) -> list[tuple[int, int]]:
    return [(start, min(last, start + size - 1)) for start in range(first, last + 1, size)]


def extract_segment(pdf_path: Path, first: int, last: int) -> bytes:
    with fitz.open(pdf_path) as source, fitz.open() as target:
        target.insert_pdf(source, from_page=first - 1, to_page=last - 1)
        return target.tobytes(garbage=3, deflate=True)


_IMG_TAG = re.compile(r"<img\b[^>]*?\bsrc=\"([^\"]+)\"[^>]*?/?>", re.I)
_ALT = re.compile(r"\balt=\"([^\"]*)\"", re.I)
_DIV = re.compile(r"<div\b[^>]*>((?:(?!<div\b).)*?)</div>", re.I | re.S)
_DISPLAY_MATH = re.compile(r"^[ \t]*\$\$[ \t]*(.+?)[ \t]*\$\$[ \t]*$", re.M)


def _single_dollars(line: str) -> list[int]:
    """Positions of unescaped ``$`` that are not part of ``$$``."""
    positions, index = [], 0
    while index < len(line):
        if line.startswith("$$", index):
            index += 2
        elif line[index] == "$" and (index == 0 or line[index - 1] != "\\"):
            positions.append(index)
            index += 1
        else:
            index += 1
    return positions


def tidy_inline_math(line: str) -> str:
    """``  $ d_k $`` -> ``$d_k$``: PaddleOCR pads inline math with spaces."""
    positions = _single_dollars(line)
    if not positions or len(positions) % 2:
        return line
    pieces, cursor = [], 0
    for start, end in zip(positions[::2], positions[1::2]):
        pieces.append(line[cursor:start])
        pieces.append("$" + line[start + 1 : end].strip() + "$")
        cursor = end + 1
    pieces.append(line[cursor:])
    return re.sub(r" {2,}(?=\$)", " ", "".join(pieces))


def normalize_markdown(text: str, links: dict[str, str]) -> str:
    """Turn PaddleOCR's HTML-flavoured Markdown into plain Markdown.

    ``links`` maps the image paths PaddleOCR used to our published figure
    paths; an image without a mapping (its download failed) is dropped.
    """

    def image(match: re.Match) -> str:
        target = links.get(html.unescape(match.group(1)))
        if not target:
            return ""
        alt = _ALT.search(match.group(0))
        label = alt.group(1) if alt and alt.group(1).lower() not in {"", "image"} else ""
        return f"![{label}]({target})"

    text = _IMG_TAG.sub(image, text)
    previous = None
    while previous != text:
        previous = text
        text = _DIV.sub(lambda m: m.group(1).strip(), text)
    text = _DISPLAY_MATH.sub(lambda m: f"$$\n{m.group(1).strip()}\n$$", text)
    text = "\n".join(tidy_inline_math(line).rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class PaddleOcr:
    """Runs OCR for one page range and serves each page when it is ready."""

    def __init__(
        self,
        tokens: list[str],
        *,
        client: httpx.AsyncClient,
        work_dir: Path,
        model: str = DEFAULT_MODEL,
        job_url: str = JOB_URL,
        segment_pages: int = SEGMENT_PAGES,
        parallel_jobs: int = PARALLEL_JOBS,
        poll_seconds: float = POLL_SECONDS,
        job_timeout: float = JOB_TIMEOUT_SECONDS,
        retry_pause: float = 5.0,
    ) -> None:
        self.tokens = tokens
        self.client = client
        self.work_dir = work_dir
        self.model = model
        self.job_url = job_url.rstrip("/")
        self.segment_pages = segment_pages
        self.jobs = asyncio.Semaphore(parallel_jobs)
        self.downloads = asyncio.Semaphore(FIGURE_DOWNLOADS)
        self.poll_seconds = poll_seconds
        self.job_timeout = job_timeout
        self.retry_pause = retry_pause
        self.turn = 0
        self.fatal: str | None = None
        self.pages: dict[int, asyncio.Future] = {}
        self.stats = {"jobs": 0, "pages": 0, "failed_pages": 0, "figures": 0, "seconds": 0.0}

    @property
    def enabled(self) -> bool:
        return bool(self.tokens)

    def _next_token_headers(self) -> dict[str, str]:
        """Round-robin over tokens; a job is polled with the token that owns it."""
        token = self.tokens[self.turn % len(self.tokens)]
        self.turn += 1
        return {"Authorization": f"bearer {token}"}

    def start(self, pdf_path: Path, first: int, last: int, width: int) -> asyncio.Task | None:
        loop = asyncio.get_running_loop()
        self.pages = {number: loop.create_future() for number in range(first, last + 1)}
        if not self.enabled:
            for future in self.pages.values():
                future.set_result(None)
            return None
        return asyncio.create_task(self._run(pdf_path, first, last, width))

    async def page(self, number: int) -> OcrPage | None:
        future = self.pages.get(number)
        return None if future is None else await asyncio.shield(future)

    def _resolve(self, number: int, value: OcrPage | None) -> None:
        future = self.pages.get(number)
        if future is not None and not future.done():
            future.set_result(value)

    async def _run(self, pdf_path: Path, first: int, last: int, width: int) -> None:
        started = time.monotonic()
        try:
            await asyncio.gather(*(
                self._segment(pdf_path, start, end, width)
                for start, end in plan_segments(first, last, self.segment_pages)
            ))
        finally:
            for number in self.pages:
                self._resolve(number, None)
            self.stats["seconds"] = round(time.monotonic() - started, 1)
            print(f"[ocr] finished {json.dumps(self.stats)}", flush=True)

    async def _segment(self, pdf_path: Path, first: int, last: int, width: int) -> None:
        name = f"{first}-{last}"
        try:
            async with self.jobs:
                data = await asyncio.to_thread(extract_segment, pdf_path, first, last)
                too_big = len(data) > MAX_UPLOAD_BYTES and last > first
                if not too_big:
                    pages = await self._parse_with_retries(data, name, last - first + 1)
            if too_big:
                middle = (first + last) // 2
                print(f"[ocr] {name}: {len(data) >> 20} MiB, splitting", flush=True)
                await asyncio.gather(
                    self._segment(pdf_path, first, middle, width),
                    self._segment(pdf_path, middle + 1, last, width),
                )
                return
            for offset, result in enumerate(pages):
                number = first + offset
                self._resolve(number, await self._page_from_result(number, width, result))
            self.stats["pages"] += len(pages)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every page falls back to vision only
            self.stats["failed_pages"] += last - first + 1
            print(f"[ocr] {name} failed: {type(exc).__name__}: {exc}; "
                  "these pages use image-only transcription", flush=True)
            for number in range(first, last + 1):
                self._resolve(number, None)

    async def _parse_with_retries(self, data: bytes, name: str, expected: int) -> list[dict]:
        for attempt in range(1, SEGMENT_ATTEMPTS + 1):
            if self.fatal:
                raise OcrFatalError(self.fatal)
            try:
                return await self._parse(data, name, expected)
            except OcrFatalError as exc:
                self.fatal = str(exc)
                raise
            except (OcrRetryableError, httpx.TransportError, TimeoutError, ValueError) as exc:
                if attempt == SEGMENT_ATTEMPTS:
                    raise
                pause = self.retry_pause * attempt
                print(f"[ocr] {name} attempt {attempt}/{SEGMENT_ATTEMPTS} failed: "
                      f"{type(exc).__name__}: {exc}; retrying in {pause:.0f}s", flush=True)
                await asyncio.sleep(pause)
        raise AssertionError("unreachable")

    @staticmethod
    def _check(response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        code = body.get("code", response.status_code)
        message = f"HTTP {response.status_code} code={code} {str(body.get('msg', ''))[:200]}"
        if code in FATAL_CODES or response.status_code in (401, 403):
            raise OcrFatalError(message)
        if code in RETRYABLE_CODES or response.status_code == 429 or response.status_code >= 500:
            raise OcrRetryableError(message)
        if response.status_code != 200 or code != 0:
            raise RuntimeError(message)
        return body.get("data") or {}

    async def _parse(self, data: bytes, name: str, expected: int) -> list[dict]:
        started = time.monotonic()
        headers = self._next_token_headers()
        response = await self.client.post(
            self.job_url,
            headers=headers,
            data={"model": self.model, "optionalPayload": json.dumps(OPTIONAL_PAYLOAD)},
            files={"file": (f"pages-{name}.pdf", data, "application/pdf")},
            timeout=120.0,
        )
        job_id = self._check(response)["jobId"]
        self.stats["jobs"] += 1
        print(f"[ocr] {name}: job {job_id} submitted ({len(data) >> 10} KiB)", flush=True)
        async with asyncio.timeout(self.job_timeout):
            while True:
                status = self._check(await self.client.get(
                    f"{self.job_url}/{job_id}", headers=headers, timeout=60.0))
                state = status.get("state")
                if state == "done":
                    break
                if state == "failed":
                    raise OcrRetryableError(f"job {job_id} failed: {status.get('errorMsg')}")
                await asyncio.sleep(self.poll_seconds)
            result = await self.client.get(status["resultUrl"]["jsonUrl"], timeout=120.0)
            result.raise_for_status()
        pages = [
            page
            for line in result.text.splitlines() if line.strip()
            for page in json.loads(line)["result"]["layoutParsingResults"]
        ]
        if len(pages) != expected:
            raise ValueError(f"job {job_id} returned {len(pages)} pages, expected {expected}")
        print(f"[ocr] {name}: {expected} pages in {time.monotonic() - started:.1f}s", flush=True)
        return pages

    async def _page_from_result(self, number: int, width: int, result: dict) -> OcrPage:
        markdown = result.get("markdown") or {}
        sources = markdown.get("images") or {}
        figure_dir = self.work_dir / "figures"
        figure_dir.mkdir(parents=True, exist_ok=True)

        async def fetch(index: int, source: str, url: str) -> tuple[str, str, Path] | None:
            suffix = Path(source).suffix.lower() or ".jpg"
            if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                suffix = ".jpg"
            link = f"images/p{number:0{width}d}-{index}{suffix}"
            path = figure_dir / Path(link).name
            try:
                async with self.downloads:
                    response = await self.client.get(url, timeout=60.0)
                response.raise_for_status()
                path.write_bytes(response.content)
            except Exception as exc:  # noqa: BLE001 - a missing figure is not fatal
                print(f"[ocr] page {number}: figure {source} not downloaded "
                      f"({type(exc).__name__})", flush=True)
                return None
            return source, link, path

        fetched = await asyncio.gather(*(
            fetch(index, source, url)
            for index, (source, url) in enumerate(sources.items(), start=1)
        ))
        figures = {link: path for item in fetched if item for _, link, path in [item]}
        links = {source: link for item in fetched if item for source, link, _ in [item]}
        self.stats["figures"] += len(figures)
        text = normalize_markdown(markdown.get("text") or "", links)
        return OcrPage(number, text, figures)
