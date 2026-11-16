import json
import unittest
from unittest.mock import patch
from response_capture import generate_content_snapshot, interaction_snapshot, MAX_BYTES
from gemini_text import build_payload, parse_answer
from gemini_agent import parse_interaction


class CaptureTests(unittest.TestCase):
    def test_content_preserves_public_thought_tools_citations_safety_and_usage(self):
        source = {'responseId': 'r1', 'modelVersion': 'gemini-test', 'usageMetadata': {'totalTokenCount': 30, 'toolUsePromptTokenCount': 4},
                  'candidates': [{'finishReason': 'STOP', 'safetyRatings': [{'category': 'safe'}],
                    'groundingMetadata': {'groundingChunks': [{'web': {'uri': 'https://example.com', 'title': 'Source'}}]},
                    'content': {'parts': [{'thought': True, 'text': 'Public summary', 'thoughtSignature': 'private signature'},
                      {'functionCall': {'id': 'call1', 'name': 'lookup', 'args': {'q': 'test'}}},
                      {'functionResponse': {'id': 'call1', 'name': 'lookup', 'response': {'value': 42}}},
                      {'executableCode': {'language': 'PYTHON', 'code': 'print(42)'}},
                      {'codeExecutionResult': {'outcome': 'OUTCOME_OK', 'output': '42'}}, {'text': 'Answer'}]}}]}
        answer = parse_answer(source)
        response = answer['response']
        self.assertEqual(answer['reply'], 'Answer')
        self.assertEqual(answer['usage']['toolUseTokenCount'], 4)
        self.assertEqual([p['type'] for p in response['parts']], ['reasoning', 'tool-call', 'tool-result', 'tool-call', 'tool-result', 'text'])
        self.assertEqual(response['parts'][1]['data']['args']['q'], 'test')
        self.assertIn('groundingMetadata', response['metadata']['candidates'][0])
        self.assertNotIn('private signature', json.dumps(response))

    def test_agent_keeps_order_and_association_without_raw_thought_or_credentials(self):
        with patch.dict('os.environ', {'ANTIGRAVITY_GITHUB_TOKEN': 'fixture-super-secret'}):
            result = parse_interaction({'status': 'completed', 'steps': [
                {'type': 'thought', 'signature': 'private', 'content': 'never expose', 'summary': [{'type': 'text', 'text': 'Public summary'}]},
                {'type': 'code_execution_call', 'id': 'c1', 'code': 'print(1)', 'headers': {'Authorization': 'secret'}},
                {'type': 'code_execution_result', 'call_id': 'c1', 'output': 'fixture-super-secret 1'},
                {'type': 'model_output', 'content': [{'type': 'text', 'text': 'Done'}]},
            ], 'usage': {'total_tokens': 123}})
        response = result['response']
        self.assertEqual([p['type'] for p in response['parts']], ['reasoning', 'tool-call', 'tool-result', 'text'])
        self.assertEqual(response['parts'][2]['callId'], 'c1')
        raw = json.dumps(response)
        for secret in ('never expose', 'fixture-super-secret', 'private', 'Authorization'):
            self.assertNotIn(secret, raw)
        self.assertIn('[REDACTED]', raw)
        self.assertEqual(result['usage']['totalTokenCount'], 123)

    def test_oversized_traces_are_bounded_with_explicit_omissions(self):
        data = {'steps': [{'type': 'function_call', 'name': 'shell', 'arguments': '中' * 100000} for _ in range(600)]}
        response = interaction_snapshot(data)
        self.assertLessEqual(len(json.dumps(response, ensure_ascii=False).encode()), MAX_BYTES)
        self.assertTrue(response['capture']['truncated'])
        self.assertGreater(response['capture']['omittedParts'], 0)
        self.assertEqual(response['capture']['totalParts'], len(response['parts']) + response['capture']['omittedParts'])

    def test_empty_summary_is_explicit_and_followup_has_persisted_history(self):
        response = interaction_snapshot({'steps': [{'type': 'thought', 'signature': 'private'}]})
        self.assertEqual(response['parts'][0]['text'], '')
        payload = build_payload({'model': 'gemini-3.5-flash-lite', 'prompt': 'continue',
            'history': [{'role': 'user', 'text': 'old question'}, {'role': 'model', 'text': 'old answer'}],
            'options': {'maxOutputTokens': 8192, 'thinkingLevel': ''}}, {})
        self.assertTrue(payload['generationConfig']['thinkingConfig']['includeThoughts'])
        self.assertEqual([t['role'] for t in payload['contents']], ['user', 'model', 'user'])

    def test_generation_is_non_streaming_and_agent_no_longer_observes_sse(self):
        from pathlib import Path
        self.assertNotIn('watch_interaction(', Path('gemini_agent.py').read_text())
        self.assertNotIn('streamGenerateContent', Path('gemini_text.py').read_text())
