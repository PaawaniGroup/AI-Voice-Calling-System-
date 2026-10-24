"""Clock abstraction for the streaming pipeline: every wait in it goes through a clock object, so tests can run it on virtual time."""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable


class RealClock:
    """The monotonic clock (the WSL host's wall clock steps by tens of seconds, so wall time is never used for pacing)."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    async def sleep_until(self, t: float) -> None:
        await asyncio.sleep(max(0.0, t - time.monotonic()))

    async def wait_for(self, aw: Awaitable, timeout: float):
        """`asyncio.wait_for` semantics: on timeout the awaited task is cancelled and asyncio.TimeoutError is raised."""
        return await asyncio.wait_for(aw, timeout)


class TimeoutOnClock(Exception):
    pass


async def wait_for_on(clock, aw: Awaitable, timeout: float):
    """`wait_for` driven by ANY clock (used by tests with a virtual clock): the awaitable is cancelled when the clock's timer fires first."""
    task = asyncio.ensure_future(aw)
    timer = asyncio.ensure_future(clock.sleep(timeout))
    try:
        done, _ = await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        timer.cancel()
        raise
    if task in done:
        timer.cancel()
        return task.result()
    task.cancel()
    try:
        await task
    except BaseException:
        pass
    raise asyncio.TimeoutError()
