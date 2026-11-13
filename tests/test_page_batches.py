"""Exercise batching through the real scheduler and request builder."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from helpers import FakeOcr
from pdf2md import (BatchResponseError, ContentBlockedError, Page, flatten_results,
                   make_batch_request_parts, parser, process_pages, split_batch_answer,
                   validate_args)


class PageBatchTests(unittest.IsolatedAsyncioTestCase):
    async def run_batch(self, count=5, batch_size=3, blank=(), ocr=None, call=None, **options):
        root = Path(self.directory.name)
        pages = []
        for number in range(7, 7 + count):
            path = root / f'{number:03d}.png'
            path.write_bytes(f'image-{number}'.encode())
            top, bottom = root / f'{number}-top.png', root / f'{number}-bottom.png'
            top.write_bytes(b'top')
            bottom.write_bytes(b'bottom')
            pages.append(Page(number, path, f'{number:03d}', number in blank, top, bottom))
        self.calls = []

        async def respond(*args):
            parts, name = args[4:6]
            self.calls.append((parts, name))
            if call:
                return await call(parts, name)
            if '-' not in name.split('-verify-')[0]:
                return f'body-{int(name.split("-")[0])}'
            numbers = [int(p['text'].split('第 ')[1].split(' 页')[0])
                       for p in parts if p.get('text', '').startswith('PDF 第 ')]
            return '\n'.join(f'<!-- 第 {n} 页 -->\nbody-{n}' for n in numbers)

        async def stream():
            for page in pages:
                yield page
        with patch('pdf2md.call_model', respond), patch.dict(os.environ, {'GEMINI_KEY_GROUPS': ''}):
            return await process_pages(
                stream(), root / 'state', '用户要求', 'fake', 2, ['dummy'], 'high',
                options.pop('verification_passes', 0), 'high', 60000, 100,
                total_pages=count, images_per_request=batch_size, ocr=ocr,
                attempts_per_page=1, retry_rounds=1, hedge_budget=0, **options)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    async def test_one_three_and_five_keep_every_page_in_order(self):
        for batch_size, requests in [(1, 5), (3, 2), (5, 1)]:
            results = await self.run_batch(batch_size=batch_size)
            self.assertEqual(len(self.calls), requests)
            self.assertEqual(sorted(r['page'] for r in results), list(range(7, 12)))
            for result in results:
                self.assertEqual(Path(result['path']).read_text().strip(), f"body-{result['page']}")

    async def test_five_page_request_contains_all_images_and_labelled_drafts(self):
        results = await self.run_batch(batch_size=5, ocr=FakeOcr({n: f'draft-{n}' for n in range(7, 12)}))
        parts = self.calls[0][0]
        self.assertEqual(sum('inlineData' in p for p in parts), 5)
        for n in range(7, 12):
            self.assertTrue(any(f'PDF 第 {n} 页' in p.get('text', '') and f'draft-{n}' in p.get('text', '') for p in parts))
        self.assertTrue(all(r['source'] == 'ocr+model' for r in results))

    async def test_blanks_keep_batch_boundaries_but_are_not_sent(self):
        results = await self.run_batch(batch_size=3, blank=(8, 10, 11))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum('inlineData' in p for p in self.calls[0][0]), 2)
        self.assertEqual(sorted(r['page'] for r in results if r['status'] == 'blank'), [8, 10, 11])

    async def test_one_nonblank_in_a_batch_uses_one_page_contract(self):
        results = await self.run_batch(batch_size=5, blank=(7, 8, 9, 10))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], '011')
        self.assertEqual(sum(r['status'] == 'ok' for r in results), 1)

    async def test_refusal_falls_back_per_page_and_reports_missing_ocr(self):
        async def refuse(*_):
            raise ContentBlockedError('blocked')
        results = await self.run_batch(batch_size=5, ocr=FakeOcr({7: 'draft-7', 9: 'draft-9'}), call=refuse)
        self.assertEqual(sorted(r['page'] for r in results if r.get('source') == 'ocr'), [7, 9])
        self.assertEqual(sorted(r['page'] for r in results if r['status'] == 'failed'), [8, 10, 11])

    async def test_missing_page_cannot_be_reported_as_success(self):
        async def incomplete(*_):
            return '<!-- 第 7 页 -->\nonly one page'
        results = await self.run_batch(batch_size=5, call=incomplete)
        self.assertTrue(all(r['status'] == 'failed' for r in results))
        self.assertTrue(all('BatchResponseError' in r['error'] for r in results))

    async def test_verification_receives_the_whole_batch_again(self):
        results = await self.run_batch(batch_size=5, verification_passes=1)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[1][1].endswith('-verify-1'))
        self.assertEqual(sum('inlineData' in p for p in self.calls[1][0]), 5)
        self.assertEqual(sum('<verification_draft>' in p.get('text', '') for p in self.calls[1][0]), 5)
        self.assertEqual(len(results), 5)

    async def test_adjacent_strips_only_come_from_outside_batch(self):
        await self.run_batch(count=6, batch_size=3, adjacent_pages=True)
        by_name = dict((name, parts) for parts, name in self.calls)
        labels = [p.get('text', '') for p in by_name['007-009']]
        self.assertIn('参考截图：第 10 页顶部', labels)
        self.assertNotIn('参考截图：第 8 页顶部', labels)
        labels = [p.get('text', '') for p in by_name['010-012']]
        self.assertIn('参考截图：第 9 页底部', labels)

    async def test_progress_and_partial_publish_keep_real_page_numbers(self):
        published = []
        await self.run_batch(batch_size=3, publish_progress=lambda rows: published.extend(rows))
        root = Path(self.directory.name) / 'state'
        progress = json.loads((root / 'progress.json').read_text())
        self.assertEqual(progress['rendered_pages'], 5)
        self.assertEqual(progress['total_pages'], 5)
        self.assertEqual(progress['total_chunks'], 2)
        self.assertEqual(progress['percent'], 100)
        self.assertEqual(progress['success_pages'], 5)
        self.assertEqual(progress['completed_pages'], 5)
        saved = flatten_results(json.loads((root / 'results.json').read_text()))
        self.assertEqual(sorted(r['page'] for r in saved), list(range(7, 12)))


class BatchFramingTests(unittest.TestCase):
    def test_default_alias_and_invalid_limits(self):
        args = parser().parse_args(['--source-url', 'https://example.com/a.pdf'])
        self.assertEqual(args.images_per_request, 1)
        for flag in ['--images-per-request', '--pages-per-request']:
            args = parser().parse_args(['--source-url', 'https://example.com/a.pdf', flag, '5'])
            validate_args(args)
            self.assertEqual(args.images_per_request, 5)
        for value in [0, 21]:
            args.images_per_request = value
            with self.assertRaises(ValueError):
                validate_args(args)

    def test_reject_duplicate_reordered_unknown_empty_and_unframed_pages(self):
        pages = [Page(n, Path('unused'), str(n)) for n in [7, 8]]
        for text in ['text', '<!-- 第 7 页 -->a<!-- 第 7 页 -->b',
                     '<!-- 第 8 页 -->a<!-- 第 7 页 -->b',
                     '<!-- 第 7 页 -->a<!-- 第 9 页 -->b',
                     '<!-- 第 7 页 --><!-- 第 8 页 -->b',
                     'prefix<!-- 第 7 页 -->a<!-- 第 8 页 -->b']:
            with self.assertRaises(BatchResponseError):
                split_batch_answer(text, pages)

    def test_inline_limit_counts_all_images(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'a.png'
            path.write_bytes(b'X' * (8 * 1024 * 1024))
            pages = [Page(n, path, str(n)) for n in [1, 2]]
            with self.assertRaisesRegex(ValueError, 'too large'):
                make_batch_request_parts(pages, 'prompt', 'high', {})
