"""One page: OCR draft -> model refinement, and every way it can fall back."""
import tempfile
import unittest
from pathlib import Path

import fitz
import httpx

from helpers import FakeOcr, page, run_process_pages
from key_pool import QuotaPoolExhaustedError
from pdf2md import (
    ContentBlockedError,
    EmptyAnswerError,
    ModelUnavailableError,
    Page,
    PermanentProviderError,
    answer_from_payload,
    call_model,
    call_model_once,
    is_blank_page,
    is_retryable_error,
    keep_known_figures,
    make_request_parts,
    page_ink_stats,
)
from providers import GEMINI


def render_page(path: Path, *, lines: tuple[int, ...]) -> None:
    """Write a rendered page image: `lines` are y positions of one text line each."""
    with fitz.open() as document:
        pdf_page = document.new_page()
        for y in lines:
            pdf_page.insert_text((72, y), "Transcribe this line of the page", fontsize=11)
        pix = pdf_page.get_pixmap(matrix=fitz.Matrix(240 / 72, 240 / 72), alpha=False)
        path.write_bytes(pix.tobytes("png"))


class BlankPageTests(unittest.TestCase):
    def measure(self, path: Path):
        with fitz.open(path) as document:
            return is_blank_page(document[0]), page_ink_stats(document[0])

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
        with tempfile.TemporaryDirectory() as directory:
            sparse = Path(directory) / "sparse.png"
            render_page(sparse, lines=(420,))
            verdict, stats = self.measure(sparse)
        self.assertFalse(verdict[0], f"ink={stats[0]:.6f} darkest={stats[1]}")


class RequestPartsTests(unittest.TestCase):
    def build(self, draft):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "007.png"
            image.write_bytes(b"png")
            return make_request_parts(Page(7, image, "007"), "转录要求原文", "high", draft)[0]

    def test_draft_and_image_travel_together(self):
        parts = self.build("# OCR 草稿")
        self.assertIn("第 7 页", parts[0]["text"])
        self.assertIn("<ocr_draft>\n# OCR 草稿\n</ocr_draft>", parts[0]["text"])
        self.assertIn("转录要求原文", parts[0]["text"])
        self.assertIn("inlineData", parts[1])

    def test_without_ocr_the_model_transcribes_the_image(self):
        parts = self.build(None)
        self.assertNotIn("ocr_draft", parts[0]["text"])
        self.assertIn("转录要求原文", parts[0]["text"])

    def test_an_empty_draft_is_announced(self):
        self.assertIn("OCR 未识别到内容", self.build("")[0]["text"])

    def test_neighbouring_strips_follow_the_page_with_their_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("006.bottom.png", "007.png", "008.top.png"):
                (root / name).write_bytes(b"png")
            previous = Page(6, root / "006.png", "006", bottom_path=root / "006.bottom.png")
            following = Page(8, root / "008.png", "008", top_path=root / "008.top.png")
            current = Page(7, root / "007.png", "007")
            both, _ = make_request_parts(current, "要求", "high", "# 草稿", previous, following)
            only_next, _ = make_request_parts(current, "要求", "high", None, None, following)
        self.assertEqual([("text" in part, "inlineData" in part) for part in both],
                         [(True, False), (False, True)] * 3)
        self.assertIn("第一张图片是 PDF 第 7 页", both[0]["text"])
        self.assertIn("上一页（第 6 页）底部", both[2]["text"])
        self.assertIn("下一页（第 8 页）顶部", both[4]["text"])
        self.assertIn("补完整", both[0]["text"])
        self.assertIn("省略这段后半截", both[0]["text"])
        self.assertTrue(both[0]["text"].rstrip().endswith("</ocr_draft>"), "the draft stays last")
        self.assertIn("补完整", only_next[0]["text"])
        self.assertNotIn("省略", only_next[0]["text"], "no rule for a strip that is not attached")
        self.assertEqual(len(only_next), 4)

    def test_only_figures_that_exist_survive(self):
        text = "a ![](images/p001-1.jpg) b ![x](images/made-up.png) c"
        kept = keep_known_figures(text, {"images/p001-1.jpg": Path("x")})
        self.assertEqual(kept, "a ![](images/p001-1.jpg) b  c")


class PageFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_refines_the_ocr_draft(self):
        seen = []

        async def model(_c, _p, _prov, _m, parts, name, *_rest):
            seen.append(parts[0]["text"])
            return "# Refined ![](images/p001-1.jpg)"

        ocr = FakeOcr({1: "# draft"}, {1: {"images/p001-1.jpg": Path("f.jpg")}})
        with tempfile.TemporaryDirectory() as directory:
            results = await run_process_pages([page(1)], directory, model, ocr=ocr)
            written = (Path(directory) / "pages/001.md").read_text()
        self.assertEqual(seen, ["page=1 draft=# draft"])
        self.assertEqual(results[0]["source"], "ocr+model")
        self.assertEqual(results[0]["figures"], ["images/p001-1.jpg"])
        self.assertEqual(written, "# Refined ![](images/p001-1.jpg)\n")

    async def test_adjacent_pages_attach_rendered_non_blank_neighbours(self):
        seen = {}

        async def model(_c, _p, _prov, _m, parts, name, *_rest):
            seen[name] = parts[0]["text"]
            return "# text"

        async def stream():
            for item in (page(1), page(2), page(3, blank=True), page(4)):
                yield item

        with tempfile.TemporaryDirectory() as directory:
            results = await run_process_pages(stream(), directory, model, ocr=FakeOcr({}),
                                              total_pages=4, adjacent_pages=True)
        self.assertEqual([r["status"] for r in results], ["ok", "ok", "blank", "ok"])
        self.assertEqual(seen, {
            "001": "page=1 draft=None previous=None following=2",
            "002": "page=2 draft=None previous=1 following=None",
            "004": "page=4 draft=None",
        })

    async def test_page_without_ocr_is_transcribed_from_the_image(self):
        async def model(_c, _p, _prov, _m, parts, *_rest):
            self.assertEqual(parts[0]["text"], "page=2 draft=None")
            return "# From image"

        with tempfile.TemporaryDirectory() as directory:
            results = await run_process_pages([page(2)], directory, model, ocr=FakeOcr({}))
        self.assertEqual(results[0]["source"], "model")

    async def test_refused_page_falls_back_to_the_ocr_draft_without_retrying(self):
        calls = []

        async def model(*args):
            calls.append(args[5])
            raise ContentBlockedError("RECITATION")

        with tempfile.TemporaryDirectory() as directory:
            results = await run_process_pages(
                [page(3)], directory, model, ocr=FakeOcr({3: "OCR text"}), retry_rounds=3
            )
            written = (Path(directory) / "pages/003.md").read_text()
        self.assertEqual(calls, ["003"])
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["source"], "ocr")
        self.assertIn("RECITATION", results[0]["fallback_reason"])
        self.assertEqual(written, "OCR text\n")

    async def test_capacity_errors_retry_before_the_last_round_uses_the_draft(self):
        calls = []

        async def model(*args):
            calls.append(args[5])
            raise ModelUnavailableError("HTTP 503")

        with tempfile.TemporaryDirectory() as directory:
            results = await run_process_pages(
                [page(4)], directory, model, ocr=FakeOcr({4: "OCR text"}),
                retry_rounds=2, attempts_per_page=2,
            )
        self.assertEqual(len(calls), 4, "two rounds of two immediate attempts")
        self.assertEqual(results[0]["source"], "ocr")

    async def test_refusal_without_a_draft_is_a_permanent_failure(self):
        async def model(*_args):
            raise ContentBlockedError("SAFETY")

        with tempfile.TemporaryDirectory() as directory:
            results = await run_process_pages([page(5)], directory, model)
        self.assertEqual(results[0]["status"], "failed")
        self.assertFalse(results[0]["retryable"])

    async def test_empty_answer_without_a_draft_is_a_blank_page(self):
        async def model(*_args):
            raise EmptyAnswerError("STOP")

        with tempfile.TemporaryDirectory() as directory:
            results = await run_process_pages([page(6)], directory, model, ocr=FakeOcr({6: "  "}))
        self.assertEqual(results[0]["status"], "blank")
        self.assertEqual(results[0]["reason"], "no_transcribable_text")

    async def test_blank_page_is_skipped_without_any_request(self):
        async def model(*_args):
            raise AssertionError("blank pages must not reach the model")

        with tempfile.TemporaryDirectory() as directory:
            results = await run_process_pages([page(7, blank=True)], directory, model)
            written = (Path(directory) / "pages/007.md").read_text()
        self.assertEqual(results[0]["status"], "blank")
        self.assertEqual(results[0]["reason"], "blank_page")
        self.assertEqual(written, "")

    async def test_page_recovers_inside_the_first_round(self):
        calls = []

        async def model(*args):
            calls.append(args[5])
            if len(calls) == 1:
                raise ModelUnavailableError("HTTP 503")
            return "# Page 1"

        with tempfile.TemporaryDirectory() as directory:
            results = await run_process_pages([page(1)], directory, model, retry_rounds=1)
        self.assertEqual(calls, ["001", "001"])
        self.assertEqual(results[0]["status"], "ok")

    async def test_first_attempt_covers_all_pages_before_retrying_a_failure(self):
        calls = []

        async def model(*args):
            calls.append(args[5])
            if args[5] == "001" and calls.count("001") == 1:
                raise RuntimeError("HTTP 500")
            return f"# Page {args[5]}"

        with tempfile.TemporaryDirectory() as directory:
            results = await run_process_pages(
                [page(1), page(2), page(3)], directory, model, attempts_per_page=1
            )
        self.assertEqual(calls, ["001", "002", "003", "001"])
        self.assertTrue(all(r["status"] == "ok" for r in results))

    async def test_failed_rerun_cannot_retain_old_page_with_same_name(self):
        async def model(*_args):
            raise PermanentProviderError("HTTP 400")

        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / "pages/001.md"
            stale.parent.mkdir()
            stale.write_text("Old unrelated document")
            results = await run_process_pages([page(1)], directory, model)
            self.assertEqual(results[0]["status"], "failed")
            self.assertFalse(stale.exists())


