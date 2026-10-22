import tempfile
import unittest
from pathlib import Path

import fitz
from unittest.mock import patch

from pdf2md import read_text_layer, stream_pages
from render_stream import RenderPlan


class RenderStreamingTests(unittest.IsolatedAsyncioTestCase):
    def write_pdf(self, root, texts):
        with fitz.open() as doc:
            for text in texts:
                page = doc.new_page()
                if text:
                    page.insert_text((72, 72), text)
            doc.save(root / 'test.pdf')
        return root / 'test.pdf'

    async def test_page_available_before_later_pages_are_rendered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = self.write_pdf(root, ['Page 1', 'Page 2', 'Page 3'])
            plan = RenderPlan.from_pdf(pdf, 1, None)
            stream = stream_pages(pdf, root / 'images', plan, 72, 95, 'png')
            first = await anext(stream)
            self.assertTrue(first.image_path.exists())
            self.assertFalse((root / 'images' / '002.png').exists())
            pages = [first] + [page async for page in stream]
            self.assertEqual([p.number for p in pages], [1, 2, 3])
            self.assertEqual([p.stem for p in pages], ['001', '002', '003'])

    async def test_blank_pages_are_flagged_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = self.write_pdf(root, ['', '', 'Page 3'])
            plan = RenderPlan.from_pdf(pdf, 2, None)
            pages = [page async for page in stream_pages(pdf, root / 'images', plan, 72, 95, 'jpeg')]
            self.assertEqual([(p.number, p.blank) for p in pages], [(2, True), (3, False)])
            self.assertEqual(pages[0].image_path.suffix, '.jpg')


    async def test_usable_text_layers_travel_with_their_page(self):
        sentence = 'The quick brown fox jumps over the lazy dog.'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = self.write_pdf(root, [sentence, 'p. 2'])
            plan = RenderPlan.from_pdf(pdf, 1, None)
            pages = [page async for page in stream_pages(pdf, root / 'images', plan, 72, 95, 'png')]
            off = [page async for page in stream_pages(pdf, root / 'off', plan, 72, 95, 'png',
                                                       text_layer=False)]
        self.assertEqual([p.text_layer for p in pages], [sentence, None])
        self.assertEqual([p.text_layer for p in off], [None, None])


class TextLayerTests(unittest.TestCase):
    def layer(self, text):
        class Stub:
            def get_text(self, _kind):
                return text
        return read_text_layer(Stub())

    def test_garbled_layers_are_dropped_and_long_ones_truncated(self):
        self.assertIsNone(self.layer('\ufffd' * 30))
        self.assertIsNone(self.layer('A\ue001\ue002' * 10), 'private-use glyph codes')
        self.assertEqual(self.layer('Real words \n\n\n\nand more real words'),
                         'Real words\n\nand more real words')
        with patch('pdf2md.TEXT_LAYER_MAX_CHARS', 25):
            self.assertTrue(self.layer('Long enough sentence for a layer here').endswith('已截断）'))


if __name__ == '__main__':
    unittest.main()
