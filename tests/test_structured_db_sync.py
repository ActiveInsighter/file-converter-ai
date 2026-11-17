import json
import unittest

import httpx

from agent_progress import MessageProgress
from gemini_agent import parse_interaction
from gemini_text import build_payload, generate, parse_answer
from key_pool import KeyPool
from response_capture import extract_structured_payload


class StructuredExtractionTests(unittest.TestCase):
    def test_generate_content_extracts_parts_tools_code_annotations_and_json(self):
        payload = {
            'candidates': [{
                'finishReason': 'STOP',
                'groundingMetadata': {
                    'groundingChunks': [
                        {'web': {'uri': 'https://ai.google.dev/docs', 'title': 'Gemini Docs'}},
                        {'web': {'uri': 'https://ai.google.dev/docs', 'title': 'Gemini Docs'}},
                    ]
                },
                'urlContextMetadata': {
                    'urlMetadata': [{'retrievedUrl': 'https://example.com/spec'}]
                },
                'content': {
                    'parts': [
                        {'thought': True, 'text': 'Plan step 1', 'thoughtSignature': 'secret-sig'},
                        {'executableCode': {'id': 'ec1', 'language': 'PYTHON', 'code': 'print(6 * 7)'}},
                        {'codeExecutionResult': {'id': 'ec1', 'outcome': 'OUTCOME_OK', 'output': '42\n'}},
                        {'toolCall': {'id': 'tc1', 'toolType': 'google_search', 'args': {'query': 'gemini'}}},
                        {'toolResponse': {'id': 'tc1', 'toolType': 'google_search', 'response': {'hits': 3}}},
                        {'functionCall': {'id': 'fc1', 'name': 'fetch_doc', 'args': {'id': 9}}},
                        {'functionResponse': {'id': 'fc1', 'name': 'fetch_doc', 'response': {'ok': True}}},
                        {'text': '```json\n{"answer": 42, "token": "fixture-secret"}\n```'},
                    ]
                },
            }],
            'usageMetadata': {
                'promptTokenCount': 10,
                'candidatesTokenCount': 20,
                'thoughtsTokenCount': 5,
                'toolUsePromptTokenCount': 4,
                'totalTokenCount': 39,
            },
        }
        ans = parse_answer(payload, secrets=['fixture-secret'])
        self.assertEqual(ans['protocol'], 'generateContent')
        self.assertEqual(ans['thinkingSummary'], 'Plan step 1')
        self.assertEqual(
            [p['type'] for p in ans['parts']],
            ['reasoning', 'server_tool', 'server_tool', 'tool', 'text'],
        )
        self.assertEqual(ans['parts'][1]['toolName'], 'code_execution')
        self.assertEqual(ans['parts'][1]['status'], 'completed')
        self.assertEqual(ans['parts'][2]['toolName'], 'google_search')
        self.assertEqual(ans['parts'][2]['output'], {'hits': 3})
        self.assertEqual(len(ans['codeExecutions']), 1)
        self.assertEqual(ans['codeExecutions'][0]['output'], '42\n')
        self.assertEqual(len(ans['toolCalls']), 3)
        self.assertEqual(
            [a['url'] for a in ans['annotations']],
            ['https://ai.google.dev/docs', 'https://example.com/spec'],
        )
        self.assertEqual(ans['structuredOutput']['answer'], 42)
        self.assertNotIn('secret-sig', json.dumps(ans))
        self.assertNotIn('fixture-secret', json.dumps(ans['parts']))
        self.assertNotIn('fixture-secret', json.dumps(ans['response']))
        self.assertNotIn('fixture-secret', json.dumps(ans['structuredOutput']))

    def test_interactions_extracts_server_tools_code_annotations_and_outputs_fallback(self):
        data = {
            'status': 'completed',
            'steps': [
                {'type': 'thought', 'signature': 'sig-xyz', 'summary': [{'type': 'text', 'text': 'Checking repo'}]},
                {'type': 'google_search_call', 'id': 'gs1', 'arguments': {'queries': ['rikkahub']}},
                {'type': 'google_search_result', 'call_id': 'gs1', 'results': [{'url': 'https://example.org'}]},
                {'type': 'code_execution_call', 'id': 'ce1', 'language': 'PYTHON', 'code': 'print("ok")'},
                {'type': 'code_execution_result', 'call_id': 'ce1', 'stdout': 'ok\n', 'exit_code': 0},
                {
                    'type': 'model_output',
                    'content': [{
                        'type': 'text',
                        'text': 'All done',
                        'annotations': [{'title': 'Example', 'url': 'https://example.org'}],
                    }],
                },
            ],
            'usage': {'total_input_tokens': 50, 'total_output_tokens': 30, 'total_thought_tokens': 12, 'total_tokens': 92},
        }
        res = parse_interaction(data)
        self.assertEqual(res['protocol'], 'interactions')
        self.assertEqual(res['thinkingSummary'], 'Checking repo')
        self.assertEqual([p['type'] for p in res['parts']], ['reasoning', 'server_tool', 'server_tool', 'text'])
        self.assertEqual(res['parts'][1]['toolName'], 'google_search')
        self.assertEqual(res['parts'][2]['toolName'], 'code_execution')
        self.assertEqual(res['codeExecutions'][0]['stdout'], 'ok\n')
        self.assertEqual(res['annotations'], [{'type': 'url_citation', 'title': 'Example', 'url': 'https://example.org'}])
        self.assertNotIn('sig-xyz', json.dumps(res))

        # Older/outputs shape fallback
        out_only = extract_structured_payload('interactions', {'outputs': [{'type': 'text', 'text': '{"k":1}'}]})
        self.assertEqual(out_only['structuredOutput'], {'k': 1})

    def test_build_payload_supports_tools_and_structured_output_schema(self):
        payload = build_payload(
            {
                'model': 'gemini-3.5-flash-lite',
                'prompt': 'extract',
                'options': {
                    'maxOutputTokens': 4096,
                    'thinkingLevel': 'high',
                    'responseMimeType': 'application/json',
                    'responseSchema': {'type': 'OBJECT', 'properties': {'name': {'type': 'STRING'}}},
                    'tools': [{'googleSearch': {}}, {'codeExecution': {}}],
                },
            },
            {'inputText': 'hello'},
        )
        self.assertEqual(payload['generationConfig']['responseMimeType'], 'application/json')
        self.assertIn('responseSchema', payload['generationConfig'])
        self.assertEqual(payload['tools'], [{'googleSearch': {}}, {'codeExecution': {}}])


class StreamingAndProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_preserves_code_execution_thinking_and_grounding(self):
        progress = MessageProgress()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                chunks = [
                    {'candidates': [{'content': {'parts': [{'thought': True, 'text': 'Thinking...'}]}}]},
                    {'candidates': [{'content': {'parts': [{'executableCode': {'id': 'c1', 'language': 'PYTHON', 'code': 'print(1+1)'}}]}}]},
                    {'candidates': [{'content': {'parts': [{'codeExecutionResult': {'id': 'c1', 'outcome': 'OUTCOME_OK', 'output': '2'}}]}}]},
                    {
                        'candidates': [{
                            'finishReason': 'STOP',
                            'groundingMetadata': {'groundingChunks': [{'web': {'uri': 'https://docs.python.org', 'title': 'Python'}}]},
                            'content': {'parts': [{'text': 'Result is 2'}]},
                        }],
                        'usageMetadata': {'promptTokenCount': 5, 'candidatesTokenCount': 6, 'thoughtsTokenCount': 3, 'totalTokenCount': 14},
                    },
                ]
                for c in chunks:
                    yield ('data: ' + json.dumps(c) + '\n\n').encode()

        async def outbound(request):
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=Stream())

        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            result = await generate(
                client,
                KeyPool(['key'], rpm_per_key=1e6, rpd_per_key=100),
                {'model': 'gemini-3.5-flash-lite', 'options': {'maxOutputTokens': 4096}},
                {'inputText': '1+1?'},
                progress=progress,
            )

        self.assertEqual(result['reply'], 'Result is 2')
        self.assertEqual(result['thinkingSummary'], 'Thinking...')
        self.assertEqual([p['type'] for p in result['parts']], ['reasoning', 'server_tool', 'text'])
        self.assertEqual(len(result['codeExecutions']), 1)
        self.assertEqual(result['annotations'][0]['url'], 'https://docs.python.org')
        self.assertEqual(progress.parts['answer']['content'], 'Result is 2')
        self.assertEqual(progress.parts['reasoning']['content'], 'Thinking...')
        self.assertEqual(progress.parts['step:0']['toolKind'], 'server')
        self.assertEqual(progress.usage['thoughtsTokenCount'], 3)

    async def test_progress_keys_have_unique_stable_sequences_and_bound_payloads(self):
        writes = []
        async def post(body):
            writes.append(body)
            return {}
        p = MessageProgress(post=post)
        p.put('reasoning', 'reasoning_summary', 'Plan')
        p.put('step:0', 'tool_call', 'call', payload={'code': 'x' * 300000})
        p.put('step:0:annotations', 'annotations', '[]')
        p.put('step:1', 'tool_result', 'done')
        initial = {key: part['sequence'] for key, part in p.parts.items()}
        p.put('step:0', 'tool_call', 'updated', payload={'code': 'print(2)'})
        self.assertEqual(initial, {key: part['sequence'] for key, part in p.parts.items()})
        self.assertEqual(len(set(initial.values())), len(initial))
        await p.flush()
        self.assertFalse(p.dirty)
        self.assertLess(len(json.dumps(writes[0]).encode()), 256 * 1024)

    async def test_flush_skips_empty_heartbeat_and_consumes_delta_without_start(self):
        writes = []

        async def post(body):
            writes.append(body)
            return {}

        p = MessageProgress(post=post)
        await p.flush()
        self.assertEqual(len(writes), 0)

        p.consume({'index': 0, 'delta': {'type': 'arguments_delta', 'arguments': '{"q":'}}, 'step.delta', 'e1')
        p.consume({'index': 0, 'delta': {'type': 'arguments_delta', 'arguments': '"ok"}'}}, 'step.delta', 'e2')
        p.consume({'index': 0, 'delta': {'type': 'text_annotation_delta', 'annotations': [{'title': 'Site', 'url': 'https://a.test'}]}}, 'step.delta', 'e3')
        await p.flush()
        self.assertEqual(len(writes), 1)
        self.assertEqual(p.parts['step:0']['toolKind'], 'client')
        self.assertIn('https://a.test', p.parts['step:0:annotations']['content'])


if __name__ == '__main__':
    unittest.main()
