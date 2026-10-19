"""Builders for FreeSWITCH command strings.

Every interpolated value is validated against a strict allowlist pattern, so API input can
never inject channel variables, extra applications or additional ESL commands.
"""
from __future__ import annotations

import re

_E164 = re.compile(r"^\+[1-9][0-9]{6,14}$")
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_GATEWAY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CAUSE = re.compile(r"^[A-Z_]{3,40}$")
# Loopback dial targets are only for the isolated test profile (see fs_dial_template).
_TEST_DEST = re.compile(r"^[0-9+]{3,20}$")


def _chk(rx: re.Pattern, v: str, what: str) -> str:
    if not isinstance(v, str) or not rx.match(v):
        raise ValueError(f"unsafe {what}")
    return v


def safe_uuid(v: str) -> str:
    return _chk(_UUID, v, "uuid")


def safe_cause(v: str) -> str:
    return _chk(_CAUSE, v, "hangup cause")


def render(template: str, gateway: str, destination: str) -> str:
    _chk(_GATEWAY, gateway, "gateway")
    dest = destination if _E164.match(destination) else _chk(_TEST_DEST, destination, "destination")
    out = template.replace("{gateway}", gateway).replace("{destination}", dest)
    if any(c in out for c in "\n\r{}&,'\" ;"):
        raise ValueError("unsafe dial string")
    return out


def originate_command(*, fs_uuid: str, call_id: str, tenant_id: str, caller_id: str, dial: str, ring_timeout: int,
                      max_duration: int, bridge_dial: str | None = None) -> str:
    safe_uuid(fs_uuid)
    _chk(_TOKEN, call_id, "call id")
    _chk(_TOKEN, tenant_id, "tenant id")
    _chk(_E164, caller_id, "caller id")
    if not (1 <= ring_timeout <= 600 and 1 <= max_duration <= 86400):
        raise ValueError("timeouts out of range")
    vars_ = [
        f"origination_uuid={fs_uuid}",
        f"mme_call_id={call_id}",
        f"mme_tenant={tenant_id}",
        "mme_leg=a",
        f"origination_caller_id_number={caller_id}",
        "origination_caller_id_name=MME",
        f"originate_timeout={ring_timeout}",
        "ignore_early_media=false",
        f"execute_on_answer='sched_hangup +{max_duration} ALLOTTED_TIMEOUT'",
    ]
    app = f"&bridge({bridge_dial})" if bridge_dial else "&park()"
    return "originate {" + ",".join(vars_) + "}" + dial + " " + app


def kill_command(fs_uuid: str, cause: str) -> str:
    return f"uuid_kill {safe_uuid(fs_uuid)} {safe_cause(cause)}"


def exists_command(fs_uuid: str) -> str:
    return f"uuid_exists {safe_uuid(fs_uuid)}"


def setvar_dial_command(fs_uuid: str, dial: str) -> str:
    """Stores the (already rendered + validated) dial string used by the `mme_transfer` dialplan context."""
    if any(c in dial for c in "\n\r{}&,'\" ;$") or not re.match(r"^[A-Za-z0-9_./+:-]{3,200}$", dial):
        raise ValueError("unsafe transfer dial string")
    return f"uuid_setvar {safe_uuid(fs_uuid)} mme_xfer_dial {dial}"


def transfer_command(fs_uuid: str) -> str:
    """Transfers into the fixed, controller-owned `mme_transfer` context (never an API-supplied dialplan)."""
    return f"uuid_transfer {safe_uuid(fs_uuid)} mme_xfer XML mme_transfer"

_REC_PATH = re.compile(r"^/[A-Za-z0-9_./-]{1,200}\.(wav|mp3)$")


def _rec_path(path: str) -> str:
    if not _REC_PATH.match(path) or ".." in path:
        raise ValueError("unsafe recording path")
    return path


def record_start_command(fs_uuid: str, path: str, max_seconds: int) -> str:
    return f"uuid_record {safe_uuid(fs_uuid)} start {_rec_path(path)} {int(max_seconds)}"


def record_stop_command(fs_uuid: str, path: str) -> str:
    return f"uuid_record {safe_uuid(fs_uuid)} stop {_rec_path(path)}"