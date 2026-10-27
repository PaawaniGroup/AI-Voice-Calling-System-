"""`utterance_cap_raise` + `utterance_max_s` (tenant, default OFF; today's cap is 6 s): config, validation, survival through AiService.get_config, and the value the call uses.
SIMULATED / unit level; the live behaviour on the real stack (a 9 s utterance is ONE utterance with the flag on, split at 6 s with it off) is tested in the integration file. NOT RUN."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.service import LATENCY_FLAGS, AiService
from app.ai.streaming.config import STREAM_KEYS, PROVISIONAL, utterance_max_ms
from app.core.errors import Unprocessable
from config.settings import Settings


def svc(doc):
    coll = MagicMock()
    coll.find_one = AsyncMock(return_value=doc)
    coll.update_one = AsyncMock()
    db = MagicMock()
    db.__getitem__.return_value = coll
    return AiService(db, None, Settings(_env_file=None), None)


def test_the_default_value_is_todays_six_seconds_and_the_range_is_6_to_12():
    assert PROVISIONAL["utterance_max_s"] == 6.0 and STREAM_KEYS["utterance_max_s"] == (6.0, 12.0) and "utterance_cap_raise" in LATENCY_FLAGS


@pytest.mark.parametrize("cfg,expected", [({}, None), ({"utterance_max_s": 10.0}, None),                           # the value alone does nothing: the flag decides
                                          ({"utterance_cap_raise": True}, 6000), ({"utterance_cap_raise": True, "utterance_max_s": 9.5}, 9500),
                                          ({"utterance_cap_raise": True, "utterance_max_s": 12.0}, 12000), ({"utterance_cap_raise": True, "utterance_max_s": 99}, 12000),
                                          ({"utterance_cap_raise": True, "utterance_max_s": 1}, 6000), ({"utterance_cap_raise": False, "utterance_max_s": 9}, None)])
def test_the_per_call_maximum(cfg, expected):
    assert utterance_max_ms(cfg) == expected


async def test_the_flag_defaults_off_and_the_value_defaults_to_none_in_the_runtime_config():
    cfg = await svc(None).get_config("t")
    assert cfg["utterance_cap_raise"] is False and cfg["utterance_max_s"] is None


async def test_the_flag_survives_get_config():
    assert (await svc({"tenant_id": "t", "utterance_cap_raise": True}).get_config("t"))["utterance_cap_raise"] is True, "dropped by the allowlist"


async def test_the_value_survives_get_config():
    assert (await svc({"tenant_id": "t", "utterance_max_s": 9.5}).get_config("t"))["utterance_max_s"] == 9.5, "dropped by the allowlist"


async def test_put_config_validates_the_range():
    s = svc({"tenant_id": "t"})
    saved = await s.put_config("t", {"utterance_cap_raise": True, "utterance_max_s": 9}, "op")
    assert saved["utterance_cap_raise"] is True and saved["utterance_max_s"] == 9.0
    for bad in (5.9, 12.1, 0, -1):
        with pytest.raises(Unprocessable):
            await s.put_config("t", {"utterance_max_s": bad}, "op")


def test_the_api_model_bounds_the_value_and_accepts_the_flag():
    from pydantic import ValidationError
    from app.api.routes_platform import AiConfigIn
    ok = AiConfigIn(utterance_cap_raise=True, utterance_max_s=8.0)
    assert ok.utterance_cap_raise is True and ok.utterance_max_s == 8.0
    for bad in (5.0, 13.0):
        with pytest.raises(ValidationError):
            AiConfigIn(utterance_max_s=bad)
