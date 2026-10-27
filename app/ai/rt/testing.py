"""An in-memory socket pair for tests and self-tests of the persistent transport: no network, no port. `a` is the client side, `b` the server side."""
from __future__ import annotations

import asyncio

_EOF = object()


class MemSocket:
    def __init__(self, inbox: asyncio.Queue, outbox: asyncio.Queue, state: dict):
        self._in, self._out, self._state = inbox, outbox, state
        self.sent = 0

    async def send(self, data) -> None:
        if self._state["closed"]:
            raise ConnectionError("closed")
        self.sent += 1
        await self._out.put(data)

    async def recv(self):
        item = await self._in.get()
        if item is _EOF:
            await self._in.put(_EOF)            # every later recv sees the end too
            raise EOFError("closed")
        return item

    async def close(self) -> None:
        if not self._state["closed"]:
            self._state["closed"] = True
            await self._out.put(_EOF)
            await self._in.put(_EOF)

    @property
    def closed(self) -> bool:
        return self._state["closed"]


def make_pair() -> tuple[MemSocket, MemSocket]:
    state = {"closed": False}
    a_to_b, b_to_a = asyncio.Queue(), asyncio.Queue()
    return MemSocket(b_to_a, a_to_b, state), MemSocket(a_to_b, b_to_a, state)
