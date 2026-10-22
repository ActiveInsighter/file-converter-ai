"""Render lazily on a single worker thread; never share a PyMuPDF document."""
from dataclasses import dataclass
from pathlib import Path

import fitz


@dataclass(frozen=True)
class RenderPlan:
    total_pdf_pages: int
    first: int
    last: int
    width: int

    @property
    def page_count(self):
        return self.last - self.first + 1

    @classmethod
    def from_pdf(cls, path: Path, start_page=None, end_page=None):
        with fitz.open(path) as document:
            total = document.page_count
        first = start_page or 1
        last = end_page if end_page is not None else total
        if total < 1 or first < 1 or last > total or first > last:
            raise ValueError(f'Invalid page range {first}-{last}; PDF has {total} pages')
        return cls(total, first, last, max(3, len(str(total))))


#: Share of the page height kept in the edge strips sent with neighbouring pages.
EDGE_FRACTION = 0.4


def render_pages(pdf_path, image_dir, plan, dpi, jpeg_quality, image_format, classify_blank,
                 edges=False):
    """Yield ``(path, blank, (top, bottom))`` per page; the strips exist only with ``edges``."""
    image_dir.mkdir(parents=True, exist_ok=True)
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    extension = 'png' if image_format == 'png' else 'jpg'

    def save(pix, path):
        path.write_bytes(pix.tobytes('png') if image_format == 'png'
                         else pix.tobytes('jpeg', jpg_quality=jpeg_quality))
        return path

    with fitz.open(pdf_path) as document:
        for number in range(plan.first, plan.last + 1):
            page = document[number - 1]
            stem = f'{number:0{plan.width}d}'
            path = save(page.get_pixmap(matrix=matrix, alpha=False), image_dir / f'{stem}.{extension}')
            strips = (None, None)
            if edges:
                box, cut = page.rect, page.rect.height * EDGE_FRACTION
                top = fitz.Rect(box.x0, box.y0, box.x1, box.y0 + cut)
                bottom = fitz.Rect(box.x0, box.y1 - cut, box.x1, box.y1)
                strips = tuple(
                    save(page.get_pixmap(matrix=matrix, alpha=False, clip=clip),
                         image_dir / f'{stem}.{name}.{extension}')
                    for name, clip in (('top', top), ('bottom', bottom))
                )
            blank, ink_ratio, darkest = classify_blank(page)
            detail = f' blank ink_ratio={ink_ratio:.6f} darkest={darkest}' if blank else ''
            print(f'[render] {number}/{plan.total_pdf_pages}: {path.name}{detail}', flush=True)
            yield path, blank, strips
