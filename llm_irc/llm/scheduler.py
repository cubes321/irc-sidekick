"""Shares LM Studio's limited parallel slots between all LLM features.

Jobs have a priority. The scheduler runs at most `max_concurrency` jobs at once, and
background jobs (triage, translation, profiling) may use at most `max_concurrency - 1`
of them, so one slot is always free for something the user is waiting on.

Jobs submitted with a `key` replace any earlier job with the same key (queued or
running), and `delay` holds a job back before it may start. Together these give a
per-channel debounce: each new message resubmits "suggest:#chan" with a delay, so
suggestions are only generated once the channel goes quiet for a moment.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable


class Priority(IntEnum):
    USER = 0        # explicitly requested: hotkey, /summary, /rephrase, /profile
    SUGGEST = 1     # automatic reply suggestions for the active tab
    TRIAGE = 2
    TRANSLATE = 3
    PROFILE = 4

    @property
    def interactive(self) -> bool:
        return self <= Priority.SUGGEST


@dataclass(eq=False)  # identity semantics: jobs live in sets and are matched by identity
class _Job:
    priority: Priority
    seq: int
    factory: Callable[[], Awaitable[Any]]
    future: asyncio.Future
    key: str | None
    not_before: float
    task: asyncio.Task | None = field(default=None)


class Scheduler:
    def __init__(self, max_concurrency: int = 3,
                 on_change: Callable[[], None] | None = None) -> None:
        self.max_concurrency = max(1, max_concurrency)
        # With a single slot there is nothing to reserve.
        self.max_background = max(1, self.max_concurrency - 1)
        self.on_change = on_change
        self._pending: list[_Job] = []
        self._running: set[_Job] = set()
        self._by_key: dict[str, _Job] = {}
        self._seq = itertools.count()
        self._timer: asyncio.TimerHandle | None = None
        self.total_finished = 0

    # --- public API ---

    def submit(self, priority: Priority, factory: Callable[[], Awaitable[Any]], *,
               key: str | None = None, delay: float = 0.0) -> asyncio.Future:
        """Queue `factory()` to run when a slot is free. Returns a future for its result."""
        loop = asyncio.get_running_loop()
        if key is not None:
            self.cancel(key)
        job = _Job(priority, next(self._seq), factory, loop.create_future(), key,
                   time.monotonic() + delay)
        self._pending.append(job)
        if key is not None:
            self._by_key[key] = job
        self._dispatch()
        return job.future

    def has(self, key: str) -> bool:
        """True while a job with this key is queued or running."""
        return key in self._by_key

    def cancel(self, key: str) -> bool:
        job = self._by_key.pop(key, None)
        if job is None:
            return False
        if job in self._pending:
            self._pending.remove(job)
        if job.task is not None:
            job.task.cancel()
        if not job.future.done():
            job.future.cancel()
        self._dispatch()
        return True

    def cancel_all(self) -> None:
        for job in [*self._pending, *self._running]:
            if job.task is not None:
                job.task.cancel()
            if not job.future.done():
                job.future.cancel()
        self._pending.clear()
        self._by_key.clear()

    @property
    def running(self) -> int:
        return len(self._running)

    @property
    def queued(self) -> int:
        return len(self._pending)

    # --- internals ---

    def _background_running(self) -> int:
        return sum(1 for job in self._running if not job.priority.interactive)

    def _pick(self, now: float) -> _Job | None:
        if len(self._running) >= self.max_concurrency:
            return None
        allow_background = self._background_running() < self.max_background
        best = None
        for job in self._pending:
            if job.not_before > now:
                continue
            if not job.priority.interactive and not allow_background:
                continue
            if best is None or (job.priority, job.seq) < (best.priority, best.seq):
                best = job
        return best

    def _dispatch(self) -> None:
        now = time.monotonic()
        while (job := self._pick(now)) is not None:
            self._pending.remove(job)
            self._running.add(job)
            job.task = asyncio.ensure_future(self._run(job))
        self._arm_timer(now)
        if self.on_change is not None:
            self.on_change()

    def _arm_timer(self, now: float) -> None:
        """Wake up when the earliest delayed job becomes eligible."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        delayed = [job.not_before for job in self._pending if job.not_before > now]
        if delayed:
            loop = asyncio.get_running_loop()
            self._timer = loop.call_later(min(delayed) - now + 0.001, self._dispatch)

    async def _run(self, job: _Job) -> None:
        try:
            result = await job.factory()
        except asyncio.CancelledError:
            if not job.future.done():
                job.future.cancel()
        except Exception as exc:
            if not job.future.done():
                job.future.set_exception(exc)
        else:
            if not job.future.done():
                job.future.set_result(result)
        finally:
            self._running.discard(job)
            if job.key is not None and self._by_key.get(job.key) is job:
                del self._by_key[job.key]
            self.total_finished += 1
            self._dispatch()
