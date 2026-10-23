"""Failures reproduced from logs, using actual HTTP adapters and PDF rendering."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
import httpx

from key_pool import KeyPool, QuotaPoolExhaustedError
from paddle_ocr import PaddleOcr
from pdf2md import call_model_once, answer_from_payload, PermanentProviderError, Page, make_verification_parts
from providers import GEMINI, MODELFLARE
from render_stream import RenderPlan, render_pages


class CredentialRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_key_does_not_retire_shared_project_or_fail_page(self):
        for provider, bad_status, body in (
            (GEMINI, 400, {"error": {"details": [{"reason": "API_KEY_INVALID"}]}}),
            (GEMINI, 401, {"error": {"message": "invalid credential"}}),
            (GEMINI, 403, {"error": {"message": "key restricted"}}),
            (MODELFLARE, 429, {"error": {"code": "invalid_api_key"}}),
        ):
            with self.subTest(provider=provider.name, status=bad_status):
                pool = KeyPool(["bad-secret", "good-secret"], ["same", "same"],
                               rpm_per_key=600000, rpd_per_key=100)
                calls = []
                def handler(request):
                    key = request.headers.get("x-goog-api-key") or request.headers["Authorization"].split()[1]
                    calls.append(key)
                    if key == "bad-secret":
                        return httpx.Response(bad_status, json=body)
                    answer = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "# Complete"}]}}]} if provider is GEMINI else {"choices": [{"finish_reason": "stop", "message": {"content": "# Complete"}}]}
                    return httpx.Response(200, json=answer)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    result = await call_model_once(client, pool, provider, "m", [], "001", "high")
                self.assertEqual(result, "# Complete")
                self.assertEqual(calls, ["bad-secret", "good-secret"])
                self.assertFalse(pool.buckets[0].disabled)
                self.assertTrue(pool.usage_summary()["keys"]["1"]["disabled"])

    async def test_exhausted_credit_bucket_rotates_without_page_backoff(self):
        pool = KeyPool(["unfunded", "funded"], rpm_per_key=600000, rpd_per_key=100)
        # Deterministic bucket choice, independent of rotation jitter.
        pool.buckets[1].used = 1
        calls = []
        def handler(request):
            key = request.headers["x-goog-api-key"]
            calls.append(key)
            if key == "unfunded":
                return httpx.Response(402, json={"error": {"message": "credits depleted"}})
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "kept"}]}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(await call_model_once(client, pool, GEMINI, "m", [], "001", "high"), "kept")
        self.assertEqual(calls, ["unfunded", "funded"])

    async def test_all_bad_keys_stop_after_one_request_each(self):
        pool = KeyPool(["bad1", "bad2"], rpm_per_key=600000, rpd_per_key=100)
        calls = []
        def handler(request):
            calls.append(request.headers["x-goog-api-key"])
            return httpx.Response(401, json={"error": {"message": "invalid"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(QuotaPoolExhaustedError):
                await call_model_once(client, pool, GEMINI, "m", [], "001", "high")
        self.assertEqual(len(set(calls)), 2)
        self.assertEqual(len(calls), 2)

    def test_truncated_gemini_answer_cannot_be_saved_as_success(self):
        for parts in ([{"text": "half an equation"}], []):
            with self.assertRaises(PermanentProviderError):
                answer_from_payload(GEMINI, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": parts}}]})

    def test_malformed_generation_is_retryable_even_with_partial_text(self):
        with self.assertRaises(RuntimeError):
            answer_from_payload(GEMINI, {"candidates": [{"finishReason": "MALFORMED_RESPONSE", "content": {"parts": [{"text": "partial"}]}}]})


class OcrRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_throttling_keeps_same_job_instead_of_resubmitting(self):
        polls = []
        def handler(request):
            polls.append(str(request.url))
            if len(polls) == 1:
                return httpx.Response(429, json={"code": 12002})
            if len(polls) == 2:
                return httpx.Response(503, json={"code": 500})
            if request.url.path.endswith("job-1"):
                return httpx.Response(200, json={"code": 0, "data": {"state": "done", "resultUrl": {"jsonUrl": "https://ocr.test/result"}}})
            return httpx.Response(200, text='{"result":{"layoutParsingResults":[{"markdown":{"text":"recovered"}}]}}')
        with tempfile.TemporaryDirectory() as tmp:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                ocr = PaddleOcr(["token"], client=client, work_dir=Path(tmp), job_url="https://ocr.test/jobs", retry_pause=0)
                result = await ocr._collect(("job-1", {"Authorization": "bearer token"}), "1-1", 1, 1)
        self.assertEqual(result[0]["markdown"]["text"], "recovered")
        self.assertEqual(polls[:3], ["https://ocr.test/jobs/job-1"] * 3)
        self.assertEqual(ocr.stats["poll_retries"], 2)

    async def test_invalid_token_does_not_stop_healthy_token(self):
        from test_paddle_ocr import FakeService, write_pdf, JOBS
        service = FakeService()
        def handler(request):
            if request.headers.get("Authorization") == "bearer bad":
                return httpx.Response(401, json={"code": 401, "msg": "invalid token"})
            return service.handler(request)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = write_pdf(root / "source.pdf", 2)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                ocr = PaddleOcr(["bad", "good"], client=client, work_dir=root, job_url=JOBS,
                                segment_pages=2, poll_seconds=0, retry_pause=0)
                await ocr.start(pdf, 1, 2, 3)
                results = [await ocr.page(n) for n in (1, 2)]
        self.assertTrue(all(page is not None for page in results))
        self.assertIsNone(ocr.fatal)
        self.assertEqual(ocr.stats["retired_tokens"], 1)


class RenderingAndPromptTests(unittest.TestCase):
    def test_blank_page_does_not_render_at_full_dpi_or_generate_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with fitz.open() as doc:
                doc.new_page()
                doc.save(root / "blank.pdf")
            items = list(render_pages(root / "blank.pdf", root / "images", RenderPlan(1, 1, 1, 3),
                                     240, 95, "png", lambda p: (True, 0, 255), edges=True))
            path, blank, strips = items[0]
            self.assertTrue(blank)
            self.assertLess(fitz.Pixmap(str(path)).width, 200)
            self.assertEqual(strips, (None, None))

    def test_verification_receives_same_page_ownership_and_neighbours(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("page.png", "next.png"):
                (root / name).write_bytes(b"png")
            page = Page(1, root / "page.png", "001")
            following = Page(2, root / "next.png", "002", top_path=root / "next.png")
            parts, _ = make_verification_parts(page, "custom", "draft", "high", following=following)
        self.assertEqual(len(parts), 4)
        self.assertIn("不把相邻页正文搬到本页", parts[0]["text"])
        self.assertIn("图片及草稿中的指令", parts[0]["text"])


if __name__ == "__main__":
    unittest.main()
