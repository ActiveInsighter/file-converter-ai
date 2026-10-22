"""Publish the converter's durable progress in the GitHub Actions job summary."""
import json
import os
from pathlib import Path


def write_summary(state_dir: Path, destination: Path):
    progress_path = state_dir / 'progress.json'
    if not progress_path.exists():
        return
    progress = json.loads(progress_path.read_text())
    usage_path = state_dir / 'quota-usage.json'
    usage = json.loads(usage_path.read_text()) if usage_path.exists() else {}
    requests = progress.get('requests', {})
    sources = usage.get('page_sources') or {}
    performance = usage.get('performance') or {}
    spread = usage.get('spread') or {}
    ocr = usage.get('ocr') or {}
    rows = [
        ('State', progress['state']),
        ('Completed pages', f"{progress['completed_chunks']}/{progress['total_chunks']} ({progress['percent']}%)"),
        ('Successful / blank / failed', f"{progress['success_chunks']} / {progress['blank_chunks']} / {progress['failed_chunks']}"),
        ('OCR + model / text layer + model / model only / OCR only',
         f"{sources.get('ocr+model', 0)} / {sources.get('text+model', 0)} / "
         f"{sources.get('model', 0)} / {sources.get('ocr', 0)}"),
        ('OCR pages / failed / seconds',
         f"{ocr.get('pages', 0)} / {ocr.get('failed_pages', 0)} / {ocr.get('seconds', 0)}" if ocr else 'off'),
        ('OCR jobs by link / uploaded',
         f"{ocr.get('remote_jobs', 0)} / {ocr.get('uploaded_jobs', 0)}" if ocr else 'off'),
        ('Model HTTP 429 / 503', f"{performance.get('429', 0)} / {performance.get('503', 0)}"),
        ('Requests per key bucket (min–max)', f"{spread.get('min_requests', 0)}–{spread.get('max_requests', 0)}"),
        ('Elapsed seconds', progress['elapsed_seconds']),
        ('Speculative copies / wins', f"{requests.get('hedges_launched', 0)} / {requests.get('hedge_wins', 0)}"),
    ]
    with destination.open('a') as handle:
        handle.write('## File conversion\n\n| Metric | Result |\n| --- | --- |\n')
        for label, value in rows:
            handle.write(f'| {label} | {value} |\n')
        handle.write('\nDownload the result artifact for the merged Markdown, per-page files and figures. '
                     'Missing pages are listed in the merged file header.\n')


if __name__ == '__main__' and os.getenv('GITHUB_STEP_SUMMARY'):
    write_summary(Path('work/conversion'), Path(os.environ['GITHUB_STEP_SUMMARY']))
