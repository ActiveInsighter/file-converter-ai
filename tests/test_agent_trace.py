import asyncio
import hashlib
import json
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock

import httpx

from agent_diagnostics import diagnose
from agent_trace import AgentTrace, MAX_EVENT_BYTES, observe_stream, sse_events


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts

    async def __aiter__(self):
        for part in self.parts:
            yield part


def event(kind, **fields):
    return ('data: ' + json.dumps({'event_type': kind, **fields}, ensure_ascii=False) + '\n\n').encode()


class TraceTests(unittest.TestCase):
    def make_trace(self):
        rows = []
        now = [10.0]
        def sink(line, **_):
            rows.append(json.loads(line.removeprefix('[agent.trace] ')))
        trace = AgentTrace(job='j' * 15, segment=2, secrets=['fixture-secret'], clock=lambda: now[0], sink=sink)
        return trace, rows, now

    def test_tool_timings_split_secrets_summary_only_and_bounded_content(self):
        trace, rows, now = self.make_trace()
        trace.consume({'event_type': 'step.start', 'index': 1, 'step': {'type': 'thought'}})
        trace.consume({'event_type': 'step.delta', 'index': 1, 'delta': {'type': 'text', 'text': 'raw-thought-forbidden'}})
        trace.consume({'event_type': 'step.delta', 'index': 1, 'delta': {'type': 'thought_signature', 'signature': 'encrypted-forbidden'}})
        for text in ['Public summary fixture-', 'secret']:
            trace.consume({'event_type': 'step.delta', 'index': 1,
                           'delta': {'type': 'thought_summary', 'content': {'type': 'text', 'text': text}}})
        trace.consume({'event_type': 'step.stop', 'index': 1})
        trace.consume({'event_type': 'step.start', 'index': 2,
                       'step': {'type': 'code_execution_call', 'name': 'bash', 'code': 'npm run build'}})
        for text in ['fixture-', 'secret', 'X' * 20000]:
            trace.consume({'event_type': 'step.delta', 'index': 2, 'delta': {'type': 'arguments_delta', 'arguments': text}})
        now[0] = 52.5
        trace.consume({'event_type': 'step.stop', 'index': 2})
        text = json.dumps(rows)
        self.assertNotIn('fixture-secret', text)
        self.assertNotIn('raw-thought-forbidden', text)
        self.assertNotIn('encrypted-forbidden', text)
        self.assertIn('Public summary', text)
        stop = next(r for r in rows if r['event'] == 'step_stop' and r['index'] == 2)
        self.assertEqual(stop['observed_seconds'], 42.5)
        self.assertTrue(stop['tool_payload']['truncated'])
        self.assertLess(len(stop['tool_payload']['preview']), 1801)
        self.assertTrue(all(r['job'] == 'j' * 15 and r['segment'] == 2 for r in rows))

    def test_snapshot_never_dumps_raw_thought_and_redacts_unknown_tokens(self):
        trace, rows, _ = self.make_trace()
        trace.snapshot({'status': 'completed', 'steps': [
            {'type': 'thought', 'content': [{'text': 'private-reasoning'}], 'signature': 'private-signature'},
            {'type': 'mcp_server_tool_result', 'name': 'actions_list',
             'result': {'authorization': 'Bearer unknown-token', 'password': 'other-password',
                        'url': 'https://user:credential@host/path?access_token=abcdef', 'message': 'fixture-secret'}},
        ]})
        text = json.dumps(rows)
        for secret in ['private-reasoning', 'private-signature', 'unknown-token', 'other-password',
                       'credential@', 'abcdef', 'fixture-secret']:
            self.assertNotIn(secret, text)
        self.assertIn('actions_list', text)

    def test_replayed_event_ids_are_deduplicated(self):
        trace, rows, _ = self.make_trace()
        item = {'event_type': 'step.start', 'event_id': 'e1', 'index': 0, 'step': {'type': 'thought'}}
        trace.consume(item); trace.consume(item)
        self.assertEqual(len(rows), 1)
        self.assertEqual(trace.last_event_id, 'e1')

    def test_tool_duration_includes_gap_between_call_and_result(self):
        trace, rows, now = self.make_trace()
        trace.consume({'event_type': 'step.start', 'index': 2,
                       'step': {'type': 'mcp_server_tool_call', 'id': 'call1', 'name': 'actions_list'}})
        trace.consume({'event_type': 'step.stop', 'index': 2})
        now[0] = 70
        self.assertEqual(trace.pending()[0]['observed_seconds'], 60)
        trace.consume({'event_type': 'step.start', 'index': 3,
                       'step': {'type': 'mcp_server_tool_result', 'call_id': 'call1'}})
        trace.consume({'event_type': 'step.stop', 'index': 3})
        finished = next(r for r in rows if r['event'] == 'tool_finished')
        self.assertEqual(finished['observed_seconds'], 60)
        self.assertEqual(finished['association'], 'call_id')
        self.assertEqual(trace.pending(), [])

    def test_structured_tool_argument_delta_is_captured(self):
        trace, rows, _ = self.make_trace()
        trace.consume({'event_type': 'step.start', 'index': 0, 'step': {'type': 'code_execution_call'}})
        trace.consume({'event_type': 'step.delta', 'index': 0,
                       'delta': {'type': 'arguments_delta', 'arguments': {'language': 'bash', 'code': 'npm run build'}}})
        trace.consume({'event_type': 'step.stop', 'index': 0})
        self.assertIn('npm run build', json.dumps(rows))

    def test_nested_json_tool_results_redact_unconfigured_credentials(self):
        trace, _, _ = self.make_trace()
        value = {'result': json.dumps({'content': [{'text': json.dumps({'password': 'unconfigured-nested-value',
                                                                     'answer': 'public-output'})}]})}
        preview = trace.preview(value)
        self.assertNotIn('unconfigured-nested-value', preview['preview'])
        self.assertIn('public-output', preview['preview'])


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_uses_only_non_streaming_snapshots(self):
        from gemini_agent import generate_agent
        from key_pool import KeyPool
        requests, state = [], {}
        async def checkpoint(update=None):
            if update:
                state.update(update)
            return {'state': state, 'canceled': False}
        async def outbound(request):
            requests.append((request.method, request.url.path))
            if request.method == 'POST':
                return httpx.Response(200, json={'id': 'saved', 'status': 'in_progress'})
            return httpx.Response(200, json={'id': 'saved', 'status': 'completed', 'output_text': 'answer'})
        async def sleep(_):
            await asyncio.sleep(0.001)
        with patch('agent_trace.watch_interaction', new_callable=AsyncMock) as observer:
            async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
                result = await generate_agent(client, KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100),
                    {'model': 'Antigravity', 'prompt': 'hi', 'options': {}}, {'index': 1}, checkpoint=checkpoint, sleep=sleep)
        self.assertEqual(result['reply'], 'answer')
        observer.assert_not_awaited()
        self.assertEqual(requests, [('POST', '/v1beta/interactions'), ('GET', '/v1beta/interactions/saved')])

    async def test_workflow_trace_prefix_routes_without_claiming_job(self):
        from gemini_text import main
        with patch.dict('os.environ', {'REQUEST_ID': 'trace-' + 'j' * 15}), \
             patch('sys.argv', ['gemini_text.py']), \
             patch('agent_diagnostics.main', new_callable=AsyncMock, return_value=0) as diagnostic, \
             patch('gemini_text.run_job', new_callable=AsyncMock) as run_job:
            self.assertEqual(await main(), 0)
            diagnostic.assert_awaited_once_with('j' * 15)
            run_job.assert_not_awaited()

    async def test_utf8_crlf_multiline_comments_and_done(self):
        body = ': heartbeat\r\nid: e1\r\nevent: step.start\r\ndata: {"event_type":"step.start",\r\ndata: "index":0,"step":{"type":"thought","name":"中文"}}\r\n\r\ndata: [DONE]\n\n'.encode()
        response = httpx.Response(200, stream=BytesStream([bytes([b]) for b in body]))
        rows = [r async for r in sse_events(response)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0]['step']['name'], '中文')
        self.assertEqual(rows[0][2], 'e1')

    async def test_oversized_event_is_rejected_before_json_decode(self):
        response = httpx.Response(200, stream=BytesStream([b'data: ' + b'x' * MAX_EVENT_BYTES]))
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            _ = [r async for r in sse_events(response)]

    async def test_reconnect_uses_event_id_and_never_posts(self):
        requests, rows = [], []
        trace = AgentTrace(secrets=['fixture-key'], sink=lambda line, **_: rows.append(line))
        async def outbound(request):
            requests.append(request)
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.params['stream'], 'true')
            if len(requests) == 1:
                body = event('step.start', event_id='e1', index=0, step={'type': 'code_execution_call', 'code': 'npm test'})
            else:
                self.assertEqual(request.url.params['last_event_id'], 'e1')
                body = event('step.stop', event_id='e2', index=0) + event('interaction.completed', interaction={'status': 'completed'})
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=BytesStream([body]))
        async def sleep(_):
            pass
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            await observe_stream(client, 'https://fixture/interactions/id', 'fixture-key', trace, sleep=sleep)
        self.assertEqual(len(requests), 2)
        self.assertEqual(trace.status, 'completed')
        self.assertNotIn('fixture-key', ''.join(rows))

    async def test_stream_rejection_is_best_effort(self):
        trace = AgentTrace(sink=lambda *_args, **_kwargs: None)
        async def outbound(request):
            return httpx.Response(400, json={'error': 'unsupported'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            await observe_stream(client, 'https://fixture/id', 'fixture-key', trace)
        self.assertEqual(trace.status, 'unknown')

    async def test_diagnostic_lookup_and_trace_are_read_only(self):
        methods = []
        key = 'fixture-key'
        async def outbound(request):
            methods.append(request.method)
            self.assertEqual(request.method, 'GET')
            if request.url.host == 'pb.fixture':
                self.assertEqual(request.headers['Authorization'], 'fixture-pb')
                return httpx.Response(200, json={'items': [{'id': 'm' * 15, 'index': 1, 'status': 'running',
                    'providerState': {'id': 'v1_saved', 'credentialHash': hashlib.sha256(key.encode()).hexdigest()}}]})
            self.assertTrue(request.url.path.endswith('/v1_saved'))
            if request.url.params.get('stream'):
                return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                    stream=BytesStream([event('interaction.completed', interaction={'status': 'completed'})]))
            return httpx.Response(200, json={'status': 'in_progress', 'steps': []})
        with patch('builtins.print'):
            async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
                result = await diagnose(client, 'https://pb.fixture', 'fixture-pb', 'j' * 15, [key], 10)
        self.assertEqual(result, 0)
        self.assertEqual(methods, ['GET', 'GET', 'GET'])
