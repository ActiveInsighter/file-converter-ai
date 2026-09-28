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
the 66 Gemini API keys in the existing `GEMINI_API_KEYS` secret. A successful
Action startup logs `Valkey pool ready keys=66 projects=66`.

The service is safe to restart. Valkey AOF preserves counters, cooldowns and
leases; leases expire from the active set after 180 seconds if a client never
reports. Pacific Time day rollover resets daily Project counters.
