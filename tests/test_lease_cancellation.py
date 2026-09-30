import asyncio
import unittest
from uuid import uuid4

import httpx
from quota_client import ProjectQuotaPool
from unittest.mock import patch


class LeaseCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_wall_clock_lease_timeout_retries_same_request_identity(self):
        identities = []
        async with httpx.AsyncClient() as client:
            pool = ProjectQuotaPool(['key'], ['project'], 15, 500, client, 'https://quota.example', 'test')
            async def lease_response(identity):
                identities.append(identity)
                if len(identities) == 1:
                    raise TimeoutError('lost response')
                return httpx.Response(200, json={'keyIndex': 0, 'leaseId': identity})
            with patch.object(pool, '_lease_response', lease_response), patch('quota_client.random.uniform', return_value=0):
                await pool.acquire()
            self.assertEqual(len(identities), 2)
            self.assertEqual(identities[0], identities[1])
            self.assertEqual(sum(pool.used), 1)

    async def test_lease_granted_while_client_cancelled_is_still_reported(self):
        granted = asyncio.Event()
        response_ready = asyncio.Event()
        outstanding = set()
        used = []
        async def handle(request):
            import json
            body = json.loads(request.content)
            if request.url.path.endswith('/lease'):
                lease = body['requestId']
                outstanding.add(lease)
                used.append(lease)
                granted.set()
                await response_ready.wait()
                return httpx.Response(200, json={'keyIndex': 0, 'leaseId': lease})
            if request.url.path.endswith('/report'):
                self.assertEqual(body['httpStatus'], 499)
                outstanding.discard(body['leaseId'])
                return httpx.Response(200, json={'reported': True})
            raise AssertionError('unexpected endpoint')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            pool = ProjectQuotaPool(['key'], ['project'], 15, 500, client, 'https://quota.example', 'test')
            task = asyncio.create_task(pool.acquire())
            await granted.wait()
            task.cancel()
            await asyncio.sleep(0)
            response_ready.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(outstanding)
            self.assertEqual(len(used), 1)
            self.assertEqual(sum(pool.used), 1)
            self.assertEqual(sum(pool.cancelled), 1)
