from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import fitz
import gdown
import httpx

DEFAULT_PROMPT = """请按图片原始顺序逐页、忠实地把这些 PDF 页面转写为 Markdown。这是高精度文档转录任务，不是总结、改写或解题任务。

要求：
1. 不要遗漏正文、标题、列表、表格、公式、题目、选项、注释、评分等有效内容，并严格保持原始顺序。
2. 数学内容必须逐字符核对图片：重点检查正负号、等号/不等号、上下标、根号、分数、积分/求和上下限、导数阶数、矩阵元素、向量转置、希腊字母和括号。不要仅凭上下文猜公式。
3. 不要自行修正原文、补全推导或加入解释。看不清时写 [无法辨认]，不要编造。
4. 数学公式使用标准 LaTeX。行内公式只用成对的 $...$；多行公式、aligned、cases、matrix 等必须使用成对的 $...$，禁止用单个 $ 跨多行包裹。
5. 不要输出处理备注、模型解释、占位说明或类似“此行视具体排版”的元信息。
6. 忽略页眉、页脚、纯页码、扫描水印、装饰性字符和无意义的重复数字/编码；除非它们明显属于正文。
7. 表格尽量转换为 Markdown 表格；无法可靠转换时按原有阅读顺序保留文本，不要虚构表格结构。
8. 输出前自行复核一遍图片与 Markdown，尤其复核所有数学公式和数字。
9. 直接输出 Markdown，不要使用 Markdown 代码围栏。
"""
GOOGLE_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_INLINE_REQUEST_BYTES = 18 * 1024 * 1024


@dataclass(frozen=True)
class Chunk:
    start_page: int
    end_page: int
    image_paths: tuple[Path, ...]
    stem: str


class KeyPool:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("No Gemini API keys configured")
        self.keys = keys
        self.index = 0
        self.lock = asyncio.Lock()

    async def next(self) -> str:
        async with self.lock:
            key = self.keys[self.index % len(self.keys)]
            self.index += 1
            return key


def parse_api_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    result = []
    seen = set()
    for item in re.split(r"[\r\n,;]+", raw):
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def is_google_drive_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "drive.google.com" or host.endswith(".drive.google.com")


def download_pdf(source_url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if is_google_drive_url(source_url):
        result = gdown.download(url=source_url, output=str(destination), quiet=False, fuzzy=True)
        if not result or not destination.exists():
            raise RuntimeError(
                "Google Drive download failed. Make sure the file is shared as Anyone with the link."
            )
        return

    with httpx.stream("GET", source_url, follow_redirects=True, timeout=120.0) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for data in response.iter_bytes(chunk_size=1024 * 1024):
                handle.write(data)


def render_pdf(
    pdf_path: Path, image_dir: Path, dpi: int, jpeg_quality: int
) -> tuple[list[Path], int]:
    image_dir.mkdir(parents=True, exist_ok=True)
    document = fitz.open(pdf_path)
    total_pages = document.page_count
    if total_pages < 1:
        raise ValueError("PDF has no pages")

    width = max(3, len(str(total_pages)))
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    paths: list[Path] = []

    for index, page in enumerate(document, start=1):
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        path = image_dir / f"{index:0{width}d}.jpg"
        path.write_bytes(pix.tobytes("jpeg", jpg_quality=jpeg_quality))
        paths.append(path)
        print(f"[render] {index}/{total_pages}: {path.name}", flush=True)

    document.close()
    return paths, width


def build_chunks(
    image_paths: list[Path], images_per_request: int, width: int
) -> list[Chunk]:
    chunks = []
    for offset in range(0, len(image_paths), images_per_request):
        batch = tuple(image_paths[offset : offset + images_per_request])
        start_page = offset + 1
        end_page = offset + len(batch)
        stem = (
            f"{start_page:0{width}d}"
            if start_page == end_page
            else f"{start_page:0{width}d}-{end_page:0{width}d}"
        )
        chunks.append(Chunk(start_page, end_page, batch, stem))
    return chunks


def make_request_parts(chunk: Chunk, prompt: str) -> tuple[list[dict], int]:
    page_label = (
        f"第 {chunk.start_page} 页"
        if chunk.start_page == chunk.end_page
        else f"第 {chunk.start_page}-{chunk.end_page} 页"
    )
    parts = [
        {
            "text": (
                f"以下图片按顺序对应 PDF {page_label}。\n"
                f"请严格按图片顺序处理。\n\n{prompt.strip()}"
            )
        }
    ]
    raw_bytes = 0

    for path in chunk.image_paths:
        data = path.read_bytes()
        raw_bytes += len(data)
        parts.append(
            {
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": base64.b64encode(data).decode("ascii"),
                }
            }
        )

    estimated = int(raw_bytes * 4 / 3) + len(prompt.encode("utf-8")) + 64 * 1024
    if estimated > MAX_INLINE_REQUEST_BYTES:
        raise ValueError(
            f"Chunk {chunk.stem} is about {estimated / 1024 / 1024:.1f} MiB after base64, "
            "too large for inline image input. Lower images_per_request, dpi, or jpeg_quality."
        )
    return parts, estimated


