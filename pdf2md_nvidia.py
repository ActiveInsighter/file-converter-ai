from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

import httpx

from pdf2md import (
    DEFAULT_PROMPT,
    Chunk,
    build_chunks,
    download_pdf,
    merge_markdown,
    parse_api_keys,
    render_pdf,
    repair_multiline_math_delimiters,
    validate_markdown_output,
)

DEFAULT_NVIDIA_API_BASE = "https://integrate.api.nvidia.com/v1"
DEFAULT_NVIDIA_MODEL = "deepseek-ai/deepseek-v4.1-flash"
MAX_INLINE_REQUEST_BYTES = 18 * 1024 * 1024


class NvidiaKeyPool:
    def __init__(self, keys: list[str], rpm_per_key: float) -> None:
        if not keys:
            raise ValueError("No NVIDIA API keys configured")
        if rpm_per_key <= 0:
            raise ValueError("rpm_per_key must be > 0")
        self.keys = keys
        self.interval = (60.0 / rpm_per_key) * 1.05
        self.next_index = 0
        self.cooldown_until = [0.0 for _ in keys]
        self.next_allowed = [0.0 for _ in keys]
        self.disabled = [False for _ in keys]
        self.requests = [0 for _ in keys]
        self.success = [0 for _ in keys]
        self.http_429 = [0 for _ in keys]
        self.errors = [0 for _ in keys]
        self.lock = asyncio.Lock()

    async def acquire(self) -> tuple[int, str]:
        while True:
            async with self.lock:
                now = time.monotonic()
                ready = [
                    i
                    for i in range(len(self.keys))
                    if (
                        not self.disabled[i]
                        and self.cooldown_until[i] <= now
                        and self.next_allowed[i] <= now
                    )
                ]
                if ready:
                    index = min(
                        ready,
                        key=lambda i: (i - self.next_index) % len(self.keys),
                    )
                    self.next_index = (index + 1) % len(self.keys)
                    self.requests[index] += 1
                    self.next_allowed[index] = now + self.interval
                    return index, self.keys[index]

                pending = [
                    max(self.cooldown_until[i], self.next_allowed[i])
                    for i in range(len(self.keys))
                    if not self.disabled[i]
                ]
                if not pending:
                    raise RuntimeError("All NVIDIA API keys are disabled")
                wait_for = max(0.05, min(pending) - now)
            await asyncio.sleep(wait_for)

    async def mark_success(self, index: int) -> None:
        async with self.lock:
            self.success[index] += 1

    async def mark_error(self, index: int) -> None:
        async with self.lock:
            self.errors[index] += 1

    async def rate_limited(self, index: int, seconds: float) -> None:
        async with self.lock:
            self.http_429[index] += 1
            self.cooldown_until[index] = max(
                self.cooldown_until[index],
                time.monotonic() + seconds,
            )

    async def disable(self, index: int) -> None:
        async with self.lock:
            self.disabled[index] = True

    def summary(self) -> dict:
        return {
            "keys": {
                str(i + 1): {
                    "requests": self.requests[i],
                    "success": self.success[i],
                    "http_429": self.http_429[i],
                    "errors": self.errors[i],
                    "disabled": self.disabled[i],
                }
                for i in range(len(self.keys))
            }
        }


def page_label(chunk: Chunk) -> str:
    if chunk.start_page == chunk.end_page:
        return f"第 {chunk.start_page} 页"
    return f"第 {chunk.start_page}-{chunk.end_page} 页"


def image_content(path: Path) -> tuple[dict, int]:
    data = path.read_bytes()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(data).decode("ascii")
    return (
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        },
        len(data),
    )


def make_content(chunk: Chunk, prompt: str) -> tuple[list[dict], int]:
    text = (
        f"以下图片按顺序对应 PDF {page_label(chunk)}。\n"
        f"请严格按图片顺序处理。\n\n{prompt.strip()}"
    )
    content: list[dict] = [{"type": "text", "text": text}]
    raw_bytes = 0
    for path in chunk.image_paths:
        image, size = image_content(path)
        content.append(image)
        raw_bytes += size

    estimated = int(raw_bytes * 4 / 3) + len(text.encode("utf-8")) + 64 * 1024
    if estimated > MAX_INLINE_REQUEST_BYTES:
        raise ValueError(
            f"Chunk {chunk.stem} is about {estimated / 1024 / 1024:.1f} MiB "
            "after base64. Lower images_per_request or dpi."
        )
    return content, estimated


