import asyncio
import os
import time
import unittest
from uuid import uuid4
from unittest.mock import patch

import httpx
import redis.asyncio as redis

from quota_api.scheduler import QuotaScheduler
from quota_api.server import create_app
from quota_client import ProjectQuotaPool


@unittest.skipUnless(os.getenv("VALKEY_TEST_URL"), "VALKEY_TEST_URL is required")
class ValkeyQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = redis.Redis.from_url(os.environ["VALKEY_TEST_URL"], decode_responses=True)
        self.prefix = f"test:gemini-quota:{uuid4().hex}"
        self.now = [int(time.time() * 1000)]
        self.scheduler = QuotaScheduler(
            self.client,
            prefix=self.prefix,
            clock_ms=lambda: self.now[0],
            random_ms=lambda minimum, maximum: minimum,
        )

    async def asyncTearDown(self):
        cursor = 0
        while True:
            cursor, keys = await self.client.scan(cursor, match=f"{self.prefix}:*", count=100)
            if keys:
                await self.client.delete(*keys)
            if cursor == 0:
                break
        await self.client.aclose()

    async def configure(self, count=66, rpd=500):
        groups = [f"project-{index + 1}" for index in range(count)]
        result = await self.scheduler.configure(groups, rpm_per_project=15, rpd_per_project=rpd)
        self.assertEqual(result.status, 200, result.body)
        self.assertEqual(result.body["projectCount"], count)

    async def test_concurrent_leases_obey_shared_burst_and_use_distinct_projects(self):
        await self.configure()
        other_action = QuotaScheduler(
            self.client,
            prefix=self.prefix,
            clock_ms=lambda: self.now[0],
            random_ms=lambda minimum, maximum: minimum,
        )
        results = await asyncio.gather(*(
            (self.scheduler if index % 2 else other_action).lease()
            for index in range(20)
        ))
        granted = [item.body for item in results if item.status == 200]
        waiting = [item.body for item in results if item.status == 202]
        self.assertEqual(len(granted), 8)
        self.assertEqual(len(waiting), 12)
        self.assertEqual(len({item["projectIndex"] for item in granted}), 8)
        self.assertTrue(all(item["reason"] == "global_rate" for item in waiting))
        state = (await self.scheduler.status()).body
        self.assertEqual(state["global"]["activeLeases"], 8)

    async def test_reports_are_idempotent_and_503s_reduce_global_capacity(self):
        await self.configure()
        leases = []
        for _ in range(10):
            self.now[0] += 1_000
            leases.append((await self.scheduler.lease()).body)
        for lease in leases:
            result = await self.scheduler.report(lease["leaseId"], 503)
            self.assertEqual(result.status, 200)
        duplicate = await self.scheduler.report(leases[0]["leaseId"], 503)
        self.assertTrue(duplicate.body["duplicate"])
        state = (await self.scheduler.status()).body["global"]
        self.assertEqual(state["recentOutcomes"], 10)
        self.assertEqual(state["maxInflight"], 12)
        self.assertGreater(state["cooldownRemainingMs"], 0)
        self.now[0] += 31_000
        next_lease = (await self.scheduler.lease()).body
        await self.scheduler.report(next_lease["leaseId"], 503)
        state = (await self.scheduler.status()).body["global"]
        self.assertEqual(state["maxInflight"], 8)
        self.assertEqual(state["adaptiveStage"], 2)

    async def test_floor_stage_does_not_rearm_the_global_cooldown(self):
        """At the floor the controller must stop touching the cooldown clock.

        Re-entering the step-down branch while already at stage 3 re-armed
        controller_changed_at + cooldown_until every 30 s. That parked the whole
        pool (global_inflight=0) in 30-60 s bursts and starved every worker
        waiting on a lease: a real 82-page run stalled for four minutes and took
        16m13s, against 3m13s for the same document earlier the same day.
        """
        await self.configure()
        for _ in range(10):
            self.now[0] += 1_000
            lease = (await self.scheduler.lease()).body
            await self.scheduler.report(lease["leaseId"], 503)
        state = (await self.scheduler.status()).body["global"]
        self.assertEqual(state["maxInflight"], 12)
        for _ in range(2):
            self.now[0] += 31_000
            lease = (await self.scheduler.lease()).body
            await self.scheduler.report(lease["leaseId"], 503)
        state = (await self.scheduler.status()).body["global"]
        self.assertEqual(state["maxInflight"], 8)
        self.assertEqual(state["adaptiveStage"], 3)

        # Let the parking cooldown lapse, then keep the upstream failing well
        # past the 30 s controller window. A floor-stage pool must leave the
        # clock alone, so the remaining in-flight budget stays usable.
        self.now[0] += 120_000
        for _ in range(3):
            self.now[0] += 31_000
            lease = (await self.scheduler.lease()).body
            await self.scheduler.report(lease["leaseId"], 503)
        state = (await self.scheduler.status()).body["global"]
        self.assertEqual(state["adaptiveStage"], 3)
        self.assertEqual(state["maxInflight"], 8)
        self.assertEqual(state["cooldownRemainingMs"], 0)

    async def test_partial_success_recovery_lifts_capacity_off_the_floor(self):
        """A clean-enough success window must lift capacity off the floor.

        The previous gate demanded 19 successes out of the last 20 outcomes,
        which a free tier answering 503 on a third of its calls can never
        satisfy, so the pool stayed at 8 in-flight plus cooldowns forever.
        """
        await self.configure()
        for _ in range(10):
            self.now[0] += 1_000
            lease = (await self.scheduler.lease()).body
            await self.scheduler.report(lease["leaseId"], 503)
        self.now[0] += 31_000
        lease = (await self.scheduler.lease()).body
        await self.scheduler.report(lease["leaseId"], 503)
        state = (await self.scheduler.status()).body["global"]
        self.assertEqual(state["maxInflight"], 8)

        # Stay out of the cooldown the controller parks while the sampled window
        # still looks unhealthy, and add enough clean outcomes for the earlier
        # 503s to age out of that window.
        for _ in range(20):
            self.now[0] += 31_000
            lease = (await self.scheduler.lease()).body
            await self.scheduler.report(lease["leaseId"], 200)

        state = (await self.scheduler.status()).body["global"]
        self.assertGreater(state["maxInflight"], 8)
        self.assertLess(state["adaptiveStage"], 2)

    async def test_explicit_daily_exhaustion_removes_only_that_project(self):
        await self.configure()
        first = (await self.scheduler.lease()).body
        report = await self.scheduler.report(
            first["leaseId"], 429, cooldown_seconds=60,
            daily_exhausted=True, quota_type="rpd",
        )
        self.assertEqual(report.status, 200)
        second = (await self.scheduler.lease()).body
        self.assertNotEqual(second["projectIndex"], first["projectIndex"])
        state = (await self.scheduler.status()).body
        self.assertTrue(state["projects"][first["projectIndex"]]["dailyExhausted"])

    async def test_expired_lease_releases_global_inflight_slot(self):
        self.scheduler.max_inflight = 1
        await self.configure()
        first = await self.scheduler.lease()
        self.assertEqual(first.status, 200)
        self.assertEqual((await self.scheduler.lease()).body["reason"], "max_inflight")
        self.now[0] += 181_000
        second = await self.scheduler.lease()
        self.assertEqual(second.status, 200)
        self.assertNotEqual(second.body["leaseId"], first.body["leaseId"])

    async def test_mapping_change_remaps_an_idle_pool(self):
        """Adding a key must not wedge every later Action.

        The mapping hash covers the whole key list, so adding or removing one
        key changes it. Refusing the reconfiguration left the pool unusable
        until an operator wiped the Valkey state by hand.
        """
        await self.configure()
        groups = [f"project-{index + 1}" for index in range(67)]
        result = await self.scheduler.configure(groups, rpm_per_project=15, rpd_per_project=500)
        self.assertEqual(result.status, 200, result.body)
        self.assertTrue(result.body["remapped"])
        self.assertEqual(result.body["projectCount"], 67)
        state = (await self.scheduler.status()).body
        self.assertEqual(state["keyCount"], 67)
        lease = await self.scheduler.lease()
        self.assertEqual(lease.status, 200)

    async def test_mapping_change_is_rejected_while_a_lease_is_active(self):
        await self.configure()
        lease = await self.scheduler.lease()
        self.assertEqual(lease.status, 200)
        groups = [f"project-{index + 1}" for index in range(67)]
        result = await self.scheduler.configure(groups, rpm_per_project=15, rpd_per_project=500)
        self.assertEqual(result.status, 409)
        self.assertEqual(result.body["error"], "project_mapping_changed")
        self.assertEqual(result.body["activeLeases"], 1)
        await self.scheduler.report(lease.body["leaseId"], 200)

    async def test_retrying_the_same_request_id_reuses_the_lease(self):
        await self.configure()
        request_id = str(uuid4())
        first = await self.scheduler.lease(request_id)
        repeated = await self.scheduler.lease(request_id)
        self.assertEqual(first.status, 200)
        self.assertEqual(repeated.body["leaseId"], first.body["leaseId"])
        self.assertTrue(repeated.body["replayed"])
        self.assertEqual((await self.scheduler.status()).body["requestsToday"], 1)

    async def test_generic_429_cools_project_and_global_pool(self):
        await self.configure()
        lease = (await self.scheduler.lease()).body
        await self.scheduler.report(
            lease["leaseId"], 429, cooldown_seconds=30, quota_type="unknown",
        )
        state = (await self.scheduler.status()).body
        self.assertGreater(state["global"]["cooldownRemainingMs"], 0)
        self.assertGreater(
            state["projects"][lease["projectIndex"]]["cooldownUntil"], self.now[0],
        )
        self.assertEqual((await self.scheduler.lease()).body["reason"], "global_cooldown")

    async def test_pacific_new_day_resets_daily_project_quota(self):
        await self.configure(rpd=1)
        first = await self.scheduler.lease()
        self.assertEqual(first.status, 200)
        self.now[0] += 24 * 60 * 60 * 1_000
        second = await self.scheduler.lease()
        self.assertEqual(second.status, 200)
        state = (await self.scheduler.status()).body
        self.assertEqual(state["requestsToday"], 1)

    async def test_authenticated_http_contract(self):
        app = create_app(self.scheduler, token="test-token-" + "x" * 32)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://quota.test") as client:
            denied = await client.post("/v1/status", json={})
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(denied.json(), {"error": "unauthorized"})
            headers = {"Authorization": "Bearer test-token-" + "x" * 32}
            configured = await client.post(
                "/v1/configure",
                headers=headers,
                json={
                    "keyGroups": [f"project-{index + 1}" for index in range(66)],
                    "rpmPerProject": 15,
                    "rpdPerProject": 500,
                },
            )
            self.assertEqual(configured.status_code, 200)
            leased = await client.post("/v1/lease", headers=headers, json={})
            self.assertEqual(leased.status_code, 200)
            reported = await client.post(
                "/v1/report", headers=headers,
                json={"leaseId": leased.json()["leaseId"], "httpStatus": 200},
            )
            self.assertEqual(reported.status_code, 200)
            status = await client.post("/v1/status", headers=headers, json={})
            self.assertEqual(status.json()["requestsToday"], 1)

    async def test_conversion_client_uses_valkey_api_contract(self):
        token = "test-token-" + "x" * 32
        app = create_app(self.scheduler, token=token)
        groups = [f"project-{index + 1}" for index in range(66)]
        keys = [f"dummy-key-{index + 1}" for index in range(66)]
        with patch.dict(os.environ, {
            "GEMINI_QUOTA_API_URL": "http://localhost",
            "GEMINI_QUOTA_API_TOKEN": token,
        }):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost",
            ) as client:
                pool = await ProjectQuotaPool.create(keys, groups, 15, 500, client)
                key_index, api_key, lease_id = await pool.acquire()
                self.assertEqual(api_key, keys[key_index])
                await pool.mark_success(key_index, lease_id)
                state = await pool.remote_status()
                self.assertEqual(state["requestsToday"], 1)
                self.assertEqual(state["projects"][key_index]["successCount"], 1)


if __name__ == "__main__":
    unittest.main()
