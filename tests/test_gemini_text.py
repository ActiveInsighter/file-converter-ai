import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from gemini_text import (PocketBase, build_payload, create_key_pool, generate, parse_answer,
                         recover, run_job, safe_error_code, search_fallback_allowed,
                         supports_google_search)
from key_pool import KeyPool


def job(concurrency=2):
    return {"model": "gemini-test", "prompt": "shared prompt", "systemPrompt": "system instruction",
            "options": {"concurrency": concurrency, "maxOutputTokens": 8192, "thinkingLevel": ""},
            "messages": [{"id": f"{i:015}", "index": i + 1, "inputText": f"segment {i}", "status": "queued"} for i in range(5)]}


class TextPayloadTests(unittest.TestCase):
    def test_only_text_and_explicit_prompts_no_pdf_rules(self):
        data = build_payload(job(), {"inputText": "# markdown"})
        self.assertEqual(data["contents"], [{"role": "user", "parts": [{"text": "shared prompt"}, {"text": "# markdown"}]}])
        self.assertEqual(data["systemInstruction"], {"parts": [{"text": "system instruction"}]})
        self.assertNotIn("thinkingConfig", data["generationConfig"])
        self.assertNotIn("inlineData", json.dumps(data))

    def test_default_google_search_for_supported_chat_models_only(self):
        for model in ('gemini-3.5-flash-lite', 'gemini-3.8-flash', 'gemini-3.1-pro-preview',
                      'gemini-2.5-pro', 'gemini-2.0-flash', 'gemini-4-hydrogen',
                      'gemini-4-argon', 'gemini-4-carbon', 'gemini-3.1-flash-lite'):
            value = job()
            value['model'] = model
            self.assertTrue(supports_google_search(model), model)
            payload = build_payload(value, {'inputText': '今天有什么新闻？'})
            self.assertEqual(payload['tools'], [{'googleSearch': {}}])
            self.assertEqual(payload['contents'][0]['parts'][0]['text'], 'shared prompt')
            self.assertNotIn('inlineData', json.dumps(payload))
        for model in ('gemini-test', 'my-custom-model', 'gemini-1.5-pro',
                      'gemini-3.1-flash-image-preview', 'gemini-3.5-live-audio'):
            value = job()
            value['model'] = model
            self.assertFalse(supports_google_search(model), model)
            self.assertNotIn('tools', build_payload(value, {'inputText': 'test'}))

    def test_search_rejection_classifier_never_retries_auth_or_possible_paid_loops(self):
        self.assertEqual(safe_error_code({'error': {'status': 'INVALID_ARGUMENT'}}), 'INVALID_ARGUMENT')
        self.assertEqual(safe_error_code({'error': {'message': 'secret-token'}}), 'UNKNOWN')
        self.assertTrue(search_fallback_allowed(400, {'error': {'status': 'INVALID_ARGUMENT',
                          'message': 'googleSearch not available for model'}}, 0.5))
        self.assertTrue(search_fallback_allowed(400, {'error': {'status': 'INVALID_ARGUMENT',
                          'message': 'Unsupported input argument'}}, 0.5))
        self.assertTrue(search_fallback_allowed(403, {'error': {'status': 'PERMISSION_DENIED',
                          'message': 'Google Search grounding not enabled'}}, 1))
        self.assertFalse(search_fallback_allowed(401, {'error': {'message': 'google search'}}, 1))
        self.assertFalse(search_fallback_allowed(429, {'error': {'message': 'google search'}}, 1))
        self.assertFalse(search_fallback_allowed(400, {'error': {'status': 'INVALID_ARGUMENT'}}, 30))
        self.assertFalse(search_fallback_allowed(400, {'error': {'message': 'too many tool calls'}}, 1))
        self.assertFalse(search_fallback_allowed(400, {
            'error': {'status': 'INVALID_ARGUMENT'}, 'usageMetadata': {'totalTokenCount': 4000}}, 1))

    def test_grounding_response_includes_google_sources_and_search_suggestions(self):
        blob = base64.b64encode(json.dumps([
            ['UEFA EURO winners', 'https://www.google.com/search?q=UEFA+EURO+winners'],
            ['Invalid', 'javascript:alert(1)'],
        ]).encode()).decode()
        response = {'candidates': [{
            'finishReason': 'STOP',
            'content': {'parts': [{'text': 'Spain won.'}]},
            'groundingMetadata': {
                'groundingChunks': [
                    {'web': {'title': 'UEFA', 'uri': 'https://www.uefa.com/euro2024'}},
                    {'web': {'title': 'UEFA duplicate', 'uri': 'https://www.uefa.com/euro2024'}},
                    {'web': {'title': 'Malicious', 'uri': 'javascript:alert(1)'}},
                ],
                'searchEntryPoint': {'sdkBlob': blob},
            },
        }], 'usageMetadata': {'totalTokenCount': 46}}
        parsed = parse_answer(response)
        self.assertEqual(parsed['usage']['totalTokenCount'], 46)
        self.assertEqual(parsed['error'], '')
        self.assertIn('Spain won.\n\n**Google 搜索参考来源**', parsed['reply'])
        self.assertIn('[UEFA](https://www.uefa.com/euro2024)', parsed['reply'])
        self.assertIn('[UEFA EURO winners](https://www.google.com/search?q=UEFA+EURO+winners)', parsed['reply'])
        self.assertEqual(parsed['reply'].count('https://www.uefa.com/euro2024'), 1)
        self.assertNotIn('javascript:', parsed['reply'])

    def test_google_rendered_suggestions_fallback_has_safe_links(self):
        answer = parse_answer({'candidates': [{
            'finishReason': 'STOP',
            'content': {'parts': [{'text': 'Result'}]},
            'groundingMetadata': {
                'webSearchQueries': ['latest AI model'],
                'searchEntryPoint': {
                    'renderedContent': '<style>.x{display:block}</style>'
                                       '<a href="https://www.google.com/search?q=latest">搜索最新模型</a>'
                                       '<a href="javascript:alert(1)">Danger</a>',
                },
            },
        }]})
        self.assertIn('**Google 搜索建议**', answer['reply'])
        self.assertIn('[搜索最新模型](https://www.google.com/search?q=latest)', answer['reply'])
        self.assertNotIn('javascript:', answer['reply'])
        self.assertEqual(answer['usage']['googleSearchCount'], 1)

    def test_thoughts_are_excluded_and_truncated_text_is_retained(self):
        answer = parse_answer({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"thought": True, "text": "private reasoning"}, {"text": "partial reply"}]}}]})
        self.assertEqual(answer["reply"], "partial reply")
        self.assertTrue(answer["error"])
        self.assertTrue(parse_answer({"promptFeedback": {"blockReason": "SAFETY"}})["error"])
        self.assertTrue(parse_answer({})["error"])


class TextRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_executor_startup_accepts_empty_and_shared_project_groups(self):
        options = {"rpmPerKey": 15, "rpdPerKey": 500}
        default = await create_key_pool(["fixture-a", "fixture-b"], options)
        self.assertEqual(len(default.buckets), 2)
        grouped = await create_key_pool(["fixture-a", "fixture-b"], options, "project-one\nproject-one")
        self.assertEqual(len(grouped.buckets), 1)
        self.assertIs(grouped.bucket_of[0], grouped.bucket_of[1])
        self.assertEqual(grouped.rpd_per_key, 500)

    async def test_repository_server_token_takes_precedence_and_stays_in_authorization(self):
        seen = []
        async def outbound(request):
            seen.append(request)
            return httpx.Response(200, json={"id": "claimed"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            pb = PocketBase(client, "https://pb.example")
            await pb.authenticate(token=" fixture-server-token ", email="unused", password="unused")
            await pb.post("jobs/aaaaaaaaaaaaaaa/claim", {"executionId": "123:1"})
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0].headers["Authorization"], "fixture-server-token")
            self.assertNotIn("fixture-server-token", seen[0].content.decode())
            with self.assertRaisesRegex(RuntimeError, 'POCKETBASE_SERVER_TOKEN'):
                await PocketBase(client, "https://pb.example").authenticate()

    async def test_bounded_parallelism_and_persistence_retry_does_not_generate_again(self):
        active = peak = requests = writes = 0
        value = job()

        async def outbound(request):
            nonlocal active, peak, requests, writes
            if request.url.host == "pb.example":
                writes += 1
                return httpx.Response(503 if writes <= 2 else 200, json={"id": "saved"})
            requests += 1
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "reply"}]}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            writer = PocketBase(client, "https://pb.example")
            class PB:
                async def post(self, path, data, **kwargs):
                    if path.endswith('/claim'):
                        return value
                    if path.endswith('/start'):
                        return {"send": True}
                    return await writer.post(path, data, **kwargs)

            async def pool_factory(_):
                return KeyPool(["fixture-key"] * 10, rpm_per_key=1e6, rpd_per_key=100)

            with tempfile.TemporaryDirectory() as directory:
                result = await run_job(PB(), client, 'a' * 15, '123:1', pool_factory, Path(directory))
                self.assertEqual(result, 0)
                self.assertEqual(list(Path(directory).glob('*.json')), [])
        self.assertEqual(requests, 5)
        self.assertEqual(peak, 2)
        self.assertGreater(writes, requests)

    async def test_supported_chat_sends_google_search_with_generate_content(self):
        captured = []
        value = job(1)
        value['model'] = 'gemini-3.5-flash-lite'
        async def outbound(request):
            captured.append(request)
            return httpx.Response(200, json={
                'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'Current news'}]}}],
                'usageMetadata': {'totalTokenCount': 12},
            })
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            result = await generate(client, KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100),
                                    value, value['messages'][0])
        self.assertEqual(result['reply'], 'Current news')
        self.assertEqual(result['usage']['totalTokenCount'], 12)
        self.assertEqual(len(captured), 1)
        self.assertEqual(json.loads(captured[0].content)['tools'], [{'googleSearch': {}}])

    async def test_fast_google_search_400_falls_back_once_and_returns_answer(self):
        requests = []
        value = job(1)
        value['model'] = 'gemini-3.5-flash-lite'
        async def outbound(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(400, json={'error': {
                    'status': 'INVALID_ARGUMENT', 'message': 'Google Search grounding is not enabled'}})
            return httpx.Response(200, json={
                'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'Offline reply'}]}}],
                'usageMetadata': {'totalTokenCount': 12}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            result = await generate(client, KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100),
                                    value, value['messages'][0])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]['tools'], [{'googleSearch': {}}])
        self.assertNotIn('tools', requests[1])
        self.assertIn('不联网回答', result['reply'])
        self.assertIn('Offline reply', result['reply'])
        self.assertEqual(result['usage']['totalTokenCount'], 12)
        self.assertTrue(result['usage']['googleSearchFallback'])
        self.assertEqual(result['usage']['googleSearchErrorCode'], 'INVALID_ARGUMENT')

    async def test_hydrogen_sends_search_and_preserves_non_search_http_error(self):
        requests = []
        value = job(1)
        value['model'] = 'gemini-4-hydrogen'
        async def outbound(request):
            requests.append(json.loads(request.content))
            return httpx.Response(403, json={'error': {
                'status': 'PERMISSION_DENIED', 'message': 'fixture-error-secret'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403.*PERMISSION_DENIED'):
                await generate(client, KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100),
                               value, value['messages'][0])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['tools'], [{'googleSearch': {}}])

    async def test_database_outage_keeps_recovery_artifact_and_no_model_resend(self):
        value = job(1)
        value['messages'] = value['messages'][:1]
        async def pool_factory(_):
            return KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100)
        class PB:
            async def post(self, path, data, **kwargs):
                if path.endswith('/claim'): return value
                if path.endswith('/start'): return {"send": True}
                raise RuntimeError('PB offline')
        async def answer(*_): return {"reply": 'unsaved reply', "error": '', "finishReason": 'STOP', "usage": {}}
        with tempfile.TemporaryDirectory() as directory, patch('gemini_text.generate', side_effect=answer) as model:
            result = await run_job(PB(), None, 'a' * 15, '123:1', pool_factory, Path(directory))
            self.assertEqual(result, 2)
            self.assertEqual(model.call_count, 1)
            paths = list(Path(directory).glob('*.json'))
            self.assertEqual(len(paths), 1)
            self.assertEqual(json.loads(paths[0].read_text())['result']['reply'], 'unsaved reply')
            saved = []
            class RecoveryPB:
                async def post(self, path, data, **kwargs): saved.append((path, data))
            await recover(RecoveryPB(), Path(directory))
            self.assertEqual(saved[0][1]['executionId'], '123:1')
            self.assertEqual(model.call_count, 1)

    async def test_cancel_or_ambiguous_permit_never_sends(self):
        value = job()
        async def pool_factory(_): return KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100)
        class PB:
            async def post(self, path, data, **kwargs):
                return value if path.endswith('/claim') else {"send": False, "canceled": True}
        with tempfile.TemporaryDirectory() as directory, patch('gemini_text.generate') as model:
            await run_job(PB(), None, 'a' * 15, '123:1', pool_factory, Path(directory))
            model.assert_not_called()

    async def test_transport_timeout_is_not_retried(self):
        calls = 0
        async def outbound(request):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout('timed out')
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            with self.assertRaisesRegex(RuntimeError, '未自动补发'):
                await generate(client, KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100), job(), job()['messages'][0])
        self.assertEqual(calls, 1)


if __name__ == '__main__':
    unittest.main()
