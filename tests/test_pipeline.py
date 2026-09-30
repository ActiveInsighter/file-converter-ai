import asyncio
import tempfile
import unittest
from pathlib import Path

from pipeline import Progress, schedule_chunks
from request_policy import RequestPolicy


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_starts_conversion_before_rendering_finishes(self):
        converted = asyncio.Event()
        async def source():
            yield '1'
            await asyncio.wait_for(converted.wait(), 0.5)
            yield '2'
        async def run(chunk, round_number):
            converted.set()
            return {'chunk': chunk, 'status': 'ok'}
        with tempfile.TemporaryDirectory() as root:
            progress = Progress(Path(root), total_chunks=2, total_pages=2)
            results = await schedule_chunks(source(), run, 1, 1, progress)
        self.assertEqual([r['chunk'] for r in results], ['1', '2'])

    async def test_failed_page_retries_while_another_page_is_still_running(self):
        recovered = asyncio.Event()
        async def source():
            yield '1'
            yield '2'
        async def run(chunk, round_number):
            if chunk == '1' and round_number == 1:
                return {'chunk': chunk, 'status': 'failed', 'retryable': True}
            if chunk == '2':
                await asyncio.wait_for(recovered.wait(), 0.5)
            else:
                recovered.set()
            return {'chunk': chunk, 'status': 'ok'}
        with tempfile.TemporaryDirectory() as root:
            progress = Progress(Path(root), total_chunks=2, total_pages=2)
            results = await schedule_chunks(source(), run, 2, 2, progress, retry_delay=lambda _: 0)
            snapshot = progress.snapshot()
        self.assertTrue(all(r['status'] == 'ok' for r in results))
        self.assertEqual(snapshot['percent'], 100)
        self.assertEqual(snapshot['failed_chunks'], 0)
        self.assertEqual(snapshot['retrying_chunks'], 0)

    async def test_producer_failure_preserves_completed_results(self):
        async def source():
            yield '1'
            raise ValueError('broken render')
        async def run(chunk, round_number):
            return {'chunk': chunk, 'status': 'ok'}
        with tempfile.TemporaryDirectory() as root:
            progress = Progress(Path(root), total_chunks=2, total_pages=2)
            with self.assertRaisesRegex(ValueError, 'broken render'):
                await schedule_chunks(source(), run, 1, 1, progress)
            self.assertEqual(progress.snapshot()['state'], 'failed')
            self.assertTrue((Path(root) / 'progress.json').exists())


class HedgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_response_is_kept_when_reporting_crosses_http_deadline(self):
        calls = []
        async def attempt(started):
            index = len(calls)
            calls.append(index)
            started()
            if index:
                await asyncio.Event().wait()
            await asyncio.sleep(0.03)
            policy.mark_validated()
            # The real converter separately bounds report cleanup to 8 seconds.
            await asyncio.sleep(0.05)
            return 'valid before deadline'
        policy = RequestPolicy(concurrency=3, hedge_after=0.02, hedge_budget=2)
        result = await asyncio.wait_for(policy.run(attempt, '051', max_duration=0.06), 0.3)
        self.assertEqual(result, 'valid before deadline')

    async def test_slow_duplicates_cannot_extend_primary_total_deadline(self):
        calls = []
        cancelled = []
        async def attempt(started):
            index = len(calls)
            calls.append(index)
            started()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(index)
                raise
        policy = RequestPolicy(concurrency=3, hedge_after=0.02, hedge_budget=2)
        with self.assertRaisesRegex(RuntimeError, 'deadline'):
            await asyncio.wait_for(policy.run(attempt, '051', max_duration=0.06), 0.3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sorted(cancelled), [0, 1, 2])

    async def test_slow_request_uses_first_valid_duplicate_and_cancels_losers(self):
        calls = []
        cancelled = []
        async def attempt(started):
            index = len(calls)
            calls.append(index)
            started()
            try:
                if index == 0:
                    await asyncio.Event().wait()
                if index == 1:
                    raise RuntimeError('invalid output')
                return 'valid markdown'
            except asyncio.CancelledError:
                cancelled.append(index)
                raise
        policy = RequestPolicy(concurrency=3, hedge_after=0.01, hedge_budget=2)
        result = await asyncio.wait_for(policy.run(attempt, '051'), 0.5)
        self.assertEqual(result, 'valid markdown')
        self.assertEqual(len(calls), 3)
        self.assertEqual(cancelled, [0])
        self.assertEqual(policy.summary()['hedge_wins'], 1)

    async def test_time_waiting_for_quota_does_not_trigger_duplicate(self):
        calls = []
        async def attempt(started):
            calls.append(1)
            await asyncio.sleep(0.03)
            started()
            return 'done'
        policy = RequestPolicy(concurrency=3, hedge_after=0.01, hedge_budget=2)
        self.assertEqual(await policy.run(attempt, '1'), 'done')
        self.assertEqual(len(calls), 1)

    async def test_extra_requests_obey_budget_and_concurrency(self):
        gate = asyncio.Event()
        active = 0
        peak = 0
        calls = 0
        async def attempt(started):
            nonlocal active, peak, calls
            calls += 1
            active += 1
            peak = max(peak, active)
            started()
            try:
                if calls <= 2:
                    await gate.wait()
                return 'done'
            finally:
                active -= 1
        policy = RequestPolicy(concurrency=2, hedge_after=0.01, hedge_budget=1)
        tasks = [asyncio.create_task(policy.run(attempt, str(i))) for i in range(2)]
        await asyncio.sleep(0.03)
        gate.set()
        self.assertEqual(await asyncio.gather(*tasks), ['done', 'done'])
        self.assertLessEqual(peak, 2)
        self.assertLessEqual(calls, 3)
        self.assertEqual(active, 0)

    async def test_capacity_errors_suspend_speculation(self):
        calls = []
        async def attempt(started):
            calls.append(1)
            started()
            await asyncio.sleep(0.03)
            return 'done'
        policy = RequestPolicy(concurrency=3, hedge_after=0.01, hedge_budget=2,
                               can_hedge=lambda: False)
        self.assertEqual(await policy.run(attempt, '1'), 'done')
        self.assertEqual(len(calls), 1)
