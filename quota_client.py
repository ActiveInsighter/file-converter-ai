from __future__ import annotations

import asyncio
import email.utils
import os
import random
import re
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx


class QuotaPoolExhaustedError(RuntimeError):
    """No project has any remaining requests for the current Pacific day."""


def classify_quota_error(payload: object) -> str:
    """Return a quota category only when Google's error details identify one."""
    fragments: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                fragments.append(str(key))
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            fragments.append(value)

    collect(payload)
    text = re.sub(r"[^a-z0-9]+", " ", " ".join(fragments).lower())
    compact = text.replace(" ", "")
    if "request" in compact and (
        "perday" in compact or "daily" in compact or "requestsday" in compact
    ):
        return "rpd"
    if "tpm" in compact or ("token" in compact and "perminute" in compact):
        return "tpm"
    if "rpm" in compact or ("request" in compact and "perminute" in compact):
        return "rpm"
    if any(marker in compact for marker in ("spend", "spending", "costlimit")):
        return "spend"
    return "unknown"


def retry_after_seconds(headers: httpx.Headers, payload: object) -> float | None:
    """Read standard Retry-After or Google's google.rpc.RetryInfo delay."""
    candidates: list[float] = []
    raw_header = headers.get("Retry-After")
    if raw_header:
        try:
            candidates.append(max(0.0, float(raw_header)))
        except ValueError:
            try:
                retry_at = email.utils.parsedate_to_datetime(raw_header)
                candidates.append(max(0.0, retry_at.timestamp() - time.time()))
            except (TypeError, ValueError, OverflowError):
                pass

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "retryDelay" and isinstance(item, str):
                    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", item)
                    if match:
                        candidates.append(float(match.group(1)))
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload)
    return max(candidates) if candidates else None


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
    """Client for the globally coordinated Valkey quota API."""

    def __init__(
        self,
        keys: list[str],
        groups: list[str],
        rpm_per_project: float,
        rpd_per_project: int,
        client: httpx.AsyncClient,
        api_url: str,
        api_token: str,
    ) -> None:
        if not keys:
            raise ValueError("No Gemini API keys configured")
        if len(groups) != len(keys):
            raise ValueError("Key/project mapping length mismatch")
        if rpm_per_project <= 0 or rpd_per_project <= 0:
            raise ValueError("Project RPM and RPD limits must be > 0")
        if len(set(groups)) != len(groups):
            raise ValueError(
                "The quota pool requires one independent Google Cloud Project per API key."
            )

        self.keys = keys
        self.groups = groups
        self.rpm_per_project = rpm_per_project
        self.rpd_per_project = rpd_per_project
        self.client = client
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.used = [0 for _ in keys]
        self.successes = [0 for _ in keys]
        self.errors = [0 for _ in keys]
        self.rate_limited_counts = [0 for _ in keys]
        self.http_counts = {"success": 0, "429": 0, "503": 0, "other_error": 0}
        self.request_latencies: list[float] = []

    @classmethod
    async def create(
        cls,
        keys: list[str],
        groups: list[str],
        rpm_per_project: float,
        rpd_per_project: int,
        client: httpx.AsyncClient,
    ) -> "ProjectQuotaPool":
        api_url = os.getenv("GEMINI_QUOTA_API_URL", "").strip()
        api_token = os.getenv("GEMINI_QUOTA_API_TOKEN", "").strip()
        if not api_url or not api_token:
            raise RuntimeError(
                "GEMINI_QUOTA_API_URL and GEMINI_QUOTA_API_TOKEN are required."
            )
        parsed_url = urlparse(api_url)
        if not (
            parsed_url.scheme == "https"
            or (parsed_url.scheme == "http" and parsed_url.hostname in {"localhost", "127.0.0.1"})
        ):
            raise ValueError("GEMINI_QUOTA_API_URL must use HTTPS")

        pool = cls(
            keys,
            groups,
            rpm_per_project,
            rpd_per_project,
            client,
            api_url,
            api_token,
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
            raise RuntimeError("Quota API returned an unexpected key/project count")
        print(
            "[quota] Valkey pool ready "
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
            self.api_url + path,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "User-Agent": "file-converter-ai/1.0",
            },
            json=payload,
            timeout=35.0,
        )
        if response.status_code not in accepted_statuses:
            detail = response.text[:1000]
            raise RuntimeError(
                f"Quota API {path} returned HTTP "
                f"{response.status_code}: {detail}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Quota API {path} returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise RuntimeError(f"Quota API {path} returned an invalid response")
        return body

    async def acquire(self) -> tuple[int, str, str]:
        request_id = str(uuid4())
        transport_failures = 0
        while True:
            try:
                response = await self.client.post(
                    self.api_url + "/v1/lease",
                    headers={
                        "Authorization": f"Bearer {self.api_token}",
                        "User-Agent": "file-converter-ai/1.0",
                    },
                    json={"requestId": request_id},
                    timeout=15.0,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                transport_failures += 1
                if transport_failures >= 5:
                    raise RuntimeError("Quota API lease request failed repeatedly") from exc
                await asyncio.sleep(random.uniform(0.5, min(8.0, 2 ** transport_failures)))
                continue
            transport_failures = 0
            if response.status_code == 202:
                try:
                    wait_ms = int(response.json().get("waitMs", 250))
                except (TypeError, ValueError):
                    wait_ms = 250
                bounded_wait = max(0.25, min(wait_ms, 30_000) / 1000)
                await asyncio.sleep(random.uniform(bounded_wait * 0.8, bounded_wait * 1.2))
                continue
            if response.status_code == 429:
                raise QuotaPoolExhaustedError(
                    "All Gemini projects reached the configured Pacific-day request limit."
                )
            if response.status_code != 200:
                raise RuntimeError(
                    "Quota API /v1/lease returned HTTP "
                    f"{response.status_code}: {response.text[:1000]}"
                )
            body = response.json()
            key_index = body.get("keyIndex")
            lease_id = body.get("leaseId")
            if (
                not isinstance(key_index, int)
                or isinstance(key_index, bool)
                or key_index < 0
                or key_index >= len(self.keys)
                or not isinstance(lease_id, str)
                or not lease_id
            ):
                raise RuntimeError("Quota API returned an invalid lease")
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
        quota_type: str = "unknown",
    ) -> None:
        payload = {
            "leaseId": lease_id,
            "httpStatus": http_status,
            "cooldownSeconds": max(0, cooldown_seconds),
            "dailyExhausted": daily_exhausted,
            "quotaType": quota_type,
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
            f"Could not report the Gemini response to quota API for key#{key_index + 1}: "
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
        quota_type: str = "unknown",
    ) -> None:
        self.rate_limited_counts[key_index] += 1
        await self._report(
            key_index,
            lease_id,
            429,
            cooldown_seconds=cooldown_seconds,
            daily_exhausted=daily_exhausted,
            quota_type=quota_type,
        )

    def record_http_result(self, status: int, elapsed_seconds: float) -> None:
        if status == 200:
            self.http_counts["success"] += 1
        elif status == 429:
            self.http_counts["429"] += 1
        elif status == 503:
            self.http_counts["503"] += 1
        else:
            self.http_counts["other_error"] += 1
        self.request_latencies.append(max(0.0, elapsed_seconds))
        if len(self.request_latencies) > 1000:
            del self.request_latencies[: len(self.request_latencies) - 1000]

    async def remote_status(self) -> dict:
        return await self._post("/v1/status", {})

    def performance_summary(self) -> dict[str, object]:
        ordered = sorted(self.request_latencies)

        def percentile(value: float) -> float | None:
            if not ordered:
                return None
            index = round((len(ordered) - 1) * value)
            return round(ordered[index], 2)

        return {
            **self.http_counts,
            "requests": len(ordered),
            "p50_seconds": percentile(0.5),
            "p95_seconds": percentile(0.95),
        }

    async def close(self) -> None:
        return None

    def usage_summary(self) -> dict[str, object]:
        return {
            "scope": "current_action",
            "performance": self.performance_summary(),
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
