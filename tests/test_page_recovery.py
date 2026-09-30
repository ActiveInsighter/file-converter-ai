"""Page-level recovery: blank scan pages and Gemini content refusals."""
import contextlib
import io
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import fitz

from pdf2md import (
    Chunk,
    ContentBlockedError,
    EmptyAnswerError,
    ModelUnavailableError,
    PermanentProviderError,
    _page_ink_rows,
    build_chunks,
    call_model_once,
    choose_split_row,
    answer_from_payload,
    is_blank_page,
    is_retryable_error,
    page_ink_stats,
    process_chunks,
    split_page_image,
)
from providers import GEMINI
from quota_client import QuotaPoolExhaustedError


def png_size(path: Path) -> tuple[int, int]:
    """Pixel size straight from the PNG header (independent of DPI metadata)."""
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{path} is not a PNG")
    return struct.unpack(">II", header[16:24])


def render_page(path: Path, *, lines: tuple[int, ...]) -> None:
    """Write a rendered page image: `lines` are y positions of one text line each."""
    document = fitz.open()
    page = document.new_page()
    for y in lines:
        page.insert_text((72, y), "Transcribe this line of the page", fontsize=11)
    pix = page.get_pixmap(matrix=fitz.Matrix(240 / 72, 240 / 72), alpha=False)
    path.write_bytes(pix.tobytes("png"))
    document.close()


class FakePool:
    used = [0]

    async def acquire(self):
        return 0, "fake-key", "fake-lease"

    async def mark_success(self, *args, **kwargs):
        pass

    async def mark_error(self, *args, **kwargs):
        pass

    async def rate_limited(self, *args, **kwargs):
        pass

    def record_http_result(self, *args, **kwargs):
        pass

    async def close(self):
        pass

    def usage_summary(self):
        return {"scope": "test"}


def run_process_chunks(chunks, directory, fake_call, **overrides):
    """Drive process_chunks with the quota pool and the HTTP layer stubbed out."""

    async def driver():
        with (
            patch.dict(os.environ, {"GEMINI_KEY_GROUPS": ""}),
            patch("pdf2md.ProjectQuotaPool.create", new=AsyncMock(return_value=FakePool())),
            patch("pdf2md.make_request_parts", return_value=([{"text": "page"}], 64)),
            patch("pdf2md.call_model", new=fake_call),
            patch("pdf2md.random.uniform", return_value=0.0),
        ):
            return await process_chunks(
                chunks,
                Path(directory),
                prompt="Transcribe",
                model="fake-model",
                concurrency=1,
                keys=["dummy-key"],
                thinking_level="high",
                verification_passes=0,
                media_resolution="high",
                rpm_per_key=15,
                rpd_per_key=500,
                **overrides,
            )

    return driver()


class BlankPageTests(unittest.TestCase):
    def measure(self, path: Path) -> tuple[tuple[bool, float, int], tuple[float, int]]:
        document = fitz.open(path)
        try:
            return is_blank_page(document[0]), page_ink_stats(document[0])
        finally:
            document.close()

    def test_white_page_is_blank_and_a_text_page_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            blank = Path(directory) / "blank.png"
            content = Path(directory) / "content.png"
            render_page(blank, lines=())
            render_page(content, lines=(100, 400, 700))

            blank_verdict, blank_stats = self.measure(blank)
            content_verdict, content_stats = self.measure(content)

        self.assertTrue(blank_verdict[0])
        self.assertLess(blank_stats[0], 0.001)
        self.assertFalse(content_verdict[0])
        self.assertGreater(content_stats[0], 0.001)

    def test_a_single_faint_line_is_not_a_blank_page(self):
        """A sparse page still has content: only a page with no dark ink is blank."""
        with tempfile.TemporaryDirectory() as directory:
            sparse = Path(directory) / "sparse.png"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 420), "One short line only", fontsize=11)
            pix = page.get_pixmap(matrix=fitz.Matrix(240 / 72, 240 / 72), alpha=False)
            sparse.write_bytes(pix.tobytes("png"))
            document.close()

            verdict, stats = self.measure(sparse)

        self.assertFalse(verdict[0], f"ink={stats[0]:.6f} darkest={stats[1]}")

    def test_build_chunks_only_marks_fully_blank_batches(self):
        paths = [Path(f"{number:03d}.png") for number in (1, 2, 3)]

        single = build_chunks(paths, 1, 3, {1})
        self.assertEqual([chunk.blank for chunk in single], [True, False, False])

        paired = build_chunks(paths[1:], 2, 3, {2})
        self.assertEqual(paired[0].blank, False, "one blank page must not blank the batch")

        blank_batch = build_chunks(paths[:2], 2, 3, {1, 2})
        self.assertEqual([chunk.blank for chunk in blank_batch], [True])


class BlankChunkFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_chunk_is_skipped_without_any_request(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            async def fake_call(*args, **kwargs):
                calls.append(args[4])
                return "# should not be used"

            results = await run_process_chunks(
                [Chunk(6, 6, (Path("006.png"),), "006", True)],
                directory,
                fake_call,
            )
            written = (Path(directory) / "pages" / "006.md").read_text()

        self.assertEqual(calls, [])
        self.assertEqual(written, "")
        self.assertEqual(results[0]["status"], "blank")
        self.assertEqual(results[0]["reason"], "blank_page")
        self.assertFalse(results[0]["retryable"])


class ContentErrorClassificationTests(unittest.TestCase):
    def test_recitation_is_a_content_block(self):
        payload = {"candidates": [{"finishReason": "RECITATION"}]}

        with self.assertRaises(ContentBlockedError) as caught:
            answer_from_payload(GEMINI, payload)

        self.assertIn("RECITATION", str(caught.exception))
        self.assertIsInstance(caught.exception, RuntimeError)

    def test_safety_finish_reason_is_a_content_block(self):
        payload = {"candidates": [{"finishReason": "PROHIBITED_CONTENT"}]}

        with self.assertRaises(ContentBlockedError):
            answer_from_payload(GEMINI, payload)

    def test_blocked_prompt_is_a_content_block(self):
        payload = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}

        with self.assertRaises(ContentBlockedError) as caught:
            answer_from_payload(GEMINI, payload)

        self.assertIn("blockReason=SAFETY", str(caught.exception))

    def test_empty_stop_answer_is_an_empty_answer(self):
        payload = {"candidates": [{"finishReason": "STOP"}]}

        with self.assertRaises(EmptyAnswerError):
            answer_from_payload(GEMINI, payload)

        self.assertIsInstance(EmptyAnswerError("x"), ContentBlockedError)

    def test_non_text_parts_are_ignored_when_looking_for_an_answer(self):
        payload = {
            "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"thought": True, "text": "hm"}]}}
            ]
        }

        with self.assertRaises(EmptyAnswerError):
            answer_from_payload(GEMINI, payload)

    def test_content_errors_never_enter_the_backoff_rounds(self):
        self.assertFalse(is_retryable_error(ContentBlockedError("RECITATION")))
        self.assertFalse(is_retryable_error(EmptyAnswerError("STOP")))
        self.assertFalse(is_retryable_error(PermanentProviderError("HTTP 400")))
        self.assertFalse(is_retryable_error(QuotaPoolExhaustedError("daily")))
        self.assertFalse(is_retryable_error(ValueError("bad chunk")))
        self.assertTrue(is_retryable_error(ModelUnavailableError("HTTP 503")))
        self.assertTrue(is_retryable_error(RuntimeError("Gemini HTTP 500: boom")))


