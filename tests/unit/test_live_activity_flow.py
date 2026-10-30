"""Live dashboard building blocks: the activity bus and the per-call flow derivation (pure code, no database)."""
import asyncio

import pytest
from prometheus_client import CollectorRegistry, Histogram

from app.api import routes_live
from app.observability.activity import ActivityBus
from app.services.flow import flow_stages


# ------------------------------------------------------------------ ActivityBus
def test_entries_are_ordered_and_tenant_scoped():
    b = ActivityBus()
    a1 = b.publish("t1", "state.ringing", call_id="c1")
    b.publish("t2", "state.ringing", call_id="c2")
    a3 = b.publish("t1", "state.answered", call_id="c1")
    items, lost = b.since(0, "t1")
    assert [i["seq"] for i in items] == [a1, a3] and lost == 0
    assert all(i["call_id"] == "c1" for i in items)
    assert [i["call_id"] for i in b.since(0, "t2")[0]] == ["c2"]
    assert b.since(a3, "t1") == ([], 0)  # nothing newer
    assert "_tenant" not in items[0]  # the owner tag never leaves the bus


def test_data_is_redacted_and_bounded_before_it_is_stored():
    b = ActivityBus()
    b.publish("t1", "ai.turn", data={"to": "+919876543210", "api_key": "sekrit-value", "password": "x", "note": "z" * 500,
                                      "deep": {"a": {"b": {"c": {"d": 1}}}}})
    e = b.recent("t1")[0]
    text = str(e)
    assert "9876543210" not in text and "sekrit-value" not in text and e["data"]["api_key"] == "***"
    assert len(e["data"]["note"]) <= 120


def test_ring_is_bounded_and_a_slow_reader_is_told_what_it_missed():
    b = ActivityBus(size=10)
    for i in range(25):
        b.publish("t1", "state.x", data={"i": i})
    items, lost = b.since(0, "t1")
    assert len(items) == 10 and items[0]["data"]["i"] == 15
    assert lost == 15  # entries 1..15 were dropped from the ring


def test_publish_never_raises_and_ignores_entries_without_a_tenant():
    b = ActivityBus()
    assert b.publish(None, "x") == 0 and b.publish("", "x") == 0
    class Weird:  # an object whose repr explodes must not take the caller down
        def __repr__(self):
            raise RuntimeError("boom")
    b.publish("t1", "state.x", data={"obj": Weird()})  # must not raise
    assert b.last_seq >= 0


@pytest.mark.parametrize("kind,level", [("state.answered", "info"), ("ai.session.failed", "error"), ("webhook.dead", "error"),
                                        ("webhook.retrying", "warn"), ("media.disconnect", "warn"), ("ai.autostart_skipped", "warn")])
def test_levels(kind, level):
    b = ActivityBus()
    b.publish("t1", kind)
    assert b.recent("t1")[0]["level"] == level


async def test_wait_wakes_immediately_on_publish_and_times_out_otherwise():
    b = ActivityBus()
    assert await b.wait(0, 0.05) is False
    task = asyncio.create_task(b.wait(0, 2.0))
    await asyncio.sleep(0.05)
    b.publish("t1", "state.x")
    assert await asyncio.wait_for(task, 1.0) is True
    assert await b.wait(0, 5.0) is True  # something newer than 0 already exists: no waiting


# ------------------------------------------------------------------ histogram quantiles
def test_histogram_quantile_estimates(monkeypatch):
    reg = CollectorRegistry()
    h = Histogram("mme_tel_ai_turn_seconds", "x", registry=reg, buckets=(1, 2, 4, 8))
    monkeypatch.setattr(routes_live.m, "REGISTRY", reg)
    assert routes_live.hist_quantile("mme_tel_ai_turn_seconds", 0.5) is None  # no observations: no invented number
    for v in [0.5] * 50 + [3.0] * 45 + [20.0] * 5:
        h.observe(v)
    assert routes_live.hist_quantile("mme_tel_ai_turn_seconds", 0.5) <= 1.0
    assert 2.0 < routes_live.hist_quantile("mme_tel_ai_turn_seconds", 0.9) <= 4.0
    assert routes_live.hist_quantile("mme_tel_ai_turn_seconds", 0.99) == 8  # beyond the last finite bucket: the bound, not a guess


# ------------------------------------------------------------------ flow derivation
def stat(f, key):
    return next(s for s in f["stages"] if s["key"] == key)["status"]


def call(**kw):
    return {"_id": "c1", "tenant_id": "t1", "state": "queued", "direction": "inbound", "provider": "plivo_api", "created_at": "t0", **kw}


def test_ringing_call_without_ai():
    f = flow_stages(call(state="ringing"))
    assert f["overall"] == "live"
    assert [stat(f, k) for k in ("created", "ringing", "answered", "session", "media", "ai", "ended")] == \
        ["done", "active", "waiting", "waiting", "skipped", "skipped", "waiting"]


def test_answered_ai_call_waiting_for_the_stream():
    f = flow_stages(call(state="answered", answered_at="t1", ai_enabled=True), {"status": "answered"}, {"state": "starting", "turns": 0})
    assert stat(f, "answered") == "done" and stat(f, "media") == "active" and stat(f, "ai") == "active"


def test_active_ai_conversation_shows_turns_and_latency():
    f = flow_stages(call(state="answered", answered_at="t1", ai_enabled=True, stream_connected=True), {"status": "active"},
                    {"state": "active", "turns": 3, "timings_ms": {"turn_ms": 1234.0}, "media": {"connected": True}})
    ai = next(s for s in f["stages"] if s["key"] == "ai")
    assert stat(f, "media") == "done" and ai["status"] == "active" and "3 turn(s)" in ai["detail"] and "1234 ms" in ai["detail"]


def test_media_reconnecting_is_visible():
    f = flow_stages(call(state="answered", answered_at="t1", ai_enabled=True), {"status": "active"}, {"state": "media_disconnected", "turns": 1})
    assert stat(f, "media") == "active" and "reconnecting" in next(s for s in f["stages"] if s["key"] == "media")["detail"]


def test_completed_ai_call():
    f = flow_stages(call(state="completed", answered_at="t1", ended_at="t2", ai_enabled=True, hangup_cause="NORMAL_CLEARING", connected_duration_s=42.0),
                    {"status": "ended"}, {"state": "stopped", "turns": 2, "stop_reason": "call_ended"})
    assert f["overall"] == "ended" and stat(f, "ai") == "done" and stat(f, "ended") == "done"
    assert "42s connected" in next(s for s in f["stages"] if s["key"] == "ended")["detail"]


def test_failures_are_shown_where_they_happened():
    no_answer = flow_stages(call(state="no_answer", hangup_cause="NO_ANSWER"))
    assert no_answer["overall"] == "failed" and stat(no_answer, "answered") == "failed" and stat(no_answer, "ended") == "failed"
    media_failed = flow_stages(call(state="answered", answered_at="t1", ai_enabled=True), {"status": "answered"},
                               {"state": "failed", "stop_reason": "media_connect_timeout", "turns": 0})
    assert stat(media_failed, "media") == "failed" and stat(media_failed, "ai") == "failed"
    no_ai_session = flow_stages(call(state="completed", answered_at="t1", ai_enabled=True))
    assert stat(no_ai_session, "ai") == "failed"  # AI was requested, the call ended, and no AI session ever existed


def test_missing_evidence_is_never_reported_as_done():
    f = flow_stages(call(state="initiating"))
    assert all(s["status"] != "done" for s in f["stages"] if s["key"] not in ("created",))
