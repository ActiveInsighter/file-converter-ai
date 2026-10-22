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


if __name__ == '__main__':
    unittest.main()
