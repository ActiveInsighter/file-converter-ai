"""Publish the converter's durable progress in the GitHub Actions job summary."""
import json
import os
from pathlib import Path


def write_summary(state_dir: Path, destination: Path):
    progress_path = state_dir / 'progress.json'
    if not progress_path.exists():
        return
    progress = json.loads(progress_path.read_text())
    requests = progress.get('requests', {})
    rows = [
        ('State', progress['state']),
        ('Completed page blocks', f"{progress['completed_chunks']}/{progress['total_chunks']} ({progress['percent']}%)"),
        ('Rendered pages', f"{progress['rendered_pages']}/{progress['total_pages']}"),
        ('Successful / blank / failed', f"{progress['success_chunks']} / {progress['blank_chunks']} / {progress['failed_chunks']}"),
        ('Elapsed seconds', progress['elapsed_seconds']),
        ('Speculative copies / wins', f"{requests.get('hedges_launched', 0)} / {requests.get('hedge_wins', 0)}"),
    ]
    with destination.open('a') as handle:
        handle.write('## File conversion\n\n| Metric | Result |\n| --- | --- |\n')
        for label, value in rows:
            handle.write(f'| {label} | {value} |\n')
        handle.write('\nDownload the result artifact for source.md and the final Markdown. Missing pages or regions are listed in the preface.\n')


if __name__ == '__main__' and os.getenv('GITHUB_STEP_SUMMARY'):
    write_summary(Path('work/conversion'), Path(os.environ['GITHUB_STEP_SUMMARY']))
