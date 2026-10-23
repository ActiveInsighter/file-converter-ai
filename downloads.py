"""Atomic PDF downloads with bounded recovery from interrupted transfers."""
from pathlib import Path
import random
import time
from urllib.parse import urlparse

import gdown
import httpx
import requests


def is_google_drive_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "drive.google.com" or host.endswith(".drive.google.com")


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
                        with temporary.open("wb") as handle:
                            for data in response.iter_bytes(chunk_size=1024 * 1024):
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
