from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CallCreate(Strict):
    to: str = Field(min_length=5, max_length=32, description="Destination (E.164 preferred)")
    from_number: str = Field(min_length=5, max_length=32, validation_alias=AliasChoices("from_number", "from"),
                             description="Approved caller ID (accepted as `from` or `from_number`)")
    tenant_id: str | None = Field(None, max_length=100, description="Optional; must equal the tenant in the token. Never used for authorization.")
    ai_enabled: bool = Field(False, description="Request AI handling. The AI session still only starts when the media bridge is ready.")
    language: str = Field("auto", description="auto | hi | en | hinglish")
    webhook_url: str | None = Field(None, description="Not supported per call; register /v1/webhooks/endpoints instead (must be null).")
    idempotency_key: str | None = Field(None, min_length=8, max_length=128, description="Alternative to the Idempotency-Key header (header wins).")
    bridge_to: str | None = Field(None, max_length=32, description="Optional second party to bridge on answer")
    max_duration_s: int | None = Field(None, ge=10, le=14400)
    metadata: dict[str, str | int | bool] | None = Field(None, description="Opaque client metadata (<=10 keys)")

    @field_validator("metadata")
    @classmethod
    def _meta(cls, v):
        if v and (len(v) > 10 or any(len(k) > 40 or (isinstance(x, str) and len(x) > 200) for k, x in v.items())):
            raise ValueError("metadata too large")
        return v


class TransferRequest(Strict):
    destination: str = Field(min_length=5, max_length=32)


class CallOut(BaseModel):
    id: str
    tenant_id: str
    session_id: str | None = None
    ai_enabled: bool = False
    language: str = "auto"
    direction: str
    state: str
    provider: str
    from_number: str
    to_number: str
    bridge_to: str | None = None
    provider_call_id: str | None = None
    fs_uuid: str | None = None
    created_at: datetime
    queued_at: datetime | None = None
    initiated_at: datetime | None = None
    ringing_at: datetime | None = None
    answered_at: datetime | None = None
    bridged_at: datetime | None = None
    ended_at: datetime | None = None
    ring_duration_s: float | None = None
    connected_duration_s: float | None = None
    hangup_cause: str | None = None
    failure_reason: str | None = None
    state_version: int
    transfer: dict[str, Any] | None = None
    recording_ids: list[str] = []
    correlation_id: str | None = None
    metadata: dict[str, Any] | None = None


def call_out(c: dict, mask: bool = False) -> dict:
    """mask=True hides caller/destination digits (read-only viewers)."""
    from app.core.logging import mask_phone
    mk = (lambda n: mask_phone(n) if n else n) if mask else (lambda n: n)
    return CallOut(
        id=c["_id"], tenant_id=c["tenant_id"], session_id=c.get("session_id"), ai_enabled=bool(c.get("ai_enabled")),
        language=c.get("language", "auto"), direction=c.get("direction", "outbound"), state=c["state"],
        provider=c.get("provider", ""), from_number=mk(c.get("from_number", "")), to_number=mk(c.get("to_number", "")),
        bridge_to=mk(c.get("bridge_to")), provider_call_id=c.get("provider_call_id"), fs_uuid=c.get("fs_uuid"),
        created_at=c["created_at"], queued_at=c.get("queued_at"), initiated_at=c.get("initiated_at"),
        ringing_at=c.get("ringing_at"), answered_at=c.get("answered_at"), bridged_at=c.get("bridged_at"),
        ended_at=c.get("ended_at"), ring_duration_s=c.get("ring_duration_s"),
        connected_duration_s=c.get("connected_duration_s"), hangup_cause=c.get("hangup_cause"),
        failure_reason=c.get("failure_reason"), state_version=c.get("state_version", 0),
        transfer=_transfer(c.get("transfer")), recording_ids=c.get("recording_ids", []),
        correlation_id=c.get("correlation_id"), metadata=c.get("metadata") or None,
    ).model_dump(mode="json")


def _transfer(t: dict | None) -> dict | None:
    if not t:
        return None
    return {"current": bool(t.get("current")), "attempts": [
        {"destination_masked": a.get("destination_masked"), "outcome": a.get("outcome")} for a in t.get("attempts", [])]}


class PolicyIn(Strict):
    enabled: bool = True
    max_active_calls: int | None = Field(None, ge=1, le=1000)
    max_duration_s: int | None = Field(None, ge=10, le=14400)
    allowed_countries: list[str] = Field(default_factory=list, max_length=50)
    deny_prefixes: list[str] = Field(default_factory=list, max_length=200)
    allow_prefixes: list[str] = Field(default_factory=list, max_length=200)
    caller_ids: list[str] = Field(default_factory=list, max_length=50)
    allowed_callers: list[str] = Field(default_factory=list, max_length=50,
                                       description="Inbound Plivo calls: when non-empty, ONLY these caller numbers are answered; everyone else is rejected before a call exists.")
    transfer_destinations: list[str] = Field(default_factory=list, max_length=200)
    recording: Literal["on", "off"] = "off"
    # Outbound AI calls (fail-closed: all four are OFF/empty unless sent explicitly, and a PUT that omits them resets them).
    outbound_ai_enabled: bool = Field(False, description="Allow this tenant to place outbound calls that start an AI conversation.")
    outbound_ai_destinations: list[str] = Field(default_factory=list, max_length=200,
                                                description="EXACT E.164 numbers an outbound AI call may dial (no prefixes, no wildcards). Empty = none allowed.")
    outbound_ai_daily_quota: int | None = Field(None, ge=0, le=100000, description="Outbound AI calls per UTC day; omitted = the service default.")
    outbound_ai_max_duration_s: int | None = Field(None, ge=10, le=3600, description="Cap for one outbound AI call; omitted = the service default.")


class RouteIn(Strict):
    number: str = Field(max_length=32)
    action: Literal["park", "bridge", "hangup", "ai"] = "park"  # "ai": Plivo-delivered calls open an AI media stream
    target: str | None = Field(None, max_length=32)


class WebhookEndpointIn(Strict):
    url: str = Field(max_length=500)
    events: list[str] = Field(default_factory=lambda: ["*"], max_length=20)
    enabled: bool = True
