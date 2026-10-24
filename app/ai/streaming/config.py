"""Per-tenant settings of the token-streaming pipeline (stored in the tenant's `ai_config`; the flag `streaming_pipeline` defaults to OFF).

Every tuning value is None until it is set: None means the built-in PROVISIONAL default below. The defaults are deliberately not final: they are
chosen after the TTS re-measurement (fixed per-request costs) and the concurrency probe."""
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
}
PROVISIONAL = {"stream_first_min_words": 3, "stream_min_words": 6, "stream_max_words": 14, "stream_prefetch_depth": 2, "stream_prebuffer_ms": 150,
               "runpod_poll_cap_s": 0.15}


def value(cfg: dict, key: str):
    v = (cfg or {}).get(key)
    return PROVISIONAL[key] if v is None else v


def stream_config_from(cfg: dict):
    from app.ai.streaming.turn import StreamConfig
    first, mn, mx = value(cfg, "stream_first_min_words"), value(cfg, "stream_min_words"), value(cfg, "stream_max_words")
    return StreamConfig(segmenter=SegmenterConfig(first_min_words=first, min_words=min(mn, mx), max_words=mx),
                        playout=PlayoutConfig(prebuffer_ms=value(cfg, "stream_prebuffer_ms")),
                        prefetch_depth=value(cfg, "stream_prefetch_depth"))
