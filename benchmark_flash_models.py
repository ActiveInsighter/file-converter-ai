from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path

import fitz
import gdown
import httpx

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
PAGES = [11, 20, 64, 66, 72, 86, 128, 149, 151, 156, 185, 186, 215, 269, 270]
PROMPT = """你正在做高精度数学 PDF 转录基准。

下面会按给定页码发送多张 PDF 页面图片。请逐页忠实转录为 Markdown，不要总结，不要改写，不要补充推导。

重点要求：
1. 数学公式必须逐字符核对，特别注意正负号、数字 0/2/4、上下标、指数中的分式、积分上下限、根号、矩阵元素、区间端点括号和变量。
2. 行内公式用 $...$，独立公式用 $$...$$。
3. 不确定时写 [无法辨认]，不要猜。
4. 不要把多页内容混在一起。
5. 每一页必须用下面的精确标记开头：
===== PAGE NNN =====
其中 NNN 是三位页码。
6. 除页面标记和该页 Markdown 外，不要输出任何解释。
"""


def parse_keys(raw: str) -> list[str]:
    return [x.strip() for x in re.split(r"[\r\n,;]+", raw or "") if x.strip()]


def download_pdf(url: str, out: Path) -> None:
    r = gdown.download(url=url, output=str(out), quiet=False, fuzzy=True)
    if not r or not out.exists():
        raise RuntimeError("Google Drive download failed")


