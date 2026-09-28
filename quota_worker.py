from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import urlparse

import httpx


def parse_key_groups(key_count: int, raw: str | None) -> list[str]:
    """Map each Gemini API key to its Google Cloud project group."""
    if key_count < 1:
        return []
    if not raw or not raw.strip():
        return [f"project-{index + 1}" for index in range(key_count)]

    groups = [
        item.strip()
        for item in raw.replace(",", "\n").replace(";", "\n").splitlines()
        if item.strip()
    ]
    if len(groups) != key_count:
        raise ValueError(
            "GEMINI_KEY_GROUPS must contain exactly one non-empty group name "
            f"per API key: expected {key_count}, got {len(groups)}"
        )
    return groups


class ProjectQuotaPool:
    """Client for the globally coordinated Cloudflare Durable Object pool."""

    def __init__(
        self,
        keys: list[str],
        groups: list[str],
        rpm_per_project: float,
        rpd_per_project: int,
        client: httpx.AsyncClient,
        worker_url: str,
        worker_token: str,
    ) -> None:
        if not keys:
            raise ValueError("No Gemini API keys configured")
        if len(groups) != len(keys):
            raise ValueError("Key/project mapping length mismatch")
        if rpm_per_project <= 0 or rpd_per_project <= 0:
            raise ValueError("Project RPM and RPD limits must be > 0")
        if len(set(groups)) != len(groups):
            raise ValueError(
                "The Cloudflare pool requires one independent Google Cloud Project per API key."
            )

        self.keys = keys
        self.groups = groups
        self.rpm_per_project = rpm_per_project
        self.rpd_per_project = rpd_per_project
        self.client = client
        self.worker_url = worker_url.rstrip("/")
        self.worker_token = worker_token
        self.used = [0 for _ in keys]
        self.successes = [0 for _ in keys]
        self.errors = [0 for _ in keys]
        self.rate_limited_counts = [0 for _ in keys]

    @classmethod
    async def create(
        cls,
        keys: list[str],
        groups: list[str],
        rpm_per_project: float,
        rpd_per_project: int,
        client: httpx.AsyncClient,
    ) -> "ProjectQuotaPool":
        worker_url = os.getenv("GEMINI_QUOTA_WORKER_URL", "").strip()
        worker_token = os.getenv("GEMINI_QUOTA_WORKER_TOKEN", "").strip()
        if not worker_url or not worker_token:
            raise RuntimeError(
                "GEMINI_QUOTA_WORKER_URL and GEMINI_QUOTA_WORKER_TOKEN are required."
            )
        parsed_url = urlparse(worker_url)
        if parsed_url.scheme != "https" and parsed_url.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("GEMINI_QUOTA_WORKER_URL must use HTTPS")

        pool = cls(
            keys,
            groups,
            rpm_per_project,
            rpd_per_project,
            client,
            worker_url,
            worker_token,
        )
        result = await pool._post(
            "/v1/configure",
            {
                "keyGroups": groups,
                "rpmPerProject": rpm_per_project,
                "rpdPerProject": rpd_per_project,
            },
        )
        if result.get("keyCount") != len(keys) or result.get("projectCount") != len(set(groups)):
            raise RuntimeError("Cloudflare quota pool returned an unexpected key/project count")
        print(
            "[quota] Cloudflare pool ready "
            f"keys={result['keyCount']} projects={result['projectCount']} "
            f"pacific_date={result.get('pacificDate')}",
            flush=True,
        )
        return pool

    async def _post(
        self,
        path: str,
        payload: dict,
        accepted_statuses: tuple[int, ...] = (200,),
    ) -> dict:
        response = await self.client.post(
            self.worker_url + path,
            headers={
                "Authorization": f"Bearer {self.worker_token}",
                "User-Agent": "file-converter-ai/1.0",
            },
            json=payload,
            timeout=35.0,
        )
        if response.status_code not in accepted_statuses:
            detail = response.text[:1000]
            raise RuntimeError(
                f"Cloudflare quota Worker {path} returned HTTP "
                f"{response.status_code}: {detail}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Cloudflare quota Worker {path} returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise RuntimeError(f"Cloudflare quota Worker {path} returned an invalid response")
        return body

    async def acquire(self) -> tuple[int, str, str]:
        while True:
            response = await self.client.post(
                self.worker_url + "/v1/lease",
                headers={
                    "Authorization": f"Bearer {self.worker_token}",
                    "User-Agent": "file-converter-ai/1.0",
                },
                json={},
                timeout=35.0,
            )
            if response.status_code == 202:
                try:
                    wait_ms = int(response.json().get("waitMs", 250))
                except (TypeError, ValueError):
                    wait_ms = 250
                await asyncio.sleep(max(0.05, min(wait_ms, 30_000) / 1000))
                continue
            if response.status_code == 429:
                raise RuntimeError(
                    "All Gemini projects reached the configured Pacific-day request limit."
                )
            if response.status_code != 200:
                raise RuntimeError(
                    "Cloudflare quota Worker /v1/lease returned HTTP "
                    f"{response.status_code}: {response.text[:1000]}"
                )
            body = response.json()
            key_index = body.get("keyIndex")
            lease_id = body.get("leaseId")
            if (
                not isinstance(key_index, int)
                or key_index < 0
                or key_index >= len(self.keys)
                or not isinstance(lease_id, str)
                or not lease_id
            ):
                raise RuntimeError("Cloudflare quota Worker returned an invalid lease")
            self.used[key_index] += 1
            return key_index, self.keys[key_index], lease_id

    async def _report(
        self,
        key_index: int,
        lease_id: str,
        http_status: int,
        *,
        cooldown_seconds: float = 0,
        daily_exhausted: bool = False,
    ) -> None:
        payload = {
            "leaseId": lease_id,
            "httpStatus": http_status,
            "cooldownSeconds": max(0, cooldown_seconds),
            "dailyExhausted": daily_exhausted,
        }
        last_error: Exception | None = None
        for attempt in range(1, 6):
            try:
                await self._post("/v1/report", payload)
                return
            except (httpx.HTTPError, RuntimeError) as exc:
                last_error = exc
                if isinstance(exc, RuntimeError) and "returned HTTP 4" in str(exc):
                    break
                if attempt < 5:
                    await asyncio.sleep(min(attempt, 4))
        raise RuntimeError(
            f"Could not report the Gemini response to Cloudflare for key#{key_index + 1}: "
            f"{last_error}"
        ) from last_error

    async def mark_success(self, key_index: int, lease_id: str) -> None:
        self.successes[key_index] += 1
        await self._report(key_index, lease_id, 200)

    async def mark_error(
        self,
        key_index: int,
        lease_id: str,
        http_status: int = 0,
    ) -> None:
        self.errors[key_index] += 1
        await self._report(key_index, lease_id, http_status)

    async def rate_limited(
        self,
        key_index: int,
        lease_id: str,
        cooldown_seconds: float,
        daily_exhausted: bool = False,
    ) -> None:
        self.rate_limited_counts[key_index] += 1
        await self._report(
            key_index,
            lease_id,
            429,
            cooldown_seconds=cooldown_seconds,
            daily_exhausted=daily_exhausted,
        )

    def is_daily_quota_message(self, message: str) -> bool:
        lower = message.lower()
        explicit_daily_markers = (
            "perday",
            "per_day",
            "per day",
            "requestsperday",
            "requests per day",
            "daily",
        )
        if any(marker in lower for marker in explicit_daily_markers):
            return True
        limit_match = re.search(
            r"quota exceeded for metric:[^\n]*requests[^\n]*limit:\s*(\d+)",
            lower,
        )
        return bool(limit_match and int(limit_match.group(1)) == int(self.rpd_per_project))

    async def close(self) -> None:
        return None

    def usage_summary(self) -> dict[str, object]:
        return {
            "scope": "current_action",
            "projects": {
                group: {
                    "requests": self.used[index],
                    "success": self.successes[index],
                    "http_429": self.rate_limited_counts[index],
                    "errors": self.errors[index],
                }
                for index, group in enumerate(self.groups)
            },
            "keys": {
                str(index + 1): {
                    "project": self.groups[index],
                    "requests": self.used[index],
                    "success": self.successes[index],
                    "http_429": self.rate_limited_counts[index],
                    "errors": self.errors[index],
                }
                for index in range(len(self.keys))
            },
        }
