import json
import unittest

import httpx

from agent_progress_sync import VerifiedProgressWriter
from gemini_text import PocketBase


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_heartbeat_ack_cannot_be_mistaken_for_saved_parts(self):
        sequence, writes, saved = 0, [], []
        part = {'key': 'step:7', 'sequence': 8, 'type': 'text', 'status': 'completed', 'content': 'new step'}
        async def outbound(request):
            nonlocal sequence, saved
            self.assertEqual(request.url.host, 'pb.fixture')
            if request.method == 'POST':
                self.assertTrue(request.url.path.endswith('/progress'))
                body = json.loads(request.content); writes.append(body)
                self.assertEqual(body['executionId'], '123:1')
                self.assertEqual(body['usage'], {}, 'repair must not overwrite primary runner counters')
                sequence = body['sequence']
                if len(writes) == 1:
                    return httpx.Response(200, json={'accepted': False})
                saved = body['parts']
                return httpx.Response(200, json={'accepted': True})
            if '/aw_gemini_jobs/' in request.url.path:
                return httpx.Response(200, json={'status': 'running', 'executionId': '123:1'})
            if '/aw_gemini_messages/' in request.url.path:
                return httpx.Response(200, json={'status': 'running', 'uploadSequence': sequence})
            return httpx.Response(200, json={'items': saved})
        async def sleep(_): pass
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            writer = VerifiedProgressWriter(client, PocketBase(client, 'https://pb.fixture'), 'a' * 15, 'b' * 15, '123:1', sleep=sleep)
            ack = await writer({'executionId': '123:1', 'sequence': 1, 'parts': [part], 'usage': {'agentStepCount': 8}})
        self.assertTrue(ack['accepted'])
        self.assertEqual([body['sequence'] for body in writes], [1, 2])
        self.assertEqual(saved, [part])

    async def test_identity_change_stops_without_any_write(self):
        methods = []
        async def outbound(request):
            methods.append(request.method)
            return httpx.Response(200, json={'status': 'running', 'executionId': 'new-owner'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            writer = VerifiedProgressWriter(client, PocketBase(client, 'https://pb.fixture'), 'a' * 15, 'b' * 15, '123:1')
            self.assertTrue((await writer({'parts': [], 'usage': {}}))['closed'])
        self.assertEqual(methods, ['GET'])

    async def test_lost_ack_reads_back_the_already_committed_step(self):
        part = {'key': 'step:7', 'sequence': 8, 'type': 'text', 'status': 'completed', 'content': 'committed'}
        writes, saved = [], []
        async def outbound(request):
            nonlocal saved
            if request.method == 'POST':
                writes.append(json.loads(request.content))
                if len(writes) == 1:
                    saved = writes[-1]['parts']
                    raise httpx.ConnectError('response lost after commit')
                return httpx.Response(200, json={'accepted': False})
            if '/aw_gemini_jobs/' in request.url.path:
                return httpx.Response(200, json={'status': 'running', 'executionId': '123:1'})
            if '/aw_gemini_messages/' in request.url.path:
                return httpx.Response(200, json={'status': 'running', 'uploadSequence': 0})
            return httpx.Response(200, json={'items': saved})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            writer = VerifiedProgressWriter(client, PocketBase(client, 'https://pb.fixture'), 'a' * 15, 'b' * 15, '123:1')
            await writer({'executionId': '123:1', 'sequence': 1, 'parts': [part], 'usage': {}})
        self.assertEqual(writes[0], writes[1], 'lost ACK retries must use the exact batch')
        self.assertEqual(saved, [part])
