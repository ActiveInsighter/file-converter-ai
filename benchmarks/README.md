# Reproducible converter checks

The synthetic PDF covers Chinese negation and choices, formulas, decimal
precision, two columns, a table with an empty cell, footnotes, and a continuation
across pages. It contains no user document. `baseline-prompts.json` preserves
the prompts from `b12eaef`. The PDF is committed so both revisions download
exactly the same bytes; regeneration is optional:

```bash
.venv/bin/python benchmarks/make_corpus.py
```

The real API comparison uses the existing `pdf-to-md.yml` workflow, first at
`fix/large-pdf-relay-adjacent` (`b12eaef`), then at the candidate revision. Use
the committed PDF's raw URL as `source_url`. Keep the following equal:

```text
provider=gemini model=gemini-3.5-flash-lite model_fallbacks=none
concurrency=3 dpi=240 image_format=png thinking_level=high
media_resolution=ultra_high adjacent_pages=true verification_passes=0
```

Run one pair with both prompts empty and another with `system_prompt` set to
the `default` value in `baseline-prompts.json`. The latter verifies compatibility
with an already saved system prompt. Download each result artifact, then run:

```bash
.venv/bin/python benchmarks/score_outputs.py work/audit/live/<baseline-run> work/audit/live/<candidate-run>
```

Scores distinguish document content, formula symbols, table cells, and page
placement. A baseline that moves a continuation to the preceding page retains
the content at document level; that must not be reported as lost content or a
general OCR accuracy improvement. The checks are a small regression corpus,
not an estimate of production accuracy. Outputs still need review for arbitrary
PDFs and alternate equivalent formula syntax may need human adjudication.

For local timing, unpack the baseline into an isolated directory under `work/`
and run the two commands **sequentially** on the same machine:

```bash
mkdir -p work/baseline/work
git archive b12eaef | tar -x -C work/baseline
.venv/bin/python benchmarks/runtime_compare.py --code-root work/baseline --result work/audit/runtime-baseline.json
.venv/bin/python benchmarks/runtime_compare.py --code-root . --result work/audit/runtime-candidate.json
```

Rendering uses 20 pages at 240 DPI PNG with adjacent strips, three repeats of
blank and text-only documents. Credit recovery replays HTTP 402 followed by a
valid response through the real page scheduler and HTTP adapter using a local
mock transport. Its page backoff is fixed at 3 seconds to make the comparison
repeatable; it is not a measurement of API network latency. Request counts and
final page status are recorded with timing.

The nine original reliability regression tests were also executed against an
unmodified baseline checkout and reproduced failures before the fixes. Local
logs and historical document URLs remain under ignored `work/audit/`.

See [the audit report](../docs/reliability-quality-2026-10-23.md) for run links,
timings, scope, and checked-in synthetic outputs.
