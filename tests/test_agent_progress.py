import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from agent_progress import MessageProgress, MAX_CAPTURE_BYTES, observe_progress
from gemini_agent import generate_agent
from gemini_text import generate
from key_pool import KeyPool


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_compacted_terminal_snapshot_preserves_streamed_step_identity(self):
        writes = []
        async def post(body):
            writes.extend(body['parts'])
            return {'accepted': True}

        progress = MessageProgress(post=post)
        progress.consume({'index': 1, 'step': {'type': 'text', 'text': 'written reply'}}, 'step.stop')
        await progress.flush()

        progress.snapshot({'status': 'completed', 'steps': [
            {'type': 'text', 'text': 'written reply'},
            {'type': 'function_call', 'name': 'tool from a shifted position'},
        ]})
        await progress.flush()

        self.assertEqual(progress.parts['step:1']['type'], 'text')
        self.assertEqual(progress.parts['step:1']['sequence'], 2)
        self.assertNotIn('step:0', progress.parts)
        self.assertEqual([part['type'] for part in writes if part['key'] == 'step:1'], ['text'])

    async def test_quiet_agent_sends_heartbeat_without_rewriting_content(self):
        now, writes = [0.0], []
        async def post(body): writes.append((now[0], json.loads(json.dumps(body)))); return {}
        async def sleep(seconds):
            now[0] += seconds
            if now[0] >= 40: raise asyncio.CancelledError()
        p = MessageProgress(post=post, clock=lambda: now[0])
        p.put('agent-status', 'status', 'in_progress', status='running')
        with patch('agent_progress.asyncio.sleep', new=sleep):
            with self.assertRaises(asyncio.CancelledError): await p.pump()
        self.assertEqual([at for at, _ in writes], [5, 35])
        self.assertEqual([body['sequence'] for _, body in writes], [1, 2])
        self.assertEqual(writes[1][1]['parts'], [])

    async def test_long_agent_reconnects_past_six_streams_and_keeps_cursor(self):
        p = MessageProgress()
        requests, delays = [], []
        async def outbound(request):
            requests.append(request)
            number = len(requests)
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.params.get('last_event_id'), f'event-{number - 1}' if number > 1 else None)
            data = {'index': number - 1, 'step': {'type': 'function_call', 'name': f'tool-{number}'}}
            kind = 'step.start'
            if number == 9:
                # Providers can put the event name in JSON rather than SSE's event field.
                data = {'event_type': 'interaction.completed', 'interaction': {'status': 'completed'}}
                kind = ''
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                text=f'id: event-{number}\nevent: {kind}\ndata: {json.dumps(data)}\n\n')
        async def sleep(delay): delays.append(delay)
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            await observe_progress(client, 'https://fixture.test/agent', 'fixture-key', p, sleep=sleep)
        self.assertEqual(len(requests), 9)
        self.assertEqual(len(delays), 8)
        self.assertIn('step:7', p.parts)
        self.assertEqual(p.usage['agentStepCount'], 8)
        p.snapshot({'status': 'in_progress'})
        self.assertEqual(p.usage['agentStepCount'], 8)

    async def test_stream_retries_transient_failures_before_first_event(self):
        p = MessageProgress()
        attempts, delays = 0, []
        async def outbound(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1: raise httpx.ConnectError('fixture-key must not be logged')
            if attempts < 9: return httpx.Response(503)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                text='data: {"event_type":"interaction.completed","interaction":{"status":"completed"}}\n\n')
        async def sleep(delay): delays.append(delay)
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            await observe_progress(client, 'https://fixture.test/agent', 'fixture-key', p, sleep=sleep)
        self.assertEqual(attempts, 9)
        self.assertEqual(delays, [2, 4, 8, 16, 30, 30, 30, 30])

    async def test_stream_stops_on_permanent_rejection(self):
        for response in [httpx.Response(403), httpx.Response(200, json={}),
                         httpx.Response(400, json={'error': {'code': 'invalid_request', 'message': 'bad parameters'}})]:
            requests = []
            async def outbound(request): requests.append(request); return response
            async def sleep(_): self.fail('Unsupported streams must not loop')
            async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
                await observe_progress(client, 'https://fixture.test/agent', 'fixture-key', MessageProgress(), sleep=sleep)
            self.assertEqual(len(requests), 1)

    async def test_cursorless_replay_survives_precondition_and_saves_each_step_once(self):
        writes, requests, delay = [], [], []
        async def post(body): writes.append(json.loads(json.dumps(body))); return {'accepted': True}
        p = MessageProgress(post=post)
        def frame(kind, **data): return 'data: ' + json.dumps({'event_type': kind, **data}) + '\n\n'
        def step(index, text):
            return (frame('step.start', index=index, step={'type': 'model_output'})
                    + frame('step.delta', index=index, delta={'type': 'text', 'text': text})
                    + frame('step.stop', index=index))
        async def outbound(request):
            requests.append(request)
            self.assertNotIn('last_event_id', request.url.params)
            if len(requests) == 2:
                return httpx.Response(400, json={'error': {'code': 'invalid_request', 'message': 'Precondition check failed.'}})
            body = step(0, 'hello')
            if len(requests) == 3:
                body += step(1, 'world') + frame('interaction.completed', interaction={'status': 'completed'})
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=body)
        async def sleep(seconds): delay.append(seconds)
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            await observe_progress(client, 'https://fixture/agent', 'fixture-key', p, sleep=sleep)
        self.assertEqual(len(requests), 3)
        self.assertEqual([p.parts[f'step:{index}']['content'] for index in [0, 1]], ['hello', 'world'])
        saved = [part for batch in writes for part in batch['parts'] if part['key'] == 'step:0' and part['status'] == 'completed']
        self.assertEqual(len(saved), 1, 'replaying an identical completed step must not rewrite it')
        self.assertEqual([len(batch['parts']) for batch in writes], [1, 1])

    async def test_step_stop_is_acked_before_reading_the_next_event(self):
        writes, attempt = [], 0
        async def post(body):
            nonlocal attempt
            attempt += 1
            if attempt == 1: raise RuntimeError('database temporarily unavailable')
            writes.append(json.loads(json.dumps(body))); return {'accepted': True}
        p = MessageProgress(post=post)
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"event_type":"step.start","index":0,"step":{"type":"text","text":"first"}}\n\n'
                yield b'data: {"event_type":"step.stop","index":0}\n\n'
                self_outer.assertEqual(len(writes), 1, 'next event must wait for the first step database ACK')
                yield b'data: {"event_type":"interaction.completed","interaction":{"status":"completed"}}\n\n'
        self_outer = self
        async def outbound(request): return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream())
        async def sleep(_): pass
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            await observe_progress(client, 'https://fixture/agent', 'fixture-key', p, sleep=sleep)
        self.assertEqual(attempt, 2)
        self.assertEqual(writes[0]['parts'][0]['content'], 'first')

    async def test_closed_job_keeps_unacknowledged_step_in_spool(self):
        async def post(body): return {'closed': True, 'accepted': False}
        with tempfile.TemporaryDirectory() as directory:
            p = MessageProgress(post=post, target=Path(directory) / 'progress.json')
            p.put('step:0', 'text', 'unsaved')
            await p.persist_step(0)
            self.assertTrue(p.closed)
            self.assertIn('step:0', p.dirty)
            self.assertEqual(json.loads(p.target.read_text())['pending']['parts'][0]['content'], 'unsaved')

    async def test_stream_reconnect_backoff_can_be_canceled(self):
        entered = asyncio.Event()
        async def outbound(request): return httpx.Response(503)
        async def sleep(_): entered.set(); await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            observer = asyncio.create_task(observe_progress(client, 'https://fixture.test/agent', 'fixture-key', MessageProgress(), sleep=sleep))
            await entered.wait()
            observer.cancel()
            with self.assertRaises(asyncio.CancelledError): await observer

    def test_snapshot_retains_only_public_summaries_and_redacts_tools(self):
        progress = MessageProgress(secrets=['fixture-api-key'])
        source = {'status': 'in_progress', 'steps': [
            {'type': 'thought', 'content': [{'type': 'text', 'text': 'hidden raw thought'}],
             'signature': 'hidden signature', 'summary': [{'type': 'text', 'text': '检查现有实现'}]},
            {'type': 'code_execution_call', 'call_id': 'call-1', 'code': 'echo fixture-api-key', 'signature': 'secret'},
            {'type': 'code_execution_result', 'call_id': 'call-1', 'result': {'authorization': 'private', 'stdout': 'ok'}},
            {'type': 'model_output', 'content': [{'type': 'text', 'text': 'done'}]},
        ], 'usage': {'total_tokens': 100, 'total_cached_tokens': 80}}
        progress.snapshot(source)
        progress.snapshot({**source, 'usage': {'total_tokens': 150}})
        output = json.dumps(progress.parts, ensure_ascii=False)
        self.assertIn('检查现有实现', output)
        for private in ('hidden', 'fixture-api-key', 'signature', 'authorization', 'private'):
            self.assertNotIn(private, output)
        self.assertEqual(len(progress.parts), 5)
        self.assertEqual(progress.usage['totalTokenCount'], 150)
        self.assertEqual(progress.usage['cachedContentTokenCount'], 80)
        self.assertEqual(progress.parts['step:1']['callId'], progress.parts['step:2']['callId'])

    def test_duplicate_sse_deltas_are_not_appended_twice(self):
        p = MessageProgress()
        p.consume({'index': 1, 'step': {'type': 'thought'}}, 'step.start', 'a')
        delta = {'index': 1, 'delta': {'type': 'thought_summary', 'content': {'type': 'text', 'text': 'summary'}}}
        p.consume(delta, 'step.delta', 'b'); p.consume(delta, 'step.delta', 'b')
        p.consume({'index': 1}, 'step.stop', 'c')
        self.assertEqual(p.parts['step:1']['content'], 'summary')
        self.assertEqual(p.parts['step:1']['status'], 'completed')

    def test_late_summary_keeps_provider_step_order(self):
        p = MessageProgress()
        p.snapshot({'status': 'in_progress', 'steps': [{'type': 'thought'}, {'type': 'function_call', 'name': 'search'}]})
        p.consume({'index': 0, 'step': {'type': 'thought'}}, 'step.start', 'first')
        p.consume({'index': 0, 'delta': {'type': 'thought_summary', 'content': {'type': 'text', 'text': 'plan'}}}, 'step.delta', 'second')
        ordered = sorted(p.parts.values(), key=lambda part: part['sequence'])
        self.assertEqual([part['type'] for part in ordered], ['reasoning_summary', 'tool_call', 'status'])

    async def test_lost_ack_retries_exact_batch_and_keeps_newer_local_content(self):
        sent = []
        async def post(body):
            sent.append(json.loads(json.dumps(body)))
            if len(sent) == 1: raise RuntimeError('lost ack')
            return {'accepted': len(sent) != 2}
        with tempfile.TemporaryDirectory() as directory:
            p = MessageProgress(post=post, target=Path(directory) / 'progress.json')
            p.put('call', 'tool_call', 'initial', status='running')
            with self.assertRaises(RuntimeError): await p.flush()
            self.assertTrue(p.target.exists())
            p.put('call', 'tool_call', 'changed', status='completed')
            await p.flush()
            self.assertEqual(sent[0], sent[1])
            self.assertEqual([b['sequence'] for b in sent], [1, 1, 2])
            self.assertEqual(sent[2]['parts'][0]['content'], 'changed')
            self.assertFalse(p.dirty)
            self.assertEqual(json.loads(p.target.read_text())['sequence'], 2)

    async def test_batches_are_bounded_and_order_is_stable(self):
        batches = []
        async def post(body): batches.append(body); return {}
        p = MessageProgress(post=post)
        for i in range(12): p.put(f'tool:{i}', 'tool_result', '中' * 30000)
        await p.flush()
        self.assertTrue(all(len(json.dumps(b, ensure_ascii=False).encode()) <= 256 * 1024 for b in batches))
        self.assertEqual([x['sequence'] for b in batches for x in b['parts']], list(range(1, 13)))
        self.assertTrue(all(x['truncated'] for b in batches for x in b['parts']))
        p.put('tool:0', 'tool_result', 'changed')
        self.assertEqual(p.parts['tool:0']['sequence'], 1)
        self.assertLessEqual(sum(len(x['content'].encode()) for x in p.parts.values()), MAX_CAPTURE_BYTES)

    async def test_agent_uploads_before_terminal_and_creates_once(self):
        snapshots, writes, creates = 0, [], 0
        p = MessageProgress(post=lambda _: None)
        async def upload(body): writes.append(body); return {}
        p.post = upload
        state = {}
        async def checkpoint(update=None):
            if update: state.update(update)
            return {'state': state, 'canceled': False}
        async def outbound(request):
            nonlocal creates, snapshots
            if request.method == 'POST':
                creates += 1
                return httpx.Response(200, json={'id': 'agent-1', 'status': 'in_progress'})
            if request.url.params.get('stream'):
                return httpx.Response(200, json={})  # Snapshot fallback when SSE is unavailable.
            snapshots += 1
            return httpx.Response(200, json={'id': 'agent-1', 'status': 'completed' if snapshots == 2 else 'in_progress',
                'steps': [{'type': 'code_execution_call', 'code': 'npm test'}],
                'output_text': 'answer' if snapshots == 2 else '', 'usage': {'total_tokens': snapshots * 100}})
        async def sleep(_):
            await p.flush()
            await asyncio.sleep(0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            result = await generate_agent(client, KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100),
                {'model': 'Antigravity', 'options': {}}, {}, checkpoint=checkpoint, sleep=sleep, progress=p)
        self.assertEqual(creates, 1)
        self.assertTrue(any(any(part['type'] == 'tool_call' for part in batch['parts']) for batch in writes))
        self.assertEqual(result['reply'], 'answer')
        self.assertEqual(p.usage['totalTokenCount'], 200)

    async def test_ordinary_chat_streams_text_and_usage_through_same_parts(self):
        p = MessageProgress()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"candidates":[{"content":{"parts":[{"text":"hello "}]}}]}\n\n'
                self_outer.assertEqual(p.parts['answer']['content'], 'hello ')
                yield b'data: {"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"world"}]}}],"usageMetadata":{"totalTokenCount":10}}\n\n'
        self_outer = self
        async def outbound(request):
            self.assertIn(':streamGenerateContent', str(request.url))
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=Stream())
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            result = await generate(client, KeyPool(['key'], rpm_per_key=1e6, rpd_per_key=100),
                {'model': 'gemini-test', 'options': {'maxOutputTokens': 8192}}, {'inputText': 'hi'}, progress=p)
        self.assertEqual(result['reply'], 'hello world')
        self.assertEqual(result['response']['parts'][0]['text'], 'hello world')
        self.assertEqual(result['response']['metadata']['usageMetadata']['totalTokenCount'], 10)
        self.assertEqual(p.parts['answer']['status'], 'completed')
        self.assertEqual(p.usage['totalTokenCount'], 10)
