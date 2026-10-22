"""Real PDF, local OCR job API, local model gateway, actual orchestration."""
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
from unittest.mock import AsyncMock, patch

import fitz
import pdf2md
from pdf2md import async_main, parser


def one_page_pdf(text):
    def download(_url, destination):
        with fitz.open() as document:
            document.new_page().insert_text((72, 72), text)
            document.save(destination)
    return download


class ConversionE2ETests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / 'output'

    def args(self, *extra):
        return parser().parse_args([
            '--source-url', 'https://example.test/source.pdf', '--dpi', '72',
            '--output-dir', str(self.output), '--work-dir', str(self.root / 'work'), *extra,
        ])

    async def test_cancel_survives_diagnostic_write_failure_during_setup(self):
        setup_started = asyncio.Event()

        async def blocked_setup(*args, **kwargs):
            setup_started.set()
            await asyncio.Future()

        with patch.dict(os.environ, {'GEMINI_API_KEYS': 'fake', 'PADDLEOCR_TOKENS': ''}), \
                patch('pdf2md.download_pdf', one_page_pdf('Source page')), \
                patch('pdf2md.process_pages', blocked_setup), \
                patch('pdf2md.Progress.write', side_effect=OSError('diagnostic disk full')):
            task = asyncio.create_task(async_main(self.args('--output-name', 'notes')))
            await asyncio.wait_for(setup_started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        # The already published package remains available even when the
        # cancelled setup cannot write its private progress diagnostics.
        self.assertEqual({p.name for p in self.output.iterdir()}, {'notes.md'})
        self.assertIn('未完成页码：1', (self.output / 'notes.md').read_text())

    async def test_progress_publication_failure_cannot_override_cancellation(self):
        failed_refresh = asyncio.Event()
        actual_write = pdf2md.write_result
        publications = 0

        def publish(**options):
            nonlocal publications
            publications += 1
            if publications == 2:
                failed_refresh.set()
                raise OSError('Temporary output failure')
            return actual_write(**options)

        async def transcribe(*_args, **_kwargs):
            await asyncio.Future()

        actual_wait = asyncio.wait_for

        async def short_metrics_wait(awaitable, timeout):
            return await actual_wait(awaitable, 0.02 if timeout == 5 else timeout)

        with patch.dict(os.environ, {'MODELFLARE_API_KEYS': 'fake', 'PADDLEOCR_TOKENS': ''}), \
                patch('pdf2md.download_pdf', one_page_pdf('Wait for AI')), \
                patch('pdf2md.call_model', transcribe), \
                patch('pdf2md.write_result', publish), \
                patch('pdf2md.asyncio.wait_for', short_metrics_wait):
            task = asyncio.create_task(async_main(self.args('--provider', 'modelflare', '--model', 'test-vision')))
            await actual_wait(failed_refresh.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual({p.name for p in self.output.iterdir()}, {'merged.md'})

    async def test_missing_credentials_do_not_republish_old_custom_output(self):
        self.output.mkdir()
        (self.output / 'old-custom.md').write_text('Old unrelated document')
        with patch.dict(os.environ, {'GEMINI_API_KEYS': ''}), self.assertRaises(RuntimeError):
            await async_main(self.args())
        self.assertEqual(list(self.output.iterdir()), [])

    async def test_cancel_keeps_completed_pages_in_clean_partial_package(self):
        def download(_url, destination):
            with fitz.open() as document:
                for number in range(3):
                    document.new_page().insert_text((72, 72), f'Page {number + 1}')
                document.save(destination)

        blocked = asyncio.Event()

        async def transcribe(_client, _pool, _provider, _model, _parts, name, *_rest):
            if name == '001':
                return '# Kept page 1'
            blocked.set()
            await asyncio.Future()

        with patch.dict(os.environ, {'MODELFLARE_API_KEYS': 'fake', 'PADDLEOCR_TOKENS': ''}), \
                patch('pdf2md.download_pdf', download), patch('pdf2md.call_model', transcribe):
            task = asyncio.create_task(async_main(self.args(
                '--provider', 'modelflare', '--model', 'test-vision', '--concurrency', '1',
                '--output-name', 'notes')))
            await asyncio.wait_for(blocked.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        merged = (self.output / 'notes.md').read_text()
        self.assertEqual({p.name for p in self.output.iterdir()}, {'notes.md', 'pages'})
        self.assertIn('未完成页码：2–3', merged)
        self.assertIn('# Kept page 1', merged)
        self.assertEqual((self.output / 'pages/001.md').read_text(), '# Kept page 1\n')

    async def test_setup_failure_does_not_publish_previous_run_results(self):
        self.output.mkdir()
        (self.output / 'merged.md').write_text('Old unrelated document')
        with patch.dict(os.environ, {'GEMINI_API_KEYS': 'fake', 'PADDLEOCR_TOKENS': ''}), \
                patch('pdf2md.download_pdf', one_page_pdf('New document')), \
                patch('pdf2md.KeyPool.create', AsyncMock(side_effect=RuntimeError('pool unavailable'))):
            self.assertEqual(await async_main(self.args()), 2)
        progress = json.loads((self.root / 'work/conversion/progress.json').read_text())
        self.assertEqual(progress['completed_chunks'], 0)
        self.assertEqual({p.name for p in self.output.iterdir()}, {'merged.md'})
        merged = (self.output / 'merged.md').read_text()
        self.assertIn('未完成页码：1', merged)
        self.assertNotIn('Old unrelated document', merged)

    async def test_ocr_drafts_are_refined_hedged_and_published_in_order(self):
        root = self.root
        with fitz.open() as doc:
            for index in range(4):
                page = doc.new_page()
                if index != 3:
                    page.insert_text((72, 72), f'Page {index + 1}')
            doc.save(root / 'source.pdf')
        counts, drafts, jobs = {}, {}, {}
        lock = threading.Lock()

        class Server(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, data, content_type='application/json'):
                if isinstance(data, (dict, list)):
                    data = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass

            def do_GET(self):
                if self.path == '/source.pdf':
                    return self.reply((root / 'source.pdf').read_bytes(), 'application/pdf')
                if self.path == '/fig.jpg':
                    return self.reply(b'figure-bytes', 'image/jpeg')
                if self.path.startswith('/ocr/jobs/'):
                    job = self.path.rsplit('/', 1)[1]
                    return self.reply({'code': 0, 'data': {
                        'state': 'done', 'resultUrl': {'jsonUrl': f'{base}/ocr/result/{job}'}}})
                if self.path.startswith('/ocr/result/'):
                    texts = jobs[self.path.rsplit('/', 1)[1]]
                    pages = [{'markdown': {
                        'text': f'<div style="text-align: center;"><img src="imgs/f.jpg" /></div>\n\n{text} ocr',
                        'images': {'imgs/f.jpg': f'{base}/fig.jpg'} if text == 'Page 1' else {},
                    }} for text in texts]
                    return self.reply(json.dumps({'result': {'layoutParsingResults': pages}}).encode())
                self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                if self.path == '/ocr/jobs':
                    assert self.headers['Authorization'] == 'bearer ocr-token'
                    # The service fetches the source link itself, one page range per job.
                    request = json.loads(body)
                    assert request['fileUrl'] == f'{base}/source.pdf'
                    first, last = map(int, request['pageRanges'].split('-'))
                    with fitz.open(root / 'source.pdf') as document:
                        texts = [document[number - 1].get_text().strip()
                                 for number in range(first, last + 1)]
                    with lock:
                        job = str(len(jobs) + 1)
                        jobs[job] = texts
                    return self.reply({'code': 0, 'data': {'jobId': job}})
                text = json.loads(body)['messages'][0]['content'][0]['text']
                page = int(re.search(r'第 (\d+) 页', text).group(1))
                draft = re.search(r'<ocr_draft>\n(.*)\n</ocr_draft>', text, re.S)
                with lock:
                    counts[page] = counts.get(page, 0) + 1
                    attempt = counts[page]
                    drafts[page] = draft.group(1) if draft else None
                time.sleep(1.5 if page == 2 and attempt == 1 else 0.015)
                figure = '![](images/p001-1.jpg)\n\n' if page == 1 else ''
                self.reply({'choices': [{'finish_reason': 'stop', 'message': {
                    'content': f'{figure}# Page {page}\n\nRefined text.'}}]})

        server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        args = parser().parse_args([
            '--provider', 'modelflare', '--model', 'test-vision', '--api-base', base + '/v1',
            '--ocr-url', base + '/ocr/jobs', '--ocr-segment-pages', '2',
            '--source-url', base + '/source.pdf', '--work-dir', str(root / 'work'),
            '--output-dir', str(self.output), '--dpi', '72', '--concurrency', '4',
            '--rpm-per-key', '6000', '--hedge-after', '0.3', '--hedge-budget', '2'])
        try:
            with patch.dict(os.environ, {'MODELFLARE_API_KEYS': 'fake-1\nfake-2\nfake-3\nfake-4',
                                         'PADDLEOCR_TOKENS': 'ocr-token'}), \
                    patch('paddle_ocr.POLL_SECONDS', 0):
                result = await asyncio.wait_for(async_main(args), 10)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()

        self.assertEqual(result, 0)
        self.assertEqual(sorted(len(texts) for texts in jobs.values()), [2, 2])
        self.assertEqual(drafts[1], '![](images/p001-1.jpg)\n\nPage 1 ocr')
        self.assertEqual(drafts[3], 'Page 3 ocr')
        self.assertNotIn(4, counts, 'the blank page never reaches the model')
        progress = json.loads((root / 'work/conversion/progress.json').read_text())
        usage = json.loads((root / 'work/conversion/quota-usage.json').read_text())
        self.assertEqual(progress['state'], 'completed')
        self.assertEqual(usage['speculation']['hedge_wins'], 1)
        self.assertEqual(usage['page_sources'], {'ocr+model': 3, 'text+model': 0, 'model': 0, 'ocr': 0})
        self.assertLessEqual(sum(counts.values()), 5)

        self.assertEqual({p.name for p in self.output.iterdir()}, {'merged.md', 'pages', 'images'})
        self.assertEqual(sorted(p.name for p in (self.output / 'pages').iterdir()),
                         ['001.md', '002.md', '003.md'])
        self.assertEqual((self.output / 'images/p001-1.jpg').read_bytes(), b'figure-bytes')
        self.assertIn('](../images/p001-1.jpg)', (self.output / 'pages/001.md').read_text())
        merged = (self.output / 'merged.md').read_text()
        self.assertIn('空白页码：4', merged)
        self.assertIn('识别流程：PaddleOCR-VL-1.6 → modelflare / test-vision', merged)
        self.assertLess(merged.index('# Page 1'), merged.index('# Page 2'))
        self.assertLess(merged.index('# Page 2'), merged.index('# Page 3'))


if __name__ == '__main__':
    unittest.main()
