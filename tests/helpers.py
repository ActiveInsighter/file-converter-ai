"""Shared drivers for tests that exercise process_pages without HTTP."""
import os
from pathlib import Path
from unittest.mock import patch

from paddle_ocr import OcrPage
from pdf2md import Page, process_pages


def page(number: int, blank: bool = False) -> Page:
    return Page(number, Path(f"{number:03d}.png"), f"{number:03d}", blank)


def fake_parts(page, prompt, media_resolution, draft=None, previous=None, following=None):
    """Request parts that expose the OCR draft and attached neighbours to a fake model call."""
    neighbours = ""
    if previous or following:
        neighbours = (f" previous={previous.number if previous else None}"
                      f" following={following.number if following else None}")
    return [{"text": f"page={page.number} draft={draft}{neighbours}"}], 64


class FakeOcr:
    """Serves fixed OCR drafts; a missing page means OCR failed for it."""

    enabled = True

    def __init__(self, drafts: dict[int, str], figures: dict[int, dict] | None = None):
        self.drafts = drafts
        self.figures = figures or {}
        self.stats = {"pages": len(drafts)}

    async def page(self, number):
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
