"""Race bounded speculative copies only after the primary HTTP request starts."""
from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar


CURRENT_POLICY = ContextVar('request_policy', default=None)


class RequestPolicy:
    def __init__(self, *, concurrency, hedge_after=60.0, hedge_budget=0,
                 can_hedge=lambda: True, latency_p95=lambda: None):
        self.slots = asyncio.Semaphore(concurrency)
        self.extra_slots = asyncio.Semaphore(2)
        self.hedge_after = hedge_after
        self.budget = hedge_budget
        self.can_hedge = can_hedge
        self.latency_p95 = latency_p95
        self.hedges = 0
        self.hedge_wins = 0
        self.active = {}
        self.validated = set()

    def mark_validated(self):
        """HTTP/parsing finished successfully; only bounded reporting remains."""
        self.validated.add(asyncio.current_task())

    def summary(self):
        return {'hedges_launched': self.hedges, 'hedge_wins': self.hedge_wins,
                'hedge_budget': self.budget, 'http_active': len(self.active),
                'oldest_http_seconds': round(max((time.monotonic() - t for t in self.active.values()), default=0), 1)}

    async def run(self, attempt, name, *, max_duration=None):
        started = asyncio.Event()
        tasks = []
        deadline = None

        async def send(index):
            async def with_slot():
                async with self.slots:
                    task = asyncio.current_task()
                    def on_started():
                        nonlocal deadline
                        self.active[task] = time.monotonic()
                        if index == 0:
                            if max_duration is not None:
                                deadline = time.monotonic() + max_duration
                            started.set()
                    try:
                        return await attempt(on_started)
                    finally:
                        self.active.pop(task, None)
            if index:
                async with self.extra_slots:
                    return await with_slot()
            return await with_slot()

        primary = asyncio.create_task(send(0))
        tasks.append(primary)
        start_waiter = asyncio.create_task(started.wait())
        first_error = None
        def winner(task, result):
            if task is not primary:
                self.hedge_wins += 1
                print(f'[hedge-win] {name}: duplicate returned valid Markdown', flush=True)
            return result

        async def finish_at_deadline():
            # The converter marks validity before reporting and separately
            # bounds that cleanup. Do not throw away text received in time.
            for task in tasks:
                if task not in self.validated and not task.done():
                    task.cancel()
            for task in tasks:
                if task in self.validated:
                    return winner(task, await task)
            raise first_error or RuntimeError(f'{name}: request group deadline exceeded ({max_duration:g}s)')
        try:
            await asyncio.wait([primary, start_waiter], return_when=asyncio.FIRST_COMPLETED)
            if primary.done():
                return primary.result()
            if self.hedge_after <= 0:
                return await primary
            delay = max(self.hedge_after, 2 * (self.latency_p95() or 0))
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            done, _ = await asyncio.wait([primary], timeout=delay)
            if done:
                return primary.result()
            if deadline is not None and time.monotonic() >= deadline:
                return await finish_at_deadline()
            copies = min(2, max(0, self.budget - self.hedges)) if self.can_hedge() else 0
            if not copies:
                return await primary
            self.hedges += copies
            print(f'[hedge] {name} slow_after={delay:.1f}s copies={copies} budget={self.hedges}/{self.budget}', flush=True)
            tasks.extend(asyncio.create_task(send(i + 1)) for i in range(copies))
            pending = set(tasks)
            while pending:
                remaining = max(0, deadline - time.monotonic()) if deadline is not None else None
                done, pending = await asyncio.wait(pending, timeout=remaining,
                                                   return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    return await finish_at_deadline()
                # Prefer the original when simultaneous valid responses arrive.
                for task in [t for t in tasks if t in done]:
                    try:
                        result = task.result()
                    except Exception as exc:
                        first_error = first_error or exc
                        continue
                    return winner(task, result)
            raise first_error or RuntimeError('No request returned usable output')
        finally:
            start_waiter.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(start_waiter, *tasks, return_exceptions=True)
            self.validated.difference_update(tasks)
