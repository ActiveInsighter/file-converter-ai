import asyncio
import json
import unittest

import httpx
from pdf2md import call_model_once
from providers import GEMINI
from quota_client import ProjectQuotaPool
from request_policy import CURRENT_POLICY, RequestPolicy
from unittest.mock import patch


class LeaseCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_keeps_draining_charged_hedge_leases_after_valid_report(self):
        outstanding = set()
        cancelled_reports = []
        leases_granted = 0
        hedges_granted = asyncio.Event()
        success_report_started = asyncio.Event()
        release_success_report = asyncio.Event()
        release_hedge_responses = asyncio.Event()
        text = '# Valid page\n\nSource text.'

        async def handle(request):
            nonlocal leases_granted
            body = json.loads(request.content)
            if request.url.path.endswith('/lease'):
                leases_granted += 1
                lease = body['requestId']
                outstanding.add(lease)
                if leases_granted > 1:
                    if leases_granted == 3:
                        hedges_granted.set()
                    await release_hedge_responses.wait()
                return httpx.Response(200, json={'keyIndex': 0, 'leaseId': lease})
            if request.url.path.endswith('/report'):
                if body['httpStatus'] == 200:
                    success_report_started.set()
                    await release_success_report.wait()
                else:
                    self.assertEqual(body['httpStatus'], 499)
                    cancelled_reports.append(body['leaseId'])
                outstanding.discard(body['leaseId'])
                return httpx.Response(200, json={'reported': True})
            # Return valid text only once both hedge leases have been charged;
            # those lease responses stay in transit across the group deadline.
            await hedges_granted.wait()
            return httpx.Response(200, json={
                'candidates': [{'content': {'parts': [{'text': text}]}}],
            })

        async def release_reports_and_leases():
            await success_report_started.wait()
            await asyncio.sleep(0.18)
            release_success_report.set()
            # Let winner reporting finish while hedge responses are draining.
            await asyncio.sleep(0.025)
            release_hedge_responses.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            pool = ProjectQuotaPool(['key'], ['project'], 15, 500, client,
                                    'https://quota.example', 'test')
            policy = RequestPolicy(concurrency=3, hedge_after=0.01, hedge_budget=2)
            token = CURRENT_POLICY.set(policy)
            release_task = asyncio.create_task(release_reports_and_leases())
            try:
                with patch.object(GEMINI, 'request_timeout_seconds', 0.12):
                    result = await asyncio.wait_for(call_model_once(
                        client, pool, GEMINI, 'test-model', [{'text': 'page'}],
                        '001', 'high'), 1)
                await release_task
                self.assertEqual(result, text)
                self.assertFalse(outstanding, 'charged hedge leases must finish cancellation reporting')
                self.assertEqual(leases_granted, 3)
                self.assertEqual(len(cancelled_reports), 2)
                self.assertEqual(sum(pool.used), 3)
                self.assertEqual(sum(pool.cancelled), 2)
            finally:
                release_success_report.set()
                release_hedge_responses.set()
                release_task.cancel()
                await asyncio.gather(release_task, return_exceptions=True)
                CURRENT_POLICY.reset(token)

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
