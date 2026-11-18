import json
import unittest
from unittest.mock import AsyncMock, MagicMock

import fast_model
from agent_progress import MessageProgress


class FastModelTests(unittest.IsolatedAsyncioTestCase):
    def test_should_generate_title_only_for_fresh_turn(self):
        self.assertTrue(fast_model.should_generate_title({'id': 'job1', 'threadRoot': 'job1', 'prompt': '帮我分析一下代码'}))
        self.assertFalse(fast_model.should_generate_title({'id': 'job1', 'titleGenerated': True, 'prompt': 'hello'}))
        self.assertFalse(fast_model.should_generate_title({'id': 'job2', 'parentMessage': 'msg1', 'prompt': '继续'}))
        self.assertFalse(fast_model.should_generate_title({'id': 'job2', 'history': [{'role': 'user', 'text': 'hi'}], 'prompt': '继续'}))
        self.assertFalse(fast_model.should_generate_title({'id': 'job2', 'threadRoot': 'job1', 'prompt': '继续'}))
        self.assertFalse(fast_model.should_generate_title({'id': 'job1', 'prompt': '   '}))

    def test_sanitize_title_clamps_and_strips(self):
        self.assertEqual(fast_model.sanitize_title('「Kotlin 协程优化方案」。'), 'Kotlin 协程优化方案')
        long_title = '这是一个非常非常非常非常非常非常非常非常非常长的标题用于测试截断逻辑'
        self.assertLessEqual(len(fast_model.sanitize_title(long_title)), 36)

    async def test_generate_title_uses_default_fast_model(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            'candidates': [{'content': {'parts': [{'text': '代码性能分析与优化'}]}}]
        }
        client = MagicMock()
        client.post = AsyncMock(return_value=response)

        title = await fast_model.generate_title(client, ['key-1'], '请帮我分析一下这段 Python 异步代码的性能瓶颈')
        self.assertEqual(title, '代码性能分析与优化')
        called_url = client.post.call_args.args[0]
        self.assertIn('gemini-3.5-flash-lite:generateContent', called_url)

    async def test_summarize_agent_progress_parses_structured_json(self):
        payload = {
            'overview': '已完成代码检索，正在修改界面',
            'sentSummary': '用户请求新增快速模型总结与时间线弹窗',
            'actions': ['读取 AgentPage.kt', '修改 gemini_agent.py'],
            'completed': ['完成后端快速模型封装'],
            'issues': [],
        }
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            'candidates': [{'content': {'parts': [{'text': json.dumps(payload, ensure_ascii=False)}]}}]
        }
        client = MagicMock()
        client.post = AsyncMock(return_value=response)

        summary = await fast_model.summarize_agent_progress(
            client,
            ['key-1'],
            user_prompt='请帮我完成功能',
            history=[],
            parts={'step:0': {'sequence': 1, 'type': 'reasoning_summary', 'content': '正在分析需求', 'status': 'completed'}},
            step_count=1,
            status='running',
        )
        self.assertIsNotNone(summary)
        self.assertEqual(summary['overview'], payload['overview'])
        self.assertEqual(summary['actions'], payload['actions'])
        self.assertEqual(summary['model'], 'gemini-3.5-flash-lite')
        self.assertEqual(summary['stepCount'], 1)

    async def test_progress_tracks_timeline_and_summary(self):
        tick = [100.0]
        posted = []

        async def fake_post(body):
            posted.append(body)
            return {'ok': True}

        progress = MessageProgress(post=fake_post, job_id='job1', message_id='msg1', clock=lambda: tick[0])
        tick[0] = 101.25
        progress.mark_submitted()
        tick[0] = 104.8
        progress.put('step:0', 'reasoning_summary', '正在思考', status='completed')
        tick[0] = 112.4
        progress.put('step:1', 'tool_call', '{"cmd":"ls"}', status='completed', tool_name='code_execution')
        progress.set_summary({'overview': '正在执行', 'sentSummary': '测试', 'actions': ['ls'], 'completed': [], 'issues': []})
        await progress.flush()

        self.assertEqual(len(posted), 1)
        body = posted[0]
        self.assertEqual(body['summary']['overview'], '正在执行')
        self.assertEqual(body['timeline']['submittedOffsetMs'], 1250)
        self.assertEqual(body['timeline']['firstReplyOffsetMs'], 4800)
        self.assertEqual(len(body['timeline']['steps']), 2)
        self.assertEqual(body['timeline']['steps'][1]['deltaMs'], 7600)
