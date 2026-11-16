"""Exercise download -> OCR -> recovered crop -> HTTP vision calls -> package."""
import asyncio
import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import fitz

from pdf2md import async_main, parser
from prompts import DEFAULT_SVG_PROMPT


class SvgConversionE2E(unittest.IsolatedAsyncioTestCase):
    async def test_recovered_image_reaches_independent_svg_gateway_and_fallback_is_packaged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with fitz.open() as doc:
                page = doc.new_page(width=600, height=800)
                page.draw_rect((50, 80, 150, 180), color=(1, 0, 0))
                page.draw_line((150, 130), (170, 130), color=(0, 0, 1))
                page.draw_rect((170, 80, 270, 180), color=(0, 1, 0))
                page.insert_text((50, 60), 'Flow diagram')
                page.insert_text((50, 360), 'Independent illustration')
                raster = page.get_pixmap(clip=fitz.Rect(50, 400, 150, 500)).tobytes('png')
                doc.save(root / 'source.pdf')
            sources = ['imgs/img_in_image_box_50_80_150_180.png',
                       'imgs/img_in_image_box_170_80_270_180.png',
                       'imgs/img_in_image_box_50_400_150_500.png']
            requests, svg_images, md_drafts = [], [], []
            valid_svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 220 100"><rect x="0" y="0" width="100" height="100"/><path d="M100 50L120 50"/><rect x="120" y="0" width="100" height="100"/></svg>'

            class Server(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def reply(self, payload, mime='application/json'):
                    if not isinstance(payload, bytes):
                        payload = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', mime)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def do_GET(self):
                    requests.append(self.path)
                    if self.path == '/source.pdf':
                        return self.reply((root / 'source.pdf').read_bytes(), 'application/pdf')
                    if self.path == '/figure/2':
                        return self.reply(raster, 'image/png')
                    if self.path == '/ocr/jobs/job':
                        return self.reply({'code': 0, 'data': {'state': 'done', 'resultUrl': {'jsonUrl': base + '/ocr/result'}}})
                    if self.path == '/ocr/result':
                        text = '\n'.join(f'<img src="{src}" />' for src in sources[:2])
                        text += '\n\n图题与正文必须保留\n\n' + f'<img src="{sources[2]}" />'
                        return self.reply(json.dumps({'result': {'layoutParsingResults': [{
                            'prunedResult': {'width': 600, 'height': 800},
                            'markdown': {'text': text, 'images': {src: base + f'/figure/{i}' for i, src in enumerate(sources)}},
                        }]}}).encode())
                    self.send_error(404)

                def do_POST(self):
                    requests.append(self.path)
                    data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if self.path == '/ocr/jobs':
                        assert data['fileUrl'] == base + '/source.pdf'
                        return self.reply({'code': 0, 'data': {'jobId': 'job'}})
                    if self.path == '/md/chat/completions':
                        assert self.headers['Authorization'] == 'Bearer md-key'
                        assert data['model'] == 'test-md'
                        prompt = data['messages'][0]['content'][0]['text']
                        assert 'images/p001-merged-1.png' in prompt
                        assert sources[0] not in prompt
                        md_drafts.append(prompt)
                        return self.reply({'choices': [{'finish_reason': 'stop', 'message': {'content':
                            '![流程](images/p001-merged-1.png)\n\n图题与正文必须保留\n\n![插图](images/p001-3.png)'}}]})
                    assert self.path == '/svg/test-svg:generateContent'
                    assert self.headers['x-goog-api-key'] == 'svg-key'
                    parts = data['contents'][0]['parts']
                    assert parts[1]['mediaResolution'] == {'level': 'MEDIA_RESOLUTION_ULTRA_HIGH'}
                    assert parts[0]['text'] == DEFAULT_SVG_PROMPT
                    encoded = next(part['inlineData']['data'] for part in parts if 'inlineData' in part)
                    pixels = fitz.Pixmap(base64.b64decode(encoded))
                    svg_images.append((pixels.width, pixels.height))
                    # Only the recovered wide diagram succeeds. The separate
                    # image deliberately fails SVG validation and keeps PNG.
                    answer = valid_svg if pixels.width > 2 * pixels.height else '<svg></svg>'
                    return self.reply({'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': answer}]}}]})

            server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f'http://127.0.0.1:{server.server_port}'
            args = parser().parse_args([
                '--source-url', base + '/source.pdf', '--provider', 'modelflare', '--model', 'test-md',
                '--api-base', base + '/md', '--ocr-url', base + '/ocr/jobs',
                '--work-dir', str(root / 'work'), '--output-dir', str(root / 'output'), '--dpi', '72',
                '--attempts-per-page', '1', '--hedge-budget', '0', '--rpm-per-key', '60000',
                '--image-config', json.dumps({'jpeg_quality': 90, 'svg': {'enabled': True, 'provider': 'gemini', 'model': 'test-svg', 'api_base': base + '/svg'}}),
            ])
            try:
                with patch.dict(os.environ, {'MODELFLARE_API_KEYS': 'md-key', 'GEMINI_API_KEYS': 'svg-key',
                                             'GEMINI_KEY_GROUPS': '', 'PADDLEOCR_TOKENS': 'ocr-key'}), \
                        patch('paddle_ocr.POLL_SECONDS', 0):
                    result = await asyncio.wait_for(async_main(args), 15)
            finally:
                await asyncio.to_thread(server.shutdown)
                server.server_close()
            self.assertEqual(result, 0)
            self.assertEqual(len(md_drafts), 1)
            self.assertEqual(len(svg_images), 2)
            self.assertNotIn('/figure/0', requests)
            self.assertNotIn('/figure/1', requests)
            output = root / 'output'
            merged = (output / 'merged.md').read_text()
            self.assertIn('![流程](images/p001-merged-1.svg)', merged)
            self.assertIn('![插图](images/p001-3.png)', merged)
            self.assertIn('图题与正文必须保留', merged)
            self.assertIn('../images/p001-merged-1.svg', (output / 'pages/001.md').read_text())
            self.assertEqual((output / 'images/p001-merged-1.svg').read_text().strip(), valid_svg)
            self.assertTrue((output / 'images/p001-merged-1.png').is_file())
            self.assertTrue((output / 'images/p001-3.png').is_file())
            usage = json.loads((root / 'work/conversion/quota-usage.json').read_text())
            self.assertEqual(usage['svg']['succeeded'], 1)
            self.assertEqual(usage['svg']['failed'], 1)
            self.assertFalse(usage['svg']['shared_quota_pool'])
            self.assertEqual(usage['svg']['quota']['performance']['success'], 2)
