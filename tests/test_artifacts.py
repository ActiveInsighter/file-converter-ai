import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from artifacts import prepare_directories, validate_output_name, write_result
from helpers import page
from render_stream import RenderPlan


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = prepare_directories(self.root / 'work', self.root / 'output')
        (self.state / 'pages').mkdir()
        (self.state / 'figures').mkdir()

    def write(self, pages, results, **overrides):
        options = dict(output_dir=self.root / 'output', state_dir=self.state,
                       name='数学笔记', source_url='https://example.test/book.pdf',
                       plan=RenderPlan(10, 2, 3, 3), provider='gemini', model='vision',
                       ocr_model='PaddleOCR-VL-1.6', converted_at='2026-09-30T10:00:00+00:00',
                       pages=pages, results=results, pipeline_error=None)
        options.update(overrides)
        return write_result(**options)

    def page_text(self, number, text, source='ocr+model'):
        (self.state / 'pages' / f'{number:03d}.md').write_text(text)
        return {'chunk': f'{number:03d}', 'status': 'ok', 'source': source}

    def test_package_is_merged_file_page_files_and_figures(self):
        (self.state / 'figures/p003-1.jpg').write_bytes(b'figure')
        (self.state / 'figures/p003-2.jpg').write_bytes(b'unused figure')
        results = [self.page_text(3, 'Page 3 ![](images/p003-1.jpg)'), self.page_text(2, 'Page 2')]
        files = self.write([page(3), page(2)], results)
        output = self.root / 'output'
        self.assertFalse(files.partial)
        self.assertEqual(files.markdown, output / '数学笔记.md')
        self.assertEqual({p.name for p in output.iterdir()}, {'数学笔记.md', 'pages', 'images'})
        self.assertEqual(sorted(p.name for p in (output / 'pages').iterdir()), ['002.md', '003.md'])
        self.assertEqual([p.name for p in (output / 'images').iterdir()], ['p003-1.jpg'])
        self.assertEqual((output / 'pages/003.md').read_text(), 'Page 3 ![](../images/p003-1.jpg)\n')
        merged = files.markdown.read_text()
        self.assertTrue(merged.startswith('# 文档信息\n'))
        self.assertIn('- 识别流程：PaddleOCR-VL-1.6 → gemini / vision', merged)
        self.assertIn('- 转换结果：完成（2/2 页）', merged)
        self.assertTrue(merged.endswith('<!-- 第 2 页 -->\n\nPage 2\n\n<!-- 第 3 页 -->\n\n'
                                        'Page 3 ![](images/p003-1.jpg)\n'))
        self.assertEqual(merged.count('https://example.test/book.pdf'), 1)

    def test_fallback_sources_are_listed(self):
        results = [self.page_text(2, 'A', source='ocr'), self.page_text(3, 'B', source='model')]
        merged = self.write([page(2), page(3)], results).markdown.read_text()
        self.assertIn('- 模型失败、直接使用 OCR 结果的页码：2', merged)
        self.assertIn('- 未用 OCR 草稿（仅图片转写）的页码：3', merged)

    def test_without_ocr_the_pipeline_names_only_the_model(self):
        merged = self.write([], [], ocr_model=None).markdown.read_text()
        self.assertIn('- 识别流程：gemini / vision', merged)

    def test_failed_and_unrendered_pages_are_visible_in_partial_header(self):
        results = [self.page_text(2, 'Kept content'), {'chunk': '003', 'status': 'failed'}]
        files = self.write([page(2), page(3)], results, plan=RenderPlan(10, 2, 5, 3),
                           pipeline_error='private internal details')
        merged = files.markdown.read_text()
        self.assertTrue(files.partial)
        self.assertEqual(files.markdown.name, '数学笔记.md')
        self.assertIn('- 未完成页码：3–5', merged)
        self.assertIn('部分完成（1/4 页）', merged)
        self.assertNotIn('private internal details', merged)
        self.assertIn('Kept content', merged)

    def test_blank_pages_get_no_page_file(self):
        (self.state / 'pages/002.md').write_text('')
        files = self.write([page(2), page(3)], [{'chunk': '002', 'status': 'blank'},
                                               self.page_text(3, 'Text')])
        self.assertIn('- 空白页码：2', files.markdown.read_text())
        self.assertEqual([p.name for p in (self.root / 'output/pages').iterdir()], ['003.md'])

    def test_source_url_cannot_inject_markdown(self):
        merged = self.write([], [], source_url='https://example.test/a>\n<script>.pdf').markdown.read_text()
        self.assertIn('%3E%0A%3Cscript%3E', merged)
        self.assertNotIn('<script>', merged)

    def test_failed_write_does_not_leave_temporary_files_in_package(self):
        actual_write = Path.write_text

        def disk_failure(path, text, **kwargs):
            actual_write(path, text, **kwargs)
            raise OSError('Disk failure after partial write')

        with patch('artifacts.Path.write_text', disk_failure), self.assertRaises(OSError):
            self.write([], [])
        self.assertEqual(list((self.root / 'output').iterdir()), [])

    def test_republishing_keeps_files_current(self):
        self.write([page(2)], [self.page_text(2, 'First')])
        self.write([page(2)], [self.page_text(2, 'Second')])
        self.assertEqual((self.root / 'output/pages/002.md').read_text(), 'Second\n')


class DirectoryTests(unittest.TestCase):
    def test_rerun_clears_previous_generated_package_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_directories(root / 'work', root / 'output')
            (root / 'output/old.md').write_text('Unrelated old document')
            (root / 'work/conversion/results.json').write_text('Old results')
            prepare_directories(root / 'work', root / 'output')
            self.assertEqual(list((root / 'output').iterdir()), [])
            self.assertEqual(list((root / 'work/conversion').iterdir()), [])

    def test_output_cannot_erase_work_or_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for work, output in [(root, root), (root / 'work', root),
                                 (root, root / 'output'), (root / 'work', Path.cwd())]:
                with self.subTest(work=work, output=output), self.assertRaises(ValueError):
                    prepare_directories(work, output)

    def test_state_directory_cannot_erase_current_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'work/conversion/project'
            project.mkdir(parents=True)
            marker = project / 'README.md'
            marker.write_text('Keep this project')
            previous = Path.cwd()
            try:
                os.chdir(project)
                with self.assertRaises(ValueError):
                    prepare_directories(root / 'work', root / 'output')
                self.assertEqual(marker.read_text(), 'Keep this project')
            finally:
                os.chdir(previous)

    def test_names_are_basenames(self):
        self.assertEqual(validate_output_name('数学笔记'), '数学笔记')
        self.assertEqual(validate_output_name('notes.v2'), 'notes.v2')
        for name in ['../escape', 'a/b', 'a\\b', '.', '.notes', 'CON.txt',
                     'notes.md', 'notes.zip', 'x\nhello', 'a' * 121, '数' * 81, '🙂' * 61]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_output_name(name)


if __name__ == '__main__':
    unittest.main()
