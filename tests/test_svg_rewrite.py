import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from artifacts import write_result
from paddle_ocr import OcrPage
from pdf2md import Page, _parse_svg_config, parser, process_pages
from providers import GEMINI, MODELFLARE
from render_stream import RenderPlan
from svg_rewrite import InvalidSvgError, SvgRewriter, extract_svg


class SvgExtractionTests(unittest.TestCase):
    def test_extracts_plain_and_fenced_svg(self):
        raw = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'
        self.assertEqual(extract_svg(raw), raw)
        fenced = f"```svg\n{raw}\n```"
        self.assertEqual(extract_svg(fenced), raw)

    def test_rejects_missing_or_malformed_svg(self):
        with self.assertRaises(InvalidSvgError):
            extract_svg("no svg here")
        with self.assertRaises(InvalidSvgError):
            extract_svg('<svg xmlns="http://www.w3.org/2000/svg"><g></svg>')

    def test_parse_svg_config_merges_json_and_flags(self):
        args = parser().parse_args([
            "--source-url", "https://example.com/a.pdf",
            "--svg-config", json.dumps({
                "enabled": True,
                "provider": "modelflare",
                "model": "gpt-6-sol",
                "prompt": "custom svg prompt",
            }),
        ])
        cfg = _parse_svg_config(args)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["provider"], "modelflare")
        self.assertEqual(cfg["model"], "gpt-6-sol")
        self.assertEqual(cfg["prompt"], "custom svg prompt")


class FakeOcr:
    enabled = True
    stats = {"pages": 1, "failed_pages": 0, "figures": 2}

    def __init__(self, ocr_page: OcrPage):
        self._page = ocr_page

    async def page(self, number: int) -> OcrPage:
        return self._page


class SvgPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_svg_ai_runs_with_md_and_updates_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "work" / "conversion"
            output_dir = root / "output"
            figures_dir = state_dir / "figures"
            images_dir = root / "work" / "images"
            figures_dir.mkdir(parents=True)
            images_dir.mkdir(parents=True)
            output_dir.mkdir(parents=True)

            # Create a dummy PDF and page image
            pdf_path = root / "source.pdf"
            doc = fitz.open()
            p = doc.new_page()
            p.insert_text((72, 72), "Page 1")
            doc.save(pdf_path)
            doc.close()

            page_img = images_dir / "001.png"
            page_img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            fig1 = figures_dir / "p001-merged-1.png"
            fig1.write_bytes(b"\x89PNG\r\n\x1a\nfig1")
            fig2 = figures_dir / "p001-2.jpg"
            fig2.write_bytes(b"\xff\xd8\xff\xe0fig2")

            ocr_page = OcrPage(
                number=1,
                markdown="Hello\n\n![](images/p001-merged-1.png)\n\n![](images/p001-2.jpg)",
                figures={
                    "images/p001-merged-1.png": fig1,
                    "images/p001-2.jpg": fig2,
                },
            )
            calls: list[tuple[str, str, str, str]] = []

            async def fake_call_model(
                client, key_pool, provider, model, parts, chunk_name,
                thinking_level, fallback_models=(), reasoning_effort="",
                api_base="", max_output_tokens=0,
            ) -> str:
                text_part = next(p["text"] for p in parts if "text" in p)
                calls.append((chunk_name, provider.name, model, text_part))
                if chunk_name.startswith("svg:"):
                    if chunk_name == "svg:p001-2":
                        raise RuntimeError("svg failed permanently")
                    return '```svg\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"><line x1="0" y1="0" x2="100" y2="50"/></svg>\n```'
                return "Hello\n\n![diagram](images/p001-merged-1.png)\n\n![photo](images/p001-2.jpg)"

            svg_map: dict[str, str] = {}
            pages = [Page(number=1, image_path=page_img, stem="001", blank=False)]
            plan = RenderPlan(total_pdf_pages=1, first=1, last=1, width=3)

            with patch("pdf2md.call_model", side_effect=fake_call_model):
                results = await process_pages(
                    pages,
                    state_dir,
                    "MD_PROMPT",
                    "gemini-3.5-flash-lite",
                    concurrency=4,
                    keys=["gemini-key-1"],
                    thinking_level="high",
                    verification_passes=0,
                    media_resolution="high",
                    rpm_per_key=60000,
                    rpd_per_key=100,
                    attempts_per_page=1,
                    provider=GEMINI,
                    ocr=FakeOcr(ocr_page),
                    svg_enabled=True,
                    svg_provider=MODELFLARE,
                    svg_model="gpt-6-sol",
                    svg_prompt="CUSTOM_SVG_PROMPT",
                    svg_keys=["mf-key-1"],
                    svg_map_out=svg_map,
                )

            self.assertEqual(svg_map, {"images/p001-merged-1.png": "images/p001-merged-1.svg"})
            md_calls = [c for c in calls if not c[0].startswith("svg:")]
            svg_calls = [c for c in calls if c[0].startswith("svg:")]
            self.assertEqual(len(md_calls), 1)
            self.assertEqual(md_calls[0][1], "gemini")
            self.assertEqual(md_calls[0][2], "gemini-3.5-flash-lite")
            self.assertEqual(len(svg_calls), 2)
            self.assertTrue(all(c[1] == "modelflare" and c[2] == "gpt-6-sol" and c[3] == "CUSTOM_SVG_PROMPT" for c in svg_calls))

            files = write_result(
                output_dir=output_dir,
                state_dir=state_dir,
                name="out",
                source_url="https://example.com/a.pdf",
                plan=plan,
                provider="gemini",
                model="gemini-3.5-flash-lite",
                ocr_model="PaddleOCR-VL",
                converted_at="2026-01-01T00:00:00+00:00",
                pages=pages,
                results=results,
                pipeline_error=None,
                svg_map=svg_map,
            )
            merged_md = files.markdown.read_text(encoding="utf-8")
            self.assertIn("![diagram](images/p001-merged-1.svg)", merged_md)
            self.assertIn("![photo](images/p001-2.jpg)", merged_md)
            self.assertTrue((output_dir / "images" / "p001-merged-1.svg").is_file())
            self.assertTrue((output_dir / "images" / "p001-merged-1.png").is_file())
            self.assertTrue((output_dir / "images" / "p001-2.jpg").is_file())
            page_md = (output_dir / "pages" / "001.md").read_text(encoding="utf-8")
            self.assertIn("![diagram](../images/p001-merged-1.svg)", page_md)
