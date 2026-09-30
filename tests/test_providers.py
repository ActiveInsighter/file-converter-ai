"""Provider adapters: one wire protocol per gateway, one retry policy above them.

The Gemini tests here are deliberately written as *golden* assertions on the
exact URL, headers and body: the adapter is meant to be a pure extraction of the
code that used to live inline in ``_call_model_attempt``, so any drift is a bug.
"""

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pdf2md import (
    Chunk,
    ContentBlockedError,
    ModelUnavailableError,
    PermanentProviderError,
    answer_from_payload,
    call_model,
    is_retryable_error,
    process_chunks,
    resolve_fallback_models,
)
from providers import (
    GEMINI,
    MODELFLARE,
    ProviderRefusal,
    normalize_reasoning_effort,
    provider_names,
    resolve_model,
    resolve_provider,
)
from quota_client import LocalKeyPool, QuotaPoolExhaustedError

GEMINI_LITE = "gemini-3.5-flash-lite"


class FallbackSelectionTests(unittest.TestCase):
    def test_custom_gemini_model_does_not_silently_downgrade_to_lite(self):
        self.assertEqual(resolve_fallback_models(GEMINI, "gemini-3.8-flash", ""), ())
        self.assertEqual(resolve_fallback_models(GEMINI, "gemini-3.7-flash", ""), ())

    def test_default_model_keeps_its_same_tier_fallback(self):
        self.assertEqual(
            resolve_fallback_models(GEMINI, GEMINI.default_model, ""),
            GEMINI.default_model_fallbacks,
        )

    def test_explicit_fallbacks_and_disable_are_honored(self):
        self.assertEqual(
            resolve_fallback_models(GEMINI, "gemini-3.8-flash", "gemini-3.7-flash"),
            ("gemini-3.7-flash",),
        )
        self.assertEqual(resolve_fallback_models(GEMINI, GEMINI.default_model, "none"), ())


class FallbackExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_model_unavailability_uses_an_explicit_fallback(self):
        with patch(
            "pdf2md.call_model_once",
            new=AsyncMock(side_effect=[ModelUnavailableError("503"), "# recovered"]),
        ) as send:
            result = await call_model(
                None, None, GEMINI, "gemini-3.8-flash", [{"text": "page"}],
                "001", "low", ("gemini-3.7-flash",),
            )
        self.assertEqual(result, "# recovered")
        self.assertEqual([call.args[3] for call in send.call_args_list], ["gemini-3.8-flash", "gemini-3.7-flash"])

    async def test_transport_failure_does_not_change_the_requested_model(self):
        with patch("pdf2md.call_model_once", new=AsyncMock(side_effect=RuntimeError("network"))) as send:
            with self.assertRaisesRegex(RuntimeError, "network"):
                await call_model(
                    None, None, GEMINI, "gemini-3.8-flash", [{"text": "page"}],
                    "001", "low", ("gemini-3.7-flash",),
                )
        self.assertEqual(send.call_count, 1)


def image_part(data: bytes = b"png-bytes", mime: str = "image/png", level: str | None = None):
    part = {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}}
    if level is not None:
        part["mediaResolution"] = {"level": f"MEDIA_RESOLUTION_{level.upper()}"}
    return part


class RegistryTests(unittest.TestCase):
    def test_gemini_stays_the_default(self):
        self.assertIs(resolve_provider(None), GEMINI)
        self.assertIs(resolve_provider(""), GEMINI)
        self.assertIs(resolve_provider("GEMINI"), GEMINI)

    def test_modelflare_is_registered(self):
        self.assertIs(resolve_provider("modelflare"), MODELFLARE)
        self.assertIn("modelflare", provider_names())

    def test_an_unknown_provider_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            resolve_provider("openai")
        self.assertIn("Unknown provider", str(caught.exception))

    def test_each_provider_resolves_its_own_model(self):
        self.assertEqual(resolve_model(GEMINI, ""), GEMINI_LITE)
        self.assertEqual(resolve_model(GEMINI, " custom "), "custom")

    def test_a_gateway_without_a_safe_default_asks_instead_of_guessing(self):
        with self.assertRaises(ValueError) as caught:
            resolve_model(MODELFLARE, "")
        self.assertIn("--model is required", str(caught.exception))

    def test_only_gemini_needs_the_shared_quota_pool(self):
        self.assertTrue(GEMINI.uses_shared_quota_pool)
        self.assertFalse(MODELFLARE.uses_shared_quota_pool)

    def test_each_provider_reads_its_own_key_list(self):
        self.assertEqual(GEMINI.api_key_env, "GEMINI_API_KEYS")
        self.assertEqual(MODELFLARE.api_key_env, "MODELFLARE_API_KEYS")


