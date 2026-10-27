"""Per-tenant settings of the token-streaming pipeline (stored in the tenant's `ai_config`; the flag `streaming_pipeline` defaults to OFF).

Every tuning value is None until it is set: None means the built-in default below. The clause sizes come from the TTS re-measurement of 25 Oct 2026;
the prefetch depth stays provisional until the concurrency probe is run."""
from __future__ import annotations

from app.ai.streaming.playout import PlayoutConfig
from app.ai.streaming.segmenter import SegmenterConfig

# key -> (min, max); a float bound means the value is a float
STREAM_KEYS: dict[str, tuple] = {
    "stream_first_min_words": (1, 8),
    "stream_min_words": (1, 20),
    "stream_max_words": (6, 40),
    "stream_prefetch_depth": (1, 4),
    "stream_prebuffer_ms": (0, 400),
    "runpod_poll_cap_s": (0.03, 1.0),
    "ack_after_ms": (0, 3000),                  # cached acknowledgement (flag `ack_enabled`): wait after the speech-to-text final before it plays
    "keepalive_interval_s": (2.0, 30.0),        # keep-alive ping of the voice endpoint during a call (flag `keepalive_enabled`)
    "keepalive_max_s": (30.0, 900.0),           # a keep-alive stops by itself after this long, whatever else happens
}
# Clause sizes from the TTS re-measurement of 25 Oct 2026 (docs/evidence/phase0b_tts_v3.json, ulaw8k + named voice, no reference): wall time per second of audio is
# 1.52 for 4 words (a first clause cannot be ahead of playout, it only has to be short), 1.21 for 8, 0.99 for 12 and 0.97 for 16: only clauses of 12 words or more
# are generated as fast as they are played, so later clauses are cut at 12-14 words. Prefetch depth 2 is unchanged until the concurrency probe is run.
PROVISIONAL = {"stream_first_min_words": 4, "stream_min_words": 12, "stream_max_words": 14, "stream_prefetch_depth": 2, "stream_prebuffer_ms": 150,
               "runpod_poll_cap_s": 0.15, "ack_after_ms": 300, "keepalive_interval_s": 5.0, "keepalive_max_s": 300.0}


def value(cfg: dict, key: str):
    v = (cfg or {}).get(key)
    return PROVISIONAL[key] if v is None else v


def stream_config_from(cfg: dict):
    from app.ai.streaming.turn import StreamConfig
    first, mn, mx = value(cfg, "stream_first_min_words"), value(cfg, "stream_min_words"), value(cfg, "stream_max_words")
    return StreamConfig(segmenter=SegmenterConfig(first_min_words=first, min_words=min(mn, mx), max_words=mx),
                        playout=PlayoutConfig(prebuffer_ms=value(cfg, "stream_prebuffer_ms")),
                        prefetch_depth=value(cfg, "stream_prefetch_depth"),
                        ack_after_s=value(cfg, "ack_after_ms") / 1000)
