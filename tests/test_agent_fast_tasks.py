import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from agent_fast_tasks import (
    AgentSummaryWorker,
    AgentTimingTracker,
    clean_generated_title,
    is_continuation_job,
    maybe_generate_title,
)


class FakeProgress:
    def __init__(self):
        self.parts = {}
        self.usage = {}


class FastTasksTests(unittest.IsolatedAsyncioTestCase):
    def test_clean_generated_title(self):
        self.assertEqual(clean_generated_title('“代码重构与性能优化。”'), '代码重构与性能优化')
        self.assertEqual(clean_generated_title('# **Hello World!**\nSecond line'), 'Hello World')
        self.assertEqual(len(clean_generated_title('一' * 40)), 16)

    def test_is_continuation_job(self):
        self.assertFalse(is_continuation_job({'id': 'job1', 'threadRoot': 'job1', 'history': []}))
        self.assertTrue(is_continuation_job({'id': 'job2', 'threadRoot': 'job1'}))
        self.assertTrue(is_continuation_job({'id': 'job2', 'parentMessage': 'msg1'}))
        self.assertTrue(is_continuation_job({'id': 'job2', 'history': [{'role': 'user', 'text': 'hi'}]}))

    async def test_maybe_generate_title_skips_continuation_and_existing_ai_title(self):
        with patch('agent_fast_tasks.call_fast_model', new=AsyncMock(return_value='新标题')) as call_mock:
            for job in [{'parentMessage': 'm1'}, {'titleSource': 'ai'}, {'titleSource': 'manual'}, {'replacesMessage': 'm1'}]:
                self.assertIsNone(await maybe_generate_title(None, None, None, {'id': 'job1', 'prompt': 'hi', **job},
                    {'index': 1}, endpoint='messages/m1', identity={'executionId': '1:1'}))
            call_mock.assert_not_called()

    async def test_maybe_generate_title_updates_new_conversation(self):
        post = AsyncMock(return_value={'titleUpdated': True})
        with patch('agent_fast_tasks.call_fast_model', new=AsyncMock(return_value='《Android草稿缓存修复》')), \
             patch('agent_fast_tasks._post_insights', new=post):
            title = await maybe_generate_title(
                None, None, None,
                {'id': 'job1', 'prompt': '帮我修复 Android 输入框草稿缓存问题'},
                {'inputText': '', 'index': 1}, endpoint='messages/m1', identity={'executionId': '1:1'},
            )
        self.assertEqual(title, 'Android草稿缓存修复')
        post.assert_awaited_once_with(None, 'messages/m1', {'executionId': '1:1'}, title=title)

    def test_timing_tracker_milestones_and_deltas(self):
        now = [100.0]
        tracker = AgentTimingTracker(clock=lambda: now[0], t0=100.0)
        now[0] = 101.5
        tracker.mark_dispatched()
        now[0] = 105.0
        tracker.record_step(0, 'thought', status='completed')
        now[0] = 109.25
        tracker.record_step(1, 'code_execution_call', label='bash', status='completed')
        now[0] = 115.0
        tracker.mark_completed('completed')

        data = tracker.to_dict()
        self.assertEqual(data['dispatchSeconds'], 1.5)
        self.assertEqual(data['firstReplySeconds'], 5.0)
        self.assertEqual(data['totalSeconds'], 15.0)
        self.assertEqual(len(data['steps']), 2)
        self.assertEqual(data['steps'][0]['elapsedSeconds'], 5.0)
        self.assertEqual(data['steps'][1]['elapsedSeconds'], 9.25)
        self.assertEqual(data['steps'][1]['deltaSeconds'], 4.25)

    async def test_completed_timing_flushes_without_waiting_for_the_throttle(self):
        tracker = AgentTimingTracker()
        finished = asyncio.Event()

        async def post(*args, **kwargs):
            if not finished.is_set():
                tracker.mark_completed('completed')
                finished.set()
            return {'titleUpdated': True}

        tracker.mark_dispatched()
        with patch('agent_fast_tasks._post_insights', new=AsyncMock(side_effect=post)) as mock:
            await asyncio.wait_for(tracker.pump(None, 'm1', {}, finished), timeout=0.5)
        self.assertEqual(mock.await_count, 2)
        self.assertEqual(mock.await_args.kwargs['agentTimings']['status'], 'completed')

    async def test_summary_worker_requires_new_steps_unless_forced(self):
        progress = FakeProgress()
        progress.usage['agentStepCount'] = 2
        progress.parts['step:0'] = {'sequence': 1, 'type': 'tool_call', 'toolName': 'git', 'status': 'completed', 'content': 'clone'}
        worker = AgentSummaryWorker(None, None, object(), {'id': 'j1', 'prompt': 'test'}, {'id': 'm1'}, progress, endpoint='m1', identity={})

        summary_json = json.dumps({
            'sentWhat': '测试任务',
            'actionsDone': ['克隆仓库'],
            'completed': ['完成克隆'],
            'issues': [],
            'summary': '进展顺利',
        }, ensure_ascii=False)

        patch_mock = AsyncMock(return_value={'titleUpdated': False})
        with patch('agent_fast_tasks.call_fast_model', new=AsyncMock(return_value=summary_json)), \
             patch('agent_fast_tasks._post_insights', new=patch_mock):
            first = await worker.summarize_once(force=False)
            self.assertIsNotNone(first)
            self.assertEqual(first['sentWhat'], '测试任务')
            # No new steps -> should skip
            second = await worker.summarize_once(force=False)
            self.assertIsNone(second)
            # New step -> should run
            progress.usage['agentStepCount'] = 3
            third = await worker.summarize_once(force=False)
            self.assertIsNotNone(third)


if __name__ == '__main__':
    unittest.main()