class ReasoningEffortTests(unittest.TestCase):
    def test_blank_means_let_the_gateway_decide(self):
        self.assertEqual(normalize_reasoning_effort(""), "")
        self.assertEqual(normalize_reasoning_effort(" unspecified "), "")

    def test_a_gemini_thinking_level_is_accepted_as_an_alias(self):
        self.assertEqual(normalize_reasoning_effort("minimal"), "low")
        self.assertEqual(normalize_reasoning_effort("HIGH"), "high")

    def test_a_native_effort_passes_through(self):
        self.assertEqual(normalize_reasoning_effort("xhigh"), "xhigh")

    def test_an_unknown_effort_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_reasoning_effort("turbo")


class GeminiWireFormatUnchangedTests(unittest.TestCase):
    def test_endpoint_auth_and_body_match_the_legacy_shape(self):
        parts = [{"text": "page"}]

        self.assertEqual(
            GEMINI.endpoint(GEMINI.default_base_url, "m"),
            "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
        )
        self.assertEqual(
            GEMINI.build_headers("k"),
            {"x-goog-api-key": "k", "Content-Type": "application/json"},
        )
        self.assertEqual(
            GEMINI.build_payload("m", parts, thinking_level="high"),
            {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"thinkingConfig": {"thinkingLevel": "high"}},
            },
        )

    def test_max_output_tokens_is_omitted_unless_asked_for(self):
        parts = [{"text": "page"}]
        default = GEMINI.build_payload("m", parts, thinking_level="high")
        self.assertNotIn("maxOutputTokens", default["generationConfig"])

        capped = GEMINI.build_payload("m", parts, thinking_level="high", max_output_tokens=8000)
        self.assertEqual(capped["generationConfig"]["maxOutputTokens"], 8000)

    def test_refusals_keep_the_legacy_wording(self):
        with self.assertRaises(ProviderRefusal) as recitation:
            GEMINI.extract_text({"candidates": [{"finishReason": "RECITATION"}]})
        self.assertEqual(recitation.exception.kind, "blocked")
        self.assertIn("RECITATION", recitation.exception.detail)

        with self.assertRaises(ProviderRefusal) as blocked:
            GEMINI.extract_text({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})
        self.assertEqual(blocked.exception.kind, "blocked")
        self.assertIn("blockReason=SAFETY", blocked.exception.detail)

        with self.assertRaises(ProviderRefusal) as empty:
            GEMINI.extract_text({"candidates": [{"finishReason": "STOP"}]})
        self.assertEqual(empty.exception.kind, "empty")

    def test_thinking_parts_are_not_an_answer(self):
        payload = {"candidates": [{"content": {"parts": [{"thought": True, "text": "hm"}]}}]}
        with self.assertRaises(ProviderRefusal) as caught:
            GEMINI.extract_text(payload)
        self.assertEqual(caught.exception.kind, "empty")

    def test_status_classification_is_unchanged(self):
        for status in (404, 500, 503, 504):
            self.assertEqual(GEMINI.status_error_kind(status), "unavailable", status)
        for status in (400, 401, 403):
            self.assertEqual(GEMINI.status_error_kind(status), "permanent", status)
        self.assertEqual(GEMINI.status_error_kind(429), "rate_limited")
        # A status Google never returns keeps its previous generic treatment.
        self.assertEqual(GEMINI.status_error_kind(402), "retryable")


