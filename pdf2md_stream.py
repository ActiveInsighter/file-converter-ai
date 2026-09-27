from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import fitz
import httpx

from pdf2md import (
    DEFAULT_PROMPT,
    Chunk,
    KeyPool,
    call_gemini,
    download_pdf,
    make_request_parts,
    parse_api_keys,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Streaming PDF-to-Markdown pipeline: render pages and call Gemini concurrently."
    )
    p.add_argument("--source-url", required=True)
    p.add_argument("--concurrency", type=int, default=30)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--model", default="gemini-3.5-flash-lite")
    p.add_argument("--thinking-level", default="high")
    p.add_argument("--dpi", type=int, default=240)
    p.add_argument("--image-format", choices=["png", "jpeg"], default="png")
    p.add_argument("--jpeg-quality", type=int, default=95)
    p.add_argument("--media-resolution", default="ultra_high")
    p.add_argument("--rpm-per-key", type=float, default=12.0)
    p.add_argument("--rpd-per-key", type=int, default=500)
    p.add_argument("--start-page", type=int, default=None)
    p.add_argument("--end-page", type=int, default=None)
    p.add_argument("--work-dir", default="work")
    p.add_argument("--output-dir", default="output")
    p.add_argument("--keep-images", action="store_true")
    return p


async def main_async(args: argparse.Namespace) -> int:
    started = time.monotonic()
    keys = parse_api_keys(os.getenv("GEMINI_API_KEYS"))
    if not keys:
        raise RuntimeError("GEMINI_API_KEYS is empty")

    work_dir = Path(args.work_dir)
    images_dir = work_dir / "images"
    output_dir = Path(args.output_dir)
    pages_dir = output_dir / "pages"
    errors_dir = output_dir / "errors"
    for path in (work_dir, images_dir, output_dir, pages_dir, errors_dir):
        path.mkdir(parents=True, exist_ok=True)

    source_pdf = work_dir / "source.pdf"
    download_started = time.monotonic()
    await asyncio.to_thread(download_pdf, args.source_url, source_pdf)
    download_seconds = time.monotonic() - download_started

    with fitz.open(source_pdf) as document:
        total_pages = document.page_count

    first = args.start_page or 1
    last = args.end_page or total_pages
    if first < 1 or last > total_pages or first > last:
        raise ValueError(f"Invalid page range {first}-{last} for {total_pages}-page PDF")

    width = max(3, len(str(total_pages)))
    page_count = last - first + 1
    queue: asyncio.Queue[Chunk | None] = asyncio.Queue(
        maxsize=max(args.concurrency * 2, 16)
    )
    loop = asyncio.get_running_loop()
    worker_count = min(args.concurrency, page_count)
    render_metrics: dict[str, float] = {}
    results: list[dict] = []

    print(
        f"[config] pages={page_count} range={first}-{last} "
        f"concurrency={worker_count} keys={len(keys)} "
        f"dpi={args.dpi} format={args.image_format} "
        f"rpm_per_key={args.rpm_per_key:g}",
        flush=True,
    )

    def render_producer() -> None:
        render_started = time.monotonic()
        extension = "png" if args.image_format == "png" else "jpg"
        matrix = fitz.Matrix(args.dpi / 72.0, args.dpi / 72.0)
        document = fitz.open(source_pdf)
        try:
            for page_number in range(first, last + 1):
                page = document[page_number - 1]
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                image_path = images_dir / f"{page_number:0{width}d}.{extension}"
                if args.image_format == "png":
                    image_path.write_bytes(pix.tobytes("png"))
                else:
                    image_path.write_bytes(
                        pix.tobytes("jpeg", jpg_quality=args.jpeg_quality)
                    )
                chunk = Chunk(
                    start_page=page_number,
                    end_page=page_number,
                    image_paths=(image_path,),
                    stem=f"{page_number:0{width}d}",
                )
                fut = asyncio.run_coroutine_threadsafe(queue.put(chunk), loop)
                fut.result()
                if page_number == first or page_number % 25 == 0 or page_number == last:
                    print(
                        f"[render-stream] page={page_number}/{last} "
                        f"queue={queue.qsize()}",
                        flush=True,
                    )
        finally:
            document.close()
            render_metrics["seconds"] = time.monotonic() - render_started
            for _ in range(worker_count):
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

    key_pool = KeyPool(
        keys,
        rpm_per_key=args.rpm_per_key,
        rpd_per_key=args.rpd_per_key,
    )

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(240.0, connect=30.0)
    ) as client:

        async def worker(worker_id: int) -> None:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    queue.task_done()
                    return

                md_path = pages_dir / f"{chunk.stem}.md"
                error_path = errors_dir / f"{chunk.stem}.json"
                page_started = time.monotonic()
                try:
                    parts, estimated = make_request_parts(
                        chunk,
                        args.prompt,
                        args.media_resolution,
                    )
                    text = await call_gemini(
                        client,
                        key_pool,
                        len(keys),
                        args.model,
                        parts,
                        chunk.stem,
                        args.thinking_level,
                    )
                    md_path.write_text(text.rstrip() + "\n", encoding="utf-8")
                    if error_path.exists():
                        error_path.unlink()
                    elapsed = round(time.monotonic() - page_started, 2)
                    results.append(
                        {
                            "chunk": chunk.stem,
                            "status": "ok",
                            "elapsed_seconds": elapsed,
                            "estimated_payload_bytes": estimated,
                        }
                    )
                    if int(chunk.stem) == first or int(chunk.stem) % 25 == 0 or int(chunk.stem) == last:
                        print(
                            f"[done-stream] worker={worker_id} page={chunk.stem} "
                            f"elapsed={elapsed}s",
                            flush=True,
                        )
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
                    results.append({"chunk": chunk.stem, "status": "failed", **error})
                    print(f"[failed-stream] {chunk.stem}: {error['error']}", flush=True)
                finally:
                    if not args.keep_images:
                        for image_path in chunk.image_paths:
                            image_path.unlink(missing_ok=True)
                    queue.task_done()

        workers = [
            asyncio.create_task(worker(i + 1))
            for i in range(worker_count)
        ]
        renderer = asyncio.create_task(asyncio.to_thread(render_producer))
        await renderer
        await queue.join()
        await asyncio.gather(*workers)

    failures = [item for item in results if item["status"] == "failed"]
    page_files = sorted(
        pages_dir.glob("*.md"),
        key=lambda p: int(p.stem.split("-")[0]),
    )
    blocks = [p.read_text(encoding="utf-8").strip() for p in page_files]
    merged_name = "merged.md" if not failures else "merged.partial.md"
    (output_dir / merged_name).write_text(
        "\n\n".join(blocks).rstrip() + "\n",
        encoding="utf-8",
    )

    total_seconds = time.monotonic() - started
    manifest = {
        "source_url": args.source_url,
        "total_pages": total_pages,
        "processed_pages": page_count,
        "start_page": first,
        "end_page": last,
        "model": args.model,
        "thinking_level": args.thinking_level,
        "dpi": args.dpi,
        "image_format": args.image_format,
        "media_resolution": args.media_resolution,
        "concurrency": worker_count,
        "rpm_per_key": args.rpm_per_key,
        "rpd_per_key": args.rpd_per_key,
        "key_count": len(keys),
        "key_request_counts": key_pool.usage_summary(),
        "download_seconds": round(download_seconds, 2),
        "render_seconds": round(render_metrics.get("seconds", 0.0), 2),
        "total_seconds": round(total_seconds, 2),
        "failures": len(failures),
        "merged_file": merged_name,
        "results": sorted(results, key=lambda item: item["chunk"]),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"[timing] download={download_seconds:.1f}s "
        f"render={render_metrics.get('seconds', 0.0):.1f}s "
        f"total={total_seconds:.1f}s",
        flush=True,
    )
    print(f"[keys] request_counts={key_pool.usage_summary()}", flush=True)
    print(
        f"[result] success={page_count-len(failures)}/{page_count} "
        f"output={merged_name}",
        flush=True,
    )
    return 0 if not failures else 2


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(main_async(args))
    except Exception as exc:
        print(f"[fatal] {type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
