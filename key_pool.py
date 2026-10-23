"""Local API key rotation: spread requests evenly and stay under each quota.

Every key belongs to a quota bucket. For Gemini the bucket is the Google Cloud
project, because the free tier meters RPM/RPD per project rather than per key;
other providers use one bucket per key. The pool:

* spaces requests inside a bucket at least ``60 / rpm`` seconds apart, so a
  bucket never bursts and never exceeds its per-minute limit;
* hands the next request to the ready bucket with the fewest requests so far
  (ties go to the one idle longest), which spreads load evenly over all keys;
* adds a small global gap between any two requests, so a run never starts with
  a burst of dozens of simultaneous calls;
* parks a bucket after a 429 (longer each time it repeats) and retires it for
  the rest of the run when Google says the daily quota is gone.

Everything lives in this process. Quota is plentiful, so coordinating separate
Action runs through a shared service is not worth its moving parts.
"""

from __future__ import annotations

import asyncio
import email.utils
import random
import re
import time
from dataclasses import dataclass

import httpx

#: Pace a little under the configured RPM: providers count in sliding windows
#: and their clocks are not ours.
RPM_SAFETY = 1.1
#: Upper bound for the global gap between two consecutive requests.
MAX_GLOBAL_GAP = 0.25
#: First cooldown after a 429 without a server-supplied delay, then doubled.
BASE_COOLDOWN_SECONDS = 20.0
MAX_COOLDOWN_SECONDS = 300.0


class QuotaPoolExhaustedError(RuntimeError):
    """Every bucket hit its daily limit or was disabled by a quota error."""


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
    """Map each API key to its quota bucket (Gemini: Google Cloud project).

    Empty means every key is its own bucket.
    """
    if key_count < 1:
        return []
    if not raw or not raw.strip():
        return [f"key-{index + 1}" for index in range(key_count)]
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


def latency_summary(latencies: list[float]) -> dict[str, object]:
    """p50/p95 over the recorded request latencies."""
    ordered = sorted(latencies)

    def percentile(value: float) -> float | None:
        if not ordered:
            return None
        index = round((len(ordered) - 1) * value)
        return round(ordered[index], 2)

    return {
        "requests": len(ordered),
        "p50_seconds": percentile(0.5),
        "p95_seconds": percentile(0.95),
    }


@dataclass
class Bucket:
    name: str
    keys: list[int]
    next_allowed: float = 0.0
    cooldown_until: float = 0.0
    last_used: float = 0.0
    used: int = 0
    strikes: int = 0
    disabled: bool = False
    turn: int = 0
    rate_limited: int = 0
    pace_factor: float = 1.0


