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
        ('OCR + model / model only / OCR only',
         f"{sources.get('ocr+model', 0)} / {sources.get('model', 0)} / {sources.get('ocr', 0)}"),
        ('OCR pages / failed / seconds',
         f"{ocr.get('pages', 0)} / {ocr.get('failed_pages', 0)} / {ocr.get('seconds', 0)}" if ocr else 'off'),
        ('OCR jobs by link / relay / uploaded',
         f"{ocr.get('remote_jobs', 0)} / {ocr.get('relay_jobs', 0)} / {ocr.get('uploaded_jobs', 0)}"
         if ocr else 'off'),
        ('Model HTTP 429 / 503', f"{performance.get('429', 0)} / {performance.get('503', 0)}"),
        ('Model request P50 / P95 seconds', f"{performance.get('p50_seconds')} / {performance.get('p95_seconds')}"),
        ('Cumulative quota wait seconds (across requests)', performance.get('quota_wait_seconds', 0)),
        ('Retired model keys / OCR tokens',
         f"{sum(bool(key.get('disabled')) for key in usage.get('keys', {}).values())} / {ocr.get('retired_tokens', 0)}"),
        ('OCR polling retries (same job)', ocr.get('poll_retries', 0)),
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
