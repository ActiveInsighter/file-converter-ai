"""Temporary, secret-safe Gemini API route probe for incident diagnosis."""

import base64
import os

import httpx


PIXEL = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=="
)


def probe(key: str, model: str, kind: str) -> None:
    if kind == "text":
        parts = [{"text": "Reply with OK."}]
    else:
        parts = [
            {"text": "Describe this image in one word."},
            {"inlineData": {"mimeType": "image/png", "data": PIXEL}},
        ]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        response = httpx.post(
            url,
            headers={"x-goog-api-key": key},
            json={"contents": [{"role": "user", "parts": parts}]},
            timeout=25,
        )
        payload = response.json()
        message = str((payload.get("error") or {}).get("message") or "")[:180]
        print(f"{model} {kind}: HTTP {response.status_code} {message}", flush=True)
    except Exception as exc:
        print(f"{model} {kind}: {type(exc).__name__}", flush=True)


keys = [line.strip() for line in os.environ["GEMINI_API_KEYS"].splitlines() if line.strip()]
for index, key in enumerate(keys[:3], start=1):
    print(f"key_index={index}", flush=True)
    for model in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite"):
        probe(key, model, "text")
    if index == 1:
        for model in ("gemini-3.8-flash", "gemini-3.7-flash"):
            probe(key, model, "tiny_image")
