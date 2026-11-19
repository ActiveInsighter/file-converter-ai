import asyncio
import base64
import json
import unittest

import httpx

from gemini_agent import build_agent_payload
from gemini_text import build_payload
from image_fetch import collect_job_image_urls, extract_image_markers, fetch_images_as_base64


class ImageFetchTests(unittest.TestCase):
    def test_extract_image_markers(self):
        text = '请看这两张图\n\n<!-- aw:image:https://file.any1.tech/api/library/images/sources/a.png -->\n<!-- aw:image:https://file.any1.tech/api/library/images/sources/b.jpg -->'
        clean, urls = extract_image_markers(text)
        self.assertEqual(clean, '请看这两张图')
        self.assertEqual(urls, [
            'https://file.any1.tech/api/library/images/sources/a.png',
            'https://file.any1.tech/api/library/images/sources/b.jpg',
        ])

    def test_collect_job_image_urls_deduplicates_and_respects_history(self):
        job = {
            'prompt': '看图 <!-- aw:image:https://example.com/1.png -->',
            'history': [
                {'role': 'user', 'text': '历史 <!-- aw:image:https://example.com/old.png -->'},
            ],
        }
        message = {'inputText': '补充 <!-- aw:image:https://example.com/1.png -->'}
        self.assertEqual(
            collect_job_image_urls(job, message, include_history=True),
            ['https://example.com/old.png', 'https://example.com/1.png'],
        )
        self.assertEqual(
            collect_job_image_urls(job, message, include_history=False),
            ['https://example.com/1.png'],
        )

    def test_fetch_images_as_base64_parallel(self):
        png_bytes = b'\x89PNG\r\n\x1a\nfake'
        jpg_bytes = b'\xff\xd8\xff\xe0fake'
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if str(request.url).endswith('.png'):
                return httpx.Response(200, content=png_bytes, headers={'content-type': 'image/png'})
            return httpx.Response(200, content=jpg_bytes, headers={'content-type': 'image/jpeg'})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await fetch_images_as_base64(client, [
                    'https://cdn.example/a.png',
                    'https://cdn.example/b.jpg',
                    'https://cdn.example/a.png',
                ])

        result = asyncio.run(run())
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['https://cdn.example/a.png'], ('image/png', base64.b64encode(png_bytes).decode('ascii')))
        self.assertEqual(result['https://cdn.example/b.jpg'], ('image/jpeg', base64.b64encode(jpg_bytes).decode('ascii')))

    def test_build_agent_payload_with_images_single_turn_and_history(self):
        resolved = {
            'https://cdn.example/1.png': ('image/png', 'QUFB'),
            'https://cdn.example/old.jpg': ('image/jpeg', 'QkJC'),
        }
        single = build_agent_payload(
            {'model': 'Antigravity', 'prompt': '分析图片 <!-- aw:image:https://cdn.example/1.png -->'},
            {'inputText': ''},
            resolved_images=resolved,
        )
        self.assertEqual(single['input'], [
            {'type': 'text', 'text': '分析图片'},
            {'type': 'image', 'mime_type': 'image/png', 'data': 'QUFB'},
        ])

        with_history = build_agent_payload(
            {
                'model': 'Antigravity',
                'prompt': '继续 <!-- aw:image:https://cdn.example/1.png -->',
                'history': [
                    {'role': 'user', 'text': '上一轮 <!-- aw:image:https://cdn.example/old.jpg -->'},
                    {'role': 'model', 'text': '好的'},
                ],
            },
            {'inputText': ''},
            resolved_images=resolved,
        )
        self.assertEqual(with_history['input'], [
            {'type': 'user_input', 'content': [
                {'type': 'text', 'text': '上一轮'},
                {'type': 'image', 'mime_type': 'image/jpeg', 'data': 'QkJC'},
            ]},
            {'type': 'model_output', 'content': [{'type': 'text', 'text': '好的'}]},
            {'type': 'user_input', 'content': [
                {'type': 'text', 'text': '继续'},
                {'type': 'image', 'mime_type': 'image/png', 'data': 'QUFB'},
            ]},
        ])

    def test_build_generate_content_payload_with_images(self):
        resolved = {'https://cdn.example/1.png': ('image/png', 'QUFB')}
        payload = build_payload(
            {'model': 'gemini-2.5-flash', 'prompt': '看图 <!-- aw:image:https://cdn.example/1.png -->'},
            {'inputText': ''},
            resolved_images=resolved,
        )
        parts = payload['contents'][0]['parts']
        self.assertEqual(parts, [
            {'text': '看图'},
            {'inlineData': {'mimeType': 'image/png', 'data': 'QUFB'}},
        ])


if __name__ == '__main__':
    unittest.main()
