from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

MODEL = "gemini-3.5-flash-lite"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
OUTPUT = Path("reports/gemini-key-health.json")
MAX_CONCURRENCY = 32


def parse_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    normalized = raw.replace(",", "\n").replace(";", "\n")
    return [item.strip() for item in normalized.splitlines() if item.strip()]


def parse_groups(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.splitlines() if item.strip()]


def provider_error_code(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    status = error.get("status")
    return str(status)[:80] if status is not None else None


def safe_failure_summary(status: int | None, code: str | None) -> str:
    if status is None:
        return "client_or_network_error"
    if status == 400:
        return "bad_request_or_invalid_key"
    if status == 401:
        return "authentication_failed"
    if status == 403:
        return "permission_or_api_access_denied"
    if status == 404:
        return "model_or_endpoint_not_found"
    if status == 429:
        return "rate_or_quota_limited"
    if 500 <= status <= 599:
        return "provider_server_error"
    return code or f"http_{status}"


async def probe(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    index: int,
    key: str,
    duplicate_of: int | None,
) -> dict:
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": "Reply with exactly KEY_OK"}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 16,
        },
    }

    started = time.perf_counter()
    async with semaphore:
        try:
            response = await client.post(
                API_URL,
                headers={
                    "x-goog-api-key": key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            latency_ms = round((time.perf_counter() - started) * 1000)

            try:
                body = response.json()
            except Exception:
                body = {}

            response_text = ""
            if isinstance(body, dict):
                for candidate in body.get("candidates", []) or []:
                    content = candidate.get("content") or {}
                    for part in content.get("parts", []) or []:
                        if isinstance(part, dict) and part.get("text"):
                            response_text += str(part["text"])

            error_code = provider_error_code(body)
            ok = response.status_code == 200
            return {
                "index": index,
                "label": f"key#{index}",
                "ok": ok,
                "http_status": response.status_code,
                "latency_ms": latency_ms,
                "duplicate_of": duplicate_of,
                "response_text": response_text.strip()[:80] if ok else None,
                "error_code": None if ok else error_code,
                "failure_kind": None
                if ok
                else safe_failure_summary(response.status_code, error_code),
            }
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000)
            return {
                "index": index,
                "label": f"key#{index}",
                "ok": False,
                "http_status": None,
                "latency_ms": latency_ms,
                "duplicate_of": duplicate_of,
                "response_text": None,
                "error_code": type(exc).__name__,
                "failure_kind": "client_or_network_error",
            }


async def main() -> int:
    keys = parse_keys(os.getenv("GEMINI_API_KEYS"))
    groups = parse_groups(os.getenv("GEMINI_KEY_GROUPS"))

    if not keys:
        raise SystemExit("GEMINI_API_KEYS is empty")

    first_seen: dict[str, int] = {}
    duplicate_of: list[int | None] = []
    for idx, key in enumerate(keys, start=1):
        duplicate_of.append(first_seen.get(key))
        first_seen.setdefault(key, idx)

    timeout = httpx.Timeout(45.0, connect=15.0)
    limits = httpx.Limits(max_connections=80, max_keepalive_connections=40)
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async with httpx.AsyncClient(timeout=timeout, limits=limits, http2=False) as client:
        results = await asyncio.gather(
            *[
                probe(client, semaphore, idx, key, duplicate_of[idx - 1])
                for idx, key in enumerate(keys, start=1)
            ]
        )

    success_count = sum(1 for item in results if item["ok"])
    failure_count = len(results) - success_count
    unique_count = len(first_seen)

    report = {
        "tested_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "configured_key_count": len(keys),
        "unique_key_count": unique_count,
        "duplicate_key_count": len(keys) - unique_count,
        "success_count": success_count,
        "failure_count": failure_count,
        "all_usable": failure_count == 0,
        "max_probe_concurrency": MAX_CONCURRENCY,
        "project_group_summary": {
            "configured_group_lines": len(groups),
            "distinct_groups": len(set(groups)) if groups else None,
            "matches_key_count": (len(groups) == len(keys)) if groups else None,
            "runtime_behavior_if_unset": (
                "each key is treated as an independent project quota group"
                if not groups
                else None
            ),
        },
        "results": results,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        f"Gemini key health: total={len(keys)} unique={unique_count} "
        f"success={success_count} failure={failure_count}"
    )
    for item in results:
        state = "OK" if item["ok"] else "FAIL"
        detail = item["http_status"] if item["http_status"] is not None else item["error_code"]
        print(f"{item['label']}: {state} ({detail}, {item['latency_ms']} ms)")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
