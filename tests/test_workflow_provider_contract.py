"""The Actions entry point must carry provider selection to the converter."""

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/pdf-to-md.yml"


class WorkflowProviderContractTests(unittest.TestCase):
    def test_dispatch_can_select_modelflare_without_gemini_secrets(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("PROVIDER: ${{ github.event.inputs.provider || 'gemini' }}", workflow)
        self.assertIn("MODELFLARE_API_KEYS: ${{ secrets.MODELFLARE_API_KEYS }}", workflow)
        self.assertIn('if [[ "${PROVIDER}" == "gemini" ]]', workflow)
        self.assertIn('if [[ "${PROVIDER}" == "modelflare" ]]', workflow)
        self.assertIn('args=(\n            --conversion-type "${CONVERSION_TYPE}"\n            --provider "${PROVIDER}"', workflow)

    def test_model_and_fallback_defaults_do_not_force_gemini_on_modelflare(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("MODEL: ${{ github.event.inputs.model || '' }}", workflow)
        self.assertIn("MODEL_FALLBACKS: ${{ github.event.inputs.model_fallbacks || '' }}", workflow)


    def test_ocr_token_comes_from_the_baidu_secret_and_the_whole_package_is_uploaded(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("PADDLEOCR_TOKENS: ${{ secrets.BAIDU_AI_STUDIO }}", workflow)
        self.assertIn('--ocr "${OCR}"', workflow)
        self.assertIn("OCR_SOURCE: ${{ vars.CONVERTER_OCR_SOURCE || 'link' }}", workflow)
        self.assertIn('--ocr-source "${OCR_SOURCE}"', workflow)
        self.assertIn("path: output/\n", workflow)
        self.assertNotIn("GEMINI_QUOTA_API", workflow)

    def test_legacy_dispatch_inputs_are_still_accepted(self):
        workflow = WORKFLOW.read_text()
        for name in ("retry_rounds", "attempts_per_page", "verification_passes"):
            self.assertIn(f"      {name}:\n", workflow)

    def test_adjacent_pages_is_an_opt_in_flag(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("      adjacent_pages:\n", workflow)
        self.assertIn("ADJACENT_PAGES: ${{ github.event.inputs.adjacent_pages || 'false' }}", workflow)
        self.assertIn('if [[ "${ADJACENT_PAGES}" == "true" ]]; then\n            args+=(--adjacent-pages)', workflow)

    def test_dispatch_stays_within_githubs_input_limit(self):
        inputs = WORKFLOW.read_text().split("    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
        names = [line for line in inputs.splitlines() if line.startswith("      ") and not line.startswith("       ")]
        self.assertLessEqual(len(names), 25, "workflow_dispatch accepts at most 25 inputs")


if __name__ == "__main__":
    unittest.main()
