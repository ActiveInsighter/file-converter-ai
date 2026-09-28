from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

import httpx

DEFAULT_API_BASE = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "deepseek-ai/deepseek-v4.1-flash"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Quick NVIDIA API connectivity and optional vision test."
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
    )
    p.add_argument(
        "--api-base",
        default=os.getenv("NVIDIA_API_BASE", DEFAULT_API_BASE),
    )
    p.add_argument(
        "--image",
        type=Path,
        default=None,
        help="Optional local PNG/JPEG image to test multimodal input.",
    )
    return p


def main() -> int:
    args = parser().parse_args()
    key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not key:
        raw = os.getenv("NVIDIA_API_KEYS", "").strip()
        if raw:
            key = next(
                (
                    item.strip()
                    for item in raw.replace(",", "\n").replace(";", "\n").splitlines()
                    if item.strip()
                ),
                "",
            )
    if not key:
        raise SystemExit(
            "Set NVIDIA_API_KEY or NVIDIA_API_KEYS before running this script."
        )

    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Reply with exactly NVIDIA_API_OK if you can read this request."
                if args.image is None
                else "Describe the image briefly and state whether it contains readable text."
            ),
        }
    ]

    if args.image is not None:
        data = args.image.read_bytes()
        suffix = args.image.suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        encoded = base64.b64encode(data).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{encoded}",
                },
            }
        )

    response = httpx.post(
        args.api_base.rstrip("/") + "/chat/completions",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "model": args.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "top_p": 1,
            "max_tokens": 256,
            "stream": False,
        },
        timeout=120.0,
    )
    response.raise_for_status()
    payload = response.json()
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    print(message.get("content") or payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
