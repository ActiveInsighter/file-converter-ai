"""The download package: one merged Markdown, one file per page, the figures.

    output/
    ├── <name>.md        document info + every page in order
    ├── pages/0001.md    one file per page with content
    └── images/          figure crops referenced by the Markdown

All intermediate state stays in work/.
"""
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from urllib.parse import quote

from render_stream import RenderPlan

FALLBACK_LABELS = {'model': '未用 OCR 草稿（仅图片转写）的页码', 'ocr': '模型失败、直接使用 OCR 结果的页码'}


@dataclass(frozen=True)
class ResultFiles:
    markdown: Path
    partial: bool


def validate_output_name(name: str) -> str:
    name = name.strip()
    if (not name or len(name) > 120 or len(name.encode('utf-8')) > 240 or name.startswith('.')
            or name.lower().endswith(('.md', '.zip'))
            or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)
            or name.endswith(('.', ' '))
            or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I)):
        raise ValueError('Invalid output_name: use a basename without an extension, '
                         'up to 120 characters / 240 UTF-8 bytes')
    return name


def prepare_directories(work_dir: Path, output_dir: Path) -> Path:
    """Reset dedicated generated directories, never ancestors or shared paths."""
    work, output = work_dir.resolve(), output_dir.resolve()
    cwd = Path.cwd().resolve()
    if (work == output or work in output.parents or output in work.parents
            or output == cwd or output in cwd.parents
            or work_dir.is_symlink() or output_dir.is_symlink()):
        raise ValueError('work_dir and output_dir must be separate dedicated directories')
    state = work / 'conversion'
    if state.is_symlink() or state == cwd or state in cwd.parents:
        raise ValueError('work/conversion must not be a symlink or contain the current directory')
    if any(path.exists() and not path.is_dir() for path in (state, output)):
        raise ValueError('Generated paths must be directories')
    for directory in (state, output):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
    return state


def page_ranges(pages) -> str:
    numbers = sorted(set(pages))
    ranges = []
    for number in numbers:
        if ranges and ranges[-1][1] + 1 == number:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    return '、'.join(str(first) if first == last else f'{first}–{last}'
                    for first, last in ranges)


def _text(value: str) -> str:
    value = ' '.join(value.split())
    return re.sub(r'([\\`*_{}\[\]<>()!#|])', r'\\\1', value)


def _write(path: Path, content: str) -> None:
    """Replace a file atomically so a reader never sees half of it."""
    if path.exists() and path.read_text(encoding='utf-8') == content:
        return
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_text(content, encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_result(*, output_dir: Path, state_dir: Path, name: str, source_url: str,
                 plan: RenderPlan, provider: str, model: str, ocr_model: str | None,
                 converted_at: str, pages, results: list[dict],
                 pipeline_error: str | None) -> ResultFiles:
    name = validate_output_name(name)
    indexed = {result['chunk']: result for result in results}
    completed, blanks = set(), set()
    by_source: dict[str, set[int]] = {}
    contents: list[tuple[str, str]] = []
    for page in sorted(pages, key=lambda page: page.number):
        result = indexed.get(page.stem, {})
        if result.get('status') not in {'ok', 'blank'}:
            continue
        completed.add(page.number)
        if result['status'] == 'blank':
            blanks.add(page.number)
            continue
        by_source.setdefault(result.get('source', 'model'), set()).add(page.number)
        text = (state_dir / 'pages' / f'{page.stem}.md').read_text(encoding='utf-8').strip()
        if text:
            contents.append((page.stem, text))
        else:
            blanks.add(page.number)
    missing = set(range(plan.first, plan.last + 1)) - completed
    partial = bool(missing or pipeline_error)
    status = '部分完成' if partial else '完成'
    # An angle-bracket link destination with percent-encoded control/HTML bytes
    # prevents an untrusted PDF URL from adding Markdown or HTML to the preface.
    url = quote(source_url, safe="/:?#[]@!$&'()*+,;=%~._-")
    pipeline = f'{_text(ocr_model)} → ' if ocr_model else ''
    lines = [
        '# 文档信息', '',
        f'- PDF 来源：[查看原始 PDF](<{url}>)',
        f'- 转换时间：{_text(converted_at)}',
        f'- 原始页数：{plan.total_pdf_pages}',
        f'- 转换范围：{page_ranges(range(plan.first, plan.last + 1))} 页',
        f'- 转换结果：{status}（{len(completed)}/{plan.page_count} 页）',
        f'- 识别流程：{pipeline}{_text(provider)} / {_text(model)}',
    ]
    if ocr_model:
        for source in ('model', 'ocr'):
            if by_source.get(source):
                lines.append(f'- {FALLBACK_LABELS[source]}：{page_ranges(by_source[source])}')
    if missing:
        lines.append(f'- 未完成页码：{page_ranges(missing)}')
    if blanks:
        lines.append(f'- 空白页码：{page_ranges(blanks)}')
    if pipeline_error:
        lines.append('- 处理未全部完成，已保留成功页面。')
    header = '\n'.join(lines) + '\n'

    body = '\n\n'.join(f'<!-- 第 {int(stem)} 页 -->\n\n{text}' for stem, text in contents)
    destination = output_dir / f'{name}.md'
    _write(destination, header + '\n---\n\n' + body.rstrip() + '\n')

    pages_dir = output_dir / 'pages'
    images_dir = output_dir / 'images'
    linked: set[str] = set()
    current = set()
    for stem, text in contents:
        pages_dir.mkdir(exist_ok=True)
        current.add(f'{stem}.md')
        linked.update(re.findall(r'\]\(images/([^)\s/]+)\)', text))
        # Page files sit one level down, so their figure links do too.
        _write(pages_dir / f'{stem}.md', re.sub(r'\]\(images/', '](../images/', text) + '\n')
    if pages_dir.exists():
        for stale in pages_dir.iterdir():
            if stale.name not in current:
                stale.unlink()
    for figure in sorted(linked):
        source = state_dir / 'figures' / figure
        target = images_dir / figure
        if source.is_file() and not target.exists():
            images_dir.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
    return ResultFiles(destination, partial)
