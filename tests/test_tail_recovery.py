"""Regression coverage for deadlines and cancellation of page requests."""

import asyncio
import unittest
from unittest.mock import patch

import httpx

from pdf2md import call_model_once, is_retryable_error
from providers import GEMINI


def successful_response():
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"text": "# Recovered page\n\nOriginal text."}]}}
            ]
        },
    )


class LeasePool:
    """Keep actual lease state so tests can detect leaked quota capacity."""

    def __init__(self):
        self.outstanding = set()
        self.outcomes = []
        self.http_statuses = []
        self.report_started = asyncio.Event()
        self.allow_report = asyncio.Event()
        self.allow_report.set()

    async def acquire(self):
        self.outstanding.add("page-lease")
        return 0, "test-key", "page-lease"

    async def mark_success(self, key_index, lease_id):
        self.report_started.set()
        await self.allow_report.wait()
        self.outstanding.discard(lease_id)
        self.outcomes.append("success")

    async def mark_error(self, key_index, lease_id, http_status=0):
        self.outstanding.discard(lease_id)
        self.outcomes.append(("error", http_status))

    async def mark_cancelled(self, key_index, lease_id):
        self.outstanding.discard(lease_id)
        self.outcomes.append("cancelled")

    def record_http_result(self, status, elapsed_seconds):
        self.http_statuses.append(status)


class BlockedClient:
    def __init__(self):
        self.started = asyncio.Event()
        self.allow_response = asyncio.Event()

    async def post(self, *args, **kwargs):
        self.started.set()
        await self.allow_response.wait()
        return successful_response()


class TailRecoveryRequestTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, client, pool):
        return await call_model_once(
            client, pool, GEMINI, "test-model", [{"text": "page"}], "001", "high"
        )

    async def test_total_deadline_bounds_a_response_that_keeps_making_progress(self):
        """Read activity cannot extend a request's total deadline indefinitely."""

        class TricklingClient:
            async def post(self, *args, **kwargs):
                # Model a body whose successive bytes arrive often enough to
                # keep resetting the HTTP transport's inactivity timeout.
                for _ in range(200):
                    await asyncio.sleep(0.005)
                return successful_response()

        pool = LeasePool()
        with patch.object(GEMINI, "request_timeout_seconds", 0.04):
            try:
                await asyncio.wait_for(self.request(TricklingClient(), pool), 0.5)
            except TimeoutError:
                self.fail("the request exceeded its total deadline without raising a retryable error")
            except RuntimeError as exc:
                self.assertTrue(is_retryable_error(exc))
            else:
                self.fail("a request exceeding the total deadline was accepted")

        self.assertEqual(pool.outstanding, set(), "a timed out request must release its lease")
        self.assertEqual(len(pool.outcomes), 1, "a timed out request must be reported once")
        self.assertNotIn(503, pool.http_statuses, "a transport timeout is not provider overload")

    async def test_cancelling_http_releases_capacity_and_preserves_cancellation(self):
        pool = LeasePool()
        client = BlockedClient()
        task = asyncio.create_task(self.request(client, pool))
        try:
            await asyncio.wait_for(client.started.wait(), 0.5)
            self.assertEqual(pool.outstanding, {"page-lease"})
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(pool.outstanding, set(), "a cancelled hedge must not leak its lease")
        self.assertEqual(pool.outcomes, ["cancelled"])
        self.assertNotIn(503, pool.http_statuses, "losing a hedge must not trigger overload backoff")

    async def test_cancellation_during_success_reporting_does_not_leak_or_reclassify_success(self):
        pool = LeasePool()
        pool.allow_report.clear()
        client = BlockedClient()
        client.allow_response.set()
        task = asyncio.create_task(self.request(client, pool))
        try:
            await asyncio.wait_for(pool.report_started.wait(), 0.5)
            task.cancel()
            # Give cancellation a chance to interrupt reporting, then allow
            # bounded cleanup to finish the original successful report.
            await asyncio.sleep(0)
            pool.allow_report.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.5)
        finally:
            pool.allow_report.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(pool.outstanding, set(), "completed HTTP requests still need quota cleanup")
        self.assertEqual(pool.outcomes, ["success"], "preserve a completed successful HTTP result")
        self.assertEqual(pool.http_statuses, [200])
