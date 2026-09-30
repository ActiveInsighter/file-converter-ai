"""Two user-facing Markdown files; all intermediate state stays in work/."""
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from urllib.parse import quote

from render_stream import RenderPlan


@dataclass(frozen=True)
class ResultFiles:
    markdown: Path
    partial: bool


def validate_output_name(name: str) -> str:
    name = name.strip()
    if (not name or len(name) > 120 or len(name.encode('utf-8')) > 240 or name.startswith('.')
            or name.casefold() == 'source' or name.lower().endswith(('.md', '.zip'))
            or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)
            or name.endswith(('.', ' '))
            or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I)):
        raise ValueError('Invalid output_name: use a basename without an extension, '
                         'up to 120 characters / 240 UTF-8 bytes; source is reserved')
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


def write_result(*, output_dir: Path, state_dir: Path, name: str, source_url: str,
                 plan: RenderPlan, provider: str, model: str, converted_at: str,
                 chunks, results: list[dict], pipeline_error: str | None) -> ResultFiles:
    name = validate_output_name(name)
    indexed = {result['chunk']: result for result in results}
    completed, incomplete_regions, blanks = set(), set(), set()
    blocks = []
    for chunk in sorted(chunks, key=lambda chunk: chunk.start_page):
        result = indexed.get(chunk.stem, {})
        if result.get('status') not in {'ok', 'blank'}:
            continue
        pages = range(chunk.start_page, chunk.end_page + 1)
        completed.update(pages)
        if result.get('missing_parts'):
            incomplete_regions.update(pages)
        if result['status'] == 'blank':
            blanks.update(pages)
            continue
        text = (state_dir / 'pages' / f'{chunk.stem}.md').read_text(encoding='utf-8').strip()
        if text:
            blocks.append(text)
    missing = set(range(plan.first, plan.last + 1)) - completed
    partial = bool(missing or incomplete_regions or pipeline_error)
    status = '部分完成' if partial else '完成'
    # An angle-bracket link destination with percent-encoded control/HTML bytes
    # prevents an untrusted PDF URL from adding Markdown or HTML to the preface.
    url = quote(source_url, safe="/:?#[]@!$&'()*+,;=%~._-")
    lines = [
        '# 文档信息', '',
        f'- PDF 来源：[查看原始 PDF](<{url}>)',
        f'- 转换时间：{_text(converted_at)}',
        f'- 原始页数：{plan.total_pdf_pages}',
        f'- 转换范围：{page_ranges(range(plan.first, plan.last + 1))} 页',
        f'- 转换结果：{status}（{len(completed)}/{plan.page_count} 页）',
        f'- 请求模型：{_text(provider)} / {_text(model)}',
    ]
    if missing:
        lines.append(f'- 未完成页码：{page_ranges(missing)}')
    if incomplete_regions:
        lines.append(f'- 部分内容未识别页码：{page_ranges(incomplete_regions)}')
    if blanks:
        lines.append(f'- 空白页码：{page_ranges(blanks)}')
    if pipeline_error:
        lines.append('- 处理未全部完成，已保留成功页面。')
    source = '\n'.join(lines) + '\n'
    source_path = output_dir / 'source.md'
    destination = output_dir / (name + ('.partial.md' if partial else '.md'))
    for path, content in [(source_path, source),
                          (destination, source + '\n---\n\n' + '\n\n'.join(blocks).rstrip() + '\n')]:
        temporary = path.with_suffix(path.suffix + '.tmp')
        try:
            temporary.write_text(content, encoding='utf-8')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    other = output_dir / (name + ('.md' if partial else '.partial.md'))
    other.unlink(missing_ok=True)
    return ResultFiles(destination, partial)
