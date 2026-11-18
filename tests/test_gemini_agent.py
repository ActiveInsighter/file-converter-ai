import asyncio
import hashlib
import json
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

import httpx

from gemini_agent import (AgentCanceled, DEFAULT_AGENT,
                          agent_usage_diagnostics, build_agent_payload, cancel_interaction, generate_agent,
                          parse_interaction, upstream_error)
from key_pool import KeyPool


def job():
    return {'model': 'Antigravity', 'prompt': 'question', 'systemPrompt': 'system',
            'options': {'maxOutputTokens': 8192, 'thinkingLevel': 'high'}}


class AgentPayloadTests(unittest.TestCase):
    def test_alias_uses_agent_api_without_token_budget(self):
        data = build_agent_payload(job(), {'inputText': 'document'})
        self.assertEqual(data['agent'], DEFAULT_AGENT)
        self.assertEqual(data['input'], 'question\n\ndocument')
        self.assertEqual(data['agent_config'], {'type': 'antigravity', 'model': 'gemini-4-hydrogen'})
        self.assertEqual(data['environment'], {'type': 'remote'})
        self.assertNotIn('max_total_tokens', data['agent_config'])
        self.assertEqual({tool['type'] for tool in data['tools']}, {'code_execution', 'google_search', 'url_context'})
        self.assertTrue(data['background']); self.assertTrue(data['store'])
        self.assertEqual(data['system_instruction'], 'system')
        for key in ['model', 'generationConfig', 'generation_config', 'max_output_tokens', 'thinkingConfig']:
            self.assertNotIn(key, data)

    def test_legacy_budget_is_ignored_and_model_still_applied(self):
        value = job(); value['options']['agent'] = {'id': DEFAULT_AGENT, 'model': 'gemini-4-argon', 'maxTotalTokens': 50000}
        data = build_agent_payload(value, {})
        self.assertNotIn('max_total_tokens', data['agent_config'])
        self.assertEqual(data['agent_config']['model'], 'gemini-4-argon')

    def test_github_access_depends_only_on_configured_pat(self):
        import base64
        with patch.dict('os.environ', {'ANTIGRAVITY_GITHUB_TOKEN': 'fixture-github-secret'}, clear=True):
            for owner in ['', 'a' * 15, 'b' * 15]:
                task = job()
                task['prompt'] = '检查我的 GitHub 仓库权限'
                if owner:
                    task['owner'] = owner
                payload = build_agent_payload(task, {})
                self.assertEqual(len(payload['environment']['sources']), 3)
                self.assertIn('Access is determined by the configured token', payload['system_instruction'])
                self.assertIn('Clone only the target repository', payload['system_instruction'])
                rules = payload['environment']['network']['allowlist']
                self.assertEqual(rules[0]['domain'], 'github.com')
                self.assertEqual(rules[1]['transform']['Authorization'], 'Bearer fixture-github-secret')
                self.assertEqual(rules[2], {'domain': '*'})
                self.assertEqual(rules[0]['transform']['Authorization'],
                                 'Basic ' + base64.b64encode(b'x-oauth-basic:fixture-github-secret').decode())
                self.assertNotIn('mcp_server', [tool['type'] for tool in payload['tools']])
                self.assertNotIn('fixture-github-secret', payload['system_instruction'])
        with patch.dict('os.environ', {'ANTIGRAVITY_GITHUB_ALLOWED_OWNER': 'a' * 15}, clear=True):
            task = job()
            task['owner'] = 'a' * 15
            self.assertEqual(build_agent_payload(task, {'inputText': '读取github'})['environment'], {'type': 'remote'})
            self.assertNotIn('mcp_server', [tool['type'] for tool in build_agent_payload(task, {'inputText': '读取github'})['tools']])

    def test_simple_prompts_do_not_mount_github_and_no_budget_is_sent(self):
        with patch.dict('os.environ', {'ANTIGRAVITY_GITHUB_TOKEN': 'fixture-github-secret'}):
            value = build_agent_payload(job(), {'inputText': '你是谁？'})
            self.assertIn('network', value['environment'])
            self.assertNotIn('sources', value['environment'])
            self.assertNotIn('mcp_server', [tool['type'] for tool in value['tools']])
            self.assertNotIn('fixture-github-secret', value['system_instruction'])
            self.assertNotIn('max_total_tokens', value['agent_config'])
            value = build_agent_payload(job(), {'inputText': '你能查看 gh 仓库吗？'})
            self.assertIn('sources', value['environment'])
            self.assertNotIn('mcp_server', [tool['type'] for tool in value['tools']])

    def test_github_authentication_does_not_depend_on_task_keywords(self):
        with patch.dict('os.environ', {'ANTIGRAVITY_GITHUB_TOKEN': 'fixture-github-secret'}):
            for prompt in ['重新推送部署下我的exam docs项目', '看看remote项目', '。']:
                task = job(); task['prompt'] = prompt
                data = build_agent_payload(task, {})
                self.assertEqual(data['environment']['network']['allowlist'][1]['transform']['Authorization'],
                                 'Bearer fixture-github-secret')
            task['options']['agent'] = {'githubMcpEnabled': True}
            self.assertIn('mcp_server', [t['type'] for t in build_agent_payload(task, {})['tools']])

    def test_google_cumulative_usage_is_not_duplicated_and_diagnostics_are_safe(self):
        source = {'status': 'completed', 'usage': {
            'total_tokens': 94123, 'total_input_tokens': 40000,
            'total_output_tokens': 2000, 'total_thought_tokens': 30000,
            'total_tool_use_tokens': 22123, 'total_cached_tokens': 5000,
        }, 'steps': [{'type': 'mcp_server_call', 'arguments': 'super-secret-payload'},
                     {'type': 'mcp_server_result', 'content': [{'text': 'private tool result'}]},
                     {'type': 'model_output', 'content': [{'type': 'text', 'text': 'OK'}]}]}
        result = parse_interaction(source)
        self.assertEqual(result['usage']['totalTokenCount'], 94123)
        self.assertEqual(result['usage']['toolUseTokenCount'], 22123)
        self.assertEqual(result['reply'], 'OK')
        summary = json.dumps(agent_usage_diagnostics(source))
        self.assertIn('94123', summary)
        self.assertIn('mcp_server_call', summary)
        self.assertNotIn('super-secret', summary)
        self.assertNotIn('private tool result', summary)
        # Cached tokens are a subset of input; never add counters together.
        self.assertEqual(result['usage']['totalTokenCount'], source['usage']['total_tokens'])

    def test_upstream_error_redacts_github_bearer_and_basic_token(self):
        import base64
        token = 'fixture-github-secret'
        basic = base64.b64encode(f'x-oauth-basic:{token}'.encode()).decode()
        with patch.dict('os.environ', {'ANTIGRAVITY_GITHUB_TOKEN': token}):
            error = upstream_error(400, {'error': {'message': f'Bearer {token} Basic {basic} fixture-api-key'}},
                                   'fixture-api-key')
            self.assertNotIn(token, str(error))
            self.assertNotIn(basic, str(error))
            self.assertNotIn('fixture-api-key', str(error))

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

    def test_empty_sdk_convenience_text_does_not_hide_rest_model_output(self):
        result = parse_interaction({'status': 'completed', 'output_text': '', 'steps': [
            {'type': 'model_output', 'content': [{'type': 'text', 'text': 'REST answer'}]},
        ]})
        self.assertEqual(result['reply'], 'REST answer')
        self.assertFalse(result['error'])


class AgentTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_observation_errors_never_fail_cancel_or_recreate_existing_agent(self):
        state, requests, delays = {}, [], []
        polls = 0
        failures = [400, 401, 403, 404, 408, 409, 429] + [503] * 24
        async def checkpoint(update=None):
            if update: state.update(update)
            return {'state': state.copy(), 'canceled': False}
        async def sleep(seconds): delays.append(seconds)
        async def outbound(request):
            nonlocal polls
            requests.append(request)
            self.assertEqual(request.headers['x-goog-api-key'], 'fixture-secret')
            self.assertEqual(request.headers['Accept'], 'application/json')
            if request.method == 'POST':
                self.assertEqual(request.url.path, '/v1beta/interactions')
                return httpx.Response(200, json={'id': 'v1_saved', 'status': 'in_progress'})
            self.assertEqual(request.url.path, '/v1beta/interactions/v1_saved')
            self.assertNotIn('last_event_id', request.url.params)
            polls += 1
            if polls <= len(failures):
                return httpx.Response(failures[polls - 1], json={'error': {
                    'code': 'invalid_request', 'message': 'Precondition check failed.'}})
            if polls <= len(failures) + 22:
                raise httpx.ReadTimeout('fixture-secret may not be logged')
            if polls == len(failures) + 23: return httpx.Response(200, text='<html>unavailable</html>')
            if polls == len(failures) + 24: return httpx.Response(200, json=[])
            if polls == len(failures) + 25: return httpx.Response(200, json={'id': 'wrong-id', 'status': 'failed'})
            if polls == len(failures) + 26: return httpx.Response(200, json={})
            return httpx.Response(200, json={'id': 'v1_saved', 'status': 'completed', 'output_text': 'recovered'})
        output = StringIO()
        with redirect_stdout(output):
            async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
                result = await generate_agent(client, KeyPool(['fixture-secret'], rpm_per_key=1e6, rpd_per_key=100),
                    job(), {}, checkpoint=checkpoint, sleep=sleep)
        self.assertEqual(result['reply'], 'recovered')
        self.assertFalse(result['error'])
        self.assertEqual(sum(r.method == 'POST' for r in requests), 1)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(max(delays), 30)
        self.assertNotIn('fixture-secret', output.getvalue())

    async def test_user_can_cancel_during_repeated_poll_failures(self):
        state, requests, gets = {}, [], 0
        async def checkpoint(update=None):
            if update: state.update(update)
            return {'state': state, 'canceled': gets >= 3}
        async def sleep(_): pass
        async def outbound(request):
            nonlocal gets
            requests.append(request)
            if request.url.path.endswith('/cancel'):
                return httpx.Response(200, json={'status': 'cancelled'})
            if request.method == 'POST':
                return httpx.Response(200, json={'id': 'v1_saved', 'status': 'in_progress'})
            gets += 1
            return httpx.Response(400, json={'error': {'message': 'Precondition check failed.'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            with self.assertRaises(AgentCanceled):
                await generate_agent(client, KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100),
                    job(), {}, checkpoint=checkpoint, sleep=sleep)
        self.assertEqual(gets, 3)
        self.assertEqual(sum(r.url.path.endswith('/cancel') for r in requests), 1)

    async def test_local_observer_cancellation_does_not_cancel_remote_execution(self):
        state, requests = {}, []
        entered = asyncio.Event()
        async def checkpoint(update=None):
            if update: state.update(update)
            return {'state': state, 'canceled': False}
        async def sleep(_): entered.set(); await asyncio.Event().wait()
        async def outbound(request):
            requests.append(request)
            return httpx.Response(200, json={'id': 'v1_saved', 'status': 'in_progress'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            task = asyncio.create_task(generate_agent(client,
                KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100), job(), {},
                checkpoint=checkpoint, sleep=sleep))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(state['id'], 'v1_saved')
        self.assertEqual(len(requests), 1)

    async def test_cancel_checks_official_endpoint_and_does_not_accept_http_errors(self):
        for status in [200, 404]:
            requests = []
            async def outbound(request):
                requests.append(request)
                self.assertEqual(request.url.path, '/v1beta/interactions/agent-id/cancel')
                return httpx.Response(status, json={'status': 'cancelled'} if status == 200 else {'error': {'message': 'missing endpoint'}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
                if status == 200:
                    await cancel_interaction(client, 'https://fixture/v1beta/interactions/agent-id', 'fixture-key')
                else:
                    with self.assertRaisesRegex(RuntimeError, 'HTTP 404'):
                        await cancel_interaction(client, 'https://fixture/v1beta/interactions/agent-id', 'fixture-key')
            self.assertEqual(len(requests), 1)

    async def test_legacy_timeout_does_not_cancel_long_running_agent(self):
        now, polls, requests, state = [0.0], 0, [], {}
        task = job()
        task['options']['agent'] = {'timeoutMinutes': 1}
        async def checkpoint(update=None):
            if update: state.update(update)
            return {'state': state, 'canceled': False}
        async def sleep(_): now[0] += 4 * 60 * 60
        async def outbound(request):
            nonlocal polls
            requests.append(request)
            if request.method == 'POST':
                self.assertFalse(request.url.path.endswith('/cancel'))
                return httpx.Response(200, json={'id': 'agent-long', 'status': 'in_progress'})
            polls += 1
            return httpx.Response(200, json={'id': 'agent-long', 'status': 'completed' if polls == 2 else 'in_progress',
                'output_text': 'finished' if polls == 2 else ''})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            result = await generate_agent(client, KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100),
                task, {}, checkpoint=checkpoint, sleep=sleep, clock=lambda: now[0])
        self.assertEqual(result['reply'], 'finished')
        self.assertEqual(sum(request.method == 'POST' for request in requests), 1)
        self.assertGreater(now[0], 6 * 60 * 60)

    async def test_progress_is_throttled_and_terminal_usage_is_reported_once(self):
        now, polls = [0.0], 0
        state = {}
        async def checkpoint(update=None):
            if update: state.update(update)
            return {'state': state, 'canceled': False}
        async def sleep(seconds): now[0] += seconds
        async def outbound(request):
            nonlocal polls
            self.assertNotIn('stream', request.url.params)
            if request.method == 'POST':
                return httpx.Response(200, json={'id': 'v1_agent', 'status': 'in_progress'})
            polls += 1
            completed = polls == 45
            return httpx.Response(200, json={'id': 'v1_agent',
                'status': 'completed' if completed else 'in_progress', 'output_text': 'answer' if completed else '',
                'usage': {'total_tokens': polls * 10}})
        logs = StringIO()
        with redirect_stdout(logs):
            async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
                result = await generate_agent(client, KeyPool(['fixture-secret'], rpm_per_key=1e6, rpd_per_key=100),
                    {**job(), 'id': 'j' * 15}, {'index': 3}, checkpoint=checkpoint, sleep=sleep, clock=lambda: now[0])
        rows = [json.loads(line.removeprefix('[agent.telemetry] ')) for line in logs.getvalue().splitlines()]
        progress = [row for row in rows if row['event'] == 'progress']
        self.assertEqual([row['elapsed_seconds'] for row in progress], [3, 63, 123])
        completed = [row for row in rows if row['event'] == 'completed']
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]['usage']['total_tokens'], 450)
        self.assertTrue(all(row['job'] == 'j' * 15 and row['segment'] == 3 for row in rows))
        self.assertEqual(result['usage']['totalTokenCount'], 450)
        self.assertNotIn('[agent.trace]', logs.getvalue())

    async def test_push_permission_failure_prevents_any_google_request(self):
        calls = []
        async def checkpoint(update=None):
            return {'state': {}, 'canceled': False}
        async def outbound(request):
            calls.append(request)
            self.assertEqual(request.method, 'GET')
            self.assertNotEqual(request.url.host, 'generativelanguage.googleapis.com')
            if request.url.path == '/user':
                return httpx.Response(200, json={'login': 'owner'})
            if request.url.host == 'api.github.com':
                return httpx.Response(200, json={'full_name': 'owner/remote', 'default_branch': 'main'})
            return httpx.Response(403)
        task = job(); task['prompt'] = '重构owner/remote项目并推送分支'
        with patch.dict('os.environ', {'ANTIGRAVITY_GITHUB_TOKEN': 'fixture-pat'}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
                with self.assertRaisesRegex(RuntimeError, '无法写入'):
                    await generate_agent(client, KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100),
                                         task, {}, checkpoint=checkpoint)
        self.assertEqual(len(calls), 3)

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
        logs = StringIO()
        with redirect_stdout(logs):
            async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
                result = await generate_agent(client, KeyPool(['fixture-secret'], rpm_per_key=1e6, rpd_per_key=100), job(), {}, checkpoint=checkpoint, sleep=sleep)
        self.assertEqual(result['reply'], 'agent answer')
        self.assertIn('"event":"prepared"', logs.getvalue())
        self.assertNotIn('budget_tokens', logs.getvalue())
        self.assertIn('"event":"created"', logs.getvalue())
        self.assertNotIn('"event":"progress"', logs.getvalue())
        self.assertIn('"event":"completed"', logs.getvalue())
        self.assertIn('"step_types":{"model_output":1}', logs.getvalue())
        self.assertNotIn('fixture-secret', logs.getvalue())
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
            if request.url.path.endswith('/cancel'):
                canceled.append(request.url.path)
                return httpx.Response(200, json={'id': 'v1_cancel', 'status': 'cancelled'})
            return httpx.Response(200, json={'id': 'v1_cancel', 'status': 'in_progress'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            with self.assertRaises(AgentCanceled):
                await generate_agent(client, KeyPool(['fixture-secret'], rpm_per_key=1e6, rpd_per_key=100), job(), {}, checkpoint=checkpoint)
        self.assertEqual(canceled, ['/v1beta/interactions/v1_cancel/cancel'])

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
