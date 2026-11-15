"""Sync Google's authenticated Models API to the shared PocketBase catalog.

Only public model metadata leaves this process. Credentials stay in Actions.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import httpx

from gemini_text import PocketBase

MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def parse_keys(raw: str) -> list[str]:
    return list(dict.fromkeys(key.strip() for key in re.split(r"[\r\n,;]+", raw) if key.strip()))


async def list_models(client: httpx.AsyncClient, key: str) -> list[dict]:
    models: dict[str, dict] = {}
    token, seen = "", set()
    for _ in range(10):
        params = {"pageSize": "1000"}
        if token:
            params["pageToken"] = token
        try:
            async with client.stream("GET", MODELS_URL, params=params,
                    headers={"x-goog-api-key": key}, timeout=30) as response:
                if response.status_code != 200:
                    raise RuntimeError(f"Models API returned HTTP {response.status_code}")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise RuntimeError("Models API response exceeds 2 MiB")
            data = json.loads(raw)
        except (httpx.TransportError, ValueError):
            raise RuntimeError("Models API connection or response failed") from None
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise RuntimeError("Models API response is invalid")
        for model in data["models"]:
            if not isinstance(model, dict):
                raise RuntimeError("Invalid model metadata")
            identifier = str(model.get("name", "")).removeprefix("models/")
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,109}", identifier):
                continue
            models[identifier] = {
                "id": identifier, "displayName": str(model.get("displayName") or identifier)[:128],
                "description": str(model.get("description") or "")[:2000],
                "inputTokenLimit": int(model.get("inputTokenLimit") or 0),
                "outputTokenLimit": int(model.get("outputTokenLimit") or 0),
                "methods": model.get("supportedGenerationMethods") or [],
                "thinking": model.get("thinking") is True,
            }
        if len(models) > 1000:
            raise RuntimeError("Models catalog exceeds 1000 models")
        token = data.get("nextPageToken") or ""
        if not token:
            if not models:
                raise RuntimeError("Models API returned an empty catalog")
            return sorted(models.values(), key=lambda model: model["id"])
        if not isinstance(token, str) or len(token) > 4096 or token in seen:
            raise RuntimeError("Models API pagination is invalid")
        seen.add(token)
    raise RuntimeError("Models API pagination exceeds 10 pages")


async def main():
    async with httpx.AsyncClient(follow_redirects=False) as client:
        pb = PocketBase(client, os.environ.get("GEMINI_PB_URL", "https://pb.any1.tech"), api_prefix="models/catalog")
        await pb.authenticate(token=os.environ.get("POCKETBASE_SERVER_TOKEN", ""))
        request_id = os.environ.get("REQUEST_ID", "").strip()
        if not request_id:
            claim = await pb.post("begin", {})
            if not claim.get("dispatch"):
                print("Catalog refresh already in progress or within cooldown")
                return
            request_id = claim["requestId"]
        try:
            keys = parse_keys(os.environ.get("GEMINI_API_KEYS", ""))
            if not keys:
                raise RuntimeError("GEMINI_API_KEYS is not configured")
            models = None
            # Keys may belong to retired projects. Read-only listing can safely try the next key.
            for key in keys:
                try:
                    models = await list_models(client, key)
                    break
                except RuntimeError:
                    continue
            if models is None:
                raise RuntimeError("No configured key could list the official model catalog")
            await pb.post("finish", {"requestId": request_id, "models": models}, retries=3)
            print(f"Synced {len(models)} official models")
        except Exception:
            await pb.post("finish", {"requestId": request_id, "error": True}, retries=3)
            # Deliberately do not print upstream payloads, URLs or credentials.
            raise RuntimeError("Model catalog synchronization failed") from None


if __name__ == "__main__":
    asyncio.run(main())