def extract_response_text(payload: dict) -> str:
    texts = []
    for candidate in payload.get("candidates", []):
        for part in (candidate.get("content") or {}).get("parts", []):
            if part.get("text"):
                texts.append(part["text"])

    result = "\n".join(texts).strip()
    if not result:
        raise RuntimeError(
            f"Gemini returned no text. promptFeedback={payload.get('promptFeedback') or {}}"
        )

    if result.startswith("~~~markdown") and result.endswith("~~~"):
        result = result[len("~~~markdown") : -3].strip()
    return result


async def call_gemini(
    client: httpx.AsyncClient,
    key_pool: KeyPool,
    key_count: int,
    model: str,
    parts: list[dict],
    chunk_name: str,
    thinking_level: str,
) -> str:
    url = f"{GOOGLE_API_BASE}/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "thinkingConfig": {
                "thinkingLevel": thinking_level,
            }
        },
    }
    max_attempts = max(4, min(12, key_count * 2))
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        api_key = await key_pool.next()
        try:
            response = await client.post(
                url,
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if response.status_code == 200:
                return extract_response_text(response.json())

            message = response.text[:2000]
            if response.status_code in {400, 404}:
                raise RuntimeError(
                    f"Gemini request rejected ({response.status_code}): {message}"
                )

            last_error = RuntimeError(
                f"Gemini HTTP {response.status_code}: {message}"
            )
            print(
                f"[retry] {chunk_name} attempt {attempt}/{max_attempts}: "
                f"HTTP {response.status_code}; rotating key",
                flush=True,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            print(
                f"[retry] {chunk_name} attempt {attempt}/{max_attempts}: "
                f"{type(exc).__name__}; rotating key",
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
    thinking_level: str,
) -> list[dict]:
    pages_dir = output_dir / "pages"
    errors_dir = output_dir / "errors"
    pages_dir.mkdir(parents=True, exist_ok=True)
    errors_dir.mkdir(parents=True, exist_ok=True)

    key_pool = KeyPool(keys)
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(240.0, connect=30.0)
    ) as client:

        async def run_one(chunk: Chunk) -> dict:
            md_path = pages_dir / f"{chunk.stem}.md"
            error_path = errors_dir / f"{chunk.stem}.json"
            started = time.time()

            try:
                parts, estimated = make_request_parts(chunk, prompt)
                async with semaphore:
                    text = await call_gemini(
                        client,
                        key_pool,
                        len(keys),
                        model,
                        parts,
                        chunk.stem,
                        thinking_level,
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
        return results


def merge_markdown(
    chunks: list[Chunk], pages_dir: Path, destination: Path
) -> None:
    blocks = []
    for chunk in chunks:
        path = pages_dir / f"{chunk.stem}.md"
        if path.exists():
            blocks.append(path.read_text(encoding="utf-8").strip())
    destination.write_text(
        "\n\n".join(blocks).rstrip() + "\n", encoding="utf-8"
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--source-url", required=True)
    p.add_argument("--images-per-request", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--model", default="gemini-3.5-flash-lite")
    p.add_argument(
        "--thinking-level",
        choices=["minimal", "low", "medium", "high"],
        default="high",
        help="Gemini 3 thinking level. Default: high.",
    )
    p.add_argument("--dpi", type=int, default=180)
    p.add_argument("--jpeg-quality", type=int, default=88)
    p.add_argument("--work-dir", default="work")
    p.add_argument("--output-dir", default="output")
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
    if args.thinking_level not in {"minimal", "low", "medium", "high"}:
        raise ValueError("thinking_level must be minimal, low, medium, or high")


async def async_main(args: argparse.Namespace) -> int:
    validate_args(args)
    keys = parse_api_keys(os.getenv("GEMINI_API_KEYS"))
    if not keys:
        raise RuntimeError(
            "GEMINI_API_KEYS is empty. Add a repository Actions secret with one Gemini API key per line."
        )

    work_dir = Path(args.work_dir)
    output_dir = Path(args.output_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_pdf = work_dir / "source.pdf"
    images_dir = work_dir / "images"

    print(
        f"[config] model={args.model} keys={len(keys)} "
        f"concurrency={args.concurrency} "
        f"images_per_request={args.images_per_request} "
        f"thinking_level={args.thinking_level}",
        flush=True,
    )
    print(f"[download] {args.source_url}", flush=True)
    await asyncio.to_thread(download_pdf, args.source_url, source_pdf)
    with source_pdf.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise RuntimeError(
                "Downloaded source is not a PDF. Use a direct/public PDF link; ZIP and other files are not accepted."
            )

    image_paths, width = await asyncio.to_thread(
        render_pdf,
        source_pdf,
        images_dir,
        args.dpi,
        args.jpeg_quality,
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
        args.thinking_level,
    )
    failures = [x for x in results if x.get("status") == "failed"]

    merged_name = "merged.md" if not failures else "merged.partial.md"
    merge_markdown(chunks, output_dir / "pages", output_dir / merged_name)

    manifest = {
        "source_url": args.source_url,
        "model": args.model,
        "total_pages": len(image_paths),
        "images_per_request": args.images_per_request,
        "concurrency": args.concurrency,
        "thinking_level": args.thinking_level,
        "dpi": args.dpi,
        "jpeg_quality": args.jpeg_quality,
        "chunks": [
            {
                **asdict(chunk),
                "image_paths": [str(p) for p in chunk.image_paths],
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