class ModelflarePayloadTests(unittest.TestCase):
    def test_endpoint_and_auth_follow_the_openai_contract(self):
        self.assertEqual(
            MODELFLARE.endpoint(MODELFLARE.default_base_url, "gpt-6-sol"),
            "https://modelflare.dev/v1/chat/completions",
        )
        headers = MODELFLARE.build_headers("sk-mf-abc")
        self.assertEqual(headers["Authorization"], "Bearer sk-mf-abc")
        # Modelflare sits behind Cloudflare, where an ordinary agent avoids an
        # interstitial instead of JSON.
        self.assertTrue(headers["User-Agent"])

    def test_text_and_images_become_openai_content_parts(self):
        parts = [
            {"text": "transcribe this"},
            image_part(level="ultra_high"),
            image_part(mime="image/jpeg"),
        ]

        payload = MODELFLARE.build_payload("gpt-5.6-sol", parts)
        message = payload["messages"][0]

        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(message["role"], "user")
        self.assertEqual(
            [item["type"] for item in message["content"]],
            ["text", "image_url", "image_url"],
        )
        self.assertEqual(message["content"][0]["text"], "transcribe this")

        first = message["content"][1]["image_url"]
        self.assertTrue(first["url"].startswith("data:image/png;base64,"))
        self.assertEqual(first["detail"], "high")
        self.assertTrue(message["content"][2]["image_url"]["url"].startswith("data:image/jpeg;"))

    def test_an_unspecified_resolution_omits_the_detail_field(self):
        payload = MODELFLARE.build_payload("m", [image_part()])
        self.assertNotIn("detail", payload["messages"][0]["content"][0]["image_url"])

    def test_a_low_resolution_is_passed_through(self):
        payload = MODELFLARE.build_payload("m", [image_part(level="low")])
        self.assertEqual(payload["messages"][0]["content"][0]["image_url"]["detail"], "low")

    def test_reasoning_effort_and_cap_are_opt_in(self):
        bare = MODELFLARE.build_payload("m", [{"text": "t"}])
        self.assertNotIn("reasoning_effort", bare)
        self.assertNotIn("max_tokens", bare)

        asked = MODELFLARE.build_payload("m", [{"text": "t"}], reasoning_effort="high")
        self.assertEqual(asked["reasoning_effort"], "high")

        capped = MODELFLARE.build_payload("m", [{"text": "t"}], max_output_tokens=32000)
        self.assertEqual(capped["max_tokens"], 32000)


class ModelflareResponseTests(unittest.TestCase):
    def test_plain_content_is_returned(self):
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": "# page"}}]}
        self.assertEqual(MODELFLARE.extract_text(payload), "# page")

    def test_content_parts_are_joined_in_order(self):
        payload = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": [
                            {"type": "text", "text": "first"},
                            {"type": "text", "text": "second"},
                        ]
                    },
                }
            ]
        }
        self.assertEqual(MODELFLARE.extract_text(payload), "first\nsecond")

    def test_an_explicit_refusal_is_a_content_block(self):
        payload = {
            "choices": [
                {
                    "finish_reason": "content_filter",
                    "message": {"content": "", "refusal": "cannot reproduce"},
                }
            ]
        }
        with self.assertRaises(ProviderRefusal) as caught:
            MODELFLARE.extract_text(payload)
        self.assertEqual(caught.exception.kind, "blocked")
        self.assertIn("cannot reproduce", caught.exception.detail)

    def test_a_content_filter_finish_reason_is_a_content_block(self):
        payload = {"choices": [{"finish_reason": "content_filter", "message": {"content": ""}}]}
        with self.assertRaises(ProviderRefusal) as caught:
            MODELFLARE.extract_text(payload)
        self.assertEqual(caught.exception.kind, "blocked")

    def test_an_empty_stop_is_an_empty_answer(self):
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
        with self.assertRaises(ProviderRefusal) as caught:
            MODELFLARE.extract_text(payload)
        self.assertEqual(caught.exception.kind, "empty")

    def test_a_truncated_answer_is_refused_rather_than_half_reported(self):
        payload = {"choices": [{"finish_reason": "length", "message": {"content": "# half a page"}}]}
        with self.assertRaises(ProviderRefusal) as caught:
            MODELFLARE.extract_text(payload)
        self.assertEqual(caught.exception.kind, "truncated")
        self.assertIn("max-output-tokens", caught.exception.detail)

    def test_a_partial_content_filter_answer_is_still_blocked(self):
        payload = {"choices": [{"finish_reason": "content_filter", "message": {"content": "# partial"}}]}
        with self.assertRaises(ProviderRefusal) as caught:
            MODELFLARE.extract_text(payload)
        self.assertEqual(caught.exception.kind, "blocked")

    def test_an_empty_length_answer_is_reported_as_truncated(self):
        payload = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
        with self.assertRaises(ProviderRefusal) as caught:
            MODELFLARE.extract_text(payload)
        self.assertEqual(caught.exception.kind, "truncated")


