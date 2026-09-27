from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx


PACIFIC = ZoneInfo("America/Los_Angeles")
STATE_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def pacific_date() -> str:
    return datetime.now(PACIFIC).date().isoformat()


def parse_key_groups(key_count: int, raw: str | None) -> list[str]:
    """Map keys to project quota groups.

    GEMINI_KEY_GROUPS is line-oriented and matches GEMINI_API_KEYS by position.
    If it is unset/blank, every key is treated as its own project so existing
    setups keep working. Duplicate group names mean those keys share quota.
    """
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


def fresh_state(groups: list[str]) -> dict[str, Any]:
    now = utc_now_iso()
    unique_groups = list(dict.fromkeys(groups))
    return {
        "version": STATE_VERSION,
        "pacific_date": pacific_date(),
        "updated_at": now,
        "projects": {
            group: {
                "requests_today": 0,
                "recent_requests": [],
                "cooldown_until": None,
                "daily_exhausted": False,
                "success": 0,
                "http_429": 0,
                "errors": 0,
            }
            for group in unique_groups
        },
        "keys": {
            str(index + 1): {
                "project": group,
                "requests": 0,
                "success": 0,
                "http_429": 0,
                "errors": 0,
                "disabled": False,
            }
            for index, group in enumerate(groups)
        },
    }


def normalize_state(state: dict[str, Any] | None, groups: list[str]) -> dict[str, Any]:
    today = pacific_date()
    if not state or state.get("version") != STATE_VERSION:
        return fresh_state(groups)

    if state.get("pacific_date") != today:
        return fresh_state(groups)

    # Preserve current-day counters for groups/keys that still exist and add
    # new ones if the mapping changed. Never carry a key onto a different
    # project silently: its per-key diagnostic counters reset in that case.
    projects = state.setdefault("projects", {})
    for group in dict.fromkeys(groups):
        projects.setdefault(
            group,
            {
                "requests_today": 0,
                "recent_requests": [],
                "cooldown_until": None,
                "daily_exhausted": False,
                "success": 0,
                "http_429": 0,
                "errors": 0,
            },
        )

    keys = state.setdefault("keys", {})
    for index, group in enumerate(groups, start=1):
        key_id = str(index)
        current = keys.get(key_id)
        if not current or current.get("project") != group:
            keys[key_id] = {
                "project": group,
                "requests": 0,
                "success": 0,
                "http_429": 0,
                "errors": 0,
                "disabled": False,
            }

    state["pacific_date"] = today
    state["version"] = STATE_VERSION
    return state


@dataclass
class GitHubStateStore:
    repository: str
    token: str
    branch: str = "quota-state"
    path: str = "quota-state.json"

    @classmethod
    def from_env(cls) -> "GitHubStateStore | None":
        repository = os.getenv("GITHUB_REPOSITORY", "").strip()
        token = os.getenv("GITHUB_TOKEN", "").strip()
        branch = os.getenv("GEMINI_QUOTA_STATE_BRANCH", "quota-state").strip()
        path = os.getenv("GEMINI_QUOTA_STATE_PATH", "quota-state.json").strip()
        if not repository or not token:
            return None
        return cls(repository=repository, token=token, branch=branch, path=path)

    @property
    def api_url(self) -> str:
        return (
            f"https://api.github.com/repos/{self.repository}/contents/"
            f"{self.path}"
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def load(self) -> tuple[dict[str, Any] | None, str | None]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                self.api_url,
                headers=self.headers,
                params={"ref": self.branch},
            )
        if response.status_code == 404:
            return None, None
        response.raise_for_status()
        payload = response.json()
        content = base64.b64decode(payload["content"]).decode("utf-8")
        return json.loads(content), payload.get("sha")

    async def save(
        self,
        state: dict[str, Any],
        sha: str | None,
        message: str,
    ) -> str:
        state["updated_at"] = utc_now_iso()
        raw = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.put(
                self.api_url,
                headers=self.headers,
                json=payload,
            )
        response.raise_for_status()
        return response.json()["content"]["sha"]


