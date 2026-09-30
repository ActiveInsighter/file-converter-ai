import asyncio
import tempfile
import unittest
from pathlib import Path

import fitz
from pdf2md import Chunk, merge_markdown, stream_pdf_chunks
from render_stream import RenderPlan
from test_page_recovery import run_process_chunks
from pdf2md import PermanentProviderError


class RenderStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_rerun_cannot_retain_old_page_with_same_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pages').mkdir()
            stale = root / 'pages/001.md'
            stale.write_text('Old unrelated document')
            async def fail(*args, **kwargs):
                raise PermanentProviderError('HTTP 400')
            results = await run_process_chunks([Chunk(1, 1, (), '001')], directory, fail)
            self.assertEqual(results[0]['status'], 'failed')
            self.assertFalse(stale.exists())

    async def test_chunk_available_before_later_pages_are_rendered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with fitz.open() as doc:
                for index in range(3):
                    doc.new_page().insert_text((72, 72), f'Page {index + 1}')
                doc.save(root / 'test.pdf')
            plan = RenderPlan.from_pdf(root / 'test.pdf', 1, None)
            chunks = []
            blanks = set()
            stream = stream_pdf_chunks(root / 'test.pdf', root / 'images', plan,
                                       72, 95, 'png', 1, blanks)
            first = await anext(stream)
            self.assertTrue(first.image_paths[0].exists())
            self.assertFalse((root / 'images' / '002.png').exists())
            chunks.append(first)
            async for chunk in stream:
                chunks.append(chunk)
            self.assertEqual([c.start_page for c in chunks], [1, 2, 3])
            self.assertEqual(blanks, set())

    async def test_blank_batch_and_last_short_batch_keep_page_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with fitz.open() as doc:
                doc.new_page()
                doc.new_page()
                doc.new_page().insert_text((72, 72), 'Page 3')
                doc.save(root / 'test.pdf')
            plan = RenderPlan.from_pdf(root / 'test.pdf', None, None)
            blanks = set()
            chunks = [chunk async for chunk in stream_pdf_chunks(
                root / 'test.pdf', root / 'images', plan, 72, 95, 'png', 2, blanks)]
            self.assertEqual([c.stem for c in chunks], ['001-002', '003'])
            self.assertEqual([c.blank for c in chunks], [True, False])
            self.assertEqual(blanks, {1, 2})

    async def test_merge_excludes_stale_pages_and_sorts_numerically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ['2', '10', '999']:
                (root / f'{name}.md').write_text(f'Page {name}')
            merge_markdown([Chunk(10, 10, (), '10'), Chunk(2, 2, (), '2')], root, root / 'merged.md')
            self.assertEqual((root / 'merged.md').read_text(), 'Page 2\n\nPage 10\n')