class ModelflareErrorClassificationTests(unittest.TestCase):
    def test_statuses_are_classified_like_capacity_or_configuration_problems(self):
        for status in (404, 500, 503, 504):
            self.assertEqual(MODELFLARE.status_error_kind(status), "unavailable", status)
        for status in (400, 401, 403):
            self.assertEqual(MODELFLARE.status_error_kind(status), "permanent", status)
        self.assertEqual(MODELFLARE.status_error_kind(429), "rate_limited")

    def test_an_unfunded_key_is_permanent_not_a_rate_limit(self):
        """A 429 with insufficient_quota can never succeed, so it must not retry."""
        payload = {
            "error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}
        }
        self.assertEqual(MODELFLARE.status_error_kind(429, payload), "permanent")

    def test_quota_kind_is_read_from_the_message(self):
        self.assertEqual(
            MODELFLARE.quota_kind({"error": {"message": "Rate limit reached for requests per minute"}}),
            "rpm",
        )
        self.assertEqual(
            MODELFLARE.quota_kind({"error": {"message": "daily request limit exceeded"}}), "rpd"
        )
        self.assertEqual(MODELFLARE.quota_kind({"error": {"message": "boom"}}), "unknown")

    def test_retry_after_comes_from_the_header_or_the_body(self):
        self.assertEqual(MODELFLARE.retry_after({"Retry-After": "7"}, None), 7.0)
        self.assertEqual(MODELFLARE.retry_after({}, {"retry_after": 3}), 3.0)
        self.assertIsNone(MODELFLARE.retry_after({}, {}))


class RefusalTaxonomyTests(unittest.TestCase):
    """Every refusal must land in the retry bucket its cause deserves."""

    def test_a_blocked_page_is_a_content_block(self):
        with self.assertRaises(ContentBlockedError):
            answer_from_payload(GEMINI, {"candidates": [{"finishReason": "SAFETY"}]})

    def test_a_truncated_answer_is_permanent_and_never_retried(self):
        payload = {"choices": [{"finish_reason": "length", "message": {"content": "half"}}]}
        with self.assertRaises(PermanentProviderError):
            answer_from_payload(MODELFLARE, payload)
        self.assertFalse(is_retryable_error(PermanentProviderError("truncated")))

    def test_markdown_fences_are_stripped_for_every_provider(self):
        gemini = {"candidates": [{"content": {"parts": [{"text": "```markdown\n# t\n```"}]}}]}
        self.assertEqual(answer_from_payload(GEMINI, gemini), "# t")

        gateway = {"choices": [{"finish_reason": "stop", "message": {"content": "```md\n# t\n```"}}]}
        self.assertEqual(answer_from_payload(MODELFLARE, gateway), "# t")


class LocalKeyPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_reports_like_the_shared_pool(self):
        pool = await LocalKeyPool.create(["k1", "k2"], rpm_per_key=60, rpd_per_key=10)

        index, key, lease = await pool.acquire()
        self.assertEqual(key, ["k1", "k2"][index])
        self.assertTrue(lease)

        pool.record_http_result(200, 1.5)
        await pool.mark_success(index, lease)

        summary = pool.usage_summary()
        self.assertEqual(summary["performance"]["success"], 1)
        self.assertEqual(summary["keys"][str(index + 1)]["success"], 1)

    async def test_a_daily_exhausted_key_blocks_the_pool(self):
        pool = await LocalKeyPool.create(["only"], rpm_per_key=60, rpd_per_key=10)
        index, _key, lease = await pool.acquire()

        await pool.rate_limited(index, lease, 0.0, daily_exhausted=True, quota_type="rpd")

        with self.assertRaises(QuotaPoolExhaustedError):
            await pool.acquire()

    async def test_it_exposes_the_interface_process_chunks_relies_on(self):
        pool = await LocalKeyPool.create(["k"], rpm_per_key=60, rpd_per_key=10)
        for name in ("acquire", "mark_success", "mark_error", "rate_limited", "record_http_result"):
            self.assertTrue(callable(getattr(pool, name)), name)
        self.assertEqual(await pool.remote_status(), {})
        self.assertEqual(await pool.close(), None)


class ProviderSelectionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_modelflare_runs_without_the_shared_quota_service(self):
        calls: list[str] = []

        async def fake_call(
            _client, _pool, provider, _model, _parts, chunk_name, _thinking, *_rest
        ):
            calls.append(provider.name)
            return f"# page {chunk_name}"

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("pdf2md.make_request_parts", return_value=([{"text": "page"}], 64)),
                patch("pdf2md.call_model", new=fake_call),
                patch("pdf2md.random.uniform", return_value=0.0),
                patch(
                    "pdf2md.ProjectQuotaPool.create",
                    new=AsyncMock(side_effect=AssertionError("the Valkey pool must not be used")),
                ),
            ):
                results = await process_chunks(
                    [Chunk(1, 1, (), "1")],
                    Path(directory),
                    prompt="Transcribe",
                    model="gpt-5.6-sol",
                    concurrency=1,
                    keys=["sk-mf-test"],
                    thinking_level="high",
                    verification_passes=0,
                    media_resolution="ultra_high",
                    rpm_per_key=15,
                    rpd_per_key=500,
                    provider=MODELFLARE,
                )
                markdown = (Path(directory) / "pages" / "1.md").read_text()

        self.assertEqual([result["status"] for result in results], ["ok"])
        self.assertEqual(calls, ["modelflare"])
        self.assertIn("# page 1", markdown)

    async def test_gemini_still_uses_the_shared_pool_by_default(self):
        async def fake_call(*_args, **_kwargs):
            return "# page 1"

        fake_pool = AsyncMock()
        fake_pool.usage_summary = lambda: {"scope": "test"}
        fake_pool.performance_summary = lambda: {
            "success": 1,
            "429": 0,
            "503": 0,
            "other_error": 0,
            "p50_seconds": 1.0,
            "p95_seconds": 1.0,
        }
        fake_pool.record_http_result = lambda *_args: None

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict("os.environ", {"GEMINI_KEY_GROUPS": ""}),
                patch("pdf2md.ProjectQuotaPool.create", new=AsyncMock(return_value=fake_pool)),
                patch("pdf2md.LocalKeyPool.create", new=AsyncMock(side_effect=AssertionError("no"))),
                patch("pdf2md.make_request_parts", return_value=([{"text": "page"}], 64)),
                patch("pdf2md.call_model", new=fake_call),
                patch("pdf2md.random.uniform", return_value=0.0),
            ):
                results = await process_chunks(
                    [Chunk(1, 1, (), "1")],
                    Path(directory),
                    prompt="Transcribe",
                    model=GEMINI_LITE,
                    concurrency=1,
                    keys=["dummy-key"],
                    thinking_level="high",
                    verification_passes=0,
                    media_resolution="ultra_high",
                    rpm_per_key=15,
                    rpd_per_key=500,
                )

        self.assertEqual([result["status"] for result in results], ["ok"])


if __name__ == "__main__":
    unittest.main()
