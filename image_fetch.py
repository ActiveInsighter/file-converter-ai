"""Fast parallel image fetcher for Agent Action (URL -> base64)."""
from __future__ import annotations

import asyncio
import base64
import re
from urllib.parse import urlparse

import httpx

IMAGE_MARKER_RE = re.compile(r'<!--\s*aw:image:(https?://[^\s>]+)\s*-->')
MAX_IMAGE_BYTES = 20 * 1024 * 1024

_EXT_TO_MIME = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
}


def extract_image_markers(text: str | None) -> tuple[str, list[str]]:
    """Strip ``<!-- aw:image:URL -->`` markers and return ``(clean_text, urls)``."""
    raw = str(text or '')
    if 'aw:image:' not in raw:
        return raw, []
    urls: list[str] = []

    def _collect(match: re.Match[str]) -> str:
        urls.append(match.group(1).strip())
        return ''

    cleaned = IMAGE_MARKER_RE.sub(_collect, raw)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned, urls


def collect_job_image_urls(job: dict, message: dict, *, include_history: bool = True) -> list[str]:
    """Return unique image URLs referenced in ``job`` and ``message`` in order."""
    seen: set[str] = set()
    ordered: list[str] = []

    def _add_from(value: str | None) -> None:
        _, found = extract_image_markers(value)
        for url in found:
            if url not in seen:
                seen.add(url)
                ordered.append(url)

    if include_history:
        for turn in job.get('history') or ():
            if isinstance(turn, dict):
                _add_from(turn.get('text'))
    _add_from(job.get('prompt'))
    _add_from(message.get('inputText'))
    return ordered


def _guess_mime(url: str, content_type: str | None) -> str:
    header_mime = (content_type or '').split(';')[0].strip().lower()
    if header_mime.startswith('image/'):
        return 'image/jpeg' if header_mime == 'image/jpg' else header_mime
    path = urlparse(url).path.lower()
    for ext, mime in _EXT_TO_MIME.items():
        if path.endswith(ext):
            return mime
    return 'image/png'


async def _fetch_one(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float,
    max_bytes: int,
) -> tuple[str, tuple[str, str]]:
    response = await client.get(url, timeout=timeout, follow_redirects=True)
    if response.status_code != 200:
        raise RuntimeError(f'下载图片失败（HTTP {response.status_code}）：{url}')
    data = response.content
    if not data:
        raise RuntimeError(f'图片内容为空：{url}')
    if len(data) > max_bytes:
        raise RuntimeError(f'图片超过大小限制（{max_bytes // (1024 * 1024)} MiB）：{url}')
    mime = _guess_mime(url, response.headers.get('content-type'))
    b64 = base64.b64encode(data).decode('ascii')
    return url, (mime, b64)


async def fetch_images_as_base64(
    client: httpx.AsyncClient,
    urls: list[str],
    *,
    timeout: float = 15.0,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> dict[str, tuple[str, str]]:
    """Download ``urls`` concurrently and return ``{url: (mime_type, base64_data)}``."""
    unique_urls = list(dict.fromkeys(u for u in urls if u))
    if not unique_urls:
        return {}
    results = await asyncio.gather(
        *(_fetch_one(client, url, timeout=timeout, max_bytes=max_bytes) for url in unique_urls)
    )
    return dict(results)
