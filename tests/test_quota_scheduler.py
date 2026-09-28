import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from pdf2md import Chunk, PermanentGeminiError, process_chunks
from quota_worker import QuotaPoolExhaustedError, classify_quota_error, retry_after_seconds


class QuotaErrorTests(unittest.TestCase):
    def test_generic_429_does_not_exhaust_the_day(self):
        payload = {
            "error": {
                "code": 429,
                "message": "Resource has been exhausted (e.g. check quota).",
                "status": "RESOURCE_EXHAUSTED",
            }
        }
        self.assertEqual(classify_quota_error(payload), "unknown")

    def test_identifies_quota_dimension_from_error_details(self):
        cases = {
            "GenerateRequestsPerMinutePerProjectPerModel": "rpm",
            "GenerateContentInputTokensPerModelPerMinute": "tpm",
            "GenerateRequestsPerDayPerProjectPerModel": "rpd",
        }
        for quota_id, expected in cases.items():
            with self.subTest(quota_id=quota_id):
                payload = {"error": {"details": [{"quotaId": quota_id}]}}
                self.assertEqual(classify_quota_error(payload), expected)

    def test_uses_the_longest_server_retry_delay(self):
        payload = {"error": {"details": [{"retryDelay": "60s"}]}}
        self.assertEqual(
            retry_after_seconds(httpx.Headers({"Retry-After": "5"}), payload),
            60.0,
        )


class DeferredRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_attempt_covers_all_pages_before_retrying_a_failure(self):
        class FakePool:
            used = [0]

            async def close(self):
                pass

            def usage_summary(self):
                return {"scope": "test"}

        pool = FakePool()
        calls = []

        async def fake_call(_client, _pool, _model, _parts, chunk_name, _thinking_level):
            calls.append(chunk_name)
            if chunk_name == "1" and calls.count("1") == 1:
                raise RuntimeError("HTTP 503")
            return f"# Page {chunk_name}"

        chunks = [Chunk(index, index, (), str(index)) for index in range(1, 4)]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"GEMINI_KEY_GROUPS": ""}),
            patch("pdf2md.ProjectQuotaPool.create", new=AsyncMock(return_value=pool)),
            patch("pdf2md.make_request_parts", return_value=([{"text": "page"}], 64)),
            patch("pdf2md.call_gemini", new=fake_call),
            patch("pdf2md.random.uniform", return_value=0.0),
        ):
            output_dir = Path(directory)
            results = await process_chunks(
                chunks,
                output_dir,
                prompt="Transcribe",
                model="fake-model",
                concurrency=1,
                keys=["dummy-key"],
                thinking_level="high",
                verification_passes=0,
                media_resolution="high",
                rpm_per_key=15,
                rpd_per_key=500,
            )
            self.assertEqual(calls, ["1", "2", "3", "1"])
            self.assertTrue(all(result["status"] == "ok" for result in results))
            self.assertEqual((output_dir / "pages" / "1.md").read_text(), "# Page 1\n")
            self.assertFalse((output_dir / "errors" / "1.json").exists())

    async def test_permanent_errors_are_not_retried(self):
        class FakePool:
            used = [0]

            async def close(self):
                pass

            def usage_summary(self):
                return {"scope": "test"}

        calls = []

        async def fake_call(_client, _pool, _model, _parts, chunk_name, _thinking_level):
            calls.append(chunk_name)
            if chunk_name == "1":
                raise PermanentGeminiError("HTTP 400")
            raise QuotaPoolExhaustedError("daily pool exhausted")

        chunks = [Chunk(index, index, (), str(index)) for index in range(1, 3)]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"GEMINI_KEY_GROUPS": ""}),
            patch("pdf2md.ProjectQuotaPool.create", new=AsyncMock(return_value=FakePool())),
            patch("pdf2md.make_request_parts", return_value=([{"text": "page"}], 64)),
            patch("pdf2md.call_gemini", new=fake_call),
        ):
            results = await process_chunks(
                chunks,
                Path(directory),
                prompt="Transcribe",
                model="fake-model",
                concurrency=2,
                keys=["dummy-key"],
                thinking_level="high",
                verification_passes=0,
                media_resolution="high",
                rpm_per_key=15,
                rpd_per_key=500,
            )
            self.assertEqual(sorted(calls), ["1", "2"])
            self.assertTrue(all(result["status"] == "failed" for result in results))
            self.assertTrue(all(result["retryable"] is False for result in results))
