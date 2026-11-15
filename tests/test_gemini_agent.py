import asyncio
import hashlib
import json
import unittest

import httpx

from gemini_agent import AgentCanceled, DEFAULT_AGENT, build_agent_payload, generate_agent, parse_interaction
from key_pool import KeyPool


def job():
    return {'model': 'Antigravity', 'prompt': 'question', 'systemPrompt': 'system',
            'options': {'maxOutputTokens': 8192, 'thinkingLevel': 'high'}}


class AgentPayloadTests(unittest.TestCase):
    def test_alias_uses_agent_api_with_all_tools_and_unlimited_budget(self):
        data = build_agent_payload(job(), {'inputText': 'document'})
        self.assertEqual(data['agent'], DEFAULT_AGENT)
        self.assertEqual(data['input'], 'question\n\ndocument')
        self.assertEqual(data['agent_config'], {'type': 'antigravity', 'model': 'gemini-4-hydrogen'})
        self.assertEqual(data['environment'], {'type': 'remote'})
        self.assertEqual({tool['type'] for tool in data['tools']}, {'code_execution', 'google_search', 'url_context'})
        self.assertTrue(data['background']); self.assertTrue(data['store'])
        self.assertEqual(data['system_instruction'], 'system')
        for key in ['model', 'generationConfig', 'generation_config', 'max_output_tokens', 'thinkingConfig']:
            self.assertNotIn(key, data)

    def test_explicit_budget_and_model_are_agent_config_not_generation_config(self):
        value = job(); value['options']['agent'] = {'id': DEFAULT_AGENT, 'model': 'gemini-4-argon', 'maxTotalTokens': 50000}
        data = build_agent_payload(value, {})
        self.assertEqual(data['agent_config']['max_total_tokens'], '50000')
        self.assertEqual(data['agent_config']['model'], 'gemini-4-argon')

    def test_response_only_reads_public_model_output_and_usage(self):
        result = parse_interaction({'status': 'completed', 'steps': [
            {'type': 'thought', 'content': [{'type': 'text', 'text': 'private'}]},
            {'type': 'code_execution_call'},
            {'type': 'code_execution_result', 'content': [{'type': 'text', 'text': 'tool log'}]},
            {'type': 'model_output', 'content': [{'type': 'text', 'text': 'answer'}]},
        ], 'usage': {'total_tokens': 25, 'total_input_tokens': 10}})
        self.assertEqual(result['reply'], 'answer')
        self.assertEqual(result['usage']['totalTokenCount'], 25)
        self.assertEqual(result['usage']['agentToolCalls']['code_execution_call'], 1)
        self.assertEqual(result['error'], '')
        partial = parse_interaction({'status': 'incomplete', 'output_text': 'partial'})
        self.assertEqual(partial['reply'], 'partial'); self.assertTrue(partial['error'])


class AgentTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_poll_retry_never_reposts_and_persists_identity(self):
        requests = []; state = {}; gets = 0
        async def checkpoint(update=None):
            if update: state.update(update)
            return {'state': state.copy(), 'canceled': False}
        async def outbound(request):
            nonlocal gets
            requests.append(request)
            self.assertEqual(request.headers['x-goog-api-key'], 'fixture-secret')
            if request.method == 'POST':
                self.assertEqual(request.url.path, '/v1beta/interactions')
                self.assertEqual(json.loads(request.content)['agent'], DEFAULT_AGENT)
                return httpx.Response(200, json={'id': 'v1_agent', 'status': 'in_progress'})
            gets += 1
            if gets == 1: raise httpx.ReadTimeout('GET interrupted')
            return httpx.Response(200, json={'id': 'v1_agent', 'environment_id': 'env_test', 'status': 'completed', 'steps': [{'type': 'model_output', 'content': [{'type': 'text', 'text': 'agent answer'}]}]})
        async def sleep(_): pass
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            result = await generate_agent(client, KeyPool(['fixture-secret'], rpm_per_key=1e6, rpd_per_key=100), job(), {}, checkpoint=checkpoint, sleep=sleep)
        self.assertEqual(result['reply'], 'agent answer')
        self.assertEqual(sum(request.method == 'POST' for request in requests), 1)
        self.assertEqual(state['id'], 'v1_agent'); self.assertEqual(state['environmentId'], 'env_test')
        self.assertEqual(state['credentialHash'], hashlib.sha256(b'fixture-secret').hexdigest())
        self.assertNotIn('fixture-secret', json.dumps(state))

    async def test_saved_interaction_resumes_by_get_without_creation(self):
        state = {'id': 'v1_saved', 'credentialHash': hashlib.sha256(b'fixture-secret').hexdigest()}
        async def checkpoint(update=None): return {'state': state, 'canceled': False}
        async def outbound(request):
            self.assertEqual(request.method, 'GET')
            self.assertTrue(request.url.path.endswith('v1_saved'))
            return httpx.Response(200, json={'id': 'v1_saved', 'status': 'completed', 'output_text': 'restored'})
        async def sleep(_): pass
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            result = await generate_agent(client, KeyPool(['fixture-secret'], rpm_per_key=1e6, rpd_per_key=100), job(), {}, checkpoint=checkpoint, sleep=sleep)
        self.assertEqual(result['reply'], 'restored')

    async def test_user_cancel_cancels_remote_interaction(self):
        state = {}; canceled = []
        async def checkpoint(update=None):
            if update: state.update(update)
            return {'state': state, 'canceled': bool(state.get('id'))}
        async def outbound(request):
            if request.url.path.endswith(':cancel'):
                canceled.append(request.url.path)
                return httpx.Response(200, json={'id': 'v1_cancel', 'status': 'cancelled'})
            return httpx.Response(200, json={'id': 'v1_cancel', 'status': 'in_progress'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            with self.assertRaises(AgentCanceled):
                await generate_agent(client, KeyPool(['fixture-secret'], rpm_per_key=1e6, rpd_per_key=100), job(), {}, checkpoint=checkpoint)
        self.assertEqual(canceled, ['/v1beta/interactions/v1_cancel:cancel'])

    async def test_ambiguous_create_is_not_retried(self):
        calls = 0
        async def checkpoint(update=None): return {'state': {}, 'canceled': False}
        async def outbound(request):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout('ambiguous POST')
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            with self.assertRaisesRegex(RuntimeError, '未自动重发'):
                await generate_agent(client, KeyPool(['fixture-secret'], rpm_per_key=1e6, rpd_per_key=100), job(), {}, checkpoint=checkpoint)
        self.assertEqual(calls, 1)

    async def test_upstream_diagnostic_is_useful_and_redacts_credential(self):
        async def checkpoint(update=None): return {'state': {}, 'canceled': False}
        async def outbound(request):
            return httpx.Response(400, json={'error': {'message': 'unsupported field max_output_tokens fixture-secret'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            with self.assertRaisesRegex(RuntimeError, 'unsupported field') as error:
                await generate_agent(client, KeyPool(['fixture-secret'], rpm_per_key=1e6, rpd_per_key=100), job(), {}, checkpoint=checkpoint)
        self.assertNotIn('fixture-secret', str(error.exception))

    async def test_zero_free_tier_quota_explains_account_requirement(self):
        async def checkpoint(update=None): return {'state': {}, 'canceled': False}
        async def outbound(request):
            return httpx.Response(429, json={'error': {'message':
                'Rate limit exceeded for model gemini-4-carbon (limit: 0 input tokens per minute on Free Tier).'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            with self.assertRaisesRegex(RuntimeError, '免费额度为 0'):
                await generate_agent(client, KeyPool(['fixture-secret'], rpm_per_key=1e6, rpd_per_key=100), job(), {}, checkpoint=checkpoint)

    async def test_cancel_during_access_checks_does_not_try_another_key(self):
        calls = []
        async def checkpoint(update=None): return {'state': {}, 'canceled': bool(calls)}
        async def outbound(request):
            calls.append(request)
            return httpx.Response(403, json={'error': {'message': 'Agent access denied'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            with self.assertRaises(AgentCanceled):
                await generate_agent(client, KeyPool(['key-1', 'key-2'], rpm_per_key=1e6, rpd_per_key=100), job(), {}, checkpoint=checkpoint)
        self.assertEqual(len(calls), 1)
