"""Virtual time for asyncio tests: components wait only through `clock.sleep`, and `run()` advances the clock to the next timer whenever every task is idle."""
import asyncio
import heapq
import itertools


class DeadlockError(AssertionError):
    pass


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start
        self.t0 = start
        self._heap: list = []
        self._seq = itertools.count()

    def now(self) -> float:
        return self.t

    def elapsed(self) -> float:
        return self.t - self.t0

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self.t + seconds, next(self._seq), fut))
        await fut

    async def sleep_until(self, t: float) -> None:
        await self.sleep(t - self.t)

    async def wait_for(self, aw, timeout: float):
        from app.ai.streaming.clock import wait_for_on
        return await wait_for_on(self, aw, timeout)

    async def run(self, coro, max_virtual_s: float = 3600.0):
        """Run `coro` to completion on virtual time and return its result (or raise its exception)."""
        task = asyncio.ensure_future(coro)
        while not task.done():
            for _ in range(60):
                await asyncio.sleep(0)
                if task.done():
                    break
            if task.done():
                break
            while self._heap and self._heap[0][2].done():
                heapq.heappop(self._heap)
            if not self._heap:
                for _ in range(300):
                    await asyncio.sleep(0)
                    if task.done() or self._heap:
                        break
                if not task.done() and not self._heap:
                    task.cancel()
                    raise DeadlockError("no timer is pending and the task is not finishing")
                continue
            when, _, fut = heapq.heappop(self._heap)
            self.t = max(self.t, when)
            if self.t - self.t0 > max_virtual_s:
                task.cancel()
                raise DeadlockError(f"virtual time exceeded {max_virtual_s}s")
            if not fut.done():
                fut.set_result(None)
        return task.result()