def make_verification_content(
    chunk: Chunk,
    prompt: str,
    draft: str,
) -> tuple[list[dict], int]:
    verify_prompt = f"""请逐页对照图片，审校下面这份 Markdown 草稿并直接返回修正后的完整 Markdown。

审校重点：
1. 逐字符核对所有数学公式：正负号、分数、根号、上下标、积分/求和上下限、矩阵、转置、希腊字母、括号和数字。
2. 检查是否有漏行、错行、重复、跨页错位、把页眉页脚或扫描水印误当正文。
3. 检查 Markdown/LaTeX 格式，行内公式使用成对 $...$，多行公式使用成对 $$...$$。
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
    content: list[dict] = [{"type": "text", "text": verify_prompt}]
    raw_bytes = 0
    for path in chunk.image_paths:
        image, size = image_content(path)
        content.append(image)
        raw_bytes += size

    estimated = (
        int(raw_bytes * 4 / 3)
        + len(verify_prompt.encode("utf-8"))
        + 64 * 1024
    )
    if estimated > MAX_INLINE_REQUEST_BYTES:
        raise ValueError(
            f"Verification chunk {chunk.stem} is about "
            f"{estimated / 1024 / 1024:.1f} MiB after base64. "
            "Lower images_per_request or dpi."
        )
    return content, estimated


def strip_markdown_fence(text: str) -> str:
    value = text.strip()
    tilde = "~~~"
    tick = chr(96) * 3
    for prefix, closing in (
        (tilde + "markdown", tilde),
        (tilde + "md", tilde),
        (tick + "markdown", tick),
        (tick + "md", tick),
    ):
        if value.startswith(prefix) and value.endswith(closing):
            return value[len(prefix) : -len(closing)].strip()
    return value


def extract_text(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"NVIDIA returned no choices: {payload}")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        result = content.strip()
    elif isinstance(content, list):
        result = "\n".join(
            str(item.get("text", "")).strip()
            for item in content
            if isinstance(item, dict) and item.get("text")
        ).strip()
    else:
        result = ""

    if not result:
        raise RuntimeError(
            "NVIDIA returned no answer text. "
            f"finish_reason={choices[0].get('finish_reason')}"
        )
    return strip_markdown_fence(result)


async def call_nvidia(
    client: httpx.AsyncClient,
    key_pool: NvidiaKeyPool,
    model: str,
    content: list[dict],
    chunk_name: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
    api_base: str,
) -> str:
    url = api_base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stream": False,
    }
    max_attempts = max(6, min(16, len(key_pool.keys) * 3))
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        key_index, api_key = await key_pool.acquire()
        try:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
            )

            if response.status_code == 200:
                try:
                    text = extract_text(response.json())
                    text, warnings = repair_multiline_math_delimiters(text)
                    if warnings:
                        print(
                            f"[warning] {chunk_name}: suspicious math delimiters "
                            f"remain on lines {warnings}",
                            flush=True,
                        )
                    validate_markdown_output(text)
                    await key_pool.mark_success(key_index)
                    return text
                except Exception as exc:
                    await key_pool.mark_error(key_index)
                    last_error = exc
                    print(
                        f"[retry] {chunk_name} attempt {attempt}/{max_attempts}: "
                        f"invalid response: {exc}",
                        flush=True,
                    )
            else:
                message = response.text[:2000]
                last_error = RuntimeError(
                    f"NVIDIA HTTP {response.status_code}: {message}"
                )
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    cooldown = 10.0
                    if retry_after:
                        try:
                            cooldown = max(cooldown, float(retry_after))
                        except ValueError:
                            pass
                    await key_pool.rate_limited(key_index, cooldown)
                    print(
                        f"[retry] {chunk_name} attempt {attempt}/{max_attempts}: "
                        f"HTTP 429 on key#{key_index + 1}; "
                        f"cooldown={cooldown:.0f}s",
                        flush=True,
                    )
                elif response.status_code in {401, 403}:
                    await key_pool.mark_error(key_index)
                    await key_pool.disable(key_index)
                    print(
                        f"[retry] {chunk_name}: key#{key_index + 1} rejected "
                        f"with HTTP {response.status_code}; disabling key",
                        flush=True,
                    )
                elif response.status_code in {400, 404, 422}:
                    await key_pool.mark_error(key_index)
                    raise RuntimeError(
                        f"NVIDIA request rejected "
                        f"({response.status_code}): {message}"
                    )
                else:
                    await key_pool.mark_error(key_index)
                    print(
                        f"[retry] {chunk_name} attempt {attempt}/{max_attempts}: "
                        f"HTTP {response.status_code}",
                        flush=True,
                    )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            await key_pool.mark_error(key_index)
            last_error = exc
            print(
                f"[retry] {chunk_name} attempt {attempt}/{max_attempts}: "
                f"{type(exc).__name__}",
                flush=True,
            )

        if attempt < max_attempts:
            await asyncio.sleep(min(2 ** (attempt - 1), 20))

    raise RuntimeError(
        f"Chunk {chunk_name} failed after {max_attempts} attempts: {last_error}"
    )


async def process_chunks(
    chunks: list[Chunk],
    output_dir: Path,
    prompt: str,
    model: str,
    concurrency: int,
    keys: list[str],
    verification_passes: int,
    max_tokens: int,
    temperature: float,
    top_p: float,
    api_base: str,
    rpm_per_key: float,
) -> list[dict]:
    pages_dir = output_dir / "pages"
    errors_dir = output_dir / "errors"
    pages_dir.mkdir(parents=True, exist_ok=True)
    errors_dir.mkdir(parents=True, exist_ok=True)
    key_pool = NvidiaKeyPool(keys, rpm_per_key)
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=30.0),
        limits=httpx.Limits(
            max_connections=max(50, concurrency * 2),
            max_keepalive_connections=max(20, concurrency),
            keepalive_expiry=30.0,
        ),
    ) as client:

        async def run_one(chunk: Chunk) -> dict:
            md_path = pages_dir / f"{chunk.stem}.md"
            error_path = errors_dir / f"{chunk.stem}.json"
            started = time.time()
            try:
                async with semaphore:
                    content, estimated = await asyncio.to_thread(
                        make_content,
                        chunk,
                        prompt,
                    )
                    text = await call_nvidia(
                        client,
                        key_pool,
                        model,
                        content,
                        chunk.stem,
                        max_tokens,
                        temperature,
                        top_p,
                        api_base,
                    )

                for verify_index in range(verification_passes):
                    async with semaphore:
                        verify_content, _ = await asyncio.to_thread(
                            make_verification_content,
                            chunk,
                            prompt,
                            text,
                        )
                        text = await call_nvidia(
                            client,
                            key_pool,
                            model,
                            verify_content,
                            f"{chunk.stem}-verify-{verify_index + 1}",
                            max_tokens,
                            temperature,
                            top_p,
                            api_base,
                        )

                md_path.write_text(text.rstrip() + "\n", encoding="utf-8")
                if error_path.exists():
                    error_path.unlink()
                elapsed = round(time.time() - started, 2)
                print(f"[done] {chunk.stem}.md ({elapsed}s)", flush=True)
                return {
                    "chunk": chunk.stem,
                    "status": "ok",
                    "path": str(md_path),
                    "estimated_payload_bytes": estimated,
                    "elapsed_seconds": elapsed,
                }
            except Exception as exc:
                error = {
                    "chunk": chunk.stem,
                    "start_page": chunk.start_page,
                    "end_page": chunk.end_page,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                error_path.write_text(
                    json.dumps(error, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(
                    f"[failed] {chunk.stem}: {error['error']}",
                    file=sys.stderr,
                    flush=True,
                )
                return {"chunk": chunk.stem, "status": "failed", **error}

        tasks = [asyncio.create_task(run_one(chunk)) for chunk in chunks]
        results = []
        for future in asyncio.as_completed(tasks):
            results.append(await future)

    usage = key_pool.summary()
    (output_dir / "nvidia-usage.json").write_text(
        json.dumps(usage, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return results


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--source-url", required=True)
    p.add_argument("--images-per-request", type=int, default=2)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--model", default=DEFAULT_NVIDIA_MODEL)
    p.add_argument("--max-tokens", type=int, default=32768)
    p.add_argument("--temperature", type=float, default=0.1)
    p.add_argument("--top-p", type=float, default=0.95)
    p.add_argument(
        "--rpm-per-key",
        type=float,
        default=40.0,
        help="Client-side request rate limit per NVIDIA API key. Default: 40 RPM.",
    )
    p.add_argument("--dpi", type=int, default=240)
    p.add_argument("--image-format", choices=["png", "jpeg"], default="png")
    p.add_argument("--jpeg-quality", type=int, default=95)
    p.add_argument("--verification-passes", type=int, default=0)
    p.add_argument("--start-page", type=int, default=None)
    p.add_argument("--end-page", type=int, default=None)
    p.add_argument("--work-dir", default="work")
    p.add_argument("--output-dir", default="output")
    p.add_argument(
        "--api-base",
        default=os.getenv("NVIDIA_API_BASE", DEFAULT_NVIDIA_API_BASE),
    )
    return p


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= args.images_per_request <= 20:
        raise ValueError("images_per_request must be between 1 and 20")
    if not 1 <= args.concurrency <= 100:
        raise ValueError("concurrency must be between 1 and 100")
    if not 72 <= args.dpi <= 300:
        raise ValueError("dpi must be between 72 and 300")
    if not 50 <= args.jpeg_quality <= 100:
        raise ValueError("jpeg_quality must be between 50 and 100")
    if not 0 <= args.verification_passes <= 3:
        raise ValueError("verification_passes must be between 0 and 3")
    if not 1 <= args.max_tokens <= 262144:
        raise ValueError("max_tokens must be between 1 and 262144")
    if not 0 <= args.temperature <= 2:
        raise ValueError("temperature must be between 0 and 2")
    if not 0 < args.top_p <= 1:
        raise ValueError("top_p must be > 0 and <= 1")
    if args.rpm_per_key <= 0:
        raise ValueError("rpm_per_key must be > 0")
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


async def async_main(args: argparse.Namespace) -> int:
    validate_args(args)
    keys = parse_api_keys(os.getenv("NVIDIA_API_KEYS"))
    if not keys:
        raise RuntimeError(
            "NVIDIA_API_KEYS is empty. Add a repository Actions secret "
            "with one NVIDIA API key per line."
        )

    work_dir = Path(args.work_dir)
    output_dir = Path(args.output_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    source_pdf = work_dir / "source.pdf"
    images_dir = work_dir / "images"

    print(
        f"[config] provider=nvidia model={args.model} keys={len(keys)} "
        f"concurrency={args.concurrency} "
        f"images_per_request={args.images_per_request} "
        f"image_format={args.image_format} dpi={args.dpi} "
        f"verification_passes={args.verification_passes} "
        f"max_tokens={args.max_tokens} temperature={args.temperature:g} "
        f"top_p={args.top_p:g} rpm_per_key={args.rpm_per_key:g} "
        f"page_range={args.start_page or 1}-{args.end_page or 'end'}",
        flush=True,
    )
    print(f"[download] {args.source_url}", flush=True)
    await asyncio.to_thread(download_pdf, args.source_url, source_pdf)
    with source_pdf.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise RuntimeError(
                "Downloaded source is not a PDF. Use a direct/public PDF link."
            )

    image_paths, width, pdf_total_pages = await asyncio.to_thread(
        render_pdf,
        source_pdf,
        images_dir,
        args.dpi,
        args.jpeg_quality,
        args.image_format,
        args.start_page,
        args.end_page,
    )
    chunks = build_chunks(image_paths, args.images_per_request, width)
    print(
        f"[plan] pages={len(image_paths)} chunks={len(chunks)}",
        flush=True,
    )

    results = await process_chunks(
        chunks,
        output_dir,
        args.prompt,
        args.model,
        args.concurrency,
        keys,
        args.verification_passes,
        args.max_tokens,
        args.temperature,
        args.top_p,
        args.api_base,
        args.rpm_per_key,
    )
    failures = [item for item in results if item.get("status") == "failed"]
    merged_name = "merged.md" if not failures else "merged.partial.md"
    merge_markdown(chunks, output_dir / "pages", output_dir / merged_name)

    manifest = {
        "provider": "nvidia",
        "api_base": args.api_base,
        "source_url": args.source_url,
        "model": args.model,
        "total_pages": pdf_total_pages,
        "processed_pages": len(image_paths),
        "start_page": args.start_page,
        "end_page": args.end_page,
        "images_per_request": args.images_per_request,
        "concurrency": args.concurrency,
        "dpi": args.dpi,
        "image_format": args.image_format,
        "jpeg_quality": args.jpeg_quality,
        "verification_passes": args.verification_passes,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "rpm_per_key": args.rpm_per_key,
        "chunks": [
            {
                **asdict(chunk),
                "image_paths": [str(path) for path in chunk.image_paths],
            }
            for chunk in chunks
        ],
        "results": sorted(results, key=lambda item: item["chunk"]),
        "failures": len(failures),
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
