"""Regression tests for content-preserving cleanup and structural QA."""
import unittest
from markdown_quality import clean_layout_markup, markdown_quality_metrics


class MarkdownQualityTests(unittest.TestCase):
    def test_removes_known_layout_div_without_removing_contents(self):
        source = '<div class="analysis-box">\n\n**主 句**\n\nA <u>verb</u>.\n\n</div>'
        fixed = clean_layout_markup(source)
        self.assertNotIn("<div", fixed)
        self.assertIn("**主 句**", fixed)
        self.assertIn("<u>verb</u>", fixed)

    def test_right_aligned_header_and_nested_wrapper(self):
        source = '<div align="right">Section II</div>\n<div><div>正文</div></div>'
        self.assertEqual(clean_layout_markup(source), 'Section II\n正文')

    def test_mdx_safe_table_keeps_cells_and_figure_links(self):
        source = (
            "<table border=1 style='margin: auto; word-wrap: break-word;'>"
            "<tr><td style='text-align: center; word-wrap: break-word;'>"
            "主语<br>下一行</td></tr></table>\n![](images/p010-3.jpg)"
        )
        fixed = clean_layout_markup(source)
        self.assertIn('<td align="center">', fixed)
        self.assertIn('<br />', fixed)
        self.assertNotIn("border=1", fixed)
        self.assertNotIn("style=", fixed)
        self.assertIn("![](images/p010-3.jpg)", fixed)

    def test_unfamiliar_div_and_nbsp_are_kept(self):
        source = '<div style="display:grid">x&nbsp;&nbsp;y</div>'
        self.assertEqual(clean_layout_markup(source), source)

    def test_idempotent_and_preserves_math(self):
        source = '$\\underline{\\text{who}}$<br>\n<div>解答</div>'
        fixed = clean_layout_markup(source)
        self.assertEqual(clean_layout_markup(fixed), fixed)
        self.assertIn('$\\underline{\\text{who}}$', fixed)

    def test_metrics_preserve_page_and_image_counts(self):
        doc = '<!-- 第 1 页 -->\n![](images/p001-1.jpg)\n<div>文字</div>'
        m = markdown_quality_metrics(doc)
        n = markdown_quality_metrics(clean_layout_markup(doc))
        self.assertEqual((m["pages"], m["images"]), (1, 1))
        self.assertEqual(n["div_tags"], 0)


if __name__ == "__main__":
    unittest.main()
