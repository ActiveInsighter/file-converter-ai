import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from pdf2md import (
    Chunk,
    ModelUnavailableError,
    PermanentProviderError,
    call_model,
    call_model_once,
    process_chunks,
)
from quota_api.scheduler import QuotaScheduler
from providers import GEMINI
from quota_client import QuotaPoolExhaustedError, classify_quota_error, retry_after_seconds


class _UnusedRedis:
    """Stands in for a Redis client when only the pure-Python checks run."""

    def register_script(self, _source):
        def _script(**_kwargs):
            raise AssertionError("Valkey must not be reached in this test")

        return _script


class PinnedKeyCountTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_key_count_rejects_a_different_pool_size(self):
        scheduler = QuotaScheduler(_UnusedRedis(), expected_key_count=66)
        result = await scheduler.configure(
            [f"project-{index}" for index in range(67)],
            rpm_per_project=15,
            rpd_per_project=500,
        )
        self.assertEqual(result.status, 400)
        self.assertEqual(result.body["error"], "invalid_project_mapping")
        self.assertEqual(result.body["keyCount"], 67)

    async def test_unpinned_key_count_still_requires_one_project_per_key(self):
        scheduler = QuotaScheduler(_UnusedRedis())
        result = await scheduler.configure(
            ["shared-project", "shared-project"],
            rpm_per_project=15,
            rpd_per_project=500,
        )
        self.assertEqual(result.status, 409)
        self.assertEqual(result.body["error"], "expected_one_project_per_key")


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

        async def fake_call(
            _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
        ):
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
            patch("pdf2md.call_model", new=fake_call),
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
                attempts_per_page=1,
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

        async def fake_call(
            _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
        ):
            calls.append(chunk_name)
            if chunk_name == "1":
                raise PermanentProviderError("HTTP 400")
            raise QuotaPoolExhaustedError("daily pool exhausted")

        chunks = [Chunk(index, index, (), str(index)) for index in range(1, 3)]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"GEMINI_KEY_GROUPS": ""}),
            patch("pdf2md.ProjectQuotaPool.create", new=AsyncMock(return_value=FakePool())),
            patch("pdf2md.make_request_parts", return_value=([{"text": "page"}], 64)),
            patch("pdf2md.call_model", new=fake_call),
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


class _StubPool:
    """Minimal ProjectQuotaPool stand-in for call_model_once tests."""

    def record_http_result(self, _status, _elapsed):
        return None

    async def acquire(self):
        return 0, "dummy-key", "6f1c2f0e-0000-4000-8000-000000000001"

    async def mark_error(self, *_args, **_kwargs):
        return None

    async def mark_success(self, *_args, **_kwargs):
        return None

    async def rate_limited(self, *_args, **_kwargs):
        return None


class _StubResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = "stub body"
        self.headers = {}

    def json(self):
        raise ValueError("stub response is not JSON")


class _StubClient:
    def __init__(self, status_code: int):
        self.status_code = status_code

    async def post(self, *_args, **_kwargs):
        return _StubResponse(self.status_code)


class GeminiErrorClassificationTests(unittest.IsolatedAsyncioTestCase):
    """503/404 are capacity problems; 400/401/403 are configuration problems."""

    async def test_capacity_statuses_are_retryable(self):
        for status in (404, 500, 503, 504):
            with self.subTest(status=status):
                with self.assertRaises(ModelUnavailableError):
                    await call_model_once(
                        _StubClient(status), _StubPool(), GEMINI, "m", [], "1", "high"
                    )

    async def test_client_statuses_are_permanent(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                with self.assertRaises(PermanentProviderError):
                    await call_model_once(
                        _StubClient(status), _StubPool(), GEMINI, "m", [], "1", "high"
                    )


class ModelFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_chain_is_used_when_the_primary_is_saturated(self):
        attempted: list[str] = []

        async def fake_once(
            _client, _pool, _provider, model, _parts, _chunk, _thinking, *_rest
        ):
            attempted.append(model)
            if model != "healthy-model":
                raise ModelUnavailableError("HTTP 503 UNAVAILABLE")
            return "transcribed"

        with patch("pdf2md.call_model_once", new=fake_once):
            text = await call_model(
                None, None, GEMINI, "saturated-model", [], "1", "high", ("healthy-model",)
            )

        self.assertEqual(text, "transcribed")
        self.assertEqual(attempted, ["saturated-model", "healthy-model"])

    async def test_fallback_chain_does_not_swallow_permanent_errors(self):
        async def fake_once(*_args, **_kwargs):
            raise PermanentProviderError("HTTP 403")

        with patch("pdf2md.call_model_once", new=fake_once):
            with self.assertRaises(PermanentProviderError):
                await call_model(None, None, GEMINI, "a", [], "1", "high", ("b",))

    async def test_last_error_is_raised_when_the_whole_chain_fails(self):
        async def fake_once(*_args, **_kwargs):
            raise ModelUnavailableError("HTTP 503 UNAVAILABLE")

        with patch("pdf2md.call_model_once", new=fake_once):
            with self.assertRaises(ModelUnavailableError):
                await call_model(None, None, GEMINI, "a", [], "1", "high", ("b", "c"))


class ImmediateRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_recovers_inside_the_first_round(self):
        class FakePool:
            used = [0]

            async def close(self):
                pass

            def usage_summary(self):
                return {"scope": "test"}

        calls: list[str] = []

        async def fake_call(
            _client, _pool, _provider, _model, _parts, chunk_name, _thinking_level, _fallbacks=(), *_rest
        ):
            calls.append(chunk_name)
            if len(calls) == 1:
                raise ModelUnavailableError("HTTP 503 UNAVAILABLE")
            return "# Page 1"

        chunks = [Chunk(1, 1, (), "1")]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"GEMINI_KEY_GROUPS": ""}),
            patch("pdf2md.ProjectQuotaPool.create", new=AsyncMock(return_value=FakePool())),
            patch("pdf2md.make_request_parts", return_value=([{"text": "page"}], 64)),
            patch("pdf2md.call_model", new=fake_call),
            patch("pdf2md.random.uniform", return_value=0.0),
        ):
            results = await process_chunks(
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
                attempts_per_page=2,
                retry_rounds=1,
            )

        self.assertEqual(calls, ["1", "1"])
        self.assertEqual(results[0]["status"], "ok")
