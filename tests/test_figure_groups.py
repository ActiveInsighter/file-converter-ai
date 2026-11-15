"""Diagram recovery must preserve geometry, captions and independent figures."""
import tempfile
import unittest
from pathlib import Path

import fitz

from figure_groups import merge_figure_fragments
from paddle_ocr import normalize_markdown


class FigureGroupsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / 'source.pdf'
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            page.draw_rect(fitz.Rect(50, 80, 150, 180), color=(1, 0, 0))
            page.draw_line((155, 130), (170, 130), color=(0, 0, 1))
            page.draw_rect(fitz.Rect(190, 80, 290, 180), color=(0, 1, 0))
            doc.save(self.pdf)
        self.result = {'prunedResult': {'width': 1200, 'height': 1600}}

    def image(self, box):
        source = 'imgs/img_in_image_box_' + '_'.join(map(str, box)) + '.jpg'
        return f'<div><img src="{source}" /></div>', source

    def recover(self, boxes, separators=None, result=None):
        tags, sources = zip(*(self.image(box) for box in boxes))
        text = tags[0]
        for i, tag in enumerate(tags[1:]):
            text += (separators[i] if separators else '\n\n') + tag
        recovered, figures, removed = merge_figure_fragments(
            text, dict.fromkeys(sources, 'https://test/figure'),
            result or self.result, self.pdf, 1, 3, self.root / 'figures')
        return recovered, figures, removed, sources

    def test_adjacent_tree_arrow_tree_recropped_at_original_positions(self):
        text, figures, removed, sources = self.recover([
            (100, 160, 300, 360), (320, 230, 360, 290), (380, 160, 580, 360)])
        self.assertEqual(len(figures), 1)
        self.assertEqual(removed, set(sources))
        self.assertEqual(normalize_markdown(text, {p: p for p in figures}),
                         '![](images/p001-merged-1.png)')
        pix = fitz.Pixmap(str(next(iter(figures.values()))))
        # The re-crop retains the wide horizontal layout rather than stacking
        # image fragments vertically, with original intervening pixels intact.
        self.assertGreater(pix.width / pix.height, 2.3)
        self.assertGreater(pix.width, 790)

    def test_caption_and_body_text_stop_joining(self):
        for separator in ('\nFigure 1: first tree\n', '\nQuestion 5\n'):
            with self.subTest(separator=separator):
                _, figures, removed, _ = self.recover(
                    [(100, 160, 300, 360), (320, 160, 520, 360)], [separator])
                self.assertFalse(figures)
                self.assertFalse(removed)

    def test_separate_rows_and_distant_figures_stay_separate(self):
        for box in ((800, 160, 1000, 360), (100, 420, 300, 620)):
            with self.subTest(box=box):
                _, figures, _, _ = self.recover([(100, 160, 300, 360), box])
                self.assertFalse(figures)

    def test_body_text_inside_union_blocks_recovery(self):
        metadata = {'prunedResult': {**self.result['prunedResult'], 'parsing_res_list': [
            {'block_label': 'text', 'block_bbox': [301, 200, 319, 300]}]}}
        _, figures, _, _ = self.recover(
            [(100, 160, 300, 360), (320, 160, 520, 360)], result=metadata)
        self.assertFalse(figures)

    def test_invalid_or_transformed_geometry_keeps_originals(self):
        for metadata in ({}, {'width': 1200, 'height': float('nan')},
                         {'width': 800, 'height': 1200},
                         {'width': 1200, 'height': 1600,
                          'model_settings': {'use_doc_preprocessor': True}}):
            with self.subTest(metadata=metadata):
                _, figures, removed, _ = self.recover(
                    [(100, 160, 300, 360), (320, 160, 520, 360)],
                    result={'prunedResult': metadata})
                self.assertFalse(figures)
                self.assertFalse(removed)

    def test_repeated_reference_is_not_removed(self):
        _, figures, removed, _ = self.recover([(100, 160, 300, 360)] * 2)
        self.assertFalse(figures)
        self.assertFalse(removed)


class OcrRecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovered_figures_skip_fragment_downloads(self):
        import httpx
        from paddle_ocr import PaddleOcr

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / 'source.pdf'
            with fitz.open() as doc:
                doc.new_page(width=600, height=800)
                doc.new_page(width=600, height=800)
                doc.save(pdf)
            sources = ['imgs/img_in_image_box_100_100_200_200.jpg',
                       'imgs/img_in_image_box_220_100_320_200.jpg']
            requests = []

            def handler(request):
                requests.append(request)
                return httpx.Response(500)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                ocr = PaddleOcr(['test'], client=client, work_dir=root / 'ocr')
                ocr.pdf_path = pdf
                result = {'prunedResult': {'width': 600, 'height': 800},
                          'markdown': {'text': '\n'.join(f'<img src="{p}" />' for p in sources),
                                       'images': dict.fromkeys(sources, 'https://test/fragment')}}
                page = await ocr._page_from_result(2, 3, result)
            self.assertEqual(requests, [])
            self.assertEqual(page.markdown, '![](images/p002-merged-1.png)')
            self.assertEqual(ocr.stats['figures'], 1)
            self.assertTrue(next(iter(page.figures.values())).exists())
