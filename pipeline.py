"""Bounded streaming work queue, independent page retries, durable progress."""
from __future__ import annotations

import asyncio
import heapq
import json
import random
import time
from pathlib import Path


class Progress:
    def __init__(self, output_dir: Path, *, total_chunks: int, total_pages: int):
        self.output_dir = output_dir
        self.total_chunks = total_chunks
        self.total_pages = total_pages
        self.started = time.monotonic()
        self.state = 'running'
        self.rendered_pages = 0
        self.results = {}
        self.active = {}
        self.retrying = set()
        self.queued = 0
        self.request_summary = lambda: {}

    def snapshot(self):
        ok = sum(r['status'] == 'ok' for r in self.results.values())
        blank = sum(r['status'] == 'blank' for r in self.results.values())
        failed = sum(r['status'] == 'failed' and name not in self.retrying
                     and name not in self.active for name, r in self.results.items())
        completed = ok + blank + failed
        elapsed = time.monotonic() - self.started
        pending = max(0, self.total_chunks - completed)
        return {
            'state': self.state, 'total_pages': self.total_pages,
            'rendered_pages': self.rendered_pages, 'total_chunks': self.total_chunks,
            'completed_chunks': completed, 'success_chunks': ok, 'blank_chunks': blank,
            'failed_chunks': failed, 'pending_chunks': pending,
            'active_chunks': len(self.active), 'queued_chunks': self.queued,
            'retrying_chunks': len(self.retrying),
            'percent': round(100 * completed / self.total_chunks, 1) if self.total_chunks else 100,
            'elapsed_seconds': round(elapsed, 2),
            'eta_seconds': round(elapsed / completed * pending, 1) if completed else None,
            'active': [{'chunk': name, 'elapsed_seconds': round(time.monotonic() - started, 1)}
                       for name, started in self.active.items()],
            'requests': self.request_summary(),
        }

    def write(self, *, log=False):
        snapshot = self.snapshot()
        path = self.output_dir / 'progress.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(path)
        # Keep an incremental result index even if rendering or the process fails.
        result_path = self.output_dir / 'results.json'
        temporary = result_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(list(self.results.values()), ensure_ascii=False, indent=2) + '\n')
        temporary.replace(result_path)
        if log:
            print(f"[progress] {snapshot['percent']}% "
                  f"completed={snapshot['completed_chunks']}/{self.total_chunks} "
                  f"rendered={self.rendered_pages}/{self.total_pages} "
                  f"active={len(self.active)} retrying={len(self.retrying)} "
                  f"failed={snapshot['failed_chunks']} eta={snapshot['eta_seconds']}s", flush=True)
        return snapshot


def retry_delay(round_number):
    # Delay belongs to this failed page; workers remain available for other pages.
    base = min(60.0, 5.0 * 2 ** (round_number - 2))
    return random.uniform(base, base * 1.5)


async def schedule_chunks(source, run_one, concurrency, retry_rounds, progress,
                          *, retry_delay=retry_delay):
    queue = asyncio.Queue(maxsize=max(1, concurrency * 2))
    wake = asyncio.Event()
    producing = True
    producer_error = None
    names = []

    async def produce():
        nonlocal producing, producer_error
        try:
            async for chunk in source:
                progress.rendered_pages += (getattr(chunk, 'end_page', 1)
                                            - getattr(chunk, 'start_page', 1) + 1)
                await queue.put(chunk)
                wake.set()
        except Exception as exc:
            producer_error = exc
        finally:
            producing = False
            wake.set()

    producer = asyncio.create_task(produce())
    active = {}
    retries = []
    sequence = 0
    progress.write(log=True)
    try:
        while producing or not queue.empty() or active or retries:
            wake.clear()
            while len(active) < concurrency:
                if not queue.empty():
                    chunk = queue.get_nowait()
                    name = getattr(chunk, 'stem', str(chunk))
                    names.append(name)
                    round_number = 1
                elif retries and retries[0][0] <= time.monotonic():
                    _, _, chunk, round_number = heapq.heappop(retries)
                    name = getattr(chunk, 'stem', str(chunk))
                else:
                    break
                progress.retrying.discard(name)
                progress.active[name] = time.monotonic()
                task = asyncio.create_task(run_one(chunk, round_number))
                active[task] = (chunk, name, round_number)
                task.add_done_callback(lambda _: wake.set())
            progress.queued = queue.qsize()
            for task in [task for task in active if task.done()]:
                chunk, name, round_number = active.pop(task)
                progress.active.pop(name, None)
                result = task.result()
                progress.results[name] = result
                if result['status'] == 'failed' and result.get('retryable') and round_number < retry_rounds:
                    delay = retry_delay(round_number + 1)
                    sequence += 1
                    heapq.heappush(retries, (time.monotonic() + delay, sequence, chunk, round_number + 1))
                    progress.retrying.add(name)
                    print(f'[retry-queue] {name} round={round_number + 1}/{retry_rounds} wait={delay:.1f}s', flush=True)
                progress.write(log=True)
                wake.set()
            if wake.is_set():
                continue
            if not producing and queue.empty() and not active and not retries:
                break
            timeout = max(0, retries[0][0] - time.monotonic()) if retries and len(active) < concurrency else None
            try:
                await asyncio.wait_for(wake.wait(), timeout=timeout)
            except TimeoutError:
                pass
        if producer_error is not None:
            raise producer_error
        progress.state = 'failed' if any(r['status'] == 'failed' for r in progress.results.values()) else 'completed'
        return [progress.results[name] for name in names]
    except BaseException:
        progress.state = 'failed'
        raise
    finally:
        producer.cancel()
        for task in active:
            task.cancel()
        await asyncio.gather(producer, *active, return_exceptions=True)
        progress.active.clear()
        progress.write(log=True)
