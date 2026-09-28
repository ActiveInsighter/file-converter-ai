"""Atomic Valkey quota state machine for 66 independent Gemini Projects."""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from redis.asyncio import Redis


PACIFIC = ZoneInfo("America/Los_Angeles")
LUA_SOURCE = Path(__file__).with_name("scheduler.lua").read_text(encoding="utf-8")
QUOTA_TYPES = {"unknown", "rpm", "tpm", "rpd", "spend"}


@dataclass(frozen=True)
class ApiResult:
    status: int
    body: dict


class QuotaScheduler:
    """Keep the HTTP layer thin; each state transition is one Valkey script call."""

    def __init__(
        self,
        client: Redis,
        *,
        prefix: str = "gemini-quota:v1",
        expected_key_count: int = 66,
        max_inflight: int = 24,
        requests_per_second: float = 2.0,
        burst: int = 8,
        lease_timeout_ms: int = 180_000,
        clock_ms: Callable[[], int] | None = None,
        random_ms: Callable[[int, int], int] | None = None,
    ) -> None:
        if not prefix or any(character.isspace() for character in prefix):
            raise ValueError("Invalid Valkey key prefix")
        if expected_key_count < 1 or max_inflight < 1 or burst < 1:
            raise ValueError("Quota capacity must be positive")
        if not math.isfinite(requests_per_second) or requests_per_second <= 0:
            raise ValueError("Global request rate must be positive")
        if lease_timeout_ms < 1_000:
            raise ValueError("Lease timeout must be at least one second")
        self.client = client
        self.prefix = prefix
        self.expected_key_count = expected_key_count
        self.max_inflight = max_inflight
        self.requests_per_second = requests_per_second
        self.burst = burst
        self.lease_timeout_ms = lease_timeout_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        self.random_ms = random_ms or random.randint
        self.script = client.register_script(LUA_SOURCE)

    def _time_args(self) -> tuple[int, str]:
        now = self.clock_ms()
        day = datetime.fromtimestamp(now / 1_000, PACIFIC).strftime("%Y-%m-%d")
        return now, day

    async def _call(self, operation: str, *arguments: object) -> ApiResult:
        now, day = self._time_args()
        raw = await self.script(
            keys=[self.prefix],
            args=[operation, now, day, *arguments],
        )
        response = json.loads(raw)
        return ApiResult(status=response["status"], body=response["body"])

    async def configure(
        self,
        groups: list[str],
        *,
        rpm_per_project: float,
        rpd_per_project: int,
    ) -> ApiResult:
        if (
            not isinstance(groups, list)
            or len(groups) != self.expected_key_count
            or any(
                not isinstance(group, str)
                or not group.strip()
                or len(group) > 128
                for group in groups
            )
        ):
            return ApiResult(400, {
                "error": "invalid_project_mapping",
                "expectedKeyCount": self.expected_key_count,
            })
        if len(set(groups)) != self.expected_key_count:
            return ApiResult(409, {
                "error": "expected_one_project_per_key",
                "keyCount": len(groups),
                "projectCount": len(set(groups)),
            })
        if (
            not isinstance(rpm_per_project, (int, float))
            or isinstance(rpm_per_project, bool)
            or not math.isfinite(rpm_per_project)
            or rpm_per_project <= 0
            or not isinstance(rpd_per_project, int)
            or isinstance(rpd_per_project, bool)
            or rpd_per_project <= 0
        ):
            return ApiResult(400, {"error": "invalid_quota_limits"})
        mapping = hashlib.sha256(
            json.dumps(groups, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return await self._call(
            "configure", mapping, len(groups), rpm_per_project,
            rpd_per_project, self.max_inflight,
        )

    async def lease(self, request_id: str | None = None) -> ApiResult:
        if request_id is None:
            request_id = str(uuid4())
        else:
            try:
                request_id = str(UUID(request_id))
            except (TypeError, ValueError):
                return ApiResult(400, {"error": "invalid_request_id"})
        return await self._call(
            "lease", request_id, self.max_inflight,
            self.requests_per_second, self.burst, self.lease_timeout_ms,
            self.random_ms(0, 2_000),
        )

    async def report(
        self,
        lease_id: str,
        http_status: int,
        *,
        cooldown_seconds: float = 0,
        daily_exhausted: bool = False,
        quota_type: str = "unknown",
    ) -> ApiResult:
        try:
            lease_id = str(UUID(lease_id))
        except (TypeError, ValueError):
            return ApiResult(400, {"error": "invalid_report"})
        if (
            not isinstance(http_status, int)
            or isinstance(http_status, bool)
            or not 0 <= http_status <= 599
            or not isinstance(cooldown_seconds, (int, float))
            or isinstance(cooldown_seconds, bool)
            or not math.isfinite(cooldown_seconds)
            or cooldown_seconds < 0
            or not isinstance(daily_exhausted, bool)
            or quota_type not in QUOTA_TYPES
        ):
            return ApiResult(400, {"error": "invalid_report"})
        return await self._call(
            "report", lease_id, http_status,
            min(86_400_000, math.ceil(cooldown_seconds * 1_000)),
            int(daily_exhausted), quota_type, self.max_inflight,
            self.random_ms(0, 30_000),
        )

    async def status(self) -> ApiResult:
        return await self._call(
            "status", self.max_inflight, self.requests_per_second,
            self.burst, self.lease_timeout_ms,
        )
