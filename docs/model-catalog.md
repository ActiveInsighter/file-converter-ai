# Official Gemini model catalog

`gemini-models.yml` runs daily at 02:17 UTC, via workflow_dispatch, or through Remote's private
coordinator. It uses existing `GEMINI_API_KEYS`, `POCKETBASE_SERVER_TOKEN` and optional `GEMINI_PB_URL`
(default `https://pb.any1.tech`). A manual dispatch may omit request_id; the script acquires a PB
refresh nonce. Automatic Remote refresh supplies the already acquired nonce.

`gemini_models.py` requests the official Models API using an `x-goog-api-key` header and traverses
pagination. It writes normalized public metadata to PB; keys never appear in URLs, artifacts or
error text. The workflow performs read-only catalog discovery and does not generate content.
A failed run keeps the previous good catalog. Missing/retired API keys are tried in sequence.

Deploy PB migration `1794710000_shared_models.js` and model hooks before running this workflow.
Manual first sync: `gh workflow run gemini-models.yml --ref main`. Confirm the run is green and the
PB catalog is ready before deploying the corresponding Remote UI.

Reference: https://ai.google.dev/api/models
