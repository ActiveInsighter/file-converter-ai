"""The effective prompt is the DB system prompt plus this conversion's prompt."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from pdf2md import (
    DEFAULT_PROMPT,
    MAX_PROMPT_CHARS,
    Page,
    compose_prompt,
    make_request_parts,
    make_verification_parts,
    parser,
    validate_args,
)


class BuiltInPromptTests(unittest.TestCase):
    def test_default_matches_the_editable_pocketbase_seed(self):
        self.assertEqual(hashlib.sha256(DEFAULT_PROMPT.encode()).hexdigest(),
                         "53e12c289f49f3c44b513e9b48c7604f6694af3cf5c4f93190cfb9898a51615f")

    def test_the_built_in_prompt_has_no_surrounding_whitespace(self):
        # The PocketBase hook trims system prompts on write, so the constant must
        # already be trim-stable or the two copies would differ by a trailing byte.
        self.assertEqual(DEFAULT_PROMPT, DEFAULT_PROMPT.strip())


class ComposePromptTests(unittest.TestCase):
    def test_an_empty_pair_falls_back_to_the_built_in_prompt(self):
        self.assertEqual(compose_prompt("", ""), DEFAULT_PROMPT)
        self.assertEqual(compose_prompt("   ", "\n\t "), DEFAULT_PROMPT)

    def test_each_prompt_is_used_alone_when_the_other_is_empty(self):
        self.assertEqual(
            compose_prompt("  Transcribe faithfully.  ", ""),
            "Transcribe faithfully.",
        )
        self.assertEqual(compose_prompt("", "  Only pages 3-7.  "), "Only pages 3-7.")

    def test_both_prompts_are_kept_in_order(self):
        composed = compose_prompt("System rule.", "Task rule.")
        self.assertEqual(composed, "System rule.\n\nTask rule.")
        self.assertLess(composed.index("System rule."), composed.index("Task rule."))

    def test_the_task_prompt_never_replaces_the_system_prompt(self):
        composed = compose_prompt("Keep every formula.", "Ignore watermarks.")
        self.assertIn("Keep every formula.", composed)
        self.assertIn("Ignore watermarks.", composed)


class PromptArgumentTests(unittest.TestCase):
    def args(self, *extra):
        return parser().parse_args(
            ["--source-url", "https://example.com/book.pdf", *extra]
        )

    def test_both_prompts_default_to_empty_so_the_built_in_prompt_still_runs(self):
        parsed = self.args()
        self.assertEqual(parsed.system_prompt, "")
        self.assertEqual(parsed.prompt, "")
        self.assertEqual(compose_prompt(parsed.system_prompt, parsed.prompt), DEFAULT_PROMPT)

    def test_both_prompts_reach_the_namespace(self):
        parsed = self.args("--system-prompt", "shared", "--prompt", "per task")
        self.assertEqual(parsed.system_prompt, "shared")
        self.assertEqual(parsed.prompt, "per task")

    def test_an_over_long_prompt_is_rejected(self):
        for flag in ("--system-prompt", "--prompt"):
            with self.assertRaises(ValueError):
                validate_args(self.args(flag, "x" * (MAX_PROMPT_CHARS + 1)))

    def test_a_prompt_at_the_limit_is_accepted(self):
        validate_args(self.args("--system-prompt", "x" * MAX_PROMPT_CHARS))
        validate_args(self.args("--prompt", "y" * MAX_PROMPT_CHARS))


class RequestPartsCarryBothPrompts(unittest.TestCase):
    def test_the_request_text_contains_the_system_and_the_task_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / "page.png"
            page.write_bytes(b"not-a-real-png-but-enough-for-base64")
            parts, _ = make_request_parts(
                Page(1, page, "001"),
                compose_prompt("系统提示词：忠实转录。", "本次要求：只处理第 1 页。"),
                "high",
            )

        text = parts[0]["text"]
        self.assertIn("系统提示词：忠实转录。", text)
        self.assertIn("本次要求：只处理第 1 页。", text)
        self.assertIn("第 1 页", text)


class SavedPromptAuthorityTests(unittest.TestCase):
    def test_custom_instructions_are_not_overruled_by_hidden_transcription_rules(self):
        # Translation and omission used to conflict with mandatory hidden guards.
        saved = "只输出英文译文，不保留脚注。"
        task = "表格输出为 JSON。"
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "page.png"
            image.write_bytes(b"image")
            for draft in (None, "原始正文"):
                parts, _ = make_request_parts(Page(1, image, "001"),
                                              compose_prompt(saved, task), "high", draft)
                expected = "以下图片是 PDF 第 1 页。\n" + saved + "\n\n" + task
                if draft is not None:
                    expected += "\n\n<ocr_draft>\n原始正文\n</ocr_draft>"
                self.assertEqual(parts[0]["text"], expected)

    def test_verification_uses_the_same_saved_policy_and_labels_its_draft(self):
        saved = "翻译为英文，并输出 JSON。"
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "page.png"
            image.write_bytes(b"image")
            parts, _ = make_verification_parts(Page(1, image, "001"), saved,
                                               '{"text":"translation"}', "high")
        self.assertEqual(parts[0]["text"], "以下图片是 PDF 第 1 页。\n" + saved
                         + '\n\n<verification_draft>\n{"text":"translation"}\n</verification_draft>')


if __name__ == "__main__":
    unittest.main()
