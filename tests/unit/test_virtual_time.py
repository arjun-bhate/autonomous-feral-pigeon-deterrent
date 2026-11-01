"""Tests for the test helper that fakes the passing of time for asyncio code."""

import asyncio

from support.virtual_time import VirtualTime


async def test_a_sleeper_wakes_only_when_the_clock_passes_its_time():
    vt = VirtualTime()
    woke: list[int] = []

    async def worker() -> None:
        await vt.sleep(0.5)
        woke.append(vt.clock.now_ms())

    task = asyncio.create_task(worker())
    await vt.advance(499)
    assert woke == []
    await vt.advance(1)
    assert woke == [500]
    await task


async def test_a_repeating_loop_runs_once_per_interval():
    vt = VirtualTime()
    stamps: list[int] = []

    async def loop() -> None:
        while True:
            stamps.append(vt.clock.now_ms())
            await vt.sleep(0.2)

    task = asyncio.create_task(loop())
    await vt.advance(1000)
    task.cancel()
    assert stamps == [0, 200, 400, 600, 800, 1000]


async def test_two_loops_share_one_clock_in_time_order():
    vt = VirtualTime()
    seen: list[tuple[str, int]] = []

    async def loop(name: str, seconds: float) -> None:
        while True:
            await vt.sleep(seconds)
            seen.append((name, vt.clock.now_ms()))

    tasks = [asyncio.create_task(loop("fast", 0.1)), asyncio.create_task(loop("slow", 0.25))]
    await vt.advance(500)
    for task in tasks:
        task.cancel()
    times = [t for _, t in seen]
    assert times == sorted(times)  # always woken in time order
    assert {t for name, t in seen if name == "slow"} == {250, 500}
    assert [t for name, t in seen if name == "fast"] == [100, 200, 300, 400, 500]
    assert vt.clock.now_ms() == 500


async def test_a_cancelled_sleeper_does_not_break_advance():
    vt = VirtualTime()
    task = asyncio.create_task(vt.sleep(1))
    await vt.advance(10)
    task.cancel()
    await vt.advance(2000)
    assert vt.elapsed_ms == 2010
