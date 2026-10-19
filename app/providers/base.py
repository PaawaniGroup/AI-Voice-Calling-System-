"""Provider adapter interface and the FreeSWITCH-gateway / mock implementations."""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass

from app.telephony import dialstrings as ds
from app.telephony.esl import EslCommandClient
from config.settings import Settings

log = logging.getLogger("mme_telephony.provider")


class ProviderError(Exception):
    """Definite failure: the provider did not accept the request (safe to mark the call failed)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ProviderUncertain(Exception):
    """Outcome unknown (timeout / lost response). The call may exist; NEVER auto-retry origination."""


@dataclass
class OriginateRequest:
    call_id: str
    tenant_id: str
    fs_uuid: str
    job_uuid: str
    from_number: str
    to_number: str
    bridge_to: str | None
    ring_timeout: int
    max_duration: int


@dataclass
class OriginateResult:
    provider_call_id: str | None = None
    #: True when state updates arrive via ESL events (FreeSWITCH path) instead of provider webhooks.
    events_via_esl: bool = True


class ProviderAdapter(abc.ABC):
    name: str

    @abc.abstractmethod
    async def originate(self, req: OriginateRequest) -> OriginateResult: ...

    @abc.abstractmethod
    async def hangup(self, call: dict, cause: str) -> None: ...

    async def transfer(self, call: dict, destination: str) -> None:
        raise ProviderError("transfer_unsupported")


class FreeSwitchGatewayProvider(ProviderAdapter):
    """PSTN legs through a FreeSWITCH SIP gateway (e.g. Plivo Zentrunk). Control + events via ESL."""

    name = "freeswitch_gateway"

    def __init__(self, esl: EslCommandClient, s: Settings):
        self.esl, self.s = esl, s

    async def originate(self, req: OriginateRequest) -> OriginateResult:
        from app.telephony.esl import EslCommandError, EslTimeout
        from app.core.errors import Unavailable

        dial = ds.render(self.s.fs_dial_template, self.s.fs_gateway, req.to_number)
        bridge = ds.render(self.s.fs_dial_template, self.s.fs_gateway, req.bridge_to) if req.bridge_to else None
        cmd = ds.originate_command(
            fs_uuid=req.fs_uuid, call_id=req.call_id, tenant_id=req.tenant_id, caller_id=req.from_number, dial=dial,
            ring_timeout=req.ring_timeout, max_duration=req.max_duration, bridge_dial=bridge)
        try:
            await self.esl.bgapi(cmd, req.job_uuid)
        except Unavailable as e:
            # Connection could not be used at all (never written) vs lost mid-write is not distinguishable
            # here; reconciliation by fs_uuid resolves it, so report uncertainty and do not retry.
            raise ProviderUncertain("esl_unavailable") from e
        except EslTimeout as e:
            raise ProviderUncertain("esl_timeout") from e
        except EslCommandError as e:
            raise ProviderError(f"esl_rejected:{str(e)[:60]}") from e
        return OriginateResult(provider_call_id=None, events_via_esl=True)

    async def hangup(self, call: dict, cause: str) -> None:
        await self.esl.api(ds.kill_command(call["fs_uuid"], cause))

    async def transfer(self, call: dict, destination: str) -> None:
        dial = ds.render(self.s.fs_transfer_template, self.s.fs_gateway, destination)
        await self.esl.api(ds.setvar_dial_command(call["fs_uuid"], dial))
        await self.esl.api(ds.transfer_command(call["fs_uuid"]))


class MockProvider(ProviderAdapter):
    """Test/development only. Config validation forbids it in staging/production."""

    name = "mock"

    def __init__(self) -> None:
        self.originated: list[OriginateRequest] = []
        self.hangups: list[tuple[str, str]] = []
        self.fail_with: Exception | None = None

    async def originate(self, req: OriginateRequest) -> OriginateResult:
        if self.fail_with:
            raise self.fail_with
        self.originated.append(req)
        return OriginateResult(provider_call_id=f"mock-{req.call_id}", events_via_esl=False)

    async def hangup(self, call: dict, cause: str) -> None:
        self.hangups.append((call["_id"], cause))

    async def transfer(self, call: dict, destination: str) -> None:
        return None
