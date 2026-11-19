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
                self.assertIn('repositories relevant to the request', payload['system_instruction'])
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
        result = parse_interaction({'status': 'completed', 'output_text': '', 'steps': [
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

    async def test_repository_tasks_start_directly_with_or_without_github_token(self):
        prompts = (
            '看看 GitHub 里的 RikkaHub 和工作流 remote Gemini 页面，修复后触发 GitHub Action 推送部署',
            '重构 owner/remote 和 owner/rikkahub 项目并推送分支',
        )
        for prompt in prompts:
            for token in ('fixture-pat', ''):
                with self.subTest(prompt=prompt, token_configured=bool(token)):
                    state, calls = {}, []

                    async def checkpoint(update=None):
                        if update:
                            state.update(update)
                        return {'state': state.copy(), 'canceled': False}

                    async def outbound(request):
                        calls.append((request.method, request.url.host, request.url.path))
                        self.assertEqual(request.url.host, 'generativelanguage.googleapis.com')
                        payload = json.loads(request.content)
                        self.assertEqual(payload['input'], prompt)
                        self.assertEqual('network' in payload['environment'], bool(token))
                        self.assertEqual('sources' in payload['environment'], bool(token))
                        return httpx.Response(201, json={'id': 'agent-test', 'status': 'completed', 'output_text': 'ready'})

                    pool = KeyPool(['fixture-key'], rpm_per_key=60, rpd_per_key=100)
                    try:
                        with patch.dict('os.environ', {'ANTIGRAVITY_GITHUB_TOKEN': token}), redirect_stdout(StringIO()):
                            async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
                                result = await generate_agent(client, pool, {**job(), 'prompt': prompt}, {},
                                                              checkpoint=checkpoint)
                        self.assertEqual(result['reply'], 'ready')
                        self.assertEqual(calls, [('POST', 'generativelanguage.googleapis.com', '/v1beta/interactions')])
                    finally:
                        await pool.close()

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

    async def test_user_cancel_precondition_reconciles_original_interaction(self):
        for remote_status, requests, saved_status in (
            ('cancelled', ['POST', 'cancel', 'GET'], 'cancelled'),
            ('in_progress', ['POST', 'cancel', 'GET', 'cancel', 'GET'], 'cancel_unconfirmed'),
        ):
            with self.subTest(remote_status=remote_status):
                state, sent = {}, []

                async def checkpoint(update=None):
                    if update: state.update(update)
                    return {'state': state.copy(), 'canceled': bool(state.get('id'))}

                async def outbound(request):
                    if request.url.path.endswith('/cancel'):
                        sent.append('cancel')
                        return httpx.Response(400, json={'error': {'message': 'Precondition check failed.'}})
                    sent.append(request.method)
                    status = remote_status if request.method == 'GET' else 'in_progress'
                    return httpx.Response(200, json={'id': 'fixture-cancel', 'status': status})

                async def sleep(_):
                    pass

                async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
                    with self.assertRaises(AgentCanceled):
                        await generate_agent(
                            client, KeyPool(['fixture-secret'], rpm_per_key=1e6, rpd_per_key=100),
                            job(), {}, checkpoint=checkpoint, sleep=sleep,
                        )
                self.assertEqual(sent, requests)
                self.assertEqual(state['status'], saved_status)

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

    def test_build_agent_payload_reuses_environment_id_and_previous_interaction_id(self):
        import os
        task = {**job(), 'history': [{'role': 'user', 'text': 'old'}, {'role': 'model', 'text': 'reply'}]}
        with patch.dict(os.environ, {'ANTIGRAVITY_GITHUB_TOKEN': 'ghp_test_only'}, clear=False):
            payload = build_agent_payload(
                task,
                {'inputText': 'follow up'},
                environment_id='env_saved_123',
                previous_interaction_id='v1_prev_456',
            )
        self.assertEqual(payload['environment'], 'env_saved_123')
        self.assertEqual(payload['previous_interaction_id'], 'v1_prev_456')
        self.assertEqual(payload['input'], 'question\n\nfollow up')

    async def test_generate_agent_reuses_parent_environment_and_matching_credential(self):
        import hashlib
        sent_bodies = []
        sent_keys = []
        state = {}
        async def checkpoint(update=None):
            if update:
                state.update(update)
            return {'state': state, 'canceled': False}
        async def sleep(_): pass
        async def outbound(request):
            if request.url.host == 'pb.example.com':
                if '/aw_gemini_messages/' in request.url.path:
                    return httpx.Response(200, json={
                        'id': 'msg_parent_1',
                        'providerState': {
                            'id': 'v1_parent',
                            'environmentId': 'env_reused_1',
                            'credentialHash': hashlib.sha256(b'key-2').hexdigest(),
                        },
                    })
                return httpx.Response(404, json={})
            sent_keys.append(request.headers.get('x-goog-api-key'))
            if request.method == 'POST':
                sent_bodies.append(json.loads(request.content))
                return httpx.Response(202, json={'id': 'v1_child', 'environment_id': 'env_reused_1', 'status': 'in_progress'})
            return httpx.Response(200, json={'id': 'v1_child', 'environment_id': 'env_reused_1', 'status': 'completed', 'output_text': 'continued ok'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            pb = type('PB', (), {'root': 'https://pb.example.com', 'token': 'pb-tok', 'client': client})()
            task = {**job(), 'parentMessage': 'msg_parent_1', 'history': [{'role': 'user', 'text': 'first'}]}
            pool = KeyPool(['key-1', 'key-2'], rpm_per_key=1e6, rpd_per_key=100)
            result = await generate_agent(client, pool, task, {}, checkpoint=checkpoint, sleep=sleep, pb=pb)
        self.assertEqual(pool.used, [0, 1])
        self.assertEqual(pool.disabled_keys, {})
        self.assertEqual(result['reply'], 'continued ok')
        self.assertEqual(sent_keys, ['key-2', 'key-2'])
        self.assertEqual(sent_bodies[0]['environment'], 'env_reused_1')
        self.assertEqual(sent_bodies[0]['previous_interaction_id'], 'v1_parent')
        self.assertEqual(sent_bodies[0]['input'], 'question')
        self.assertEqual(result['response']['metadata']['environment_id'], 'env_reused_1')

    async def test_empty_completed_reply_requires_the_same_interaction_and_sandbox(self):
        for upstream_status, saved_key, expected_error in [
            (200, 'key-2', None),
            (404, 'key-2', '原 Agent 任务续接被上游拒绝'),
            (200, 'missing-key', '缺少原交互、原沙盒或匹配的 API Key'),
        ]:
            with self.subTest(upstream_status=upstream_status, saved_key=saved_key):
                state, requests = {}, []

                async def checkpoint(update=None):
                    state.update(update or {})
                    return {'state': state, 'canceled': False}

                def outbound(request):
                    self.assertEqual(request.method, 'POST')
                    self.assertEqual(request.headers['x-goog-api-key'], 'key-2')
                    payload = json.loads(request.content)
                    requests.append(payload)
                    self.assertEqual(payload['environment'], 'original-sandbox')
                    self.assertEqual(payload['previous_interaction_id'], 'original-turn')
                    self.assertEqual(payload['input'], 'question')
                    return httpx.Response(upstream_status, json={
                        'id': 'follow-up', 'status': 'completed', 'output_text': 'continued',
                    } if upstream_status == 200 else {'error': {'message': 'environment not found'}})

                task = {**job(), 'history': [{'role': 'user', 'text': 'original task'}, {'role': 'model', 'text': ''}],
                        'agentContext': {'environmentId': 'original-sandbox', 'interactionId': 'original-turn',
                                         'credentialHash': hashlib.sha256(saved_key.encode()).hexdigest(), 'required': True}}
                pool = KeyPool(['key-1', 'key-2'], rpm_per_key=1e6, rpd_per_key=100)
                async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
                    if expected_error:
                        with self.assertRaisesRegex(RuntimeError, expected_error):
                            await generate_agent(client, pool, task, {}, checkpoint=checkpoint)
                    else:
                        result = await generate_agent(client, pool, task, {}, checkpoint=checkpoint)
                        self.assertEqual(result['reply'], 'continued')
                        self.assertEqual(state['environmentId'], 'original-sandbox')
                self.assertEqual(len(requests), int(saved_key == 'key-2'))

    async def test_generate_agent_falls_back_to_fresh_environment_when_expired(self):
        import hashlib
        sent_bodies = []
        state = {}
        async def checkpoint(update=None):
            if update:
                state.update(update)
            return {'state': state, 'canceled': False}
        async def sleep(_): pass
        async def outbound(request):
            if request.url.host == 'pb.example.com':
                return httpx.Response(200, json={
                    'id': 'msg_parent_old',
                    'providerState': {'id': 'v1_old', 'environmentId': 'env_expired',
                                      'credentialHash': hashlib.sha256(b'key-1').hexdigest()},
                })
            if request.method == 'POST':
                body = json.loads(request.content)
                sent_bodies.append(body)
                if body.get('environment') == 'env_expired':
                    return httpx.Response(404, json={'error': {'message': 'environment not found'}})
                return httpx.Response(202, json={'id': 'v1_fresh', 'environment_id': 'env_new', 'status': 'in_progress'})
            return httpx.Response(200, json={'id': 'v1_fresh', 'environment_id': 'env_new', 'status': 'completed', 'output_text': 'fresh ok'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            pb = type('PB', (), {'root': 'https://pb.example.com', 'token': 'pb-tok', 'client': client})()
            task = {**job(), 'parentMessage': 'msg_parent_old', 'history': [{'role': 'user', 'text': 'old turn'}]}
            result = await generate_agent(client, KeyPool(['key-1'], rpm_per_key=1e6, rpd_per_key=100), task, {}, checkpoint=checkpoint, sleep=sleep, pb=pb)
        self.assertEqual(result['reply'], 'fresh ok')
        self.assertEqual(len(sent_bodies), 2)
        self.assertEqual(sent_bodies[0]['environment'], 'env_expired')
        self.assertEqual(sent_bodies[1]['environment'], {'type': 'remote'})
        self.assertNotIn('previous_interaction_id', sent_bodies[1])
        self.assertIn('user: old turn', sent_bodies[1]['input'])
        self.assertEqual(state['environmentId'], 'env_new')

    async def test_fresh_agent_dispatch_makes_single_pre_create_checkpoint(self):
        events = []
        state = {}
        async def checkpoint(update=None):
            events.append(('checkpoint', dict(update) if update else None))
            if update:
                state.update(update)
            return {'state': state, 'canceled': False}
        async def sleep(_): pass
        async def outbound(request):
            events.append((request.method, str(request.url)))
            return httpx.Response(200, json={'id': 'v1_fast', 'status': 'completed', 'output_text': 'ok'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            result = await generate_agent(client, KeyPool(['key-1'], rpm_per_key=1e6, rpd_per_key=100),
                                          {**job(), 'agentContext': {}}, {}, checkpoint=checkpoint, sleep=sleep)
        self.assertEqual(result['reply'], 'ok')
        post_index = next(i for i, item in enumerate(events) if item[0] == 'POST')
        pre_create_checkpoints = [e for e in events[:post_index] if e[0] == 'checkpoint']
        self.assertEqual(len(pre_create_checkpoints), 1)

    async def test_sse_terminal_event_wakes_polling_immediately(self):
        import time
        from agent_progress import MessageProgress
        state = {}
        async def checkpoint(update=None):
            if update:
                state.update(update)
            return {'state': state, 'canceled': False}
        async def outbound(request):
            if request.method == 'POST':
                return httpx.Response(202, json={'id': 'v1_sse', 'status': 'in_progress'})
            if request.url.params.get('stream') == 'true':
                sse = b'event: interaction.completed\ndata: {"event_type":"interaction.completed","interaction":{"id":"v1_sse","status":"completed"}}\n\n'
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse)
            return httpx.Response(200, json={'id': 'v1_sse', 'status': 'completed', 'output_text': 'fast reply'})
        progress = MessageProgress()
        t0 = time.monotonic()
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            result = await generate_agent(client, KeyPool(['key-1'], rpm_per_key=1e6, rpd_per_key=100),
                                          {**job(), 'agentContext': {}}, {}, checkpoint=checkpoint, progress=progress)
        elapsed = time.monotonic() - t0
        self.assertEqual(result['reply'], 'fast reply')
        self.assertLess(elapsed, 1.0)


