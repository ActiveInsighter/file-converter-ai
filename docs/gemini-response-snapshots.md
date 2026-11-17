# Public Gemini response capture

The Actions text executor now saves a version 1 `response` alongside `reply`, `usage`,
`finishReason`, and `error` through PocketBase's existing authenticated `complete`
endpoint. It receives a single JSON generateContent response, or polls a background
Antigravity interaction with GET. Normal execution does not open an SSE stream.
`httpx.client.stream()` only bounds the bytes of those JSON HTTP responses; it does
not select a streaming model endpoint or forward partial tokens.

`response_capture.py` converts provider parts/steps in order: public thought summaries,
function/code/search/MCP/URL tool calls and results, public output text and artifact
descriptors. Candidate safety ratings, citations, grounding, URL context, provider
usage, finish reasons and model/response identifiers are retained in metadata. Tool
call/result IDs are preserved where the provider supplies them, without guessing
associations or execution durations. Unknown public step kinds remain inspectable.

Known Gemini thinking models request `includeThoughts: true`. Antigravity uses its
supported agent configuration; no undocumented field is added to the managed agent
request. Its final `thought.summary` is captured when returned. A thought token counter
is not a public summary: raw thought bodies and encrypted signatures are excluded.

Snapshots are limited to 2 MiB and 500 parts, with 64,000 characters per scalar and
bounded nested collections. Truncation/omissions are explicit, while the reply is
stored independently under its existing 2 MiB limit. Configured API/GitHub/database
credentials, Authorization values and common token formats are redacted from tool
payloads before snapshots or recovery artifacts are written. Structured secret fields
are removed. No request headers or remote-environment configuration is stored.

Follow-ups use `job.history`, built by the server from owned persisted messages.
Gemini receives proper user/model history entries; managed agents receive the saved
conversation transcript as context for a new interaction. Tool execution remains on
the provider, never in the browser. Each batch segment remains independent.

Deploy the additive backend migration/hooks first, push this executor branch, then
pin the coordinator's `GITHUB_GEMINI_RESPONSE_REF` to this tested commit. New clients'
`responseVersion: 1` selects that trusted ref; legacy tasks still dispatch main.
No workflow input accepts arbitrary code, secrets, prompts or Git refs.

Validation: `python -m unittest discover -s tests` (305 tests at implementation time)
includes public summary capture, tool association, metadata preservation, secret
redaction, trace bounds, durable history, no SSE, retry/persistence and existing PDF
regressions. Old replies remain readable; unavailable historical telemetry is not
invented by the migration.
