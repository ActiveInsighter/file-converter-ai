# Valkey quota API deployment

The public path is `GitHub Actions → HTTPS reverse proxy → quota API → local Valkey`.
Only the HTTPS reverse proxy is public. Keep Valkey on `127.0.0.1:6379` and the
quota API on `127.0.0.1:8788`.

## Valkey

On a host with Docker Compose:

```bash
cd deploy/valkey
docker compose up -d
docker exec file-converter-valkey valkey-cli ping
```

The Compose file pins the official `valkey/valkey:9.1.2` image, enables AOF with
`appendfsync everysec`, takes RDB snapshots after 1000 changes in 60 seconds,
and maps port 6379 to loopback only. Keep `deploy/valkey/data/` on persistent
storage and back it up.

On Ubuntu without Docker, install `valkey-server` from the distribution. Set
`bind 127.0.0.1 -::1`, `protected-mode yes`, `appendonly yes`,
`appendfsync everysec`, and `save 60 1000` in `/etc/valkey/valkey.conf`, then
restart `valkey-server` and verify `valkey-cli ping` returns `PONG`.

## Quota API

Install Python 3.12 and `python3.12-venv`, then copy `quota_api/` to
`/opt/file-converter-quota-api/quota_api/`. Create a virtual environment in
`/opt/file-converter-quota-api/venv` and install
`quota_api/requirements.txt`. Create a dedicated `quota-api` system user.

Create `/etc/file-converter-quota-api.env` (mode `0600`) from
`deploy/quota-api.env.example`. Give `QUOTA_API_TOKEN` at least 32 random
characters. `VALKEY_URL` points to the local Valkey instance. On a shared host,
set a Valkey password and include it in this URL. Never commit either secret.

Install `deploy/quota-api.service` as
`/etc/systemd/system/file-converter-quota-api.service`, then run:

```bash
systemctl daemon-reload
systemctl enable --now file-converter-quota-api.service
curl --fail http://127.0.0.1:8788/healthz
```

Route an HTTPS path to `127.0.0.1:8788` through the existing reverse proxy,
or through a loopback-only SSH reverse tunnel to the gateway. The local gateway
configuration uses `deploy/quota-caddy-route.caddy` and the optional
`deploy/quota-api-tunnel.service`; it exposes the quota API under
`/file-converter-quota/` on the existing HTTPS n8n hostname. Require TLS
externally. Do not expose Uvicorn or Valkey directly.

Set the repository variable `GEMINI_QUOTA_API_URL` to that HTTPS origin, and
set the repository secret `GEMINI_QUOTA_API_TOKEN` to the same API token. Keep
the Gemini API keys in the existing `GEMINI_API_KEYS` secret. A successful
Action startup logs `Valkey pool ready keys=N projects=N`.

The service is safe to restart. Valkey AOF preserves counters, cooldowns and
leases; leases expire from the active set after 180 seconds if a client never
reports. Pacific Time day rollover resets daily Project counters.

## Tuning the quota API

Every capacity knob is read from the service environment, so the pool can be
retuned without touching source code. Add these to
`/etc/file-converter-quota-api.env` and restart the service.

| Variable | Default | Meaning |
| --- | --- | --- |
| `GEMINI_EXPECTED_KEY_COUNT` | `0` | Pin the pool size. `0` accepts whatever the workflow configures. Pin it to catch an accidental key-list change. |
| `GEMINI_MAX_INFLIGHT` | `24` | Global ceiling on concurrent Gemini requests. The adaptive controller moves between this value and 8. |
| `GEMINI_REQUESTS_PER_SECOND` | `2.0` | Global token-bucket refill rate across all Projects. |
| `GEMINI_BURST` | `8` | Global token-bucket size. |
| `GEMINI_LEASE_TIMEOUT_MS` | `180000` | How long a lease holds an in-flight slot without a report. |
| `GEMINI_DEGRADE_RATIO` | `0.35` | 503 share in the recent window that reduces global capacity. |
| `GEMINI_RECOVER_RATIO` | `0.60` | Success share required to raise global capacity again. |

`GEMINI_EXPECTED_KEY_COUNT` previously defaulted to a hard-coded `66`, so
adding or removing one key in `GEMINI_API_KEYS` made `/v1/configure` fail with
`invalid_project_mapping` and every Action died at startup. Leave it at `0`
unless you want that strictness.

Adding or removing a key also changes the mapping hash, which covers the whole
list. `/v1/configure` now rebuilds the pool and resets the current Pacific-day
per-Project counters when the list changes and no lease is in flight, so a
one-key edit does not wedge every later Action. It only refuses with 409
`project_mapping_changed` while a run is mid-flight, and the next idle run then
adopts the new list.

The global rate and in-flight ceilings apply to the whole account, not per
Project. They are the only thing standing between "one more Project" and "one
more request per second against a model that answers 503 when pushed", so
raise them in small steps and watch `http_503` in the `[metrics]` line.

## GEMINI_KEY_GROUPS

Gemini free-tier quota is counted **per Google Cloud Project, not per API key**,
so two keys from the same Project share one 500 RPD budget. The quota pool
detects that only if each key carries its real Project ID: pass them in the same
order as the keys through the repository variable `GEMINI_KEY_GROUPS`
(comma or newline separated).

Generate the exact value with the helper next to this file:

```bash
GEMINI_API_KEYS="$(cat keys.txt)" python3 deploy/print-key-groups.py
gh variable set GEMINI_KEY_GROUPS -R ActiveInsighter/file-converter-ai < groups.txt
```

If the variable is empty the workflow still runs, but the pool labels keys
`project-1..N` and cannot tell two keys of one Project apart, so a shared
budget would be handed out twice. The Action prints a warning in that case.

## Free-tier model quotas

Measured per Project in September 2026; Google no longer publishes these:

| Model | RPM | RPD | Usable for page images |
| --- | --- | --- | --- |
| `gemini-3.5-flash-lite` | 15 | 500 | yes |
| `gemini-3.1-flash-lite` | 15 | 500 | yes, but weaker output - not used as a fallback |
| `gemini-3.6-flash` / `3.7` / `3.8` | 5 | 20 | no (503 on every image request) |
| `gemini-3.5-flash` | 5 | 20 | no |

Only the `-lite` models are worth routing to. A plain Flash model burns its
entire daily budget in 20 requests, and they were answering 503 for every
vision request while the lite models kept serving. That is why the workflow
defaults `--rpm-per-key 15 --rpd-per-key 500` and keeps the fallback chain
inside the lite family.
