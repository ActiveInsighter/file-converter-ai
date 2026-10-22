"""Shared drivers for tests that exercise process_pages without HTTP."""
import asyncio
import os
from pathlib import Path
from unittest.mock import patch

from paddle_ocr import OcrPage
from pdf2md import Page, process_pages


def page(number: int, blank: bool = False, text_layer: str | None = None) -> Page:
    return Page(number, Path(f"{number:03d}.png"), f"{number:03d}", blank, text_layer)


def fake_parts(page, prompt, media_resolution, draft=None, text_layer=None):
    """Request parts that expose the OCR draft and text layer to a fake model call."""
    text = f"page={page.number} draft={draft}"
    if text_layer is not None:
        text += f" text={text_layer}"
    return [{"text": text}], 64


class FakeOcr:
    """Serves fixed OCR drafts; a missing page means OCR failed for it.

    Pages in ``delays`` are ready only that many seconds after the first call.
    """

    enabled = True

    def __init__(self, drafts: dict[int, str], figures: dict[int, dict] | None = None,
                 delays: dict[int, float] | None = None):
        self.drafts = drafts
        self.figures = figures or {}
        self.delays = delays or {}
        self.started = None
        self.stats = {"pages": len(drafts)}

    async def page(self, number, timeout=None):
        loop = asyncio.get_running_loop()
        self.started = self.started or loop.time()
        wait = self.started + self.delays.get(number, 0) - loop.time()
        if wait > 0:
            if timeout is not None and timeout < wait:
                await asyncio.sleep(max(0, timeout))
                return None
            await asyncio.sleep(wait)
        if number not in self.drafts:
            return None
        return OcrPage(number, self.drafts[number], self.figures.get(number, {}))


async def run_process_pages(pages, directory, fake_call, **overrides):
    """Drive process_pages with the model call stubbed out."""
    options = dict(
        prompt="Transcribe",
        model="fake-model",
        concurrency=1,
        keys=["dummy-key"],
        thinking_level="high",
        verification_passes=0,
        media_resolution="high",
        rpm_per_key=60000,
        rpd_per_key=500,
    )
    options.update(overrides)
    with (
        patch.dict(os.environ, {"GEMINI_KEY_GROUPS": ""}),
        patch("pdf2md.make_request_parts", new=fake_parts),
        patch("pdf2md.call_model", new=fake_call),
        patch("pdf2md.random.uniform", return_value=0.0),
    ):
        return await process_pages(pages, Path(directory), **options)
