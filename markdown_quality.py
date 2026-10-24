"""Conservative, content-preserving cleanup and QA for OCR/model Markdown.

Never infer text from missing figure crops or remove a Markdown image link.
"""
from __future__ import annotations

import re

_DIV_BLOCK = re.compile(
    r"<div\b(?P<attrs>[^>]*)>(?P<body>(?:(?!<div\b).)*?)</div\s*>",
    re.I | re.S,
)
_SAFE_DIV_ATTR = re.compile(
    r"""(?:class\s*=\s*["']analysis-box["']|align\s*=\s*["']?right["']?|style\s*=\s*["']\s*text-align\s*:\s*right\s*;?\s*["'])""",
    re.I,
)
_TABLE_TAG = re.compile(r"<(?P<tag>table|td|th)\b(?P<attrs>[^>]*)>", re.I)
_STYLE_ATTR = re.compile(r"""\s+style\s*=\s*(["'])(.*?)\1""", re.I | re.S)
_BORDER_ATTR = re.compile(r"""\s+border\s*=\s*(?:(["'])\d+\1|\d+)""", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_IMAGE = re.compile(r"!\[[^\]\n]*\]\(images/[^)\s]+\)")
_TAG = re.compile(r"</?[a-z][a-z\d-]*\b[^>]*>", re.I)
_PAGE = re.compile(r"<!-- 第 (\d+) 页 -->")


def clean_layout_markup(markdown: str) -> str:
    """Remove known decorative wrappers and fix MDX syntax without losing text.

    HTML tables and all image links are preserved: lossless cleanup must not
    guess whether a figure is an illustration or a grammar diagram.
    """
    def unwrap(match: re.Match[str]) -> str:
        attrs = match.group("attrs").strip()
        if not attrs or _SAFE_DIV_ATTR.fullmatch(attrs):
            return match.group("body").strip("\n")
        return match.group(0)

    previous = None
    result = markdown
    while previous != result:
        previous, result = result, _DIV_BLOCK.sub(unwrap, result)

    def sanitize_table_tag(match: re.Match[str]) -> str:
        tag, attrs = match.group("tag"), match.group("attrs")
        # Convert only simple text alignment; retain unrecognised markup.
        def style_replacement(style: re.Match[str]) -> str:
            css = style.group(2)
            alignment = re.search(r"(?:^|;)\s*text-align\s*:\s*(left|right|center)\b", css, re.I)
            if tag.lower() in {"td", "th"} and alignment:
                return f' align="{alignment.group(1).lower()}"'
            return ""
        attrs = _STYLE_ATTR.sub(style_replacement, attrs)
        attrs = _BORDER_ATTR.sub("", attrs)
        return f"<{tag}{attrs}>"

    result = _TABLE_TAG.sub(sanitize_table_tag, result)
    return _BR.sub("<br />", result)


def markdown_quality_metrics(markdown: str) -> dict:
    """Structural metrics; these are indicators, not semantic OCR accuracy."""
    parts = _PAGE.split(markdown)
    pages = {}
    for i in range(1, len(parts), 2):
        number, body = int(parts[i]), parts[i + 1]
        images = len(_IMAGE.findall(body))
        plain = _IMAGE.sub("", body)
        plain = _TAG.sub("", plain)
        pages[number] = {
            "images": images,
            "text_chars": len(re.sub(r"\s", "", plain)),
            "html_tags": len(_TAG.findall(body)),
            "nbsp": body.count("&nbsp;"),
        }
    return {
        "pages": len(pages),
        "images": len(_IMAGE.findall(markdown)),
        "html_tags": len(_TAG.findall(markdown)),
        "div_tags": len(re.findall(r"</?div\b", markdown, re.I)),
        "nbsp": markdown.count("&nbsp;"),
        "image_heavy_pages": sorted(n for n, data in pages.items() if data["images"] >= 6),
        "pages_by_number": pages,
    }
