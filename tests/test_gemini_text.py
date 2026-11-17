import asyncio
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import httpx

from gemini_text import (PocketBase, build_payload, create_key_pool, generate, log_result, parse_answer,
                         recover, run_job, safe_error_code)
from agent_trace import AgentTrace
from key_pool import KeyPool


def job(concurrency=2):
    return {"model": "gemini-test", "prompt": "shared prompt", "systemPrompt": "system instruction",
            "options": {"concurrency": concurrency, "maxOutputTokens": 8192, "thinkingLevel": ""},
            "messages": [{"id": f"{i:015}", "index": i + 1, "inputText": f"segment {i}", "status": "queued"} for i in range(5)]}


class TextPayloadTests(unittest.TestCase):
    def test_complete_reply_is_chunked_redacted_and_cannot_inject_workflow_commands(self):
        rows = []
        log = AgentTrace(job='j' * 15, segment=2, secrets=['fixture-secret'], prefix='[gemini]',
                         sink=lambda line, **_: rows.append(line))
        reply = '中文' * 1998 + 'fixture-secret\n::error::not a command\n' + '末' * 5000
        result = {'reply': reply, 'error': '', 'finishReason': 'STOP',
                  'usage': {'totalTokenCount': 123, 'promptTokenCount': 100,
                            'candidatesTokenCount': 23, 'cachedContentTokenCount': 80}}
        log_result(log, result)
        parsed = [json.loads(line.removeprefix('[gemini] ')) for line in rows]
        self.assertEqual(parsed[0]['total_tokens'], 123)
        self.assertEqual(parsed[0]['cached_tokens'], 80)
        self.assertIsNone(parsed[0]['thought_tokens'])
        self.assertEqual(''.join(row['text'] for row in parsed[1:]), log.redact(reply))
        self.assertEqual([row['part'] for row in parsed[1:]], list(range(1, len(parsed))))
        self.assertTrue(all(len(row['text']) <= 4000 for row in parsed[1:]))
        self.assertTrue(all(row['job'] == 'j' * 15 and row['segment'] == 2 for row in parsed))
        self.assertTrue(all(line.startswith('[gemini] ') and '\n' not in line for line in rows))
        self.assertNotIn('fixture-secret', ''.join(rows))
        self.assertEqual(result['reply'], reply)

    def test_only_text_and_explicit_prompts_no_pdf_rules(self):
        data = build_payload(job(), {"inputText": "# markdown"})
        self.assertEqual(data["contents"], [{"role": "user", "parts": [{"text": "shared prompt"}, {"text": "# markdown"}]}])
        self.assertEqual(data["systemInstruction"], {"parts": [{"text": "system instruction"}]})
        self.assertNotIn("thinkingConfig", data["generationConfig"])
        self.assertNotIn("inlineData", json.dumps(data))

    def test_default_chat_never_sends_google_search_tool(self):
        for model in ('gemini-3.5-flash-lite', 'gemini-4-hydrogen', 'gemini-2.5-pro', 'custom-model'):
            value = job()
            value['model'] = model
            payload = build_payload(value, {'inputText': '最新新闻'})
            self.assertNotIn('tools', payload)
            self.assertEqual(payload['contents'][0]['parts'][-1]['text'], '最新新闻')
            self.assertNotIn('inlineData', json.dumps(payload))

    def test_safe_google_error_code_never_leaks_provider_message(self):
        self.assertEqual(safe_error_code({'error': {'status': 'INVALID_ARGUMENT'}}), 'INVALID_ARGUMENT')
        self.assertEqual(safe_error_code({'error': {'message': 'secret-token'}}), 'UNKNOWN')

    def test_plain_response_ignores_search_grounding_metadata(self):
        data = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'reply'}]},
                                'groundingMetadata': {'webSearchQueries': ['test']}}],
                'usageMetadata': {'totalTokenCount': 32}}
        answer = parse_answer(data)
        self.assertEqual(answer['reply'], 'reply')
        self.assertEqual(answer['usage'], {'totalTokenCount': 32})

    def test_thoughts_are_excluded_and_truncated_text_is_retained(self):
        answer = parse_answer({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"thought": True, "text": "private reasoning"}, {"text": "partial reply"}]}}]})
        self.assertEqual(answer["reply"], "partial reply")
        self.assertTrue(answer["error"])
        self.assertTrue(parse_answer({"promptFeedback": {"blockReason": "SAFETY"}})["error"])
        self.assertTrue(parse_answer({})["error"])


class TextRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_checkpoint_outage_recovers_without_recreating_or_canceling(self):
        from gemini_agent import generate_agent
        state, requests, delays = {}, [], []
        created, failures = False, 0
        async def outbound(request):
            nonlocal created, failures
            requests.append(request)
            if request.url.host == 'pb.example':
                body = json.loads(request.content)
                if created and 'state' not in body and failures < 10:
                    failures += 1
                    return httpx.Response(502)
                state.update(body.get('state') or {})
                return httpx.Response(200, json={'state': state, 'canceled': False})
            if request.method == 'POST':
                self.assertFalse(request.url.path.endswith(':cancel'))
                created = True
                return httpx.Response(200, json={'id': 'agent-existing', 'status': 'in_progress'})
            return httpx.Response(200, json={'id': 'agent-existing', 'status': 'completed', 'output_text': 'finished'})
        async def sleep(seconds): delays.append(seconds)
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            pb = PocketBase(client, 'https://pb.example')
            async def checkpoint(update=None): return await pb.checkpoint('jobs/a/messages/b/checkpoint', {'executionId': '123:1'}, update)
            with patch('gemini_text.asyncio.sleep', new=sleep):
                result = await generate_agent(client, KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100),
                    {'model': 'Antigravity', 'options': {}}, {}, checkpoint=checkpoint, sleep=sleep)
        self.assertEqual(result['reply'], 'finished')
        self.assertEqual(failures, 10)
        self.assertGreater(sum(delays), 90)
        self.assertEqual(sum(r.method == 'POST' and r.url.host != 'pb.example' for r in requests), 1)
        for r in requests:
            if r.url.host == 'pb.example': self.assertEqual(json.loads(r.content)['executionId'], '123:1')

    async def test_agent_checkpoint_rejects_permanent_auth_failure_without_retry(self):
        requests = []
        async def outbound(request): requests.append(request); return httpx.Response(403)
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            pb = PocketBase(client, 'https://pb.example')
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
                await pb.checkpoint('jobs/a/messages/b/checkpoint', {'executionId': '123:1'})
        self.assertEqual(len(requests), 1)

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

    async def test_gemini_chat_does_not_send_search_and_fails_without_automatic_replay(self):
        for model in ('gemini-3.5-flash-lite', 'gemini-4-hydrogen'):
            captured = []
            value = job(1)
            value['model'] = model
            async def success(request):
                captured.append(json.loads(request.content))
                return httpx.Response(200, json={'candidates': [
                    {'finishReason': 'STOP', 'content': {'parts': [{'text': 'Online-free reply'}]}}],
                    'usageMetadata': {'totalTokenCount': 12}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as client:
                result = await generate(client, KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100),
                                        value, value['messages'][0])
            self.assertEqual(result['reply'], 'Online-free reply')
            self.assertEqual(len(captured), 1)
            self.assertNotIn('tools', captured[0])
            self.assertEqual(result['usage']['totalTokenCount'], 12)

        captured = []
        value = job(1)
        value['model'] = 'gemini-3.5-flash-lite'
        async def reject(request):
            captured.append(json.loads(request.content))
            return httpx.Response(400, json={'error': {'status': 'INVALID_ARGUMENT',
                                                      'message': 'fixture-error'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 400.*INVALID_ARGUMENT'):
                await generate(client, KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100),
                               value, value['messages'][0])
        self.assertEqual(len(captured), 1)
        self.assertNotIn('tools', captured[0])

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
        async def answer(*_, **_kwargs): return {"reply": 'unsaved reply', "error": '', "finishReason": 'STOP', "usage": {}}
        logs = StringIO()
        with tempfile.TemporaryDirectory() as directory, patch('gemini_text.generate', side_effect=answer) as model:
            with redirect_stdout(logs):
                result = await run_job(PB(), None, 'a' * 15, '123:1', pool_factory, Path(directory))
            self.assertEqual(result, 2)
            rows = [json.loads(line.removeprefix('[gemini] ')) for line in logs.getvalue().splitlines()]
            events = [row['event'] for row in rows]
            self.assertLess(events.index('reply'), events.index('segment_error'))
            self.assertEqual(next(row for row in rows if row['event'] == 'reply')['text'], 'unsaved reply')
            error = next(row for row in rows if row['event'] == 'segment_error')
            self.assertEqual(error['stage'], 'progress_persistence')
            self.assertEqual(error['error'], 'PB offline')
            self.assertTrue(error['recovery_available'])
            self.assertEqual(model.call_count, 1)
            paths = [path for path in Path(directory).glob('*.json') if not path.name.endswith('.progress.json')]
            self.assertEqual(len(paths), 1)
            self.assertEqual(json.loads(paths[0].read_text())['result']['reply'], 'unsaved reply')
            saved = []
            class RecoveryPB:
                async def post(self, path, data, **kwargs): saved.append((path, data))
            await recover(RecoveryPB(), Path(directory))
            self.assertEqual(saved[0][1]['executionId'], '123:1')
            self.assertEqual(model.call_count, 1)

    async def test_job_summary_sums_each_segment_once_including_failed_reply(self):
        value = job(2)
        value['messages'] = value['messages'][:2]
        async def pool_factory(_): return KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100)
        class PB:
            async def post(self, path, data, **kwargs):
                if path.endswith('/claim'): return value
                if path.endswith('/start'): return {'send': True}
                return {}
        async def answer(_client, _pool, _job, message, **_kwargs):
            failed = message['index'] == 2
            return {'reply': 'partial answer' if failed else 'answer',
                    'error': 'output limit' if failed else '', 'finishReason': 'MAX_TOKENS' if failed else 'STOP',
                    'usage': {'totalTokenCount': 120, 'promptTokenCount': 100,
                              'candidatesTokenCount': 20, 'cachedContentTokenCount': 80}}
        logs = StringIO()
        with tempfile.TemporaryDirectory() as directory, patch('gemini_text.generate', side_effect=answer), redirect_stdout(logs):
            result = await run_job(PB(), None, 'a' * 15, '123:1', pool_factory, Path(directory))
        rows = [json.loads(line.removeprefix('[gemini] ')) for line in logs.getvalue().splitlines()]
        summary = rows[-1]
        self.assertEqual(result, 2)
        self.assertEqual(summary['event'], 'job_summary')
        self.assertEqual((summary['generated'], summary['saved'], summary['failed']), (2, 2, 1))
        self.assertEqual(summary['total_tokens'], 240)
        self.assertEqual(summary['cached_tokens'], 160)
        self.assertEqual(summary['usage_reported_segments'], 2)
        self.assertEqual(len([row for row in rows if row['event'] == 'segment_result']), 2)
        self.assertEqual({row['segment'] for row in rows if row['event'] == 'reply'}, {1, 2})
        self.assertIn('partial answer', logs.getvalue())

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
