"""One authenticated WebSocket per call and service (per-call isolation: the server sees one call per connection).

  - hello with a short-lived signed token; no key ever goes over the wire;
  - heartbeats (ping/pong); a missed pong closes the connection and fails the requests in flight (they are NEVER replayed: the caller falls back to the queue path);
  - reconnect with exponential backoff and jitter, bounded attempts;
  - backpressure: every request has a bounded queue; a consumer that stalls longer than `stall_s` fails that request instead of blocking the socket for ever;
  - cancellation that really stops the work: a `cancel` message, and if the server does not acknowledge within `cancel_grace_s` the connection is CLOSED (a closed socket stops the
    server's generation); the next request reconnects;
  - the socket is an abstraction (`WsLike`): the real connector uses `websockets` (websockets_connector); tests use an in-memory pair, so everything here runs offline.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Protocol

from app.ai.rt import protocol as P
from app.ai.rt.resilience import Backoff

log = logging.getLogger("mme_telephony.ai.rt")


class RtError(Exception):
    """kind: connect | auth | lost | timeout | server_error | stalled | breaker_open | killed. `detail` is a code, never text of a call."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}:{detail}" if detail else kind)
        self.kind, self.detail = kind, detail


class WsLike(Protocol):
    async def send(self, data: "str | bytes") -> None: ...
    async def recv(self) -> "str | bytes": ...
    async def close(self) -> None: ...


Connector = Callable[[str], Awaitable[WsLike]]


async def websockets_connector(url: str) -> WsLike:
    """The real connector (websockets >= 13, `websockets.asyncio.client`). Its own ping is off: the heartbeats of this module are the liveness check."""
    from websockets.asyncio.client import connect
    return await connect(url, open_timeout=None, ping_interval=None, max_size=2 ** 22)


@dataclass
class RtConfig:
    connect_timeout_s: float = 5.0
    hello_timeout_s: float = 5.0
    hb_interval_s: float = 5.0
    hb_timeout_s: float = 12.0
    cancel_grace_s: float = 0.5
    queue_max: int = 64
    stall_s: float = 10.0
    max_connect_attempts: int = 2
    request_timeout_s: float = 25.0
    token_ttl_s: float = 120.0


class _Req:
    def __init__(self, queue_max: int):
        self.q: asyncio.Queue = asyncio.Queue(maxsize=queue_max)


