import tempfile
import unittest
from pathlib import Path
from prepare_result import prepare_result

class ResultTests(unittest.TestCase):
    def test_renames_merged_and_preserves_page_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'merged.md').write_text('# Test')
            (root / 'manifest.json').write_text('{}')
            prepare_result(root, '数学笔记.zip')
            self.assertEqual((root / '数学笔记.md').read_text(), '# Test')
            self.assertFalse((root / 'merged.md').exists())
    def test_partial_result_keeps_partial_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'merged.partial.md').write_text('partial')
            prepare_result(root, 'notes')
            self.assertTrue((root / 'notes.partial.md').exists())
    def test_invalid_paths_and_reserved_names(self):
        for name in ['../escape', 'a/b', 'a\\b', '.', 'CON', 'x\nhello', 'a'*121]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ValueError):
                    prepare_result(Path(d), name)
