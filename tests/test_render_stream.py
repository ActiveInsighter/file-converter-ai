import tempfile
import unittest
from pathlib import Path

import fitz
from pdf2md import stream_pages
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

    async def test_edge_strips_are_rendered_only_when_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = self.write_pdf(root, ['Page 1', 'Page 2'])
            plan = RenderPlan.from_pdf(pdf, 1, None)
            plain = [page async for page in stream_pages(pdf, root / 'plain', plan, 72, 95, 'png')]
            self.assertEqual({(p.top_path, p.bottom_path) for p in plain}, {(None, None)})
            pages = [page async for page in stream_pages(pdf, root / 'edges', plan, 72, 95, 'png', True)]
            first = pages[0]
            self.assertEqual((first.top_path.name, first.bottom_path.name), ('001.top.png', '001.bottom.png'))
            full = fitz.Pixmap(str(first.image_path))
            top = fitz.Pixmap(str(first.top_path))
            self.assertEqual(top.width, full.width)
            self.assertAlmostEqual(top.height / full.height, 0.4, delta=0.02)


if __name__ == '__main__':
    unittest.main()