class SplitGeometryTests(unittest.TestCase):
    def test_rows_without_any_blank_line_fall_back_to_the_lightest_row(self):
        rows = [50] * 120

        cut, clean = choose_split_row(rows, 120)

        self.assertFalse(clean)
        self.assertEqual(rows[cut], 50)

    def test_the_most_balanced_window_wins_but_any_blank_row_is_used(self):
        rows = [50] * 100
        rows[30] = 0  # blank row only inside the wider windows

        cut, clean = choose_split_row(rows, 100)

        self.assertTrue(clean)
        self.assertEqual(cut, 30)

    def test_cut_lands_on_a_blank_row_between_text_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "page.png"
            render_page(page_path, lines=(80, 120, 160, 700, 740))
            document = fitz.open(page_path)
            try:
                rows, height = _page_ink_rows(document[0])
                cut, clean = choose_split_row(rows, height)
            finally:
                document.close()

        self.assertTrue(clean)
        self.assertEqual(rows[cut], 0)
        self.assertLess(abs(cut / height - 0.5), 0.16)

    def test_a_fully_inked_page_still_splits_with_an_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "dense.png"
            document = fitz.open()
            page = document.new_page()
            page.draw_rect(fitz.Rect(40, 90, 550, 790), color=(0, 0, 0), fill=(0, 0, 0))
            pix = page.get_pixmap(matrix=fitz.Matrix(240 / 72, 240 / 72), alpha=False)
            page_path.write_bytes(pix.tobytes("png"))
            document.close()

            halves, clean = split_page_image(page_path, Path(directory) / "split")
            full_height = png_size(page_path)[1]
            total_height = sum(png_size(half)[1] for half in halves)

        self.assertFalse(clean)
        self.assertEqual(len(halves), 2)
        self.assertGreater(total_height, full_height)
        self.assertLess(total_height, full_height * 1.1)

    def test_halves_cover_the_page_and_cut_between_content(self):
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "page.png"
            render_page(page_path, lines=(80, 120, 700, 740))
            halves, clean = split_page_image(page_path, Path(directory) / "split")

            self.assertTrue(clean)
            self.assertEqual(len(halves), 2)
            self.assertTrue(all(half.exists() and half.stat().st_size > 0 for half in halves))

            sizes = [png_size(half) for half in halves]
            full_size = png_size(page_path)

        self.assertTrue(all(width == full_size[0] for width, _ in sizes), sizes)
        self.assertLess(abs(sum(height for _, height in sizes) - full_size[1]), 3)
        self.assertGreater(sizes[0][1], full_size[1] * 0.3)
        self.assertGreater(sizes[1][1], full_size[1] * 0.3)


class SplitRecoveryFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_refused_page_is_recovered_by_splitting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "page.png"
            render_page(page_path, lines=(80, 120, 700, 740))
            calls = []

            async def fake_call(
                _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
            ):
                calls.append(chunk_name)
                if chunk_name == "1":
                    raise ContentBlockedError(
                        "Gemini refused this page image. finishReasons=['RECITATION']"
                    )
                return f"# half {chunk_name}"

            results = await run_process_chunks(
                [Chunk(1, 1, (page_path,), "1")], directory, fake_call
            )
            markdown = (Path(directory) / "pages" / "1.md").read_text()

        self.assertEqual(calls, ["1", "1-part1", "1-part2"])
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["recovered_by"], "page_split")
        self.assertIn("# half 1-part1", markdown)
        self.assertIn("# half 1-part2", markdown)

    async def test_refused_page_is_not_retried_in_a_later_round(self):
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "page.png"
            render_page(page_path, lines=(80, 120, 700, 740))
            calls = []

            async def fake_call(
                _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
            ):
                calls.append(chunk_name)
                raise ContentBlockedError("Gemini refused this page image.")

            results = await run_process_chunks(
                [Chunk(1, 1, (page_path,), "1")],
                directory,
                fake_call,
                attempts_per_page=2,
                retry_rounds=4,
                blocked_page_split="none",
            )

        self.assertEqual(calls, ["1"])
        self.assertEqual(results[0]["status"], "failed")
        self.assertFalse(results[0]["retryable"])
        self.assertIn("ContentBlockedError", results[0]["error"])

    async def test_split_recovery_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "page.png"
            render_page(page_path, lines=(80, 120, 700, 740))
            calls = []

            async def fake_call(
                _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
            ):
                calls.append(chunk_name)
                raise ContentBlockedError("Gemini refused this page image.")

            results = await run_process_chunks(
                [Chunk(1, 1, (page_path,), "1")],
                directory,
                fake_call,
                blocked_page_split="none",
            )

        self.assertEqual(calls, ["1"])
        self.assertEqual(results[0]["status"], "failed")

    async def test_page_without_transcribable_text_anywhere_becomes_blank(self):
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "page.png"
            render_page(page_path, lines=(80, 120, 700, 740))

            async def fake_call(
                _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
            ):
                raise EmptyAnswerError("Gemini returned no answer text. finishReasons=['STOP']")

            results = await run_process_chunks(
                [Chunk(9, 9, (page_path,), "009")], directory, fake_call
            )
            markdown = (Path(directory) / "pages" / "009.md").read_text()

        self.assertEqual(results[0]["status"], "blank")
        self.assertEqual(results[0]["reason"], "no_transcribable_text")
        self.assertEqual(markdown, "")

    async def test_a_refused_half_is_cut_again(self):
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "page.png"
            render_page(page_path, lines=(80, 120, 400, 700, 740))
            calls = []

            async def fake_call(
                _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
            ):
                calls.append(chunk_name)
                if chunk_name in {"1", "1-part2"}:
                    raise ContentBlockedError(
                        "Gemini refused this page image. finishReasons=['RECITATION']"
                    )
                return f"# {chunk_name}"

            results = await run_process_chunks(
                [Chunk(1, 1, (page_path,), "1")], directory, fake_call
            )
            markdown = (Path(directory) / "pages" / "1.md").read_text()

        self.assertEqual(
            calls, ["1", "1-part1", "1-part2", "1-part2-part1", "1-part2-part2"]
        )
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["recovered_by"], "page_split")
        self.assertIn("# 1-part2-part1", markdown)

    async def test_split_depth_bounds_the_extra_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "page.png"
            render_page(page_path, lines=(80, 120, 700, 740))
            calls = []

            async def fake_call(
                _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
            ):
                calls.append(chunk_name)
                raise ContentBlockedError("Gemini refused this page image.")

            with patch("pdf2md.PAGE_SPLIT_DEPTH", 1):
                results = await run_process_chunks(
                    [Chunk(1, 1, (page_path,), "1")], directory, fake_call
                )

        # One page plus its two halves, and no third level.
        self.assertEqual(calls, ["1", "1-part1", "1-part2"])
        self.assertEqual(results[0]["status"], "failed")
        self.assertFalse(results[0]["retryable"])

    async def test_a_hopeless_part_keeps_its_sibling_and_is_reported(self):
        """A part refused at every size must not discard the sibling's text."""
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "page.png"
            render_page(page_path, lines=(80, 120, 400, 700, 740))
            calls = []

            async def fake_call(
                _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
            ):
                calls.append(chunk_name)
                if chunk_name == "1":
                    raise ContentBlockedError("Gemini refused this page image.")
                if chunk_name.startswith("1-part2"):
                    raise ContentBlockedError("Gemini refused this page image.")
                return f"# {chunk_name}"

            with contextlib.redirect_stdout(io.StringIO()) as logged:
                results = await run_process_chunks(
                    [Chunk(1, 1, (page_path,), "1")], directory, fake_call
                )
            markdown = (Path(directory) / "pages" / "1.md").read_text()

        self.assertEqual(
            calls, ["1", "1-part1", "1-part2", "1-part2-part1", "1-part2-part2"]
        )
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["recovered_by"], "page_split")
        self.assertEqual(results[0]["missing_parts"], ["1-part2"])
        self.assertIn("# 1-part1", markdown)
        self.assertNotIn("1-part2-part1", markdown)
        self.assertIn("[warn] 1: Gemini refused 1-part2", logged.getvalue())

    async def test_the_default_depth_cuts_a_refused_page_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "page.png"
            render_page(page_path, lines=(80, 120, 700, 740))
            calls = []

            async def fake_call(
                _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
            ):
                calls.append(chunk_name)
                raise ContentBlockedError("Gemini refused this page image.")

            results = await run_process_chunks(
                [Chunk(1, 1, (page_path,), "1")], directory, fake_call
            )

        # 1 page + 2 halves + 4 quarters, then it gives up instead of looping.
        self.assertEqual(
            calls,
            [
                "1",
                "1-part1",
                "1-part1-part1",
                "1-part1-part2",
                "1-part2",
                "1-part2-part1",
                "1-part2-part2",
            ],
        )
        self.assertEqual(results[0]["status"], "failed")
        self.assertFalse(results[0]["retryable"])

    async def test_capacity_failure_inside_the_split_stays_retryable(self):
        """A 503 on a half must not be turned into a final page failure."""
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "page.png"
            render_page(page_path, lines=(80, 120, 700, 740))
            calls = []
            half_failures = []

            async def fake_call(
                _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
            ):
                calls.append(chunk_name)
                if chunk_name == "1":
                    raise ContentBlockedError("Gemini refused this page image.")
                if chunk_name == "1-part1" and not half_failures:
                    half_failures.append(chunk_name)
                    raise ModelUnavailableError("Gemini HTTP 503: UNAVAILABLE")
                return f"# half {chunk_name}"

            results = await run_process_chunks(
                [Chunk(1, 1, (page_path,), "1")],
                directory,
                fake_call,
                attempts_per_page=1,
                retry_rounds=2,
            )
            markdown = (Path(directory) / "pages" / "1.md").read_text()

        self.assertEqual(calls, ["1", "1-part1", "1", "1-part1", "1-part2"])
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["recovered_by"], "page_split")
        self.assertIn("# half 1-part1", markdown)

    async def test_empty_halves_do_not_hide_a_capacity_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "page.png"
            render_page(page_path, lines=(80, 120, 700, 740))

            async def fake_call(
                _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
            ):
                if chunk_name == "1":
                    raise ContentBlockedError("Gemini refused this page image.")
                raise ModelUnavailableError("Gemini HTTP 503: UNAVAILABLE")

            results = await run_process_chunks(
                [Chunk(1, 1, (page_path,), "1")],
                directory,
                fake_call,
                attempts_per_page=1,
                retry_rounds=1,
            )

        self.assertEqual(results[0]["status"], "failed")
        self.assertTrue(results[0]["retryable"])
        self.assertIn("ModelUnavailableError", results[0]["error"])

    async def test_multi_page_chunk_is_never_split(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "001.png"
            second = Path(directory) / "002.png"
            render_page(first, lines=(80, 700))
            render_page(second, lines=(80, 700))
            calls = []

            async def fake_call(
                _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
            ):
                calls.append(chunk_name)
                raise ContentBlockedError("Gemini refused this page image.")

            results = await run_process_chunks(
                [Chunk(1, 2, (first, second), "001-002")], directory, fake_call
            )

        self.assertEqual(calls, ["001-002"])
        self.assertEqual(results[0]["status"], "failed")
        self.assertFalse(results[0]["retryable"])


class RefusalThroughTheHttpLayerTests(unittest.IsolatedAsyncioTestCase):
    """A refusal must keep its error type through the HTTP layer.

    ``call_model_once`` wraps every Markdown problem in a plain
    ``RuntimeError("Gemini returned unusable Markdown: ...")``, which makes it
    retryable again; a content refusal has to escape that wrapper.
    """

    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, payload):
            self.payload = payload

        async def post(self, *args, **kwargs):
            return RefusalThroughTheHttpLayerTests.FakeResponse(self.payload)

    async def call(self, payload):
        return await call_model_once(
            self.FakeClient(payload),
            FakePool(),
            GEMINI,
            "fake-model",
            [{"text": "page"}],
            "086",
            "high",
        )

    async def test_recitation_keeps_its_error_type(self):
        payload = {"candidates": [{"finishReason": "RECITATION"}]}

        with self.assertRaises(ContentBlockedError) as caught:
            await self.call(payload)

        self.assertIs(type(caught.exception), ContentBlockedError)
        self.assertFalse(is_retryable_error(caught.exception))

    async def test_empty_answer_keeps_its_error_type(self):
        payload = {"candidates": [{"finishReason": "STOP"}]}

        with self.assertRaises(EmptyAnswerError) as caught:
            await self.call(payload)

        self.assertIs(type(caught.exception), EmptyAnswerError)

    async def test_ordinary_markdown_problems_stay_retryable(self):
        payload = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "broken \ufffd output"}]},
                }
            ]
        }

        with self.assertRaises(RuntimeError) as caught:
            await self.call(payload)

        self.assertIs(type(caught.exception), RuntimeError)
        self.assertTrue(is_retryable_error(caught.exception))


if __name__ == "__main__":
    unittest.main()
