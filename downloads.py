"""Atomic PDF downloads with bounded recovery from interrupted transfers."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import random
import time
from urllib.parse import urlparse

import gdown
import httpx
import requests

#: Below this size one connection is as good as many.
PARALLEL_MIN_BYTES = 32 * 1024 * 1024
#: One connection from the origin delivered ~11 MB/s (723 MB took 64 s while the
#: scan was waiting to be cut, rendered and sent to OCR); parallel ranges scale
#: with the number of connections until the runner's own link is the limit.
RANGE_CONNECTIONS = 8
RANGE_BLOCK_BYTES = 16 * 1024 * 1024
RANGE_BLOCK_ATTEMPTS = 3


def is_google_drive_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "drive.google.com" or host.endswith(".drive.google.com")


class RangesUnsupported(RuntimeError):
    """The origin answered a Range request with something other than that range."""


def range_plan(response: httpx.Response) -> tuple[int, dict[str, str]] | None:
    """Size and validator headers when the response promises byte ranges worth using."""
    if not hasattr(os, "pwrite"):
        return None
    headers = response.headers
    if "bytes" not in headers.get("accept-ranges", "").lower():
        return None
    if headers.get("content-encoding", "identity").lower() != "identity":
        return None  # the length counts compressed bytes; ranges would not line up
    try:
        total = int(headers.get("content-length", ""))
    except ValueError:
        return None
    if total < PARALLEL_MIN_BYTES:
        return None
    validator = headers.get("etag") or headers.get("last-modified")
    # If-Range turns a changed file into a full 200 answer, which is detected.
    return total, ({"If-Range": validator} if validator and not validator.startswith("W/") else {})


def range_client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(120.0, connect=30.0),
        limits=httpx.Limits(max_connections=RANGE_CONNECTIONS,
                            max_keepalive_connections=RANGE_CONNECTIONS),
    )


def download_ranges(url: str, destination: Path, total: int, extra_headers: dict[str, str]) -> None:
    """Fetch ``total`` bytes as independent ranges over several connections."""
    blocks = [(start, min(total, start + RANGE_BLOCK_BYTES) - 1)
              for start in range(0, total, RANGE_BLOCK_BYTES)]
    with destination.open("wb") as handle:
        handle.truncate(total)
    descriptor = os.open(destination, os.O_WRONLY)
    try:
        with range_client() as client, ThreadPoolExecutor(
                max_workers=min(RANGE_CONNECTIONS, len(blocks)),
                thread_name_prefix="pdf-download") as pool:
            def fetch(block: tuple[int, int]) -> None:
                start, end = block
                for attempt in range(RANGE_BLOCK_ATTEMPTS):
                    try:
                        with client.stream("GET", url, headers={
                                "Range": f"bytes={start}-{end}", **extra_headers}) as response:
                            if response.status_code != 206:
                                if response.status_code in {408, 429} or response.status_code >= 500:
                                    response.raise_for_status()
                                raise RangesUnsupported(f"HTTP {response.status_code} for bytes={start}-{end}")
                            if not response.headers.get("content-range", "").startswith(f"bytes {start}-{end}/"):
                                raise RangesUnsupported(
                                    f"Content-Range {response.headers.get('content-range')!r} "
                                    f"for bytes={start}-{end}")
                            offset = start
                            for data in response.iter_bytes(chunk_size=1024 * 1024):
                                os.pwrite(descriptor, data, offset)
                                offset += len(data)
                        if offset != end + 1:
                            raise httpx.RemoteProtocolError(f"block {start}-{end} ended early")
                        return
                    except (httpx.TransportError, httpx.HTTPStatusError):
                        if attempt == RANGE_BLOCK_ATTEMPTS - 1:
                            raise
                        time.sleep(random.uniform(0.5, 1.5) * 2 ** attempt)

            try:
                list(pool.map(fetch, blocks))
            except BaseException:
                pool.shutdown(wait=True, cancel_futures=True)
                raise
    finally:
        os.close(descriptor)


def download_pdf(source_url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        for attempt in range(3):
            temporary.unlink(missing_ok=True)
            try:
                if is_google_drive_url(source_url):
                    result = gdown.download(url=source_url, output=str(temporary), quiet=True, fuzzy=True)
                    if not result or not temporary.exists():
                        raise RuntimeError("Google Drive download failed. Share the file as Anyone with the link.")
                else:
                    with httpx.stream("GET", source_url, follow_redirects=True, timeout=120.0) as response:
                        response.raise_for_status()
                        plan = range_plan(response)
                        if plan is None:
                            with temporary.open("wb") as handle:
                                for data in response.iter_bytes(chunk_size=1024 * 1024):
                                    handle.write(data)
                    if plan is not None:
                        started = time.monotonic()
                        try:
                            download_ranges(str(response.url), temporary, *plan)
                            print(f"[download] {plan[0] / 1e6:.0f} MB over {RANGE_CONNECTIONS} connections "
                                  f"in {time.monotonic() - started:.1f}s", flush=True)
                        except (RangesUnsupported, httpx.HTTPError, OSError) as exc:
                            print(f"[download] ranged download failed ({type(exc).__name__}: {exc}); "
                                  "streaming the whole file instead", flush=True)
                            temporary.unlink(missing_ok=True)
                            with httpx.stream("GET", source_url, follow_redirects=True, timeout=120.0) as whole:
                                whole.raise_for_status()
                                with temporary.open("wb") as handle:
                                    for data in whole.iter_bytes(chunk_size=1024 * 1024):
                                        handle.write(data)
                with temporary.open("rb") as handle:
                    if handle.read(5) != b"%PDF-":
                        raise ValueError("Downloaded source is not a PDF; use a direct/public PDF link")
                temporary.replace(destination)
                return
            except (httpx.TransportError, httpx.HTTPStatusError, requests.RequestException) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", 0)
                if attempt == 2 or (status and status not in {408, 429} and status < 500):
                    raise
                delay = random.uniform(1, 2) * 2 ** attempt
                print(f"[download-retry] {type(exc).__name__} status={status or 'transport'} "
                      f"attempt={attempt + 1}/3 wait={delay:.1f}s", flush=True)
                time.sleep(delay)
    finally:
        temporary.unlink(missing_ok=True)
