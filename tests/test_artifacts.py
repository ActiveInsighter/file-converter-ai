import tempfile
import unittest
import os
from pathlib import Path
from artifacts import prepare_directories, validate_output_name, write_result
from pdf2md import Chunk
from render_stream import RenderPlan


class ArtifactTests(unittest.TestCase):
    def write(self, root, chunks, results, **overrides):
        options = dict(output_dir=root / 'output', state_dir=root / 'work/conversion',
                       name='数学笔记', source_url='https://example.test/book.pdf',
                       plan=RenderPlan(10, 2, 3, 3), provider='gemini', model='vision',
                       converted_at='2026-09-30T10:00:00+00:00', chunks=chunks,
                       results=results, pipeline_error=None)
        options.update(overrides)
        return write_result(**options).markdown

    def test_package_has_only_source_and_final_with_ordered_current_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_directories(root / 'work', root / 'output')
            pages = root / 'work/conversion/pages'
            pages.mkdir()
            for name in ['2', '3', '999']:
                (pages / f'{name}.md').write_text(f'Page {name}')
            chunks = [Chunk(3, 3, (), '3'), Chunk(2, 2, (), '2')]
            path = self.write(root, chunks, [{'chunk': c.stem, 'status': 'ok'} for c in chunks])
            source = (root / 'output/source.md').read_text()
            self.assertEqual({p.name for p in path.parent.iterdir()}, {'source.md', '数学笔记.md'})
            self.assertTrue(path.read_text().startswith(source + '\n---\n\n'))
            self.assertTrue(path.read_text().endswith('Page 2\n\nPage 3\n'))
            self.assertEqual(path.read_text().count('https://example.test/book.pdf'), 1)
            self.assertIn('10', source)
            self.assertIn('2–3', source)
            self.assertIn('2/2', source)

    def test_failed_and_unrendered_pages_are_visible_in_partial_header(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_directories(root / 'work', root / 'output')
            pages = root / 'work/conversion/pages'
            pages.mkdir()
            (pages / '2.md').write_text('Kept content')
            path = self.write(root, [Chunk(2, 2, (), '2')], [{'chunk': '2', 'status': 'ok'}],
                              plan=RenderPlan(10, 2, 5, 3), pipeline_error='private internal details')
            source = (root / 'output/source.md').read_text()
            self.assertEqual(path.name, '数学笔记.partial.md')
            self.assertIn('3–5', source)
            self.assertIn('1/4', source)
            self.assertNotIn('private internal details', source)
            self.assertIn('Kept content', path.read_text())

    def test_recovered_page_with_missing_regions_is_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_directories(root / 'work', root / 'output')
            (root / 'work/conversion/pages').mkdir()
            (root / 'work/conversion/pages/2.md').write_text('Recovered region')
            results = [{'chunk': '2', 'status': 'ok', 'missing_parts': ['2-part1']}]
            path = self.write(root, [Chunk(2, 2, (), '2')], results, plan=RenderPlan(10, 2, 2, 3))
            self.assertEqual(path.name, '数学笔记.partial.md')
            self.assertIn('部分内容未识别页码：2', (root / 'output/source.md').read_text())

    def test_source_url_cannot_inject_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_directories(root / 'work', root / 'output')
            path = self.write(root, [], [], source_url='https://example.test/a>\n<script>.pdf')
            source = (root / 'output/source.md').read_text()
            self.assertIn('%3E%0A%3Cscript%3E', source)
            self.assertNotIn('<script>', source)
            self.assertTrue(path.read_text().startswith(source))

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

    def test_names_are_basenames_and_cannot_collide_with_source(self):
        self.assertEqual(validate_output_name('数学笔记'), '数学笔记')
        for name in ['../escape', 'a/b', 'a\\b', '.', '.notes', 'CON.txt', 'source', 'Source',
                     'notes.md', 'notes.zip', 'x\nhello', 'a'*121, '数'*81, '🙂'*61]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_output_name(name)

    def test_final_publication_removes_partial_and_keeps_dotted_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = prepare_directories(root / 'work', root / 'output')
            (state / 'pages').mkdir()
            self.write(root, [], [], name='notes.partial', plan=RenderPlan(1, 1, 1, 3))
            (state / 'pages/1.md').write_text('Complete')
            self.write(root, [Chunk(1, 1, (), '1')], [{'chunk': '1', 'status': 'ok'}],
                       name='notes.partial', plan=RenderPlan(1, 1, 1, 3))
            self.assertEqual({p.name for p in (root / 'output').iterdir()}, {'source.md', 'notes.partial.md'})
            self.assertIn('完成（1/1', (root / 'output/source.md').read_text())
