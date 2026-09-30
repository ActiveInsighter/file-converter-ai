"""Real PDF, local HTTP gateway and actual converter orchestration."""
import asyncio
import json
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch, AsyncMock

import fitz
from pdf2md import async_main, parser


class ConversionE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_failure_does_not_publish_previous_run_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output'
            output.mkdir()
            (output / 'results.json').write_text(json.dumps([{'chunk': '001', 'status': 'ok'}]))
            (output / 'merged.md').write_text('Old unrelated document')
            def download(_url, destination):
                with fitz.open() as document:
                    document.new_page().insert_text((72, 72), 'New document')
                    document.save(destination)
            args = parser().parse_args(['--source-url', 'https://example.test/source.pdf',
                                       '--output-dir', str(output), '--work-dir', str(root / 'work')])
            with patch.dict(os.environ, {'GEMINI_API_KEYS': 'fake'}), patch('pdf2md.download_pdf', download), \
                    patch('pdf2md.ProjectQuotaPool.create', AsyncMock(side_effect=RuntimeError('quota unavailable'))):
                self.assertEqual(await async_main(args), 2)
            manifest = json.loads((output / 'manifest.json').read_text())
            self.assertEqual(manifest['completed_chunks'], 0)
            self.assertEqual(manifest['results'], [])
            self.assertFalse((output / 'merged.md').exists())

    async def test_slow_page_is_hedged_and_artifacts_remain_ordered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with fitz.open() as doc:
                for index in range(4):
                    page = doc.new_page()
                    if index != 3:
                        page.insert_text((72, 72), f'Page {index + 1}')
                doc.save(root / 'source.pdf')
            counts = {}
            lock = threading.Lock()
            class Gateway(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    data = (root / 'source.pdf').read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    text = body['messages'][0]['content'][0]['text']
                    page = int(re.search(r'第 (\d+) 页', text).group(1))
                    with lock:
                        counts[page] = counts.get(page, 0) + 1
                        attempt = counts[page]
                    time.sleep(0.3 if page == 2 and attempt == 1 else 0.015)
                    data = json.dumps({'choices': [{'finish_reason': 'stop',
                                       'message': {'content': f'# Page {page}\n\nSource text.'}}]}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    try:
                        self.wfile.write(data)
                    except BrokenPipeError:
                        pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            args = parser().parse_args([
                '--provider', 'modelflare', '--model', 'test-vision', '--api-base', base + '/v1',
                '--source-url', base + '/source.pdf', '--work-dir', str(root / 'work'),
                '--output-dir', str(root / 'output'), '--dpi', '72', '--concurrency', '4',
                '--rpm-per-key', '6000', '--hedge-after', '0.05', '--hedge-budget', '2'])
            try:
                with patch.dict(os.environ, {'MODELFLARE_API_KEYS': 'fake-1\nfake-2\nfake-3\nfake-4'}):
                    result = await asyncio.wait_for(async_main(args), 5)
            finally:
                await asyncio.to_thread(server.shutdown)
                server.server_close()
            self.assertEqual(result, 0)
            progress = json.loads((root / 'output/progress.json').read_text())
            manifest = json.loads((root / 'output/manifest.json').read_text())
            quota = json.loads((root / 'output/quota-usage.json').read_text())
            merged = (root / 'output/merged.md').read_text()
            self.assertEqual(progress['percent'], 100)
            self.assertEqual(progress['state'], 'completed')
            self.assertEqual(manifest['blank_pages'], [4])
            self.assertEqual(manifest['failures'], 0)
            self.assertLess(merged.index('Page 1'), merged.index('Page 2'))
            self.assertLess(merged.index('Page 2'), merged.index('Page 3'))
            self.assertEqual(quota['speculation']['hedge_wins'], 1)
            self.assertLessEqual(sum(counts.values()), 5)
            self.assertGreaterEqual(sum(k['cancelled'] for k in quota['keys'].values()), 1)
