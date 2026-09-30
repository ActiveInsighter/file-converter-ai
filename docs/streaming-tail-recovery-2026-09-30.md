# Streaming conversion and slow request recovery

The reported run is [36664110275](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36664110275), on main commit `23a7868`.

The 196-page document used Gemini Flash-Lite, concurrency 20, 240 DPI PNG, ultra-high image resolution and no verification passes. Page 51 finished 344.84 seconds after processing began. The other 195 pages finished roughly 153 seconds after processing began. The first failure retried after 7.2 seconds; the second failure deferred to another round with an additional 51-second pause. The final request succeeded in roughly 17 seconds. Throughout the tail, 429/503 counters were zero and only 1 of 24 global slots was occupied. Original logs record only `RuntimeError`, so they do not establish the upstream cause of the two failed requests; new logs include the error detail.

Rendering ran from 03:23:02.966 to 03:24:08.079 UTC; the first model processing started after 03:24:09.250. The converter step took about 6m59s, and the complete Action took 7m11s.

HTTPX timeouts bound inactivity between network operations; they do not enforce a total request deadline. See the [official HTTPX timeout documentation](https://www.python-httpx.org/advanced/timeouts/) and [Python asyncio cancellation/deadline documentation](https://docs.python.org/3/library/asyncio-task.html).

## Implementation

- `render_stream.py`: lazy rendering, a PyMuPDF document confined to one rendering thread.
- `pipeline.py`: bounded producer queue (2 × workers), fresh-page priority, independent delayed retries, atomic progress/result indexes.
- `request_policy.py`: primary plus at most two speculative copies, first valid response wins, all attempts share the configured concurrency ceiling and quota pool. The age timer starts after lease acquisition, not while waiting for quota.
- `pdf2md.py`: provider/recovery orchestration, explicit total HTTP deadlines, bounded cancellation-safe lease reporting, partial-result salvage, merging only current successful pages.
- `quota_client.py` and scheduler Lua: cancellation cleanup, same-ID lease replay across wall-clock timeouts, neutral 499 reports that release capacity without refunding requests or changing overload ratios.

The default speculation threshold is max(60 seconds, 2 × successful HTTP P95). The default extra-task budget is max(2, ceil(chunks × 10%)). Copies still consume quota and wait for both local and shared capacity. Recent 429/503 pressure disables new speculative copies. No default global rate or image quality was changed.

Live progress is currently exposed through converter logs and progress.json; the Actions job summary publishes the final snapshot. The separate AnyWorkflow Remote UI has not been changed to consume per-page progress.

## Validation

138 local tests passed, including all 14 Valkey integration tests against isolated test keys. Regression tests were observed failing before fixes for total HTTP deadlines, cancelled HTTP/report/lease cleanup, same-ID replay, stale same-name pages and stale output indexes after setup failure. A real PDF/local HTTP gateway end-to-end fixture verifies slow-page speculation, first-valid selection, cancellation, quota charging, blank-page skipping, manifest and ordered Markdown output.

GitHub branch deployment and real-document measurements will be appended after completion.
