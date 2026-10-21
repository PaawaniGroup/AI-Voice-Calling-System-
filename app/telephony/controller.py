"""Call controller: turns ESL/provider events into persisted state transitions and executes call control.

Design rules
  * The call row is persisted BEFORE any external operation (queued == durable outbox).
  * The FreeSWITCH UUID is generated and persisted BEFORE origination, so an uncertain outcome
    can always be reconciled with `uuid_exists`. Origination is never blindly retried.
  * All state changes go through Repos.transition (CAS + dedup events).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta

from app.core.errors import ApiError, Conflict, InvalidTransition, Unavailable
from app.core.ids import aware, new_id, new_uuid, utcnow
from app.core.logging import event as log_event
from app.events.webhooks import WebhookEmitter, event_for_state
from app.observability import metrics as m
from app.persistence.repos import Repos
from app.providers.base import (OriginateRequest, ProviderAdapter, ProviderError, ProviderUncertain)
from app.telephony import dialstrings as ds
from app.telephony import state_machine as sm
from app.telephony.esl import EslCommandClient, EslCommandError, EslError, EslEventClient
from app.telephony.state_machine import S
from config.settings import Settings

log = logging.getLogger("mme_telephony.controller")


class TokenBucket:
    def __init__(self, rate: float, burst: float = 1.0):
        self.rate, self.burst, self.tokens, self.t = rate, max(burst, 1.0), max(burst, 1.0), time.monotonic()

    def try_take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class Controller:
    def __init__(self, s: Settings, repos: Repos, provider: ProviderAdapter, emitter: WebhookEmitter,
                 esl_cmd: EslCommandClient | None = None, esl_events: EslEventClient | None = None):
        self.s, self.repos, self.provider, self.emitter = s, repos, provider, emitter
        self.esl_cmd, self.esl_events = esl_cmd, esl_events
        self._inbound_seen: dict[str, float] = {}  # fs_uuid -> first-seen time (bounded); stops CREATE+PARK double handling
        self.sessions = None  # set by the app factory (SessionService); kept optional so the controller stays unit-testable
        self.ai = None        # set by the app factory (AiService)
        #: extra adapters keyed by the `provider` name stored on a call (e.g. "plivo_api" for inbound Plivo calls while the
        #: global provider is the FreeSWITCH gateway). Calls always go back through the adapter that owns them.
        self.providers: dict[str, ProviderAdapter] = {}
        self.bucket = TokenBucket(min(s.max_initiation_rate, s.provider_max_cps))
        self.max_active = min(s.max_active_global, s.provider_max_concurrent)
        self._tasks: list[asyncio.Task] = []
        self._stop = False
        self.reconciled_at: float | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._guard(f)) for f in (self._dispatch_loop, self._timeout_loop, self._reconcile_loop)]

    async def stop(self) -> None:
        self._stop = True
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _guard(self, fn):
        while not self._stop:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("controller loop %s crashed; restarting", fn.__name__)
                await asyncio.sleep(1)

    # ------------------------------------------------------------------ common
    async def _after_transition(self, outcome: str, call: dict, ev_key: str) -> None:
        m.EVENTS.labels(outcome).inc()
        if outcome != "applied":
            return
        st = call["state"]
        et = event_for_state(st)
        if et:
            await self.emitter.emit_for_call(call, et, f"{call['_id']}:{st}:{call['state_version']}")
        if st in {s.value for s in sm.TERMINAL}:
            await self.emitter.emit_for_call(call, "call.ended", f"{call['_id']}:ended:{call['state_version']}")
        if self.sessions is not None:
            await self.sessions.on_call_state(call)
        if self.ai is not None and st in {s.value for s in sm.TERMINAL}:
            await self.ai.on_call_ended(call)  # cancels the AI session/pipeline; never raises
        if st in {s.value for s in sm.TERMINAL}:
            if call.get("transfer", {}).get("current"):
                await self._finish_transfer(call, "call_ended")  # never leave a dangling attempt on a dead call
            m.CALLS_TERMINAL.labels(st).inc()
            if call.get("failure_reason"):
                m.HANGUP_CAUSES.labels(str(call["failure_reason"])[:40]).inc()
            if call.get("connected_duration_s") is not None:
                m.CONNECTED_DURATION.observe(call["connected_duration_s"])
            if call.get("ring_duration_s") is not None:
                m.RING_DURATION.observe(call["ring_duration_s"])
        if st == S.ANSWERED.value and call.get("initiated_at") and call.get("answered_at"):
            m.SETUP_LATENCY.observe(max(0.0, (aware(call["answered_at"]) - aware(call["initiated_at"])).total_seconds()))
        log_event(log, "call.transition", call_id=call["_id"], state=st, source=ev_key)

    async def apply(self, call: dict, target: S, *, source: str, dedup_id: str, ts=None, fields: dict | None = None,
                    payload: dict | None = None) -> tuple[str, dict]:
        try:
            outcome, c = await self.repos.transition(call, target, source=source, dedup_id=dedup_id, ts=ts,
                                                     fields=fields, payload=payload)
        except InvalidTransition as e:
            m.EVENTS.labels("invalid").inc()
            await self.repos.add_event(call, "invalid_transition", source, f"inv:{dedup_id}", {"error": str(e)})
            return "invalid", call
        await self._after_transition(outcome, c, source)
        return outcome, c

    async def finalize_from_cause(self, call: dict, cause: str, *, source: str, dedup_id: str, ts=None,
                                  forced: tuple[S, str] | None = None, fs_ts=None) -> tuple[str, dict]:
        answered = bool(call.get("answered_at")) or S(call["state"]) in sm.CONNECTED
        timed_out = bool(call.get("timed_out"))
        state, reason = forced or sm.classify_hangup(cause, answered=answered,
                                                      cancel_requested=bool(call.get("cancel_requested")), timed_out=timed_out)
        fields = {"hangup_cause": cause[:60], "failure_reason": reason if state != S.COMPLETED else call.get("failure_reason")}
        if fs_ts is not None:
            fields["fs_ended_ts"] = fs_ts
        if state == S.COMPLETED:
            fields["end_reason"] = reason
        # Pre-connect states cannot move straight to COMPLETED; classify already prevents this.
        return await self.apply(call, state, source=source, dedup_id=dedup_id, ts=ts, fields=fields,
                                payload={"cause": cause, "reason": reason})

    # ------------------------------------------------------------------ ESL events
    async def on_esl_event(self, ev: dict[str, str]) -> None:
        name = ev.get("Event-Name", "")
        if name == "BACKGROUND_JOB":
            return await self._on_bgjob(ev)
        uuid = ev.get("Unique-ID") or ""
        call_id = ev.get("variable_mme_call_id")
        call = await self.repos.find_call_by_fs_uuid(uuid) if uuid else None
        if call is None and call_id and ev.get("variable_mme_leg") != "a":
            call = await self.repos.get_call(call_id)
        if call is None:
            if name in ("CHANNEL_CREATE", "CHANNEL_PARK") and ev.get("Call-Direction") == "inbound" \
                    and ev.get("Caller-Context") == self.s.fs_inbound_context:
                await self._on_inbound(ev)
            elif name in ("CHANNEL_CREATE", "CHANNEL_PARK") and ev.get("Call-Direction") == "inbound" \
                    and ev.get("Caller-Context") in self.s.csv(self.s.observe_contexts):
                await self._on_observed_inbound(ev)
            return
        key = f"{name}:{uuid}:{ev.get('Event-Sequence', ev.get('Event-Date-Timestamp', ''))}"
        # Clock domains: FreeSWITCH's Event-Date-Timestamp can be hours/days off this host's wall clock (observed
        # 2.5 days on the WSL dev host). All stored timestamps and every sweeper comparison use THIS process's clock;
        # FreeSWITCH timestamps are used only as a delta between two FreeSWITCH events (same domain).
        fs_ts = _event_ts(ev)
        ts = utcnow()
        if uuid != call.get("fs_uuid"):
            return await self._on_other_leg(call, ev, key, ts)
        if name == "CUSTOM" and (ev.get("Event-Subclass") or "").startswith("mod_audio_stream::"):
            return await self._on_media_event(call, ev, key)
        if name == "CUSTOM" and ev.get("Event-Subclass") == "mme::transfer_failed":
            if call.get("transfer", {}).get("current"):
                await self.repos.add_event(call, "transfer.failed", "esl", key, {"cause": ev.get("Cause", "")[:40]})
                await self._finish_transfer(call, "rejected", revert=True)
            return
        if name in ("CHANNEL_PROGRESS", "CHANNEL_PROGRESS_MEDIA"):
            await self.apply(call, S.RINGING, source="esl", dedup_id=key, ts=ts)
        elif name == "CHANNEL_ANSWER":
            await self.apply(call, S.ANSWERED, source="esl", dedup_id=key, ts=ts, fields={"fs_answered_ts": fs_ts})
            if call.get("direction") == "outbound":
                await self._maybe_start_recording(call["_id"])
        elif name == "CHANNEL_BRIDGE":
            other = ev.get("Other-Leg-Unique-ID")
            if other:
                await self.repos.update_call(call["_id"], {}, add_to_set={"fs_uuids": other})
            fresh = await self.repos.get_call(call["_id"]) or call
            # An unanswered A-leg can bridge only after answer; ensure ordering by applying ANSWERED first.
            if fresh["state"] in (S.INITIATING.value, S.RINGING.value):
                _, fresh = await self.apply(fresh, S.ANSWERED, source="esl", dedup_id=f"implied:{key}", ts=ts)
            outcome, c = await self.apply(fresh, S.BRIDGED, source="esl", dedup_id=key, ts=ts)
            if call.get("transfer", {}).get("current"):
                await self._finish_transfer(c, "completed")
        elif name in ("CHANNEL_HANGUP", "CHANNEL_HANGUP_COMPLETE"):
            await self.finalize_from_cause(call, ev.get("Hangup-Cause", "UNKNOWN"), source="esl", dedup_id=key, ts=ts, fs_ts=fs_ts)

    async def _on_media_event(self, call: dict, ev: dict, key: str) -> None:
        """Events from FreeSWITCH mod_audio_stream. `play` = a synthesized reply was written to a temp file: play it."""
        import re as _re

        sub = ev["Event-Subclass"].split("::", 1)[1]
        await self.repos.add_event(call, f"media.{sub}", "esl", key, {"detail": (ev.get("_body") or "")[:200] if sub != "play" else {}})
        if sub != "play":
            return
        if self.ai is None or not call.get("session_id") or call.get("session_id") not in self.ai.runtime or self.esl_cmd is None:
            return  # no live AI session for this call: never play files for unknown sessions
        try:
            f = json.loads(ev.get("_body") or "{}").get("file", "")
        except ValueError:
            return
        # only the file this module just wrote for THIS channel: <tmpdir>/<channel uuid>_<n>.tmp.<ext>
        if not _re.fullmatch(r"/[A-Za-z0-9_./-]{1,200}/" + _re.escape(call["fs_uuid"]) + r"_\d{1,6}\.tmp\.(wav|r8|r16|r24|r32|r48|mp3|ogg)", f) or ".." in f:
            log.warning("refused unexpected playback path")
            return
        rt = self.ai.runtime.get(call["session_id"])
        if rt is not None and rt.sent_at is not None:  # module handoff -> event back in the controller
            await self.ai.record_turn(rt, {"module_return_ms": round((time.monotonic() - rt.sent_at) * 1000, 1)}, count=False)
        t = time.perf_counter()
        await self.esl_cmd.api(f"uuid_broadcast {ds.safe_uuid(call['fs_uuid'])} {f} aleg")
        if rt is not None:
            await self.ai.record_turn(rt, {"broadcast_cmd_ms": round((time.perf_counter() - t) * 1000, 1)}, count=False)

    async def _on_other_leg(self, call: dict, ev: dict, key: str, ts) -> None:
        # B-leg events: only its hangup before A-leg hangup is recorded (A-leg hangup drives the final state).
        if ev.get("Event-Name") == "CHANNEL_HANGUP":
            await self.repos.add_event(call, "leg_b.hangup", "esl", key, {"cause": ev.get("Hangup-Cause")}, ts=ts)

    async def _on_bgjob(self, ev: dict) -> None:
        job = ev.get("Job-UUID", "")
        call = await self.repos.db["calls"].find_one({"job_uuid": job})
        if not call:
            return
        text = ev.get("_body", "")
        if text.startswith("-ERR"):
            cause = text[4:].strip().split()[0] if text[4:].strip() else "UNKNOWN"
            m.ORIGINATIONS.labels("rejected").inc()
            await self.finalize_from_cause(call, cause, source="esl_job", dedup_id=f"job:{job}")
        elif text.startswith("+OK"):
            m.ORIGINATIONS.labels("ok").inc()

    # ------------------------------------------------------------------ inbound (SIP)
    def _first_time(self, uuid: str) -> bool:
        """Synchronous check-and-set (no await between check and set), so concurrent handlers cannot both pass."""
        if uuid in self._inbound_seen:
            return False
        if len(self._inbound_seen) > 5000:
            for k in sorted(self._inbound_seen, key=self._inbound_seen.get)[:2500]:
                self._inbound_seen.pop(k, None)
        self._inbound_seen[uuid] = time.monotonic()
        return True

    async def _on_inbound(self, ev: dict) -> None:
        if not self._first_time(ev["Unique-ID"]):
            return
        uuid, dest = ev["Unique-ID"], ev.get("Caller-Destination-Number", "")
        from app.core import phone

        try:
            to = phone.normalize(dest if dest.startswith("+") else "+" + dest.lstrip("+"))
        except ApiError:
            return await self._reject_inbound(uuid, "UNALLOCATED_NUMBER")
        route = await self.repos.get_route(to)
        if not route:
            return await self._reject_inbound(uuid, "UNALLOCATED_NUMBER")
        tenant = route["tenant_id"]
        pol = await self.repos.get_policy(tenant) or {}
        if pol.get("enabled", True) is False:
            return await self._reject_inbound(uuid, "CALL_REJECTED")
        if await self.repos.count_active() >= self.max_active or \
                await self.repos.count_active(tenant) >= pol.get("max_active_calls", self.s.max_active_per_tenant):
            return await self._reject_inbound(uuid, "NORMAL_CIRCUIT_CONGESTION")
        cid = ev.get("Caller-Caller-ID-Number", "")
        call = {
            "_id": new_id("call"), "tenant_id": tenant, "direction": "inbound", "provider": self.provider.name,
            "fs_uuid": uuid, "from_number": cid[:20], "to_number": to, "state": S.INITIATING.value,
            "initiated_at": utcnow(), "correlation_id": new_id("cor"), "route_action": route.get("action", "park"),
            "max_duration_s": min(pol.get("max_duration_s", self.s.max_call_duration_s), self.s.max_call_duration_s),
            "recording_policy": pol.get("recording", "off"),
        }
        try:
            await self.repos.insert_call(call)
        except Exception:
            log.exception("inbound insert failed")
            return await self._reject_inbound(uuid, "TEMPORARY_FAILURE")
        m.CALLS_CREATED.labels("inbound").inc()
        _, c = await self.apply(call, S.RINGING, source="inbound", dedup_id=f"in:{uuid}:ring")
        assert self.esl_cmd
        try:
            action = route.get("action", "park")
            await self.esl_cmd.api(f"uuid_answer {ds.safe_uuid(uuid)}")
            if action == "bridge" and route.get("target"):
                dial = ds.render(self.s.fs_transfer_template, self.s.fs_gateway, route["target"])
                await self.esl_cmd.api(ds.setvar_dial_command(uuid, dial))
                await self.esl_cmd.api(ds.transfer_command(uuid))
            elif action == "hangup":
                await self.esl_cmd.api(ds.kill_command(uuid, "NORMAL_CLEARING"))
        except (EslError, Unavailable, ValueError):
            await self.finalize_from_cause(c, "NORMAL_TEMPORARY_FAILURE", source="inbound", dedup_id=f"in:{uuid}:err")

    async def _on_observed_inbound(self, ev: dict) -> None:
        """RECORD-ONLY inbound handling for contexts the operator already routes elsewhere (e.g. `public`).

        Creates the call + session for a number that has an inbound route, then lets normal events (answer,
        hangup) drive state. Sends NO command to FreeSWITCH: nothing is answered, rejected, bridged or killed.
        """
        from app.core import phone

        if not self._first_time(ev["Unique-ID"]):
            return
        uuid, dest = ev["Unique-ID"], ev.get("Caller-Destination-Number", "")
        try:
            to = phone.normalize(dest if dest.startswith("+") else "+" + dest.lstrip("+"))
        except ApiError:
            return
        route = await self.repos.get_route(to)
        if not route:
            return
        call = {
            "_id": new_id("call"), "tenant_id": route["tenant_id"], "direction": "inbound", "provider": self.provider.name,
            "fs_uuid": uuid, "from_number": ev.get("Caller-Caller-ID-Number", "")[:20], "to_number": to,
            "state": S.INITIATING.value, "initiated_at": utcnow(), "correlation_id": new_id("cor"), "observed": True,
            "max_duration_s": self.s.max_call_duration_s, "ai_enabled": False, "language": "auto",
        }
        try:
            await self.repos.insert_call(call)
        except Exception:
            log.exception("observed inbound insert failed")
            return
        m.CALLS_CREATED.labels("inbound").inc()
        await self.apply(call, S.RINGING, source="observed", dedup_id=f"obs:{uuid}:ring")

    async def _reject_inbound(self, uuid: str, cause: str) -> None:
        log_event(log, "inbound.rejected", cause=cause)
        try:
            assert self.esl_cmd
            await self.esl_cmd.api(ds.kill_command(uuid, cause))
        except Exception:
            log.warning("could not reject inbound channel")

    # ------------------------------------------------------------------ dispatch (queued -> initiating -> provider)
    async def _dispatch_loop(self) -> None:
        while not self._stop:
            did = await self.dispatch_once()
            await asyncio.sleep(0 if did else self.s.dispatch_poll_s)

    async def dispatch_once(self) -> bool:
        active = await self.repos.count_active()
        queued = await self.repos.count_queued()
        m.ACTIVE_CALLS.set(active)
        m.QUEUED_CALLS.set(queued)
        if queued == 0 or active >= self.max_active:
            return False
        if self.s.fs_enabled and self.provider.name == "freeswitch_gateway" and not (
                self.esl_events and self.esl_events.connected):
            return False  # hold queue while FreeSWITCH is down; calls stay durable
        if not self.bucket.try_take():
            return False
        per_tenant: dict[str, int] = {}
        pipeline = [{"$match": {"state": {"$in": [x.value for x in sm.ACTIVE]}}},
                    {"$group": {"_id": "$tenant_id", "n": {"$sum": 1}}}]
        async for row in await self.repos.db["calls"].aggregate(pipeline):
            per_tenant[row["_id"]] = row["n"]
        limits: dict[str, int] = {}
        async for p in self.repos.db["tenant_policies"].find({}, {"tenant_id": 1, "max_active_calls": 1}):
            if "max_active_calls" in p:
                limits[p["tenant_id"]] = p["max_active_calls"]
        call = await self.repos.claim_next_queued(per_tenant, limits, self.s.max_active_per_tenant)
        if not call:
            self.bucket.tokens += 1  # nothing consumed
            return False
        await self.emitter.emit_for_call(call, "call.initiated", f"{call['_id']}:initiating:{call['state_version']}")
        await self._originate(call)
        return True

    async def _originate(self, call: dict) -> None:
        fs_uuid, job = new_uuid(), new_uuid()
        # Persist identifiers BEFORE the external call so uncertain outcomes can be reconciled.
        await self.repos.update_call(call["_id"], {"fs_uuid": fs_uuid, "job_uuid": job, "originate_started_at": utcnow()})
        if self.sessions is not None:
            await self.sessions.attach_fs_uuid(call, fs_uuid)
        fresh = await self.repos.get_call(call["_id"])
        if fresh is None or S(fresh["state"]) in sm.TERMINAL:
            return  # cancelled while being claimed: do not originate
        req = OriginateRequest(
            call_id=call["_id"], tenant_id=call["tenant_id"], fs_uuid=fs_uuid, job_uuid=job,
            from_number=call["from_number"], to_number=call["to_number"], bridge_to=call.get("bridge_to"),
            ring_timeout=self.s.ring_timeout_s, max_duration=call.get("max_duration_s", self.s.max_call_duration_s))
        try:
            res = await self.provider.originate(req)
        except ProviderError as e:
            m.ORIGINATIONS.labels("rejected").inc()
            m.PROVIDER_ERRORS.labels("rejected").inc()
            await self.apply(call, S.FAILED, source="provider", dedup_id=f"orig-fail:{call['_id']}",
                             fields={"failure_reason": e.reason[:80]})
            return
        except ProviderUncertain as e:
            m.ORIGINATIONS.labels("uncertain").inc()
            m.PROVIDER_ERRORS.labels("uncertain").inc()
            await self.repos.update_call(call["_id"], {"origination_uncertain": True, "uncertain_reason": str(e)[:60]})
            await self.repos.add_event(call, "origination.uncertain", "controller", f"unc:{call['_id']}", {"reason": str(e)[:60]})
            return  # left in `initiating`; reconcile resolves it (never re-originated)
        if res.provider_call_id:
            await self.repos.update_call(call["_id"], {"provider_call_id": res.provider_call_id})
        m.ORIGINATIONS.labels("accepted").inc()
        await self.repos.add_event(call, "origination.accepted", "controller", f"acc:{call['_id']}", {})

    # ------------------------------------------------------------------ control operations
    def provider_for(self, call: dict) -> ProviderAdapter:
        """The adapter that owns this call (falls back to the global provider)."""
        return self.providers.get(call.get("provider") or "", self.provider)

    async def hangup(self, call: dict, *, source: str = "api") -> dict:
        st = S(call["state"])
        if st in sm.TERMINAL:
            return call  # idempotent
        if st == S.QUEUED:
            _, c = await self.apply(call, S.CANCELLED, source=source, dedup_id=f"cancel:{call['_id']}",
                                    fields={"cancel_requested": True, "failure_reason": "cancelled"})
            return c
        await self.repos.update_call(call["_id"], {"cancel_requested": st in sm.PRE_CONNECT, "hangup_requested_at": utcnow()})
        if not call.get("fs_uuid") and not call.get("provider_call_id"):
            # Claimed but nothing dispatched yet: _originate re-checks state and will skip.
            _, c = await self.apply(call, S.CANCELLED, source=source, dedup_id=f"cancel:{call['_id']}",
                                    fields={"failure_reason": "cancelled"})
            return c
        cause = "ORIGINATOR_CANCEL" if st in sm.PRE_CONNECT else "NORMAL_CLEARING"
        try:
            await self.provider_for(call).hangup(call, cause)
        except ProviderError as e:
            raise ApiError("hangup could not be executed", code="hangup_failed", status=502,
                           details={"reason": e.reason}) from e
        except (EslError, Unavailable) as e:
            raise Unavailable("FreeSWITCH unavailable; hangup not confirmed", code="freeswitch_unavailable") from e
        # Final state comes from the authoritative hangup event (or reconciliation), not from our request.
        await self.repos.add_event(call, "hangup.requested", source, f"hr:{call['_id']}:{call['state_version']}", {"cause": cause})
        return await self.repos.get_call(call["_id"]) or call

    async def transfer(self, call: dict, destination: str) -> dict:
        st = S(call["state"])
        if st not in (S.ANSWERED, S.BRIDGED):
            raise Conflict(f"call not transferable in state {st.value}", code="not_transferable")
        attempt = {"destination_masked": destination[:3] + "***" + destination[-2:], "started_at": utcnow(), "outcome": None}
        _, c = await self.apply(call, S.TRANSFERRING, source="api", dedup_id=f"xfer:{call['_id']}:{call['state_version']}",
                                fields={"transfer.current": attempt, "transfer.pending_to": destination,
                                    "transfer_started_at": utcnow()})
        if c["state"] != S.TRANSFERRING.value:
            raise Conflict("call state changed", code="state_changed")
        try:
            await self.provider_for(call).transfer(call, destination)
        except (ProviderError, EslCommandError, EslError, Unavailable, ValueError) as e:
            await self._finish_transfer(c, "rejected", revert=True)
            raise ApiError("transfer rejected", code="transfer_failed", status=502, details={"reason": type(e).__name__}) from e
        return await self.repos.get_call(call["_id"]) or c

    async def _finish_transfer(self, call: dict, outcome: str, revert: bool = False) -> None:
        cur = call.get("transfer", {}).get("current") or {}
        done = {**cur, "outcome": outcome, "ended_at": utcnow()}
        await self.repos.db["calls"].update_one(
            {"_id": call["_id"]}, {"$push": {"transfer.attempts": done}, "$unset": {"transfer.current": "", "transfer.pending_to": ""}})
        if revert:
            fresh = await self.repos.get_call(call["_id"]) or call
            await self.apply(fresh, S.ANSWERED, source="controller", dedup_id=f"xfer-revert:{call['_id']}:{fresh['state_version']}")

    # ------------------------------------------------------------------ timeouts
    async def _timeout_loop(self) -> None:
        while not self._stop:
            await asyncio.sleep(2)
            await self.sweep_timeouts()

    async def sweep_timeouts(self) -> None:
        now = utcnow()
        db = self.repos.db["calls"]
        # queue wait
        async for c in db.find({"state": "queued", "queued_at": {"$lt": now - timedelta(seconds=self.s.queue_max_wait_s)}}).limit(100):
            await self.apply(c, S.FAILED, source="timeout", dedup_id=f"qto:{c['_id']}", fields={"failure_reason": "queue_timeout"})
        # initiating stuck
        async for c in db.find({"state": "initiating", "initiated_at": {"$lt": now - timedelta(seconds=self.s.initiating_timeout_s)}}).limit(100):
            if c.get("origination_uncertain") or not c.get("fs_uuid"):
                continue  # reconcile owns uncertain outcomes
            await self._kill_quietly(c, "PROGRESS_TIMEOUT")
        # Plivo inbound: an Answer that was received but never completed (process died between insert and answer)
        async for c in db.find({"provider": "plivo_api", "direction": "inbound", "state": "initiating",
                                "initiated_at": {"$lt": now - timedelta(seconds=self.s.initiating_timeout_s)}}).limit(50):
            await self.apply(c, S.FAILED, source="timeout", dedup_id=f"ans-incomplete:{c['_id']}",
                             fields={"failure_reason": "answer_not_completed"})
        # Plivo media stream promised in the Answer XML never connected: hang the call up instead of leaving silence on the line
        async for c in db.find({"provider": "plivo_api", "state": "answered", "stream_expected_by": {"$lt": now},
                                "stream_connected": {"$ne": True}, "stream_watch_done": {"$ne": True}}).limit(50):
            claimed = await db.find_one_and_update({"_id": c["_id"], "stream_watch_done": {"$ne": True}},
                                                   {"$set": {"stream_watch_done": True}})  # atomic: concurrent sweeps act once
            if not claimed:
                continue
            await self.repos.add_event(c, "stream.not_connected", "timeout", f"stream-nc:{c['_id']}", {})
            await self._kill_quietly(c, "NORMAL_CLEARING")
        # max duration (backup to sched_hangup)
        async for c in db.find({"state": {"$in": ["answered", "bridged", "transferring"]}}).limit(500):
            lim = c.get("max_duration_s", self.s.max_call_duration_s)
            if c.get("answered_at") and (now - aware(c["answered_at"])).total_seconds() > lim + 10:
                if c.get("provider") == "plivo_api":
                    # REST hangups are throttled, and an unconfirmed one must not leave the call and its AI session open forever
                    over = (now - aware(c["answered_at"])).total_seconds() - lim
                    if over > self.s.plivo_orphan_grace_s:
                        await self.apply(c, S.COMPLETED, source="timeout", dedup_id=f"orphan:{c['_id']}",
                                         fields={"hangup_cause": "TIMEOUT_UNCONFIRMED", "end_reason": "max_duration_unconfirmed", "timed_out": True})
                        continue
                    last = aware(c.get("timeout_hangup_at"))
                    if last and (now - last).total_seconds() < 20:
                        continue
                    await self.repos.update_call(c["_id"], {"timed_out": True, "timeout_hangup_at": now})
                    await self._kill_quietly(c, "ALLOTTED_TIMEOUT")
                    continue
                await self.repos.update_call(c["_id"], {"timed_out": True})
                await self._kill_quietly(c, "ALLOTTED_TIMEOUT")
        # transfer timeout
        async for c in db.find({"state": "transferring", "transfer_started_at": {"$lt": now - timedelta(seconds=self.s.transfer_timeout_s)}}).limit(50):
            if c.get("transfer", {}).get("current"):
                await self._finish_transfer(c, "timeout", revert=True)

    async def _kill_quietly(self, c: dict, cause: str) -> bool:
        try:
            await self.provider_for(c).hangup(c, cause)
            return True
        except Exception:
            log.warning("timeout hangup failed for %s", c["_id"])
            return False

    # ------------------------------------------------------------------ reconciliation
    async def _reconcile_loop(self) -> None:
        while not self._stop:
            await asyncio.sleep(self.s.reconcile_interval_s)
            try:
                await self.reconcile()
            except Unavailable:
                pass

    async def live_channels(self) -> dict[str, dict]:
        assert self.esl_cmd
        raw = await self.esl_cmd.api("show channels as json")
        if not raw or raw.startswith("0 total"):
            return {}
        try:
            rows = json.loads(raw).get("rows", [])
        except json.JSONDecodeError:
            raise Unavailable("unparseable channel list", code="freeswitch_unavailable") from None
        return {r["uuid"]: r for r in rows}

    async def reconcile(self) -> dict[str, int]:
        """Align persisted non-terminal calls with FreeSWITCH reality.

        Rules: a call is finalized only when FreeSWITCH positively reports no such channel AND the call is
        older than the grace period. If FreeSWITCH cannot be queried, nothing is changed.
        """
        if self.provider.name != "freeswitch_gateway" or not self.esl_cmd:
            return {}
        live = await self.live_channels()
        stats = {"finalized": 0, "orphan_channels": 0, "uncertain_resolved": 0}
        now = utcnow()
        q = {"state": {"$in": [x.value for x in sm.ACTIVE]}, "fs_uuid": {"$exists": True}}
        async for c in self.repos.db["calls"].find(q).limit(self.s.reconcile_batch):
            ref = aware(c.get("originate_started_at") or c.get("initiated_at") or c["created_at"])
            # A call that has not shown a channel yet may still be originating/ringing; never finalize it before the
            # ring timeout (+margin) has certainly elapsed, regardless of how small reconcile_grace_s is.
            grace = self.s.reconcile_grace_s
            if c["state"] == S.INITIATING.value or c.get("origination_uncertain"):
                grace = max(grace, self.s.ring_timeout_s + 10)
            if (now - ref).total_seconds() < grace:
                continue
            if c["fs_uuid"] in live:
                ch = live[c["fs_uuid"]]
                # Catch up if events were missed while disconnected.
                cs = (ch.get("callstate") or "").upper()
                if cs == "ACTIVE" and c["state"] in ("initiating", "ringing"):
                    await self.apply(c, S.ANSWERED, source="reconcile", dedup_id=f"rec-ans:{c['_id']}")
                elif cs in ("EARLY", "RINGING") and c["state"] == "initiating":
                    await self.apply(c, S.RINGING, source="reconcile", dedup_id=f"rec-ring:{c['_id']}")
                continue
            # Channel absent.
            if c.get("origination_uncertain"):
                stats["uncertain_resolved"] += 1
            reason = "reconciled_channel_missing"
            was_answered = bool(c.get("answered_at"))
            state, why = (S.COMPLETED, reason) if was_answered else (S.FAILED, "lost_before_answer")
            if c.get("origination_uncertain") and not was_answered:
                why = "origination_uncertain_no_channel"
            _, _ = await self.apply(c, state, source="reconcile", dedup_id=f"rec-end:{c['_id']}",
                                    fields={"hangup_cause": "RECONCILED", "failure_reason": why if state == S.FAILED else None,
                                            "end_reason": why})
            stats["finalized"] += 1
            m.RECONCILE_FIXES.labels("finalized").inc()
        # Channels still alive for calls we already recorded as terminal are orphans: hang them up.
        if live:
            async for c in self.repos.db["calls"].find(
                    {"fs_uuid": {"$in": list(live)}, "state": {"$in": [x.value for x in sm.TERMINAL]}}, {"fs_uuid": 1}):
                try:
                    await self.esl_cmd.api(ds.kill_command(c["fs_uuid"], "NORMAL_CLEARING"))
                    stats["orphans_killed"] = stats.get("orphans_killed", 0) + 1
                    m.RECONCILE_FIXES.labels("orphan_killed").inc()
                except Exception:
                    log.warning("orphan hangup failed")
        known = {c["fs_uuid"] async for c in self.repos.db["calls"].find(
            {"state": {"$in": [x.value for x in sm.ACTIVE]}}, {"fs_uuid": 1}) if c.get("fs_uuid")}
        for uuid, ch in live.items():
            # Orphan = channel carrying our marker header-less show output cannot be attributed; count unknown ones
            # whose context is ours only (avoid touching unrelated calls on a shared FreeSWITCH).
            if ch.get("context") == self.s.fs_inbound_context and uuid not in known:
                stats["orphan_channels"] += 1
                m.RECONCILE_FIXES.labels("orphan_channel").inc()
                log.error("orphan FreeSWITCH channel in managed context: alerting (not killed)")
        self.reconciled_at = time.time()
        return stats

    async def on_esl_connected(self) -> None:
        """Run after every (re)connect: events may have been missed."""
        try:
            await self.reconcile()
        except Exception:
            log.warning("post-connect reconcile failed", exc_info=False)

    # ------------------------------------------------------------------ recording
    async def _maybe_start_recording(self, call_id: str) -> None:
        from app.services.recordings import start_recording_if_allowed

        try:
            await start_recording_if_allowed(self, call_id)
        except Exception:
            m.RECORDING_FAILURES.inc()
            log.exception("recording start failed")


def _event_ts(ev: dict[str, str]):
    from datetime import datetime, timezone

    raw = ev.get("Event-Date-Timestamp")  # microseconds since epoch
    try:
        return datetime.fromtimestamp(int(raw) / 1_000_000, tz=timezone.utc) if raw else utcnow()
    except (ValueError, OverflowError):
        return utcnow()
