"""PaddleOCR-VL document parsing through Baidu AI Studio's async job API.

The whole page range is cut into segments that run as separate jobs, a few at a
time. By default each job names the original PDF link plus a ``pageRanges``
slice, so Baidu downloads the file itself and nothing is uploaded from the
runner (GitHub -> Baidu uploads crawl at 20-25 KB/s). When the service will not
fetch the link (unsupported URL, Google Drive, a fetch failure) the segment is
cut out of the local PDF and uploaded instead (scanned pages are re-encoded as
compact JPEG pages first). Each finished job yields one
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
#: The service downloads the whole linked file while each submission is open:
#: 28 MB from Cloudflare took 13-30 s and 47 MB took 23 s (about 2 MB/s), so a
#: fixed limit wrongly gives up on big files: a 120 MB book needs about a minute
#: per submission. The limit grows with the file at a conservative 1 MB/s.
REMOTE_SUBMIT_TIMEOUT = 60.0
REMOTE_SUBMIT_TIMEOUT_MAX = 300.0
REMOTE_SUBMIT_BYTES_PER_SECOND = 1_000_000
#: AI Studio refuses linked files above 200 MB; larger files are uploaded.
REMOTE_MAX_BYTES = 200 * 1000 * 1000
#: With the upload relay configured, a file above this size is not fetched
#: whole for every segment: each segment is cut out here, put on the relay
#: (GitHub -> Cloudflare is fast) and fetched from there by link.
RELAY_PREFERRED_ABOVE_BYTES = 40 * 1000 * 1000
#: The relay accepts one request of at most 90 MiB; bigger segments are halved.
RELAY_MAX_BYTES = 80 * 1024 * 1024
#: A link job that is not done by then is abandoned for an upload, which
#: finishes within about three minutes even at 20-25 KB/s.
REMOTE_JOB_TIMEOUT = 300.0
#: Attempts per segment. A full submission queue (10010) clears within
#: minutes, so retries back off exponentially up to a minute apart.
SEGMENT_ATTEMPTS = 6
MAX_RETRY_PAUSE = 60.0
FIGURE_DOWNLOADS = 8
#: Scanned pages often weigh 0.5-1.5 MB each in the original PDF. Uploading
#: them at 20-25 KB/s takes most of an hour for a book, so a segment above this
#: average is re-encoded as JPEG page images, which are 4-8x smaller and still
#: well above what the OCR model needs.
COMPACT_ABOVE_BYTES_PER_PAGE = 256 * 1024
COMPACT_DPI = 150
COMPACT_JPEG_QUALITY = 80

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


class OcrJobFailed(OcrRetryableError):
    """The service accepted the job, then reported it as failed."""


class OcrSourceRejected(RuntimeError):
    """The service will not fetch this link; upload the segment instead."""


class _SplitSegment(Exception):
    """The segment is too big to upload in one piece."""


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


def link_submit_timeout(size_bytes: int) -> float:
    """Seconds one link submission may take while the service downloads the file."""
    scaled = 30.0 + size_bytes / REMOTE_SUBMIT_BYTES_PER_SECOND
    return min(REMOTE_SUBMIT_TIMEOUT_MAX, max(REMOTE_SUBMIT_TIMEOUT, scaled))


def extract_segment(pdf_path: Path, first: int, last: int, compact: bool = True) -> bytes:
    """The pages as their own PDF; heavy scans are re-encoded unless ``compact`` is off."""
    with fitz.open(pdf_path) as source, fitz.open() as target:
        target.insert_pdf(source, from_page=first - 1, to_page=last - 1)
        data = target.tobytes(garbage=3, deflate=True)
        if not compact or len(data) <= COMPACT_ABOVE_BYTES_PER_PAGE * (last - first + 1):
            return data
        compact = _rasterise(source, first, last)
    return compact if len(compact) < len(data) else data


def _rasterise(source: fitz.Document, first: int, last: int) -> bytes:
    """The pages as JPEG images at the original page size: small enough to upload."""
    with fitz.open() as target:
        for number in range(first, last + 1):
            page = source[number - 1]
            image = page.get_pixmap(dpi=COMPACT_DPI, alpha=False).tobytes(
                "jpeg", jpg_quality=COMPACT_JPEG_QUALITY)
            target.new_page(width=page.rect.width, height=page.rect.height).insert_image(
                fitz.Rect(0, 0, page.rect.width, page.rect.height), stream=image)
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
        relay_url: str | None = None,
        relay_token: str | None = None,
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
        #: The link the service fetches; ``None`` means "upload". The first
        #: submission doubles as a probe: a rejected link
        #: (``https://arxiv.org/pdf/<id>`` gets 10004), a submission that
        #: times out, or a link job that fails switches the run to uploads.
        self.remote_url = source_url or None
        #: Holds the other segments back until the first link submission has
        #: worked, so a bad link costs one probe, not one per segment.
        self.remote_lock = asyncio.Lock()
        self.remote_proven = False
        self.submit_timeout = REMOTE_SUBMIT_TIMEOUT
        #: Upload relay (the Remote Worker's R2 bucket): segments put there are
        #: fetched by link, avoiding the 20-25 KB/s upload to the service.
        self.relay_url = relay_url if relay_url and relay_token else None
        self.relay_token = relay_token
        self.started = time.monotonic()
        self.stats = {"jobs": 0, "remote_jobs": 0, "relay_jobs": 0, "uploaded_jobs": 0,
                      "pages": 0, "failed_pages": 0, "figures": 0, "seconds": 0.0}

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
        if self.remote_url:
            size = pdf_path.stat().st_size
            if self.relay_url and size > RELAY_PREFERRED_ABOVE_BYTES:
                print(f"[ocr] {size / 1e6:.0f} MB source: segments go through the relay "
                      "instead of fetching the whole file per segment", flush=True)
                self.remote_url = None
            elif size > REMOTE_MAX_BYTES:
                self._abandon_link(f"{size / 1e6:.0f} MB is above the 200 MB link limit")
            else:
                self.submit_timeout = link_submit_timeout(size)
                print(f"[ocr] link submissions may take {self.submit_timeout:.0f}s "
                      f"for {size / 1e6:.0f} MB", flush=True)
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
            kind = "upload"
            try:
                job = await self._submit_remote(first, last, name)
                if job is not None:
                    kind = "link"
                else:
                    job = await self._submit_relay(pdf_path, first, last, name)
                    if job is not None:
                        kind = "relay"
                if job is None:
                    if upload is None:
                        upload = await asyncio.to_thread(extract_segment, pdf_path, first, last)
                        if len(upload) > TARGET_UPLOAD_BYTES and last > first:
                            print(f"[ocr] {name}: {len(upload) >> 10} KiB, splitting", flush=True)
                            raise _SplitSegment
                    job = await self._submit_upload(upload, name)
                timeout = self.job_timeout if kind == "upload" else min(self.job_timeout, REMOTE_JOB_TIMEOUT)
                return await self._collect(job, name, last - first + 1, timeout)
            except OcrFatalError as exc:
                self.fatal = str(exc)
                raise
            except (OcrRetryableError, httpx.TransportError, TimeoutError, ValueError) as exc:
                if kind != "upload" and isinstance(exc, (OcrJobFailed, ValueError, TimeoutError)):
                    # A link job that failed, came back wrong or stalled will
                    # not get better; upload this and every later segment.
                    reason = f"{name}: {type(exc).__name__}: {exc}"
                    if kind == "link":
                        self._abandon_link(reason)
                    else:
                        self._abandon_relay(reason)
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

    def _abandon_link(self, reason: str) -> None:
        if self.remote_url:
            print(f"[ocr] not using the link any more ({reason}); "
                  "uploading segments instead", flush=True)
            self.remote_url = None

    def _abandon_relay(self, reason: str) -> None:
        if self.relay_url:
            print(f"[ocr] not using the relay any more ({reason}); "
                  "uploading segments instead", flush=True)
            self.relay_url = None

    async def _submit_relay(self, pdf_path: Path, first: int, last: int,
                            name: str) -> tuple[str, dict[str, str]] | None:
        """Put the segment on the relay and let the service fetch it; ``None`` means upload."""
        if not self.relay_url:
            return None
        data = await asyncio.to_thread(extract_segment, pdf_path, first, last, False)
        if len(data) > RELAY_MAX_BYTES and last > first:
            print(f"[ocr] {name}: {len(data) >> 20} MiB, splitting for the relay", flush=True)
            raise _SplitSegment
        started = time.monotonic()
        headers = self._next_token_headers()
        try:
            url = await self._relay_upload(data, name)
            response = await self.client.post(
                self.job_url,
                headers=headers,
                json={"fileUrl": url, "model": self.model,
                      "pageRanges": f"1-{last - first + 1}", "optionalPayload": OPTIONAL_PAYLOAD},
                timeout=link_submit_timeout(len(data)),
            )
            job_id = self._check(response)["jobId"]
        except (OcrFatalError, OcrRetryableError):
            raise
        except Exception as exc:  # noqa: BLE001 - refused upload, rejected link, odd answer
            self._abandon_relay(f"{name}: {type(exc).__name__}: {exc}")
            return None
        self.stats["jobs"] += 1
        self.stats["relay_jobs"] += 1
        print(f"[ocr] {name}: job {job_id} submitted through the relay "
              f"({len(data) >> 10} KiB in {time.monotonic() - started:.1f}s)", flush=True)
        return job_id, headers

    async def _relay_upload(self, data: bytes, name: str) -> str:
        """Store ``data`` on the relay and return its public link."""
        try:
            response = await self.client.put(
                self.relay_url,
                content=data,
                headers={"Authorization": f"Bearer {self.relay_token}",
                         "Content-Type": "application/pdf",
                         "X-File-Name": f"pages-{name}.pdf"},
                timeout=120.0,
            )
        except httpx.TransportError as exc:
            raise OcrRetryableError(f"relay upload failed: {type(exc).__name__}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise OcrRetryableError(f"relay upload HTTP {response.status_code}")
        if response.status_code != 201:
            raise RuntimeError(f"relay upload HTTP {response.status_code}: {response.text[:200]}")
        url = response.json().get("sourceUrl")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise RuntimeError("relay upload returned no https link")
        return url

    async def _submit_remote(self, first: int, last: int,
                             name: str) -> tuple[str, dict[str, str]] | None:
        """Ask the service to fetch the pages itself; ``None`` means upload."""
        if not self.remote_url:
            return None
        headers = self._next_token_headers()
        if self.remote_proven:
            job_id = await self._post_link(first, last, headers)
        else:
            async with self.remote_lock:
                job_id = await self._post_link(first, last, headers)
                self.remote_proven = job_id is not None
        if job_id is None:
            return None
        self.stats["jobs"] += 1
        self.stats["remote_jobs"] += 1
        print(f"[ocr] {name}: job {job_id} submitted by link", flush=True)
        return job_id, headers

    async def _post_link(self, first: int, last: int, headers: dict[str, str]) -> str | None:
        """Submit one page range by link; ``None`` once the link is abandoned."""
        url = self.remote_url
        if not url:
            return None
        try:
            response = await self.client.post(
                self.job_url,
                headers=headers,
                json={"fileUrl": url, "model": self.model,
                      "pageRanges": f"{first}-{last}", "optionalPayload": OPTIONAL_PAYLOAD},
                timeout=self.submit_timeout,
            )
            return self._check(response)["jobId"]
        except (OcrFatalError, OcrRetryableError):
            # Bad token, used-up quota or a full queue: not the link's fault.
            raise
        except Exception as exc:  # noqa: BLE001 - rejected, timed out, odd answer
            self._abandon_link(f"{type(exc).__name__}: {exc}")
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
                       expected: int, timeout: float) -> list[dict]:
        job_id, headers = job
        started = time.monotonic()
        async with asyncio.timeout(timeout):
            while True:
                status = self._check(await self.client.get(
                    f"{self.job_url}/{job_id}", headers=headers, timeout=60.0))
                state = status.get("state")
                if state == "done":
                    break
                if state == "failed":
                    raise OcrJobFailed(f"job {job_id} failed: {status.get('errorMsg')}")
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
