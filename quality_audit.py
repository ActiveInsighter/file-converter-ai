"""Audit a converted Markdown export and optionally write a losslessly cleaned copy.

Usage: python quality_audit.py document.md --write-clean cleaned.md --json quality.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from markdown_quality import clean_layout_markup, markdown_quality_metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("markdown", type=Path)
    parser.add_argument("--write-clean", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    source = args.markdown.read_text(encoding="utf-8")
    fixed = clean_layout_markup(source)
    before = markdown_quality_metrics(source)
    after = markdown_quality_metrics(fixed)
    if args.write_clean is not None:
        args.write_clean.parent.mkdir(parents=True, exist_ok=True)
        args.write_clean.write_text(fixed, encoding="utf-8")
    report = {
        "before": {key: value for key, value in before.items() if key != "pages_by_number"},
        "after": {key: value for key, value in after.items() if key != "pages_by_number"},
        "unchanged_images": before["images"] == after["images"],
        "unchanged_pages": before["pages"] == after["pages"],
        "note": "Structural cleanup only: image OCR accuracy and figure classification not tested.",
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
