"""Plivo Voice API adapter (REST originate + signed webhooks).

Uses the official `plivo` SDK (sync, run in a thread). Callback field names are those the
SDK/docs reference (CallUUID, CallStatus, ...). Items NOT verified against a live account are
listed in docs/PROVIDER_SETUP.md.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.providers.base import (OriginateRequest, OriginateResult, ProviderAdapter, ProviderError,
                                ProviderUncertain)
from app.telephony.state_machine import S, classify_hangup
from config.settings import Settings

log = logging.getLogger("mme_telephony.plivo")

# Plivo CallStatus -> internal state (terminal mapping refined by answered-ness in map_status()).
_STATUS = {"ringing": S.RINGING, "in-progress": S.ANSWERED, "busy": S.BUSY, "no-answer": S.NO_ANSWER,
           "cancel": S.CANCELLED, "failed": S.FAILED, "timeout": S.NO_ANSWER, "completed": S.COMPLETED}


def map_status(params: dict[str, str], answered: bool) -> tuple[S, str] | None:
    status = (params.get("CallStatus") or "").lower()
    event = (params.get("Event") or "").lower()
    if event == "ring":
        status = status or "ringing"
    target = _STATUS.get(status)
    if target is None:
        return None
    if target == S.COMPLETED and not answered:
        cause = (params.get("HangupCause") or "").upper()
        return classify_hangup(cause or "NORMAL_CLEARING", answered=False, cancel_requested=False)
    return target, (params.get("HangupCauseName") or params.get("HangupCause") or status).lower()[:60]


def verify_signature(s: Settings, *, method: str, url: str, params: dict[str, str], nonce: str, signature: str) -> bool:
    from plivo.utils.signature_v3 import validate_v3_signature

    token = s.plivo_auth_token.get_secret_value()
    if not (token and nonce and signature):
        return False
    try:
        return bool(validate_v3_signature(method.upper(), url, nonce, token, signature, params))
    except Exception:
        return False


def plivo_enabled(s: Settings) -> bool:
    """Plivo Voice-Application callbacks are served when Plivo originates calls OR inbound handling is switched on."""
    return s.provider == "plivo_api" or s.plivo_inbound_enabled


def callback_url(s: Settings, path: str, query: str = "") -> str:
    """The exact public URL Plivo signed. Built ONLY from WEBHOOK_PUBLIC_BASE_URL (never Host / X-Forwarded-* headers)."""
    return s.webhook_public_base_url.rstrip("/") + path + (f"?{query}" if query else "")


def stream_wss_url(s: Settings) -> str:
    """Public wss:// URL of the media stream route, derived from the https base URL."""
    base = s.webhook_public_base_url.rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("WEBHOOK_PUBLIC_BASE_URL must be https")
    return "wss://" + base[len("https://"):] + f"{s.api_prefix}/providers/plivo/stream"


def verify_ws_signature(s: Settings, *, nonce: str, signature: str, query: str = "") -> bool:
    """Validate Plivo's handshake signature for the media WebSocket (method GET over the public URL).

    The SDK's `validate_v3_signature` only accepts http(s) URIs, while the URL Plivo connects to is `wss://`. The HMAC
    primitive is the SDK's own (`get_signature_v3`); we try the wss:// form and its https:// twin, both built from config.
    UNVERIFIED against a real Plivo handshake (which of the two Plivo signs is not stated in its reference).
    """
    import hmac

    from plivo.utils.signature_v3 import get_signature_v3

    token = s.plivo_auth_token.get_secret_value()
    if not (token and nonce and signature) or len(signature) > 512 or len(nonce) > 256:
        return False
    wss = stream_wss_url(s) + (f"?{query}" if query else "")
    candidates = [wss, "https://" + wss[len("wss://"):]]
    offered = [x.strip() for x in signature.split(",") if x.strip()]
    for uri in candidates:
        try:
            want = get_signature_v3(token.encode(), uri.encode(), nonce.encode())
        except Exception:
            continue
        want_s = want.decode() if isinstance(want, bytes) else str(want)
        if any(hmac.compare_digest(want_s, o) for o in offered):
            return True
    return False


class PlivoApiProvider(ProviderAdapter):
    name = "plivo_api"

    def __init__(self, s: Settings):
        self.s = s
        self._client: Any = None

    def _c(self):
        if self._client is None:
            import plivo

            self._client = plivo.RestClient(
                self.s.plivo_auth_id.get_secret_value(), self.s.plivo_auth_token.get_secret_value(),
                timeout=self.s.provider_timeout_s)
        return self._client

    async def originate(self, req: OriginateRequest) -> OriginateResult:
        base = self.s.webhook_public_base_url.rstrip("/")
        cb = f"{base}{self.s.api_prefix}/providers/plivo"
        kwargs = dict(
            from_=req.from_number.lstrip("+"), to_=req.to_number.lstrip("+"), answer_url=f"{cb}/answer",
            answer_method="POST", ring_url=f"{cb}/status", ring_method="POST", hangup_url=f"{cb}/status",
            hangup_method="POST", ring_timeout=req.ring_timeout, time_limit=req.max_duration)
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(lambda: self._c().calls.create(**kwargs)), self.s.provider_timeout_s + 2)
        except asyncio.TimeoutError as e:
            raise ProviderUncertain("plivo_timeout") from e
        except Exception as e:
            name = type(e).__name__
            # SDK raises typed errors for definite 4xx/5xx; connection-level errors are uncertain.
            if name in ("ValidationError", "AuthenticationError", "InvalidRequestError", "ResourceNotFoundError",
                        "ForbiddenError", "PlivoRestError"):
                raise ProviderError(f"plivo_rejected:{name}") from e
            raise ProviderUncertain(f"plivo_{name}") from e
        rid = getattr(resp, "request_uuid", None)
        if isinstance(rid, list):
            rid = rid[0] if rid else None
        return OriginateResult(provider_call_id=str(rid) if rid else None, events_via_esl=False)

    async def hangup(self, call: dict, cause: str) -> None:
        pid = call.get("provider_call_id")
        if not pid:
            raise ProviderError("no_provider_call_id")
        try:
            await asyncio.to_thread(lambda: self._c().calls.delete(pid))
        except Exception as e:
            raise ProviderError(f"plivo_hangup_failed:{type(e).__name__}") from e
