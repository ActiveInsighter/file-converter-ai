import asyncio
import hashlib
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from agent_maintenance import main


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_probe_resolves_target_and_never_changes_original_job(self):
        calls = []
        async def outbound(request):
            calls.append(request)
            self.assertEqual(request.method, 'GET')
            if '/aw_gemini_jobs/' in request.url.path:
                return httpx.Response(200, json={'id': 'j' * 15, 'prompt': '读取 owner/remote 项目'})
            if '/aw_gemini_messages/' in request.url.path:
                return httpx.Response(200, json={'items': [{'providerState': {'environmentId': 'env',
                    'credentialHash': hashlib.sha256(b'fixture-key').hexdigest()}}]})
            if request.url.path == '/user':
                return httpx.Response(200, json={'login': 'owner'})
            return httpx.Response(200, json={'full_name': 'owner/remote', 'default_branch': 'main'})
        client = httpx.AsyncClient(transport=httpx.MockTransport(outbound))
        with patch.dict('os.environ', {'GEMINI_PB_URL': 'https://pb.fixture', 'GEMINI_API_KEYS': 'fixture-key',
                                      'ANTIGRAVITY_GITHUB_TOKEN': 'fixture-pat', 'POCKETBASE_SERVER_TOKEN': 'fixture-pb'}), \
             patch('agent_maintenance.httpx.AsyncClient', return_value=client), \
             patch('agent_maintenance.asyncio.timeout', wraps=asyncio.timeout) as timeout, \
             patch('gemini_agent.generate_agent', new_callable=AsyncMock,
                   return_value={'reply': 'AW_RUNTIME_OK owner', 'error': '', 'usage': {}}) as generate:
            self.assertEqual(await main('probe', 'j' * 15), 0)
            probe_job = generate.call_args.args[2]
            self.assertIn('owner/remote', probe_job['prompt'])
            self.assertNotIn('agent', probe_job['options'])
            timeout.assert_called_once_with(180)
        self.assertEqual(len(calls), 4)
