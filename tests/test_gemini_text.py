import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from gemini_text import PocketBase, build_payload, generate, parse_answer, recover, run_job
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

    def test_thoughts_are_excluded_and_truncated_text_is_retained(self):
        answer = parse_answer({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"thought": True, "text": "private reasoning"}, {"text": "partial reply"}]}}]})
        self.assertEqual(answer["reply"], "partial reply")
        self.assertTrue(answer["error"])
        self.assertTrue(parse_answer({"promptFeedback": {"blockReason": "SAFETY"}})["error"])
        self.assertTrue(parse_answer({})["error"])


class TextRunnerTests(unittest.IsolatedAsyncioTestCase):
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
