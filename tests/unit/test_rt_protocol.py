"""Protocol and resilience helpers of the persistent transport (app/ai/rt). SIMULATED: pure code, no network."""
from __future__ import annotations

import random

import pytest

from app.ai.rt import protocol as P
from app.ai.rt.resilience import Backoff, CircuitBreaker, ReadinessTracker

SECRET = b"unit-test-secret"


def test_a_token_is_valid_for_its_call_until_it_expires():
    tok = P.make_token(SECRET, "call-1", ttl_s=60, now=1000.0)
    assert P.verify_token(SECRET, tok, "call-1", now=1030.0) is None
    assert P.verify_token(SECRET, tok, "call-1", now=1061.0) == "expired_token"
    assert P.verify_token(SECRET, tok, "call-2", now=1030.0) == "call_mismatch"
    assert P.verify_token(b"another", tok, "call-1", now=1030.0) == "bad_signature"
    assert P.verify_token(SECRET, "garbage", "call-1") == "malformed_token" and P.verify_token(SECRET, None, "call-1") == "malformed_token"


def test_a_call_id_with_dots_still_verifies():
    tok = P.make_token(SECRET, "a.b.c", now=0.0)
    assert P.verify_token(SECRET, tok, "a.b.c", now=1.0) is None


def test_the_token_never_contains_the_secret():
    assert SECRET.decode() not in P.make_token(SECRET, "call-1")


def test_frames_round_trip_and_short_frames_are_refused():
    assert P.unpack_frame(P.pack_frame(7, b"\x01\x02")) == (7, b"\x01\x02")
    with pytest.raises(P.ProtocolError):
        P.unpack_frame(b"\x00\x01")


def test_parse_accepts_objects_with_a_type_only_and_limits_the_size():
    assert P.parse('{"type":"ping","n":1}')["n"] == 1
    for bad in ("[]", '{"x":1}', "not json", "x" * (P.MAX_MESSAGE_CHARS + 1), None):
        with pytest.raises(P.ProtocolError):
            P.parse(bad)


def test_backoff_grows_is_capped_has_jitter_and_resets():
    b = Backoff(initial=0.2, factor=2.0, maximum=1.0, jitter=0.0)
    assert [round(b.next(), 3) for _ in range(5)] == [0.2, 0.4, 0.8, 1.0, 1.0]
    b.reset()
    assert round(b.next(), 3) == 0.2
    j = Backoff(initial=1.0, jitter=0.2, rng=random.Random(1))
    assert all(0.8 <= Backoff(initial=1.0, jitter=0.2, rng=random.Random(i)).next() <= 1.2 for i in range(20)) and j.next() != 1.0


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_circuit_breaker_opens_after_three_failures_probes_once_and_closes_on_success():
    c = Clock()
    br = CircuitBreaker(failures=3, window_s=30, cooldown_s=10, clock=c)
    assert br.state == "closed" and br.allow()
    for _ in range(3):
        br.record(False)
    assert br.state == "open" and not br.allow()
    c.t = 9.9
    assert not br.allow()
    c.t = 10.1
    assert br.state == "half_open" and br.allow() and not br.allow()               # exactly ONE probe
    br.record(True)
    assert br.state == "closed" and br.allow()


def test_a_failed_probe_reopens_for_a_full_cooldown_and_an_abandoned_probe_does_not_count():
    c = Clock()
    br = CircuitBreaker(failures=2, window_s=30, cooldown_s=10, clock=c)
    br.record(False)
    br.record(False)
    c.t = 11.0
    assert br.allow()
    br.record(False)
    assert br.state == "open"
    c.t = 20.0
    assert br.state == "open"                                                        # 9 s after the failed probe: still cooling down
    c.t = 21.5
    assert br.allow()
    br.abandon()                                                                      # the caller cancelled the probe: nobody is stuck waiting for its verdict
    assert br.allow()


def test_failures_outside_the_window_do_not_open_the_breaker():
    c = Clock()
    br = CircuitBreaker(failures=3, window_s=30, cooldown_s=10, clock=c)
    br.record(False)
    c.t = 20.0
    br.record(False)
    c.t = 40.0
    br.record(False)                                                                  # the first failure is 40 s old: only two in the window
    assert br.state == "closed"


def test_readiness_is_the_last_completed_request_not_a_health_listing():
    c = Clock()
    r = ReadinessTracker(window_s=60, max_first_byte_ms=500, clock=c)
    assert not r.ready("tts") and r.last_ok_age_s("tts") is None
    r.record("tts", True, 900.0)
    assert not r.ready("tts")                                                         # completed, but too slow to call it ready
    r.record("tts", True, 120.0)
    c.t = 30.0
    assert r.ready("tts") and r.last_ok_age_s("tts") == 30.0
    c.t = 61.0
    assert not r.ready("tts")                                                         # too old
    r.record("tts", False, None)
    assert not r.ready("stt")


# ---- security: single-use tokens and the URL policy
def test_a_token_works_once_with_a_replay_cache_and_a_replayed_one_is_refused():
    cache = P.ReplayCache()
    tok = P.make_token(SECRET, "call-1", ttl_s=60, now=1000.0)
    assert P.verify_token(SECRET, tok, "call-1", now=1001.0, replay=cache) is None
    assert P.verify_token(SECRET, tok, "call-1", now=1002.0, replay=cache) == "replayed_token"
    assert P.verify_token(SECRET, P.make_token(SECRET, "call-1", now=1000.0), "call-1", now=1002.0, replay=cache) is None       # a fresh token has a fresh nonce


def test_two_tokens_for_the_same_call_differ_and_a_changed_nonce_breaks_the_signature():
    a, b = P.make_token(SECRET, "c", now=5.0), P.make_token(SECRET, "c", now=5.0)
    assert a != b
    head, nonce, sig = a.rsplit(".", 2)
    assert P.verify_token(SECRET, f"{head}.{'0' * len(nonce)}.{sig}", "c", now=6.0) == "bad_signature"


def test_the_replay_cache_is_bounded_and_forgets_only_expired_nonces():
    cache = P.ReplayCache(max_entries=4)
    for i in range(2):
        assert cache.first_use(f"n{i}", exp=100, now=0.0)
    assert cache.first_use("n2", exp=100, now=0.0) and cache.first_use("n3", exp=100, now=0.0)
    assert not cache.first_use("n4", exp=100, now=1.0)                  # full of live tokens: refuse, never forget one (that would let a replay through)
    assert cache.first_use("n4", exp=300, now=200.0)                    # after the old ones expired there is room again
    assert not cache.first_use("n4", exp=300, now=201.0)


def test_url_policy_wss_always_plain_ws_only_for_loopback_unless_the_code_says_unsafe():
    assert P.check_url("wss://host.example/ws") is None and P.check_url("wss://203.0.113.7:13007/ws") is None
    for ok in ("ws://127.0.0.1:8000/ws", "ws://localhost/ws", "ws://[::1]:9/ws"):
        assert P.check_url(ok) is None
    for bad in ("ws://203.0.113.7:13007/ws", "ws://host.example/ws", "ws://10.0.0.5/ws"):
        assert P.check_url(bad) == "plain_ws_to_non_loopback" and P.check_url(bad, unsafe_allow_plain=True) is None
    for junk in ("http://host/ws", "https://host", "ftp://x", "wss://", "ws://", "", "host.example/ws", "ws:///ws"):
        assert P.check_url(junk) == "bad_url"