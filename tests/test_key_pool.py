"""Local key rotation: even spread, per-bucket spacing, 429 cooldowns."""
import unittest
from collections import Counter
from unittest.mock import patch

import httpx

from key_pool import (
    KeyPool,
    QuotaPoolExhaustedError,
    classify_quota_error,
    parse_key_groups,
    retry_after_seconds,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def make_pool(keys, groups=None, rpm=60.0, rpd=1000):
    clock = FakeClock()
    pool = KeyPool(keys, groups, rpm_per_key=rpm, rpd_per_key=rpd, clock=clock, sleep=clock.sleep)
    return pool, clock


class KeyPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Deterministic jitter: no extra spacing, no cooldown stretch.
        self.jitter = patch("key_pool.random.uniform", side_effect=lambda low, _high: low)
        self.jitter.start()

    async def asyncTearDown(self):
        self.jitter.stop()

    async def test_requests_spread_evenly_over_all_keys(self):
        pool, _ = make_pool([f"k{i}" for i in range(5)])
        picks = [(await pool.acquire())[1] for _ in range(50)]
        self.assertEqual(set(Counter(picks).values()), {10})
        self.assertEqual(len(set(picks[:5])), 5, "every key is used before any key repeats")

    async def test_a_bucket_never_exceeds_its_rpm(self):
        pool, clock = make_pool(["a", "b"], rpm=10)
        times = {"a": [], "b": []}
        for _ in range(40):
            _, key, _ = await pool.acquire()
            times[key].append(clock.now)
        for key, stamps in times.items():
            for start in stamps:
                in_window = [t for t in stamps if start <= t < start + 60]
                self.assertLessEqual(len(in_window), 10, key)
            gaps = [b - a for a, b in zip(stamps, stamps[1:])]
            self.assertGreaterEqual(min(gaps), 6.0, "requests inside a bucket are evenly spaced")

    async def test_keys_of_one_project_share_its_pacing(self):
        pool, clock = make_pool(["a1", "a2", "b1"], ["p-a", "p-a", "p-b"], rpm=6)
        stamps = []
        for _ in range(6):
            _, key, _ = await pool.acquire()
            stamps.append((key, clock.now))
        project_a = [t for key, t in stamps if key.startswith("a")]
        self.assertEqual([key for key, _ in stamps if key.startswith("a")], ["a1", "a2", "a1"])
        self.assertGreaterEqual(min(b - a for a, b in zip(project_a, project_a[1:])), 10.0)

    async def test_global_gap_prevents_a_starting_burst(self):
        pool, clock = make_pool([f"k{i}" for i in range(4)], rpm=60)
        start = clock.now
        for _ in range(4):
            await pool.acquire()
        self.assertGreaterEqual(clock.now - start, 3 * pool.global_gap - 1e-9)

    async def test_rate_limited_bucket_cools_down_and_escalates(self):
        pool, clock = make_pool(["a", "b"], rpm=600)
        index, _, lease = await pool.acquire()
        await pool.rate_limited(index, lease, None, quota_type="rpm")
        first_until = pool.buckets[index].cooldown_until
        self.assertAlmostEqual(first_until - clock.now, 20.0)
        picks = [(await pool.acquire())[0] for _ in range(5)]
        self.assertNotIn(index, picks, "a cooling bucket gets no traffic")
        await pool.rate_limited(index, lease, None)
        self.assertGreaterEqual(pool.buckets[index].cooldown_until - clock.now, 39.0)
        await pool.mark_success(index, lease)
        self.assertEqual(pool.buckets[index].strikes, 0)

    async def test_server_retry_delay_wins_when_longer(self):
        pool, clock = make_pool(["a"])
        index, _, lease = await pool.acquire()
        await pool.rate_limited(index, lease, 75.0)
        self.assertAlmostEqual(pool.buckets[0].cooldown_until - clock.now, 75.0)

    async def test_daily_exhaustion_retires_the_bucket(self):
        pool, _ = make_pool(["a", "b"])
        await pool.rate_limited(0, "lease", None, daily_exhausted=True, quota_type="rpd")
        self.assertEqual({(await pool.acquire())[1] for _ in range(4)}, {"b"})
        await pool.rate_limited(1, "lease", None, daily_exhausted=True)
        with self.assertRaises(QuotaPoolExhaustedError):
            await pool.acquire()

    async def test_retired_key_gets_no_more_traffic(self):
        pool, _ = make_pool(["a", "b"])
        pool.retire(0, "HTTP 402")
        self.assertEqual({(await pool.acquire())[1] for _ in range(4)}, {"b"})
        self.assertTrue(pool.usage_summary()["keys"]["1"]["disabled"])

    async def test_per_run_daily_cap(self):
        pool, _ = make_pool(["only"], rpd=2)
        await pool.acquire()
        await pool.acquire()
        with self.assertRaises(QuotaPoolExhaustedError):
            await pool.acquire()

    async def test_usage_summary_reports_spread_and_keys(self):
        pool, _ = make_pool(["a", "b"])
        index, _, lease = await pool.acquire()
        pool.record_http_result(200, 1.5)
        await pool.mark_success(index, lease)
        summary = pool.usage_summary()
        self.assertEqual(summary["performance"]["success"], 1)
        self.assertEqual(summary["spread"], {"buckets": 2, "min_requests": 0, "max_requests": 1})
        self.assertEqual(summary["keys"][str(index + 1)]["success"], 1)


class KeyGroupTests(unittest.TestCase):
    def test_empty_groups_mean_one_bucket_per_key(self):
        self.assertEqual(parse_key_groups(2, ""), ["key-1", "key-2"])

    def test_group_count_must_match_keys(self):
        self.assertEqual(parse_key_groups(2, "p1\np2"), ["p1", "p2"])
        with self.assertRaises(ValueError):
            parse_key_groups(2, "p1")


class QuotaErrorTests(unittest.TestCase):
    def test_generic_429_does_not_exhaust_the_day(self):
        payload = {"error": {"code": 429, "message": "Resource has been exhausted (e.g. check quota).",
                             "status": "RESOURCE_EXHAUSTED"}}
        self.assertEqual(classify_quota_error(payload), "unknown")

    def test_identifies_quota_dimension_from_error_details(self):
        cases = {
            "GenerateRequestsPerMinutePerProjectPerModel": "rpm",
            "GenerateContentInputTokensPerModelPerMinute": "tpm",
            "GenerateRequestsPerDayPerProjectPerModel": "rpd",
        }
        for quota_id, expected in cases.items():
            with self.subTest(quota_id=quota_id):
                payload = {"error": {"details": [{"quotaId": quota_id}]}}
                self.assertEqual(classify_quota_error(payload), expected)

    def test_uses_the_longest_server_retry_delay(self):
        payload = {"error": {"details": [{"retryDelay": "60s"}]}}
        self.assertEqual(retry_after_seconds(httpx.Headers({"Retry-After": "5"}), payload), 60.0)


if __name__ == "__main__":
    unittest.main()
