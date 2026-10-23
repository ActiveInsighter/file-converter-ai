"""Run isolated component benchmarks against a checkout selected by --code-root."""
import argparse
import asyncio
import contextlib
import io
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import fitz
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.code_root.resolve()))
    import pdf2md
    from render_stream import RenderPlan, render_pages
    result = {"code_root": str(args.code_root)}
    with tempfile.TemporaryDirectory(dir=args.code_root / "work") as tmp:
        root = Path(tmp)
        for name, text in (("blank", ""), ("text", "Document text with numbers 0.125 and 120.")):
            source = root / f"{name}.pdf"
            with fitz.open() as doc:
                for _ in range(20):
                    p = doc.new_page()
                    if text:
                        p.insert_text((72, 72), text)
                doc.save(source)
            samples = []
            for repeat in range(3):
                start = time.perf_counter()
                with contextlib.redirect_stdout(io.StringIO()):
                    list(render_pages(source, root / f"{name}-{repeat}", RenderPlan(20, 1, 20, 3),
                                      240, 95, "png", pdf2md.is_blank_page, edges=True))
                samples.append(time.perf_counter() - start)
            result[name + "_render"] = {"pages": 20, "repeats": 3, "seconds": samples,
                                       "median_seconds": statistics.median(samples)}
        image = root / "page.png"
        with fitz.open(root / "text.pdf") as doc:
            doc[0].get_pixmap().save(image)
        calls = []
        def handler(request):
            key = request.headers["x-goog-api-key"]
            calls.append(key)
            if key == "unfunded":
                return httpx.Response(402, json={"error": {"message": "prepayment credits depleted"}})
            return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "# Preserved page"}]}}]})
        real_client = httpx.AsyncClient
        state = root / "state"
        state.mkdir()
        start = time.perf_counter()
        with patch("httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)), \
                patch("random.random", return_value=0), patch("random.uniform", return_value=3), \
                patch.dict("os.environ", {"GEMINI_KEY_GROUPS": ""}), contextlib.redirect_stdout(io.StringIO()):
            pages = asyncio.run(pdf2md.process_pages(
                [pdf2md.Page(1, image, "001")], state, "Transcribe", "m", 1,
                ["unfunded", "funded"], "high", 0, "high", 600000, 100,
                retry_rounds=1, attempts_per_page=2, hedge_after=0))
        result["credit_failure_replay"] = {"seconds": time.perf_counter() - start,
                                           "request_count": len(calls), "status": pages[0]["status"],
                                           "injected_page_backoff_seconds": 3}
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
