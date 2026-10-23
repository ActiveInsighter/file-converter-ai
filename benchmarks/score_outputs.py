"""Deterministic anchor/ownership checks, not a general transcription accuracy score."""
import argparse
import html
import json
import re
from pathlib import Path
from html.parser import HTMLParser


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
    texts = {number: (root / "pages" / f"{int(number):03d}.md").read_text()
             if (root / "pages" / f"{int(number):03d}.md").exists() else "" for number in rubric}
    combined = normalize("\n".join(texts.values()))
    anchors = list(dict.fromkeys(anchor for rules in rubric.values() for anchor in rules["must"]))
    missing = [anchor for anchor in anchors if normalize(anchor) not in combined]
    math = texts["1"]
    for command, symbol in (("\\neq", "≠"), ("\\geq", "≥"), ("\\ge", "≥"),
                            ("\\leq", "≤"), ("\\le", "≤"), ("\\cap", "∩"),
                            ("\\varepsilon", "ε"), ("\\epsilon", "ε")):
        math = math.replace(command, symbol)
    math = normalize(math).replace("−", "-").replace("left", "").replace("right", "")
    math_checks = ["x≠0", "f(x)=x^2-3x+2", "n≥1", "P(A∩B)=0.125", "ε≤0.005"]
    math_missing = [formula for formula in math_checks if formula not in math]
    table = table_rows(texts["4"])
    expected_rows = [["A-01", "0.050", "-12.5", "未检出"],
                     ["B-02", "10.00", "+0.8", ""], ["C-03", "1.005", "0", "复测"]]
    table_missing = [row for row in expected_rows if row not in table]
    return {"page_checks_passed": sum(r["passed"] for r in rows), "page_checks_total": sum(r["total"] for r in rows),
            "document_content": {"passed": len(anchors) - len(missing), "total": len(anchors), "missing": missing},
            "formulas": {"passed": len(math_checks) - len(math_missing), "total": len(math_checks), "missing": math_missing},
            "table_rows": {"passed": len(expected_rows) - len(table_missing), "total": len(expected_rows), "missing": table_missing},
            "pages": rows}


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = ""

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.row is not None:
            self.row.append(normalize(self.cell or "").replace("−", "-"))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def table_rows(text):
    if "<table" in text.lower():
        parser = TableParser()
        parser.feed(text)
        return parser.rows
    return [[normalize(cell).replace("−", "-") for cell in line.strip()[1:-1].split("|")]
            for line in text.splitlines() if line.strip().startswith("|") and line.strip().endswith("|")]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("outputs", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps({str(root): score(root) for root in args.outputs}, ensure_ascii=False, indent=2))
