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

144 local tests passed, including all 14 Valkey integration tests against isolated test keys. Regression tests were observed failing before fixes for total HTTP deadlines, cancelled HTTP/report/lease cleanup, same-ID replay, stale same-name pages and stale output indexes after setup failure. A real PDF/local HTTP gateway end-to-end fixture verifies slow-page speculation, first-valid selection, cancellation, quota charging, blank-page skipping, manifest and ordered Markdown output.

## Branch and service deployment

- Branch: `codex/streaming-tail-recovery-20260930`.
- GitHub CI at `7fbdab2`: [36667182248](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36667182248), success. The Actions environment runs the complete suite with a real Valkey service.
- Original page 51 smoke: [36667208141](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36667208141), success; page processing 21.12 seconds, progress 100%, no failures. The converter step took 28 seconds including source download.
- The shared quota service's Lua cancellation report update was deployed while `activeLeases=0`. The original file was backed up under `/opt/file-converter-quota-api/backups/scheduler.lua-20260930-tail`. `/healthz` passed and daily counters were identical before and after the restart. Other quota limits and service settings were unchanged.
- Manual workflow inputs stay within GitHub's 25-input limit. Advanced hedge controls use CLI arguments, repository-dispatch payload, or repository Variables. Existing manual/API input names are preserved.

## First complete comparison and follow-up correction

The first full 196-page branch test [36667295224](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36667295224) succeeded with all 196 pages, no failures, no missing split regions, and a 100% progress snapshot. The converter step took 4m31s versus 6m59s in the original run (about 35% shorter). The entire job took 4m46s. 205 requests were charged versus 200 originally; two pages needed complete split recovery. HTTP p50 was 12.98s, p95 25.05s, and 429/503 counts were zero.

The first HTTP request began at 04:05:38.832 UTC and the last page was rendered at 04:07:31.885, confirming overlap. Rendering took about 114 seconds in this run, versus 65 seconds originally; shared CPU and PNG/Base64 work also affect these measurements. This is one observed run, not a guaranteed speedup.

The run exposed a further worst-case bug: page 63's original and two speculative copies all timed out. Since the copies started a minute later, the race waited roughly 180 seconds before its next retry. The page eventually succeeded in the same model with elapsed time 243.89 seconds. `RequestPolicy` now applies the primary request's total deadline to the entire race, cancels any still-pending copies at that deadline, and retries without waiting for another full timeout. A regression fixture checks all three blocked attempts are cancelled at the shared deadline. Cleanup remains separately bounded. A response validated before the group deadline is preserved even if its bounded quota-success report finishes afterwards; refusals and validation errors are not eligible for this grace.

A second full real-document run [36668645372](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36668645372), at `2bdbe50`, verifies the shared-deadline revision. Subsequent regressions additionally check that unfinished HTTP is cancelled before waiting for a validated response's report, and a refusal/permanent error keeps its classification when other copies remain stalled.


## Shared-deadline real-document result

Run [36668645372](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36668645372), at `2bdbe50`, succeeded with 196/196 page blocks, zero failures, zero missing split regions, and 100% progress. The converter step took **3m21s** versus **6m59s** in the reported baseline (about **52% shorter**). The entire job took 3m29s versus 7m11s. The pipeline snapshot measured 191.75 seconds, excluding source download and setup.

Six extra copies were launched within the 20-copy budget. One duplicate won (page 43); the remaining race outcomes used the original valid response. Six cancelled requests were reported and their charged quota was preserved. Total charged requests were **206 versus 200** originally, an increase of **3%**; 200 HTTP responses completed successfully, including complete split recovery of pages 10 and 20. No 429/503 or other HTTP errors occurred. HTTP p50 was 12.72s and p95 26.39s. Page 51 finished in 38.69s from render-ready time; page 63 finished in 41.02s.

The first HTTP request started at 04:24:07.426 UTC, while the last page was rendered at 04:26:03.483. All individual page Markdown files and the merged file, manifest, progress and quota artifacts were downloaded and verified. These are observed runs with a nondeterministic upstream, not a guaranteed runtime or transcription-quality benchmark.

Two subsequent cleanup/classification refinements were covered by regression tests: cancel unfinished HTTP immediately at the group deadline while retaining a valid response's bounded success report; preserve refusal classification; do not interrupt already-running lease cancellation cleanup with a second cancellation. The final commit's real-page smoke and CI are recorded below.

A real request/quota-client fixture additionally demonstrates delayed, already-charged hedge lease responses across the group deadline: valid Markdown is preserved; all three charged requests are counted; two cancellations are reported; no server leases remain outstanding. The pre-fix policy left two charged leases orphaned. Cancellation is now requested once per task so existing cleanup can drain safely.


## Final source revision verification

Final executable source revision `14a6773`:

- [CI 36669343338](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36669343338): **144 tests passed**, no skips; compilation passed. Independent review also ran all 144 tests, including real Valkey integration, and approved the final source.
- [Real pages 51–63, run 36669362314](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/36669362314): **13/13 success**, no pipeline errors, 100% progress, 13 charged requests. Converter step 33s; page 51 took 19.83s, page 63 took 13.24s from render-ready time. This validates the exact final executable revision after the additional cancellation guards.
- All artifacts were downloaded and checked. For the complete 196-page run, model, concurrency, DPI, image format/resolution, verification setting and effective prompt hash match the baseline. The merged file exactly matches the ordered successful page files.
- Post-test quota-service status: healthy, **0 active leases**, max inflight 24, 2 requests/s, adaptive stage 0.

[Draft PR #27](https://github.com/ActiveInsighter/file-converter-ai/pull/27) contains the branch implementation and this evidence. Converter execution is deployed for branch testing through Actions; normal main-branch dispatches continue using the original converter until this branch is merged. The shared quota service already has the backward-compatible cancellation-report update and its rollback backup.