class KeyPool:
    """In-process, evenly spread API key rotation."""

    def __init__(
        self,
        keys: list[str],
        groups: list[str] | None = None,
        *,
        rpm_per_key: float,
        rpd_per_key: int,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ) -> None:
        if not keys:
            raise ValueError("No API keys configured")
        if rpm_per_key <= 0 or rpd_per_key <= 0:
            raise ValueError("RPM and RPD limits must be > 0")
        groups = groups or [f"key-{index + 1}" for index in range(len(keys))]
        if len(groups) != len(keys):
            raise ValueError("Key/group mapping length mismatch")

        self.keys = keys
        self.rpd_per_key = rpd_per_key
        self.interval = 60.0 / rpm_per_key * RPM_SAFETY
        buckets: dict[str, Bucket] = {}
        for index, group in enumerate(groups):
            buckets.setdefault(group, Bucket(group, [])).keys.append(index)
        self.buckets = list(buckets.values())
        self.bucket_of = {index: bucket for bucket in self.buckets for index in bucket.keys}
        self.global_gap = min(MAX_GLOBAL_GAP, self.interval / len(self.buckets))
        self.next_global = 0.0
        self.clock = clock
        self.sleep = sleep
        self.lock = asyncio.Lock()

        self.used = [0 for _ in keys]
        self.successes = [0 for _ in keys]
        self.errors = [0 for _ in keys]
        self.cancelled = [0 for _ in keys]
        self.disabled_keys: dict[int, str] = {}
        self.rate_limited_counts = [0 for _ in keys]
        self.http_counts = {"success": 0, "429": 0, "503": 0, "other_error": 0}
        self.request_latencies: list[float] = []
        self.success_latencies: list[float] = []
        self.recent_statuses: list[int] = []
        self.quota_wait_seconds = 0.0

    @classmethod
    async def create(cls, keys, groups=None, *, rpm_per_key, rpd_per_key, client=None):
        del client  # kept so callers share one construction call site
        pool = cls(keys, groups, rpm_per_key=rpm_per_key, rpd_per_key=rpd_per_key)
        print(
            f"[quota] local rotation keys={len(keys)} buckets={len(pool.buckets)} "
            f"rpm_per_bucket={rpm_per_key:g} spacing={pool.interval:.2f}s "
            f"global_gap={pool.global_gap:.3f}s rpd_per_bucket={rpd_per_key}",
            flush=True,
        )
        return pool

    def _ready_at(self, bucket: Bucket) -> float:
        return max(bucket.next_allowed, bucket.cooldown_until)

    async def acquire(self) -> tuple[int, str, str]:
        waiting_since = self.clock()
        while True:
            async with self.lock:
                now = self.clock()
                live = [
                    bucket for bucket in self.buckets
                    if not bucket.disabled and bucket.used < self.rpd_per_key
                    and any(index not in self.disabled_keys for index in bucket.keys)
                ]
                if not live:
                    raise QuotaPoolExhaustedError(
                        "All API keys reached their daily limit or were disabled "
                        "by quota errors."
                    )
                ready = [bucket for bucket in live if self._ready_at(bucket) <= now]
                if ready and self.next_global <= now:
                    bucket = min(ready, key=lambda b: (b.used, b.last_used, random.random()))
                    available = [i for i in bucket.keys if i not in self.disabled_keys]
                    index = available[bucket.turn % len(available)]
                    bucket.turn += 1
                    bucket.used += 1
                    bucket.last_used = now
                    # Small jitter keeps buckets from lining up on the same tick.
                    bucket.next_allowed = now + self.interval * bucket.pace_factor * random.uniform(1.0, 1.05)
                    self.next_global = now + self.global_gap
                    self.used[index] += 1
                    self.quota_wait_seconds += max(0, now - waiting_since)
                    return index, self.keys[index], f"{bucket.name}-{bucket.used}"
                earliest = min(self._ready_at(bucket) for bucket in live)
                wait_for = max(0.01, max(earliest, self.next_global) - now)
            await self.sleep(wait_for)

    async def mark_success(self, key_index: int, lease_id: str) -> None:
        self.successes[key_index] += 1
        self.bucket_of[key_index].strikes = 0
        bucket = self.bucket_of[key_index]
        bucket.pace_factor = max(1.0, bucket.pace_factor * 0.95)

    async def mark_cancelled(self, key_index: int, lease_id: str) -> None:
        self.cancelled[key_index] += 1

    async def mark_error(self, key_index: int, lease_id: str, http_status: int = 0) -> None:
        self.errors[key_index] += 1

    async def rate_limited(
        self,
        key_index: int,
        lease_id: str,
        cooldown_seconds: float | None,
        daily_exhausted: bool = False,
        quota_type: str = "unknown",
    ) -> None:
        """Park the key's bucket; retire it when the daily quota is gone."""
        self.rate_limited_counts[key_index] += 1
        bucket = self.bucket_of[key_index]
        bucket.rate_limited += 1
        async with self.lock:
            if daily_exhausted:
                bucket.disabled = True
                print(f"[quota] {bucket.name} daily quota exhausted; retired for this run", flush=True)
                return
            bucket.strikes += 1
            bucket.pace_factor = min(8.0, bucket.pace_factor * 1.5)
            backoff = min(MAX_COOLDOWN_SECONDS, BASE_COOLDOWN_SECONDS * 2 ** min(4, bucket.strikes - 1))
            wait = max(cooldown_seconds or 0.0, backoff) * random.uniform(1.0, 1.2)
            bucket.cooldown_until = max(bucket.cooldown_until, self.clock() + wait)
            print(
                f"[quota] {bucket.name} 429 ({quota_type}) strike={bucket.strikes}; "
                f"cooling down {wait:.0f}s",
                flush=True,
            )

    def retire(self, key_index: int, reason: str) -> None:
        """Stop using a key's bucket for this run, e.g. after HTTP 402."""
        bucket = self.bucket_of[key_index]
        if not bucket.disabled:
            bucket.disabled = True
            print(f"[quota] {bucket.name} retired for this run: {reason}", flush=True)

    def retire_key(self, key_index: int, reason: str) -> None:
        """An invalid credential does not invalidate its project's other keys."""
        if key_index not in self.disabled_keys:
            self.disabled_keys[key_index] = reason
            print(f"[quota] key#{key_index + 1} retired for this run: {reason}", flush=True)

    def record_http_result(self, status: int, elapsed_seconds: float) -> None:
        if status == 200:
            self.http_counts["success"] += 1
        elif status in (429, 503):
            self.http_counts[str(status)] += 1
        else:
            self.http_counts["other_error"] += 1
        self.recent_statuses.append(status)
        del self.recent_statuses[:-20]
        elapsed = max(0.0, elapsed_seconds)
        if status == 200:
            self.success_latencies.append(elapsed)
            del self.success_latencies[:-1000]
        self.request_latencies.append(elapsed)
        del self.request_latencies[:-1000]

    def performance_summary(self) -> dict[str, object]:
        return {
            **self.http_counts,
            **latency_summary(self.request_latencies),
            "success_p95_seconds": latency_summary(self.success_latencies)["p95_seconds"],
            "quota_wait_seconds": round(self.quota_wait_seconds, 2),
        }

    async def close(self) -> None:
        return None

    def usage_summary(self) -> dict[str, object]:
        used = [bucket.used for bucket in self.buckets]
        return {
            "scope": "current_action",
            "performance": self.performance_summary(),
            "spread": {
                "buckets": len(self.buckets),
                "min_requests": min(used),
                "max_requests": max(used),
            },
            "keys": {
                str(index + 1): {
                    "bucket": self.bucket_of[index].name,
                    "requests": self.used[index],
                    "success": self.successes[index],
                    "http_429": self.rate_limited_counts[index],
                    "errors": self.errors[index],
                    "cancelled": self.cancelled[index],
                    "disabled": self.bucket_of[index].disabled or index in self.disabled_keys,
                    "disabled_reason": self.disabled_keys.get(index),
                }
                for index in range(len(self.keys))
            },
        }