def render_pages(pdf_path: Path, out_dir: Path) -> dict[int, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    matrix = fitz.Matrix(240 / 72.0, 240 / 72.0)
    result: dict[int, Path] = {}
    for page_no in PAGES:
        pix = doc[page_no - 1].get_pixmap(matrix=matrix, alpha=False)
        path = out_dir / f"{page_no:03d}.jpg"
        path.write_bytes(pix.tobytes("jpeg", jpg_quality=95))
        result[page_no] = path
        print(f"[render] page={page_no} bytes={path.stat().st_size}", flush=True)
    doc.close()
    return result


def build_parts(batch_pages: list[int], images: dict[int, Path]) -> list[dict]:
    page_list = ", ".join(f"{p:03d}" for p in batch_pages)
    parts: list[dict] = [{
        "text": PROMPT + f"\n本次图片对应页码（顺序一致）：{page_list}\n"
    }]
    for page_no in batch_pages:
        data = images[page_no].read_bytes()
        parts.append({
            "inlineData": {
                "mimeType": "image/jpeg",
                "data": base64.b64encode(data).decode("ascii"),
            },
            "mediaResolution": {
                "level": "MEDIA_RESOLUTION_HIGH"
            },
        })
    return parts


def extract_text(payload: dict) -> str:
    texts: list[str] = []
    for candidate in payload.get("candidates", []):
        for part in (candidate.get("content") or {}).get("parts", []):
            if part.get("thought"):
                continue
            if part.get("text"):
                texts.append(part["text"])
    text = "\n".join(texts).strip()
    if not text:
        raise RuntimeError(f"No text in Gemini response: {payload}")
    return text


def split_pages(text: str) -> dict[int, str]:
    pattern = re.compile(r"^===== PAGE (\d{3}) =====\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    result: dict[int, str] = {}
    for i, match in enumerate(matches):
        page = int(match.group(1))
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        result[page] = text[start:end].strip()
    return result


async def run_model(
    model: str,
    api_key: str,
    source_url: str,
    out_dir: Path,
    rpm: float,
    daily_limit: int,
) -> None:
    started = time.time()
    work = out_dir / "work"
    images_dir = work / "images"
    source_pdf = work / "source.pdf"
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    await asyncio.to_thread(download_pdf, source_url, source_pdf)
    images = await asyncio.to_thread(render_pages, source_pdf, images_dir)

    min_interval = 60.0 / rpm
    last_request_at = 0.0
    requests_used = 0
    manifest: dict = {
        "model": model,
        "thinking_level": "high",
        "rpm_limit": rpm,
        "daily_limit": daily_limit,
        "selected_pages": PAGES,
        "modes": {},
        "started_at_epoch": started,
    }

    timeout = httpx.Timeout(300.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for batch_size in (3, 5):
            mode_dir = out_dir / f"batch-{batch_size}"
            raw_dir = mode_dir / "raw"
            pages_dir = mode_dir / "pages"
            raw_dir.mkdir(parents=True, exist_ok=True)
            pages_dir.mkdir(parents=True, exist_ok=True)

            mode = {"batch_size": batch_size, "requests": [], "missing_pages": []}
            for offset in range(0, len(PAGES), batch_size):
                if requests_used >= daily_limit:
                    raise RuntimeError(
                        f"Benchmark daily guard reached: {requests_used}/{daily_limit}"
                    )

                batch_pages = PAGES[offset: offset + batch_size]
                wait = min_interval - (time.monotonic() - last_request_at)
                if wait > 0:
                    print(f"[rate] model={model} sleep={wait:.2f}s", flush=True)
                    await asyncio.sleep(wait)

                payload = {
                    "contents": [{
                        "role": "user",
                        "parts": build_parts(batch_pages, images),
                    }],
                    "generationConfig": {
                        "thinkingConfig": {
                            "thinkingLevel": "high"
                        }
                    },
                }

                req_started = time.time()
                response = None
                for attempt in range(1, 6):
                    retry_wait = min_interval - (
                        time.monotonic() - last_request_at
                    )
                    if retry_wait > 0:
                        print(
                            f"[rate] model={model} retry_sleep={retry_wait:.2f}s",
                            flush=True,
                        )
                        await asyncio.sleep(retry_wait)

                    last_request_at = time.monotonic()
                    requests_used += 1
                    response = await client.post(
                        f"{API_BASE}/{model}:generateContent",
                        headers={
                            "x-goog-api-key": api_key,
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    if response.status_code == 200:
                        break

                    body = response.text[:2000]
                    print(
                        f"[retry] model={model} pages={batch_pages} "
                        f"attempt={attempt} status={response.status_code} body={body}",
                        flush=True,
                    )
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        delay = 15.0
                        if retry_after:
                            try:
                                delay = max(delay, float(retry_after))
                            except ValueError:
                                pass
                        await asyncio.sleep(delay)
                    elif response.status_code >= 500:
                        delay = min(20.0 * attempt, 60.0)
                        print(
                            f"[backoff] model={model} status={response.status_code} "
                            f"sleep={delay:.0f}s",
                            flush=True,
                        )
                        await asyncio.sleep(delay)
                    else:
                        raise RuntimeError(
                            f"{model} HTTP {response.status_code}: {body}"
                        )

                    if requests_used >= daily_limit:
                        raise RuntimeError(
                            f"Benchmark daily guard reached during retry: "
                            f"{requests_used}/{daily_limit}"
                        )

                if response is None or response.status_code != 200:
                    raise RuntimeError(
                        f"{model} failed pages {batch_pages}: "
                        f"{response.status_code if response else 'no response'}"
                    )

                text = extract_text(response.json())
                raw_path = raw_dir / (
                    f"{batch_pages[0]:03d}-{batch_pages[-1]:03d}.md"
                )
                raw_path.write_text(text + "\n", encoding="utf-8")
                parsed = split_pages(text)

                for page_no, page_text in parsed.items():
                    if page_no in batch_pages:
                        (pages_dir / f"{page_no:03d}.md").write_text(
                            page_text + "\n", encoding="utf-8"
                        )

                missing = [p for p in batch_pages if p not in parsed]
                mode["missing_pages"].extend(missing)
                elapsed = round(time.time() - req_started, 2)
                mode["requests"].append({
                    "pages": batch_pages,
                    "elapsed_seconds": elapsed,
                    "parsed_pages": sorted(parsed),
                    "missing_pages": missing,
                })
                print(
                    f"[done] model={model} batch={batch_size} "
                    f"pages={batch_pages} elapsed={elapsed}s "
                    f"missing={missing}",
                    flush=True,
                )

            manifest["modes"][f"batch-{batch_size}"] = mode

    manifest["requests_used"] = requests_used
    manifest["total_elapsed_seconds"] = round(time.time() - started, 2)
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"[result] model={model} requests={requests_used} "
        f"elapsed={manifest['total_elapsed_seconds']}s",
        flush=True,
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--source-url", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--key-index", type=int, default=10)
    p.add_argument("--rpm", type=float, default=5.0)
    p.add_argument("--daily-limit", type=int, default=20)
    args = p.parse_args()

    keys = parse_keys(os.environ.get("GEMINI_API_KEYS", ""))
    if not keys:
        raise SystemExit("GEMINI_API_KEYS is empty")
    if args.key_index < 1 or args.key_index > len(keys):
        raise SystemExit(
            f"key-index {args.key_index} out of range; have {len(keys)} keys"
        )

    asyncio.run(
        run_model(
            args.model,
            keys[args.key_index - 1],
            args.source_url,
            Path(args.out_dir),
            args.rpm,
            args.daily_limit,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
