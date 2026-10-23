"""Deterministic anchor/ownership checks, not a general transcription accuracy score."""
import argparse
import html
import json
import re
from pathlib import Path


def normalize(text):
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+|[\\$*`{}]", "", text)
    return text


def score(root):
    rubric = json.loads((Path(__file__).parent / "fixtures/rubric.json").read_text())
    rows = []
    for number, rules in rubric.items():
        path = root / "pages" / f"{int(number):03d}.md"
        text = normalize(path.read_text() if path.exists() else "")
        checks = [(anchor, normalize(anchor) in text) for anchor in rules["must"]]
        checks += [("must not include: " + anchor, normalize(anchor) not in text) for anchor in rules["forbidden"]]
        if number == "3":
            anchors = ["左栏甲", "左栏乙", "右栏甲", "右栏乙"]
            positions = [text.find(anchor) for anchor in anchors]
            checks += [("column order", min(positions) >= 0 and positions == sorted(positions))]
        rows.append({"page": int(number), "passed": sum(ok for _, ok in checks), "total": len(checks),
                     "failed_checks": [label for label, ok in checks if not ok]})
    return {"passed": sum(r["passed"] for r in rows), "total": sum(r["total"] for r in rows), "pages": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("outputs", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps({str(root): score(root) for root in args.outputs}, ensure_ascii=False, indent=2))
