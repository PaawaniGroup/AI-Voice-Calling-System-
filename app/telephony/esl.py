"""Asynchronous FreeSWITCH Event Socket (inbound) client.

ESL is a control/event channel only - it does not carry call audio.

Two independent connections are used so a slow/blocked command can never starve events:
  * EslEventClient   - authenticates, subscribes once per connection, dispatches events.
  * EslCommandClient - serialises api/bgapi commands with a per-command timeout.
Both reconnect with bounded exponential backoff + jitter. On a command timeout the
connection is torn down (reply ordering is unknowable afterwards) and re-established.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote

from app.core.errors import Unavailable

log = logging.getLogger("mme_telephony.esl")

MAX_HEADER_LINE = 16 * 1024
MAX_BODY = 4 * 1024 * 1024

SUBSCRIBED_EVENTS = (
    "CHANNEL_CREATE CHANNEL_PROGRESS CHANNEL_PROGRESS_MEDIA CHANNEL_ANSWER CHANNEL_BRIDGE CHANNEL_UNBRIDGE "
    "CHANNEL_HANGUP CHANNEL_HANGUP_COMPLETE CHANNEL_DESTROY CHANNEL_PARK BACKGROUND_JOB RECORD_START RECORD_STOP HEARTBEAT CUSTOM mme::transfer_failed mod_audio_stream::play mod_audio_stream::connect mod_audio_stream::disconnect mod_audio_stream::error"
)


class EslError(Exception):
    pass


class EslProtocolError(EslError):
    pass


class EslCommandError(EslError):
    pass


class EslTimeout(EslError):
    pass


async def read_message(reader: asyncio.StreamReader) -> tuple[dict[str, str], bytes]:
    """Parse one ESL frame: header block, then Content-Length body if present."""
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            raise EslProtocolError("connection closed")
        if len(line) > MAX_HEADER_LINE:
            raise EslProtocolError("header line too long")
        line = line.rstrip(b"\r\n")
        if not line:
            if headers:
                break
            continue  # tolerate leading blank lines
        key, sep, value = line.decode("utf-8", "replace").partition(":")
        if not sep:
            raise EslProtocolError("malformed header")
        headers[key.strip()] = value.strip()
    body = b""
    n = headers.get("Content-Length")
    if n is not None:
        try:
            length = int(n)
        except ValueError:
            raise EslProtocolError("bad content-length") from None
        if length < 0 or length > MAX_BODY:
            raise EslProtocolError("content-length out of range")
        body = await reader.readexactly(length)
    return headers, body


def parse_plain_event(body: bytes) -> dict[str, str]:
    """text/event-plain body: URL-encoded 'Key: value' lines (an optional trailing body is ignored)."""
    ev: dict[str, str] = {}
    text = body.decode("utf-8", "replace")
    head, _, tail = text.partition("\n\n")
    for line in head.split("\n"):
        k, sep, v = line.partition(": ")
        if sep:
            ev[k.strip()] = unquote(v)
    if tail.strip():
        ev["_body"] = tail.strip()  # e.g. BACKGROUND_JOB result ("+OK <uuid>" / "-ERR CAUSE")
    return ev


def backoff_delay(attempt: int, initial: float, maximum: float, jitter: float) -> float:
    base = min(maximum, initial * (2 ** max(0, attempt)))
    return max(0.0, base * (1 + random.uniform(-jitter, jitter)))


class _Base:
    def __init__(self, host: str, port: int, password: str, *, connect_timeout: float, reconnect_initial: float,
                 reconnect_max: float, jitter: float, name: str):
        self.host, self.port, self._password = host, port, password
        self.connect_timeout, self.r_init, self.r_max, self.jitter = connect_timeout, reconnect_initial, reconnect_max, jitter
        self.name = name
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.connected = False
        self.reconnect_count = 0
        self.last_error: str | None = None
        self.last_connected_at: float | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _open(self) -> None:
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.connect_timeout)
        hdr, _ = await asyncio.wait_for(read_message(self.reader), self.connect_timeout)
        if hdr.get("Content-Type") != "auth/request":
            raise EslProtocolError("unexpected greeting")
        self.writer.write(f"auth {self._password}\n\n".encode())
        await self.writer.drain()
        hdr, _ = await asyncio.wait_for(read_message(self.reader), self.connect_timeout)
        if not hdr.get("Reply-Text", "").startswith("+OK"):
            raise EslError("ESL authentication failed")  # never echo the password

    async def _close(self) -> None:
        self.connected = False
        w, self.writer, self.reader = self.writer, None, None
        if w is not None:
            try:
                w.close()
                await asyncio.wait_for(w.wait_closed(), 2)
            except Exception:
                pass

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._close()


class EslCommandClient(_Base):
    """Serialised command channel. `connect_lazy` semantics: connects on demand, reconnects after failure."""

    def __init__(self, *a: Any, command_timeout: float = 10.0, **kw: Any):
        super().__init__(*a, name="esl-cmd", **kw)
        self.command_timeout = command_timeout
        self._lock = asyncio.Lock()
        self.command_failures = 0
        self._attempt = 0
        self._next_try = 0.0

    async def start(self) -> None:
        try:
            await self._ensure()
        except Exception as e:  # startup must not fail hard: readiness reports it
            self.last_error = type(e).__name__
            log.warning("esl command channel unavailable at startup: %s", type(e).__name__)

    async def _ensure(self) -> None:
        if self.connected:
            return
        now = time.monotonic()
        if now < self._next_try:
            raise Unavailable("FreeSWITCH control connection unavailable", code="freeswitch_unavailable")
        try:
            await self._open()
            self.connected = True
            self.last_connected_at = time.time()
            if self._attempt:
                self.reconnect_count += 1
            self._attempt = 0
        except Exception as e:
            await self._close()
            self.last_error = type(e).__name__
            self._next_try = now + backoff_delay(self._attempt, self.r_init, self.r_max, self.jitter)
            self._attempt += 1
            raise Unavailable("FreeSWITCH control connection unavailable", code="freeswitch_unavailable") from e

    async def _roundtrip(self, line: str) -> tuple[dict[str, str], bytes]:
        assert self.writer and self.reader
        self.writer.write(f"{line}\n\n".encode())
        await self.writer.drain()
        while True:
            hdr, body = await read_message(self.reader)
            ct = hdr.get("Content-Type", "")
            if ct in ("command/reply", "api/response"):
                return hdr, body
            if ct == "text/disconnect-notice":
                raise EslProtocolError("disconnect notice")
            # ignore anything else (no events are subscribed on this connection)

    async def execute(self, line: str, *, timeout: float | None = None, headers: dict[str, str] | None = None) -> str:
        """Send a raw command line and return the reply text. Raises Unavailable/EslTimeout/EslCommandError.

        `line` must be built only from validated components (see dialplan.safe_* helpers).
        """
        for v in (line, *(headers or {}).values()):
            if "\n" in v or "\r" in v:
                raise ValueError("newline in ESL command")
        if headers:
            line = line + "".join(f"\n{k}: {v}" for k, v in headers.items())
        async with self._lock:
            await self._ensure()
            try:
                hdr, body = await asyncio.wait_for(self._roundtrip(line), timeout or self.command_timeout)
            except asyncio.TimeoutError:
                self.command_failures += 1
                await self._close()  # reply ordering is lost; reconnect
                raise EslTimeout(f"command timeout: {line.split(' ', 1)[0]}") from None
            except (EslProtocolError, ConnectionError, OSError, asyncio.IncompleteReadError) as e:
                self.command_failures += 1
                await self._close()
                raise Unavailable("FreeSWITCH control connection lost", code="freeswitch_unavailable") from e
        text = (body.decode("utf-8", "replace") if body else hdr.get("Reply-Text", "")).strip()
        if text.startswith("-ERR"):
            raise EslCommandError(text)
        return text

    async def api(self, cmd: str) -> str:
        return await self.execute(f"api {cmd}")

    async def bgapi(self, cmd: str, job_uuid: str) -> str:
        """Fire a background job; the outcome arrives as a BACKGROUND_JOB event carrying job_uuid."""
        return await self.execute(f"bgapi {cmd}", headers={"Job-UUID": job_uuid})


class EslEventClient(_Base):
    """Maintains the event subscription; calls `handler(event_dict)` for each event on a bounded queue."""

    def __init__(self, *a: Any, handler: Callable[[dict[str, str]], Awaitable[None]],
                 on_connect: Callable[[], Awaitable[None]] | None = None, queue_size: int = 10000,
                 concurrency: int = 8, idle_timeout: float = 60.0, **kw: Any):
        super().__init__(*a, name="esl-events", **kw)
        self.handler, self.on_connect = handler, on_connect
        self.queue: asyncio.Queue[dict[str, str]] = asyncio.Queue(maxsize=queue_size)
        self.concurrency, self.idle_timeout = concurrency, idle_timeout
        self.last_event_at: float | None = None
        self.last_heartbeat_at: float | None = None
        self.events_received = 0
        self.events_dropped = 0
        self.malformed = 0
        self.handler_errors = 0
        self.last_lag_s = 0.0
        self._workers: list[asyncio.Task] = []
        self.subscriptions = 0  # incremented once per (re)connect; tests assert no duplicate subscribe

    async def start(self) -> None:
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self.concurrency)]
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        await super().stop()

    async def _supervise(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._open()
                assert self.writer and self.reader
                self.writer.write(f"event plain {SUBSCRIBED_EVENTS}\n\n".encode())
                await self.writer.drain()
                hdr, _ = await asyncio.wait_for(read_message(self.reader), self.connect_timeout)
                if not hdr.get("Reply-Text", "").startswith("+OK"):
                    raise EslError("event subscription rejected")
                self.subscriptions += 1
                self.connected = True
                self.last_connected_at = time.time()
                self.last_event_at = time.time()
                if attempt:
                    self.reconnect_count += 1
                attempt = 0
                log.info("esl event stream connected")
                if self.on_connect:
                    await self.on_connect()
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = type(e).__name__
                log.warning("esl event stream lost: %s", type(e).__name__)
            finally:
                await self._close()
            if self._stop.is_set():
                break
            delay = backoff_delay(attempt, self.r_init, self.r_max, self.jitter)
            attempt += 1
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except asyncio.TimeoutError:
                pass

    async def _read_loop(self) -> None:
        assert self.reader
        while True:
            # FreeSWITCH emits HEARTBEAT every 20s; silence beyond idle_timeout means a half-open socket.
            hdr, body = await asyncio.wait_for(read_message(self.reader), self.idle_timeout)
            ct = hdr.get("Content-Type", "")
            if ct == "text/disconnect-notice":
                raise EslProtocolError("disconnect notice")
            if ct != "text/event-plain":
                continue
            try:
                ev = parse_plain_event(body)
                if "Event-Name" not in ev:
                    raise ValueError("no Event-Name")
            except Exception:
                self.malformed += 1
                continue
            now = time.time()
            self.last_event_at = now
            if ev["Event-Name"] == "HEARTBEAT":
                self.last_heartbeat_at = now
                continue
            self.events_received += 1
            ev["_recv_ts"] = repr(time.monotonic())
            try:
                self.queue.put_nowait(ev)
            except asyncio.QueueFull:
                self.events_dropped += 1  # surfaced via metrics; reconciliation repairs state

    async def _worker(self) -> None:
        while True:
            ev = await self.queue.get()
            try:
                self.last_lag_s = max(0.0, time.monotonic() - float(ev.pop("_recv_ts", "0") or 0))
                await self.handler(ev)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.handler_errors += 1
                log.exception("esl event handler error")