class ProjectQuotaPool:
    """Cross-run, project-aware scheduler for Gemini keys."""

    def __init__(
        self,
        keys: list[str],
        groups: list[str],
        rpm_per_project: float,
        rpd_per_project: int,
        state: dict[str, Any],
        store: GitHubStateStore | None,
        state_sha: str | None,
        checkpoint_every: int = 10,
        checkpoint_seconds: float = 30.0,
    ) -> None:
        if not keys:
            raise ValueError("No Gemini API keys configured")
        if len(groups) != len(keys):
            raise ValueError("Key/group mapping length mismatch")
        if rpm_per_project <= 0:
            raise ValueError("rpm_per_project must be > 0")
        if rpd_per_project <= 0:
            raise ValueError("rpd_per_project must be > 0")

        self.keys = keys
        self.groups = groups
        self.rpm_per_project = rpm_per_project
        self.rpd_per_project = rpd_per_project
        self.interval = (60.0 / rpm_per_project) * 1.08
        self.state = normalize_state(state, groups)
        self.store = store
        self.state_sha = state_sha
        self.checkpoint_every = max(1, checkpoint_every)
        self.checkpoint_seconds = max(1.0, checkpoint_seconds)
        self.unsaved_requests = 0
        self.last_checkpoint = time.monotonic()
        self.lock = asyncio.Lock()

        self.next_key_index_by_group = {
            group: 0 for group in dict.fromkeys(groups)
        }

    @classmethod
    async def create(
        cls,
        keys: list[str],
        groups: list[str],
        rpm_per_project: float,
        rpd_per_project: int,
        checkpoint_every: int = 10,
        checkpoint_seconds: float = 30.0,
    ) -> "ProjectQuotaPool":
        store = GitHubStateStore.from_env()
        state = None
        sha = None
        if store is not None:
            try:
                state, sha = await store.load()
                print(
                    f"[quota-state] loaded branch={store.branch} "
                    f"path={store.path} sha={sha or 'new'}",
                    flush=True,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to load persistent quota state: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        else:
            print(
                "[quota-state] GITHUB_TOKEN/GITHUB_REPOSITORY unavailable; "
                "using in-memory quota state only",
                flush=True,
            )
        return cls(
            keys,
            groups,
            rpm_per_project,
            rpd_per_project,
            normalize_state(state, groups),
            store,
            sha,
            checkpoint_every,
            checkpoint_seconds,
        )

    def _project(self, group: str) -> dict[str, Any]:
        return self.state["projects"][group]

    def _key(self, index: int) -> dict[str, Any]:
        return self.state["keys"][str(index + 1)]

    def _clean_recent(self, group: str, now_epoch: float) -> list[float]:
        project = self._project(group)
        parsed: list[float] = []
        for value in project.get("recent_requests", []):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                ts = dt.timestamp()
            except Exception:
                continue
            if now_epoch - ts < 60.0:
                parsed.append(ts)
        project["recent_requests"] = [
            datetime.fromtimestamp(ts, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
            for ts in parsed
        ]
        return parsed

    def _cooldown_epoch(self, group: str) -> float:
        value = self._project(group).get("cooldown_until")
        if not value:
            return 0.0
        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            return 0.0

    async def _checkpoint_locked(self, force: bool, reason: str) -> None:
        if self.store is None:
            self.unsaved_requests = 0
            self.last_checkpoint = time.monotonic()
            return

        elapsed = time.monotonic() - self.last_checkpoint
        if (
            not force
            and self.unsaved_requests < self.checkpoint_every
            and elapsed < self.checkpoint_seconds
        ):
            return

        self.state_sha = await self.store.save(
            self.state,
            self.state_sha,
            f"chore: checkpoint Gemini quota state ({reason})",
        )
        self.unsaved_requests = 0
        self.last_checkpoint = time.monotonic()
        print(
            f"[quota-state] checkpoint reason={reason} sha={self.state_sha}",
            flush=True,
        )

    async def acquire(self) -> tuple[int, str]:
        while True:
            wait_for = 0.0
            async with self.lock:
                now_epoch = time.time()
                candidates: list[tuple[float, str]] = []

                for group in dict.fromkeys(self.groups):
                    project = self._project(group)
                    if project.get("daily_exhausted"):
                        continue
                    if int(project.get("requests_today", 0)) >= self.rpd_per_project:
                        project["daily_exhausted"] = True
                        continue

                    recent = self._clean_recent(group, now_epoch)
                    ready_at = max(now_epoch, self._cooldown_epoch(group))
                    if recent:
                        # Project-level RPM: spacing plus a sliding-window guard.
                        ready_at = max(ready_at, recent[-1] + self.interval)
                        if len(recent) >= int(self.rpm_per_project):
                            ready_at = max(ready_at, recent[0] + 60.05)

                    usable_indices = [
                        i
                        for i, mapped_group in enumerate(self.groups)
                        if mapped_group == group
                        and not self._key(i).get("disabled")
                    ]
                    if not usable_indices:
                        continue
                    candidates.append((ready_at, group))

                if not candidates:
                    await self._checkpoint_locked(
                        True, "all-projects-unavailable"
                    )
                    raise RuntimeError(
                        "All Gemini project quota pools are exhausted or have "
                        "no usable keys for the current Pacific day."
                    )

                ready_at, group = min(candidates)
                if ready_at <= now_epoch:
                    indices = [
                        i
                        for i, mapped_group in enumerate(self.groups)
                        if mapped_group == group
                        and not self._key(i).get("disabled")
                    ]
                    cursor = self.next_key_index_by_group[group] % len(indices)
                    key_index = indices[cursor]
                    self.next_key_index_by_group[group] = cursor + 1

                    project = self._project(group)
                    key_state = self._key(key_index)
                    project["requests_today"] = int(
                        project.get("requests_today", 0)
                    ) + 1
                    project.setdefault("recent_requests", []).append(
                        utc_now_iso()
                    )
                    key_state["requests"] = int(
                        key_state.get("requests", 0)
                    ) + 1
                    self.unsaved_requests += 1
                    await self._checkpoint_locked(False, "request")
                    return key_index, self.keys[key_index]

                wait_for = max(0.05, ready_at - now_epoch)

            await asyncio.sleep(wait_for)

    async def mark_success(self, key_index: int) -> None:
        async with self.lock:
            group = self.groups[key_index]
            project = self._project(group)
            key_state = self._key(key_index)
            project["success"] = int(project.get("success", 0)) + 1
            key_state["success"] = int(key_state.get("success", 0)) + 1

    async def mark_error(self, key_index: int) -> None:
        async with self.lock:
            group = self.groups[key_index]
            project = self._project(group)
            key_state = self._key(key_index)
            project["errors"] = int(project.get("errors", 0)) + 1
            key_state["errors"] = int(key_state.get("errors", 0)) + 1

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

        # Gemini sometimes omits the quota-id and only prints the numeric
        # limit in the human-readable message. Treat an exceeded request quota
        # whose advertised limit matches the configured project RPD as daily.
        limit_match = __import__("re").search(
            r"quota exceeded for metric:[^\n]*requests[^\n]*limit:\s*(\d+)",
            lower,
        )
        if limit_match:
            try:
                return int(limit_match.group(1)) == int(self.rpd_per_project)
            except ValueError:
                pass
        return False

    async def rate_limited(
        self,
        key_index: int,
        cooldown_seconds: float,
        daily_exhausted: bool = False,
    ) -> None:
        async with self.lock:
            group = self.groups[key_index]
            project = self._project(group)
            key_state = self._key(key_index)
            project["http_429"] = int(project.get("http_429", 0)) + 1
            key_state["http_429"] = int(key_state.get("http_429", 0)) + 1

            if daily_exhausted:
                project["daily_exhausted"] = True
                project["requests_today"] = max(
                    int(project.get("requests_today", 0)),
                    self.rpd_per_project,
                )
            else:
                until = datetime.fromtimestamp(
                    time.time() + cooldown_seconds,
                    timezone.utc,
                ).isoformat().replace("+00:00", "Z")
                current = self._cooldown_epoch(group)
                if time.time() + cooldown_seconds > current:
                    project["cooldown_until"] = until

            await self._checkpoint_locked(True, "429")

    async def disable_key(self, key_index: int, reason: str) -> None:
        async with self.lock:
            self._key(key_index)["disabled"] = True
            self._key(key_index)["disabled_reason"] = reason
            await self._checkpoint_locked(True, "disable-key")

    async def close(self) -> None:
        async with self.lock:
            await self._checkpoint_locked(True, "final")

    def usage_summary(self) -> dict[str, Any]:
        return {
            "pacific_date": self.state.get("pacific_date"),
            "projects": {
                group: {
                    "requests_today": data.get("requests_today", 0),
                    "success": data.get("success", 0),
                    "http_429": data.get("http_429", 0),
                    "daily_exhausted": data.get("daily_exhausted", False),
                }
                for group, data in self.state.get("projects", {}).items()
            },
            "keys": {
                key_id: {
                    "project": data.get("project"),
                    "requests": data.get("requests", 0),
                    "success": data.get("success", 0),
                    "http_429": data.get("http_429", 0),
                    "disabled": data.get("disabled", False),
                }
                for key_id, data in self.state.get("keys", {}).items()
            },
        }
