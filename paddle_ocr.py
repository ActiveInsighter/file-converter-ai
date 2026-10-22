"""PaddleOCR-VL document parsing through Baidu AI Studio's async job API.

The whole page range is cut into segments that run as separate jobs, a few at a
time. By default each job names the original PDF link plus a ``pageRanges``
slice, so Baidu downloads the file itself and nothing is uploaded from the
runner (GitHub -> Baidu uploads crawl at 20-25 KB/s). When the service will not
fetch the link (unsupported URL, Google Drive, a fetch failure) the segment is
cut out of the local PDF and uploaded instead. Each finished job yields one
Markdown draft per page plus the figure crops PaddleOCR found on it. Pages are
published as soon as their segment finishes, so the model stage can start on
the first segment while later ones are still parsing.

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
#: Segments are halved until an upload is at most this big. From GitHub's
#: runners a 375 KiB upload takes ~3 s while 1.8 MB stalls for ~90 s, so many
#: small parallel uploads beat one large one.
TARGET_UPLOAD_BYTES = 1024 * 1024
#: The service accepts only a few pending jobs per user and answers 10010
#: (queue full) beyond that, after the upload has already been paid for.
PARALLEL_JOBS = 3
POLL_SECONDS = 3.0
JOB_TIMEOUT_SECONDS = 900.0
#: Attempts per segment. A full submission queue (10010) clears within
#: minutes, so retries back off exponentially up to a minute apart.
SEGMENT_ATTEMPTS = 6
MAX_RETRY_PAUSE = 60.0
FIGURE_DOWNLOADS = 8

#: Service codes that mean "slow down and try again".
RETRYABLE_CODES = {10010, 12002, 500}
#: Service codes that mean OCR cannot work for the rest of this run.
FATAL_CODES = {401, 12001}
#: Submission codes that mean "this link cannot be fetched or parsed":
#: unrecognised URL, unsupported format, unparsable content.
REMOTE_REJECTED_CODES = {10002, 10004, 10005}


class OcrFatalError(RuntimeError):
    """Bad token or daily page quota used up: stop submitting jobs."""


class OcrRetryableError(RuntimeError):
    """Throttled, queue full, or a transient service failure."""


class OcrSourceRejected(RuntimeError):
    """The service will not fetch this link; upload the segment instead."""


class _SplitSegment(Exception):
    """The segment is too big to upload in one piece."""


def remote_candidates(url: str | None) -> list[str]:
    """Links to offer the service, best first.

    The service judges the format by the link's suffix and rejects extension-
    less links such as ``https://arxiv.org/pdf/2401.00001`` with 10004. A
    ``#.pdf`` fragment never reaches the origin server but gives the link a
    ``.pdf`` suffix, so it is tried second.
    """
    if not url:
        return []
    candidates = [url]
    path = url.split("#", 1)[0].split("?", 1)[0]
    if "#" not in url and not path.lower().endswith(".pdf"):
        candidates.append(url + "#.pdf")
    return candidates


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
        retry_pause: float = 10.0,
        source_url: str | None = None,
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
        #: Untested or accepted links, best first; empty means "upload".
        self.remote_urls = remote_candidates(source_url)
        #: Serialises remote submissions while the link is being probed, so a
        #: rejected link is tried once, not once per segment.
        self.remote_lock = asyncio.Lock()
        self.started = time.monotonic()
        self.stats = {"jobs": 0, "remote_jobs": 0, "uploaded_jobs": 0, "pages": 0,
                      "failed_pages": 0, "figures": 0, "seconds": 0.0}

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
        self.started = time.monotonic()
        self.pages = {number: loop.create_future() for number in range(first, last + 1)}
        if not self.enabled:
            for future in self.pages.values():
                future.set_result(None)
            return None
        return asyncio.create_task(self._run(pdf_path, first, last, width))

    async def page(self, number: int, timeout: float | None = None) -> OcrPage | None:
        """The page's draft; ``None`` if OCR failed or ``timeout`` ran out first."""
        future = self.pages.get(number)
        if future is None:
            return None
        if future.done() or timeout is None:
            return await asyncio.shield(future)
        try:
            return await asyncio.wait_for(asyncio.shield(future), max(0.0, timeout))
        except TimeoutError:
            return None

    def remaining(self, wait: float) -> float:
        """Seconds left until ``wait`` seconds after OCR started."""
        return self.started + wait - time.monotonic()

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
            try:
                async with self.jobs:
                    pages = await self._parse_with_retries(pdf_path, first, last, name)
            except _SplitSegment:
                middle = (first + last) // 2
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

    async def _parse_with_retries(self, pdf_path: Path, first: int, last: int,
                                  name: str) -> list[dict]:
        upload: bytes | None = None
        for attempt in range(1, SEGMENT_ATTEMPTS + 1):
            if self.fatal:
                raise OcrFatalError(self.fatal)
            remote = False
            try:
                job = await self._submit_remote(first, last, name)
                if job is None:
                    if upload is None:
                        upload = await asyncio.to_thread(extract_segment, pdf_path, first, last)
                        if len(upload) > TARGET_UPLOAD_BYTES and last > first:
                            print(f"[ocr] {name}: {len(upload) >> 10} KiB, splitting", flush=True)
                            raise _SplitSegment
                    job = await self._submit_upload(upload, name)
                else:
                    remote = True
                return await self._collect(job, name, last - first + 1)
            except OcrFatalError as exc:
                self.fatal = str(exc)
                raise
            except (OcrRetryableError, httpx.TransportError, TimeoutError, ValueError) as exc:
                if remote and self.remote_urls:
                    # A link the service accepted but then could not fetch or
                    # parse will not get better; upload from now on.
                    print(f"[ocr] {name}: remote job failed ({exc}); "
                          "uploading segments from now on", flush=True)
                    self.remote_urls.clear()
                if attempt == SEGMENT_ATTEMPTS:
                    raise
                pause = min(MAX_RETRY_PAUSE, self.retry_pause * 2 ** (attempt - 1))
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
        if code in REMOTE_REJECTED_CODES:
            raise OcrSourceRejected(message)
        if code in RETRYABLE_CODES or response.status_code == 429 or response.status_code >= 500:
            raise OcrRetryableError(message)
        if response.status_code != 200 or code != 0:
            raise RuntimeError(message)
        return body.get("data") or {}

    async def _submit_remote(self, first: int, last: int,
                             name: str) -> tuple[str, dict[str, str]] | None:
        """Ask the service to fetch the pages itself; ``None`` means upload."""
        if not self.remote_urls:
            return None
        async with self.remote_lock:
            while self.remote_urls:
                url = self.remote_urls[0]
                headers = self._next_token_headers()
                response = await self.client.post(
                    self.job_url,
                    headers=headers,
                    json={"fileUrl": url, "model": self.model, "pageRanges": f"{first}-{last}",
                          "optionalPayload": OPTIONAL_PAYLOAD},
                    timeout=60.0,
                )
                try:
                    job_id = self._check(response)["jobId"]
                except OcrSourceRejected as exc:
                    print(f"[ocr] link {url} rejected ({exc})", flush=True)
                    self.remote_urls.pop(0)
                    continue
                # Later segments go straight to the accepted link.
                del self.remote_urls[1:]
                self.stats["jobs"] += 1
                self.stats["remote_jobs"] += 1
                print(f"[ocr] {name}: job {job_id} submitted by link", flush=True)
                return job_id, headers
        print("[ocr] the service will not fetch this link; uploading segments", flush=True)
        return None

    async def _submit_upload(self, data: bytes, name: str) -> tuple[str, dict[str, str]]:
        started = time.monotonic()
        headers = self._next_token_headers()
        response = await self.client.post(
            self.job_url,
            headers=headers,
            data={"model": self.model, "optionalPayload": json.dumps(OPTIONAL_PAYLOAD)},
            files={"file": (f"pages-{name}.pdf", data, "application/pdf")},
            timeout=120.0,
        )
        upload_seconds = time.monotonic() - started
        try:
            job_id = self._check(response)["jobId"]
        except OcrSourceRejected as exc:
            raise RuntimeError(str(exc)) from exc
        self.stats["jobs"] += 1
        self.stats["uploaded_jobs"] += 1
        print(f"[ocr] {name}: job {job_id} submitted ({len(data) >> 10} KiB "
              f"in {upload_seconds:.1f}s)", flush=True)
        return job_id, headers

    async def _collect(self, job: tuple[str, dict[str, str]], name: str,
                       expected: int) -> list[dict]:
        job_id, headers = job
        started = time.monotonic()
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
        print(f"[ocr] {name}: {expected} pages in {time.monotonic() - started:.1f}s "
              f"({time.monotonic() - self.started:.0f}s since start)", flush=True)
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