class PersistentConnection:
    def __init__(self, service: str, url: str, call_id: str, secret_provider: Callable[[], "bytes | None"], connector: Connector, cfg: RtConfig | None = None, *,
                 clock=time.monotonic, sleep=asyncio.sleep, backoff: Backoff | None = None, resolver=None):
        self.service, self.url, self.call_id, self.secret_provider, self.connector = service, url, call_id, secret_provider, connector
        self.resolver = resolver              # async () -> current ws(s):// URL or None: a Pod's external TCP port (and, on Community Cloud, its IP) changes when the Pod resets
        self.cfg, self.clock, self._sleep = cfg or RtConfig(), clock, sleep
        self.backoff = backoff or Backoff()
        self._ws: WsLike | None = None
        self._reader: asyncio.Task | None = None
        self._hb: asyncio.Task | None = None
        self._pending: dict[int, _Req] = {}
        self._cancelling: dict[int, asyncio.Event] = {}
        self._seq = 0
        self._dead = True
        self._closed = False
        self._last_pong = 0.0
        self._lock = asyncio.Lock()
        self.counters = {"connects": 0, "connect_failures": 0, "cancels": 0, "cancel_aborts": 0, "lost": 0, "rediscovered": 0}

    # ------------------------------------------------------------------ connect
    async def ensure_ready(self) -> None:
        if self._closed:
            raise RtError("lost", "closed")
        async with self._lock:
            if self._ws is not None and not self._dead:
                return
            await self._connect()

    async def _connect(self) -> None:
        secret = self.secret_provider()
        if not secret:
            raise RtError("auth", "no_secret")
        if self._ws is not None:                                          # a dead connection that was not closed yet: never leak it
            old, self._ws = self._ws, None
            await _quiet_close(old)
        last: Exception | None = None
        if self.counters["connects"] > 0:                                 # a RE-connect: the Pod may have been reset, so ask where it is now
            await self._rediscover()
        for attempt in range(max(1, self.cfg.max_connect_attempts)):
            ws = None
            try:
                ws = await asyncio.wait_for(self.connector(self.url), self.cfg.connect_timeout_s)
                token = P.make_token(secret, self.call_id, self.cfg.token_ttl_s)
                await ws.send(P.dumps({"type": "hello", "v": P.VERSION, "service": self.service, "call": self.call_id, "token": token}))
                reply = P.parse(await asyncio.wait_for(ws.recv(), self.cfg.hello_timeout_s))
                if reply["type"] == "error":
                    await _quiet_close(ws)
                    raise RtError("auth", str(reply.get("code", "")))
                if reply["type"] != "ready":
                    raise RtError("connect", "no_ready")
                self._ws, self._dead, self._last_pong = ws, False, self.clock()
                self._reader = asyncio.ensure_future(self._read_loop(ws))
                self._hb = asyncio.ensure_future(self._hb_loop(ws))
                self.backoff.reset()
                self.counters["connects"] += 1
                return
            except RtError as e:
                if e.kind == "auth":
                    raise
                last = e
            except asyncio.CancelledError:
                if ws is not None:
                    await _quiet_close(ws)
                raise
            except Exception as e:                                        # connection refused, DNS, timeout, bad first frame ...
                last = e
            if ws is not None and self._ws is not ws:
                await _quiet_close(ws)
            self.counters["connect_failures"] += 1
            if attempt + 1 < self.cfg.max_connect_attempts:
                await self._sleep(self.backoff.next())
                await self._rediscover()
        raise RtError("connect", type(last).__name__ if last is not None else "")

    async def _rediscover(self) -> None:
        """Ask the resolver for the server's current address; a bad answer, an error or a timeout keeps the old one. Never raises."""
        if self.resolver is None:
            return
        try:
            new = await asyncio.wait_for(self.resolver(), self.cfg.connect_timeout_s)
        except Exception:
            return
        if isinstance(new, str) and new.startswith(("wss://", "ws://")) and new != self.url:
            self.url = new
            self.counters["rediscovered"] += 1

    # ------------------------------------------------------------------ background loops
    async def _read_loop(self, ws: WsLike) -> None:
        try:
            while True:
                m = await ws.recv()
                if isinstance(m, (bytes, bytearray)):
                    seq, payload = P.unpack_frame(bytes(m))
                    req = self._pending.get(seq)
                    if req is not None:
                        await self._deliver(seq, req, ("frame", payload))
                    continue
                d = P.parse(m)
                t = d["type"]
                if t == "pong":
                    self._last_pong = self.clock()
                    continue
                seq = d.get("seq")
                if not isinstance(seq, int):
                    if t == "error":
                        raise RtError("server_error", str(d.get("code", "")))
                    continue
                ev = self._cancelling.get(seq)
                if ev is not None and t in P.TERMINAL_TYPES:
                    ev.set()
                req = self._pending.get(seq)
                if req is not None:
                    await self._deliver(seq, req, (t, d))
        except asyncio.CancelledError:
            raise
        except Exception as e:                                            # closed socket, protocol error, ...
            self._fail_all(e if isinstance(e, RtError) else RtError("lost", type(e).__name__))

    async def _deliver(self, seq: int, req: _Req, item: tuple) -> None:
        try:
            await asyncio.wait_for(req.q.put(item), self.cfg.stall_s)    # backpressure: the reader waits for a slow consumer, up to stall_s
        except asyncio.TimeoutError:
            self._pending.pop(seq, None)
            self._push_failure(req, RtError("stalled"))
            asyncio.ensure_future(self._cancel_remote(seq))               # the server must stop producing for a consumer that is not there

    async def _hb_loop(self, ws: WsLike) -> None:
        n = 0
        try:
            while True:
                await self._sleep(self.cfg.hb_interval_s)
                if self.clock() - self._last_pong > self.cfg.hb_timeout_s:
                    raise RtError("lost", "heartbeat")
                n += 1
                await ws.send(P.dumps({"type": "ping", "n": n}))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._fail_all(e if isinstance(e, RtError) else RtError("lost", type(e).__name__))
            await _quiet_close(ws)

    @staticmethod
    def _push_failure(req: _Req, err: RtError) -> None:
        while req.q.full():
            try:
                req.q.get_nowait()
            except asyncio.QueueEmpty:
                break
        req.q.put_nowait(("lost", err))

    def _fail_all(self, err: RtError) -> None:
        if self._dead:
            return
        self._dead = True
        self.counters["lost"] += 1
        for req in list(self._pending.values()):
            self._push_failure(req, err)
        for ev in self._cancelling.values():
            ev.set()
        me = asyncio.current_task()
        for t in (self._reader, self._hb):                                # stop the loops of the dead connection (never the one that is calling)
            if t is not None and t is not me and not t.done():
                t.cancel()

    # ------------------------------------------------------------------ requests
    async def request(self, msg: dict, frames: Iterable[bytes] = (), trailer: dict | None = None) -> AsyncIterator[tuple[str, Any]]:
        """Send `msg` (+ binary frames + a trailer message) and yield (kind, payload) until a terminal answer. Raises RtError. Leaving the iterator early cancels the
        request on the server (see `_cancel_remote`)."""
        await self.ensure_ready()
        self._seq += 1
        seq = self._seq
        req = _Req(self.cfg.queue_max)
        self._pending[seq] = req
        done = False
        try:
            ws = self._ws
            if ws is None or self._dead:
                done = True
                raise RtError("lost", "not_ready")
            await ws.send(P.dumps({**msg, "seq": seq}))
            for fr in frames:
                await ws.send(P.pack_frame(seq, fr))
            if trailer is not None:
                await ws.send(P.dumps({**trailer, "seq": seq}))
            deadline = self.clock() + self.cfg.request_timeout_s
            while True:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise RtError("timeout")
                try:
                    kind, payload = await asyncio.wait_for(req.q.get(), remaining)
                except asyncio.TimeoutError:
                    raise RtError("timeout") from None
                if kind == "lost":
                    done = True                                           # the connection is gone: nothing to cancel
                    raise payload
                if kind == "error":
                    done = True
                    raise RtError("server_error", str(payload.get("code", "")))
                if kind in P.TERMINAL_TYPES:
                    done = True
                yield kind, payload
                if done:
                    return
        except (ConnectionError, OSError) as e:
            done = True
            self._fail_all(RtError("lost", type(e).__name__))
            raise RtError("lost", type(e).__name__) from None
        finally:
            self._pending.pop(seq, None)
            if not done and not self._dead:
                await asyncio.shield(self._cancel_remote(seq))

    async def _cancel_remote(self, seq: int) -> None:
        """Tell the server to stop; if it does not acknowledge within `cancel_grace_s`, CLOSE the socket (that is what really stops the GPU work)."""
        self.counters["cancels"] += 1
        ev = self._cancelling[seq] = asyncio.Event()
        try:
            await self._ws.send(P.dumps({"type": "cancel", "seq": seq}))
            await asyncio.wait_for(ev.wait(), self.cfg.cancel_grace_s)
        except Exception:
            self.counters["cancel_aborts"] += 1
            await self.abort("cancel_unacknowledged")
        finally:
            self._cancelling.pop(seq, None)

    async def abort(self, reason: str = "") -> None:
        """Close the socket now; the next request reconnects."""
        ws = self._ws
        self._fail_all(RtError("lost", reason or "aborted"))
        if ws is not None:
            await _quiet_close(ws)
        self._ws = None

    async def close(self) -> None:
        self._closed = True
        for t in (self._reader, self._hb):
            if t is not None and not t.done():
                t.cancel()
        ws, self._ws, self._dead = self._ws, None, True
        if ws is not None:
            await _quiet_close(ws)


async def _quiet_close(ws: WsLike) -> None:
    try:
        await ws.close()
    except Exception:
        pass
