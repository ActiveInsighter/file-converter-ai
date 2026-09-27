"""Name the merged Markdown without ever including the source PDF in artifacts."""
import os
import re
from pathlib import Path


def prepare_result(output: Path, name: str) -> None:
    name = re.sub(r'\.(zip|md)$', '', name.strip(), flags=re.I)
    if (not name or len(name) > 120 or name in {'.', '..'}
            or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)
            or name.endswith(('.', ' '))
            or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', name, re.I)):
        raise ValueError('Invalid output_name: use a filename without paths, up to 120 characters')
    for suffix in ('.md', '.partial.md'):
        source = output / ('merged' + suffix)
        target = output / (name + suffix)
        if source.exists() and source != target:
            source.rename(target)


if __name__ == '__main__':
    prepare_result(Path('output'), os.environ.get('OUTPUT_NAME') or 'merged')
