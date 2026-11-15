"""Recover adjacent pieces of one diagram without joining across body text.

Paddle's ``large`` box mode only filters nested detections. A flow diagram can
still arrive as separate trees, arrows and labels. Re-crop nearby image blocks
from the original PDF, preserving their spatial relationship and resolution.
"""
from __future__ import annotations

import html
import math
import re
from pathlib import Path

import fitz

_IMG = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"[^>]*?/?>', re.I)
_BOX = re.compile(r'img_in_(?:image|chart)_box_(\d+)_(\d+)_(\d+)_(\d+)\.[a-z]+$', re.I)
_WRAPPERS = re.compile(r'</?div\b[^>]*>', re.I)


def _near(a: fitz.Rect, b: fitz.Rect, page_width: float) -> bool:
    overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
    # Horizontal chains such as tree -> arrow -> tree. Separate diagram rows
    # and widely spaced independent illustrations retain separate references.
    gap = max(a.x0 - b.x1, b.x0 - a.x1, 0)
    return (overlap >= 0.5 * min(a.height, b.height)
            and gap <= min(0.04 * page_width, 0.5 * min(a.height, b.height)))


def merge_figure_fragments(text: str, sources: dict[str, str], result: dict,
                           pdf_path: Path | None, number: int, width: int,
                           directory: Path) -> tuple[str, dict[str, Path], set[str]]:
    """Return edited HTML, locally recovered figures and superseded sources.

    Missing/invalid geometry leaves the original OCR result intact. Only image
    tags separated by whitespace/layout wrappers may join; captions and prose
    delimit runs. Coordinates come from Paddle's image names and page metadata.
    """
    metadata = result.get('prunedResult') or {}
    if not pdf_path or not isinstance(metadata, dict):
        return text, {}, set()
    try:
        page_width, page_height = float(metadata['width']), float(metadata['height'])
    except (KeyError, TypeError, ValueError):
        return text, {}, set()
    if not all(math.isfinite(v) and v > 0 for v in (page_width, page_height)):
        return text, {}, set()
    settings = metadata.get('model_settings') or {}
    # A transformed OCR page no longer shares coordinates with the original.
    if settings.get('use_doc_preprocessor'):
        return text, {}, set()

    runs, run = [], []
    previous_end = 0
    for match in _IMG.finditer(text):
        source = html.unescape(match.group(1))
        box = _BOX.search(source)
        coords = list(map(int, box.groups())) if box else []
        rect = fitz.Rect(coords) if coords else None
        valid = (source in sources and rect is not None and not rect.is_empty
                 and rect.x0 >= 0 and rect.y0 >= 0
                 and rect.x1 <= page_width and rect.y1 <= page_height)
        if not valid or _WRAPPERS.sub('', text[previous_end:match.start()]).strip():
            if run:
                runs.append(run)
            run = []
        if valid:
            run.append((match, source, rect))
        previous_end = match.end()
    if run:
        runs.append(run)

    groups = []
    for run in runs:
        remaining = list(run)
        while remaining:
            group = [remaining.pop(0)]
            while True:
                neighbours = [item for item in remaining
                              if any(_near(item[2], other[2], page_width) for other in group)]
                if not neighbours:
                    break
                group.extend(neighbours)
                remaining = [item for item in remaining if item not in neighbours]
            if len(group) > 1 and len({item[1] for item in group}) == len(group):
                groups.append(group)
    if not groups:
        return text, {}, set()

    figures, removed, edits = {}, set(), []
    with fitz.open(pdf_path) as document:
        page = document[number - 1]
        # Account for the OCR renderer's DPI, including compact relay uploads.
        if abs((page_width / page_height) / (page.rect.width / page.rect.height) - 1) > 0.02:
            return text, {}, set()
        sx, sy = page.rect.width / page_width, page.rect.height / page_height
        for group in groups:
            bounds = fitz.Rect(group[0][2])
            for _, _, rect in group[1:]:
                bounds |= rect
            # Refuse unions that would enclose another body-text/table block.
            blocked = False
            for block in metadata.get('parsing_res_list') or []:
                if block.get('block_label') in {'image', 'chart'}:
                    continue
                coords = block.get('block_bbox')
                if not isinstance(coords, list) or len(coords) != 4:
                    continue
                rect = fitz.Rect(coords)
                intersection = rect & bounds
                if (not rect.is_empty and intersection.get_area() > 0.2 * rect.get_area()
                        and not any((rect & item[2]).get_area() > 0.8 * rect.get_area()
                                    for item in group)):
                    blocked = True
                    break
            if blocked:
                continue
            clip = fitz.Rect(bounds.x0 * sx, bounds.y0 * sy,
                             bounds.x1 * sx, bounds.y1 * sy)
            clip = (clip + (-1, -1, 1, 1)) & page.rect
            link = f'images/p{number:0{width}d}-merged-{len(figures) + 1}.png'
            path = directory / Path(link).name
            directory.mkdir(parents=True, exist_ok=True)
            page.get_pixmap(dpi=240, clip=clip, alpha=False).save(path)
            figures[link] = path
            first = min(group, key=lambda item: item[0].start())
            for match, source, _ in group:
                edits.append((match.start(), match.end(),
                              f'<img src="{link}" />' if match is first[0] else ''))
                removed.add(source)
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, figures, removed