class ErrorClassificationTests(unittest.TestCase):
    def test_refusals_are_content_blocks(self):
        for payload in (
            {"candidates": [{"finishReason": "RECITATION"}]},
            {"candidates": [{"finishReason": "PROHIBITED_CONTENT"}]},
            {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ContentBlockedError):
                answer_from_payload(GEMINI, payload)

    def test_empty_stop_answer_is_an_empty_answer(self):
        for payload in (
            {"candidates": [{"finishReason": "STOP"}]},
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"thought": True, "text": "hm"}]}}]},
        ):
            with self.subTest(payload=payload), self.assertRaises(EmptyAnswerError):
                answer_from_payload(GEMINI, payload)

    def test_content_errors_never_enter_the_backoff_rounds(self):
        self.assertFalse(is_retryable_error(ContentBlockedError("RECITATION")))
        self.assertFalse(is_retryable_error(EmptyAnswerError("STOP")))
        self.assertFalse(is_retryable_error(PermanentProviderError("HTTP 400")))
        self.assertFalse(is_retryable_error(QuotaPoolExhaustedError("daily")))
        self.assertFalse(is_retryable_error(ValueError("bad page")))
        self.assertTrue(is_retryable_error(ModelUnavailableError("HTTP 503")))
        self.assertTrue(is_retryable_error(RuntimeError("Gemini HTTP 500: boom")))


class StubPool:
    def __init__(self):
        self.retired = []

    def record_http_result(self, *_args):
        pass

    def retire(self, key_index, reason):
        self.retired.append((key_index, reason))

    async def acquire(self):
        return 0, "dummy-key", "lease"

    async def mark_error(self, *_args, **_kwargs):
        pass

    async def mark_success(self, *_args, **_kwargs):
        pass

    async def rate_limited(self, *_args, **_kwargs):
        pass


class StubClient:
    def __init__(self, response: httpx.Response):
        self.response = response

    async def post(self, *_args, **_kwargs):
        return self.response


class HttpLayerTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, response, pool=None, provider=GEMINI):
        return await call_model_once(StubClient(response), pool or StubPool(), provider, "m", [], "001", "high")

    async def test_depleted_credits_retire_the_key_and_keep_the_page_retryable(self):
        from providers import MODELFLARE
        for provider in (GEMINI, MODELFLARE):
            pool = StubPool()
            payload = {"error": {"code": 402, "message": "Your prepayment credits are depleted."}}
            with self.subTest(provider=provider.name), self.assertRaises(RuntimeError) as caught:
                await self.call(httpx.Response(402, json=payload), pool, provider)
            self.assertTrue(is_retryable_error(caught.exception))
            self.assertEqual(pool.retired[0][0], 0)
            self.assertIn("credits are depleted", pool.retired[0][1])

    async def test_capacity_statuses_mean_model_unavailable(self):
        for status in (404, 500, 503, 504):
            with self.subTest(status=status), self.assertRaises(ModelUnavailableError):
                await self.call(httpx.Response(status, text="busy"))

    async def test_client_statuses_are_permanent(self):
        for status in (400, 401, 403):
            with self.subTest(status=status), self.assertRaises(PermanentProviderError):
                await self.call(httpx.Response(status, text="bad"))

    async def test_refusal_keeps_its_error_type(self):
        with self.assertRaises(ContentBlockedError) as caught:
            await self.call(httpx.Response(200, json={"candidates": [{"finishReason": "RECITATION"}]}))
        self.assertIs(type(caught.exception), ContentBlockedError)

    async def test_ordinary_markdown_problems_stay_retryable(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "broken � output"}]}}]}
        with self.assertRaises(RuntimeError) as caught:
            await self.call(httpx.Response(200, json=payload))
        self.assertIs(type(caught.exception), RuntimeError)
        self.assertTrue(is_retryable_error(caught.exception))


class ModelFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_chain_is_used_when_the_primary_is_saturated(self):
        attempted = []

        async def once(_c, _p, _prov, model, *_rest):
            attempted.append(model)
            if model != "healthy":
                raise ModelUnavailableError("HTTP 503")
            return "transcribed"

        from unittest.mock import patch
        with patch("pdf2md.call_model_once", new=once):
            text = await call_model(None, None, GEMINI, "saturated", [], "1", "high", ("healthy",))
        self.assertEqual(text, "transcribed")
        self.assertEqual(attempted, ["saturated", "healthy"])

    async def test_fallback_chain_does_not_swallow_permanent_errors(self):
        async def once(*_args):
            raise PermanentProviderError("HTTP 403")

        from unittest.mock import patch
        with patch("pdf2md.call_model_once", new=once), self.assertRaises(PermanentProviderError):
            await call_model(None, None, GEMINI, "a", [], "1", "high", ("b",))


if __name__ == "__main__":
    unittest.main()
