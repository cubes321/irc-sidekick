import asyncio

import pytest

from llm_irc.llm.scheduler import Priority, Scheduler


class Tracker:
    def __init__(self):
        self.running = 0
        self.peak = 0
        self.order: list[str] = []

    def job(self, name: str, gate: asyncio.Event | None = None, duration: float = 0.01):
        async def run():
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.order.append(name)
            try:
                if gate is not None:
                    await gate.wait()
                else:
                    await asyncio.sleep(duration)
                return name
            finally:
                self.running -= 1
        return run


async def test_never_exceeds_max_concurrency():
    t, s = Tracker(), Scheduler(3)
    futures = [s.submit(Priority.USER, t.job(f"j{i}")) for i in range(10)]
    assert await asyncio.gather(*futures) == [f"j{i}" for i in range(10)]
    assert t.peak == 3


async def test_background_jobs_leave_one_slot_free():
    t, s = Tracker(), Scheduler(3)
    gate = asyncio.Event()
    background = [s.submit(Priority.PROFILE, t.job(f"bg{i}", gate)) for i in range(5)]
    await asyncio.sleep(0)
    assert s.running == 2  # third slot is reserved
    interactive = s.submit(Priority.USER, t.job("user"))
    assert await asyncio.wait_for(interactive, 1) == "user"
    gate.set()
    await asyncio.gather(*background)


async def test_priority_order_when_slots_free_up():
    t, s = Tracker(), Scheduler(1)
    gate = asyncio.Event()
    first = s.submit(Priority.USER, t.job("first", gate))
    later = [s.submit(Priority.PROFILE, t.job("profile")),
             s.submit(Priority.TRANSLATE, t.job("translate")),
             s.submit(Priority.USER, t.job("user"))]
    gate.set()
    await asyncio.gather(first, *later)
    assert t.order == ["first", "user", "translate", "profile"]


async def test_same_key_replaces_and_debounces():
    t, s = Tracker(), Scheduler(3)
    a = s.submit(Priority.SUGGEST, t.job("a"), key="suggest", delay=0.05)
    b = s.submit(Priority.SUGGEST, t.job("b"), key="suggest", delay=0.05)
    assert a.cancelled()
    assert await b == "b"
    assert t.order == ["b"]


async def test_same_key_cancels_running_job():
    t, s = Tracker(), Scheduler(3)
    gate = asyncio.Event()
    a = s.submit(Priority.SUGGEST, t.job("a", gate), key="k")
    await asyncio.sleep(0)
    assert s.has("k")
    b = s.submit(Priority.SUGGEST, t.job("b"), key="k")
    assert await b == "b"
    assert a.cancelled()
    assert t.running == 0


async def test_exceptions_propagate():
    s = Scheduler(2)

    async def boom():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await s.submit(Priority.USER, boom)
    assert s.running == 0
