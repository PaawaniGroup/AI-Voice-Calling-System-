"""Call lifecycle state machine (pure, no I/O). See docs/CALL_STATE_MACHINE.md."""
from __future__ import annotations

from enum import Enum

from app.core.errors import InvalidTransition


class S(str, Enum):
    QUEUED = "queued"
    INITIATING = "initiating"
    RINGING = "ringing"
    ANSWERED = "answered"
    BRIDGED = "bridged"
    TRANSFERRING = "transferring"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUSY = "busy"
    NO_ANSWER = "no_answer"


TERMINAL = {S.COMPLETED, S.FAILED, S.CANCELLED, S.BUSY, S.NO_ANSWER}
PRE_CONNECT = {S.QUEUED, S.INITIATING, S.RINGING}
CONNECTED = {S.ANSWERED, S.BRIDGED, S.TRANSFERRING}
ACTIVE = {S.INITIATING, S.RINGING} | CONNECTED  # consume a channel (queued does not)

_FAIL_ALL = {S.FAILED}
TRANSITIONS: dict[S, set[S]] = {
    S.QUEUED: {S.INITIATING, S.CANCELLED, S.FAILED},
    S.INITIATING: {S.RINGING, S.ANSWERED, S.BRIDGED, S.BUSY, S.NO_ANSWER, S.CANCELLED} | _FAIL_ALL,
    S.RINGING: {S.ANSWERED, S.BRIDGED, S.BUSY, S.NO_ANSWER, S.CANCELLED} | _FAIL_ALL,
    # answered -> completed: normal hangup. -> failed: media/transfer error after connect.
    S.ANSWERED: {S.BRIDGED, S.TRANSFERRING, S.COMPLETED} | _FAIL_ALL,
    S.BRIDGED: {S.TRANSFERRING, S.COMPLETED} | _FAIL_ALL,
    # transfer success => bridged (or answered if parked); failure keeps the original call alive.
    S.TRANSFERRING: {S.BRIDGED, S.ANSWERED, S.COMPLETED} | _FAIL_ALL,
}
for t in TERMINAL:
    TRANSITIONS[t] = set()

# Progress rank: an event implying a lower rank than the current state is "out of order"
# and is recorded but ignored (e.g. a late RINGING after ANSWERED).
RANK = {S.QUEUED: 0, S.INITIATING: 1, S.RINGING: 2, S.ANSWERED: 3, S.BRIDGED: 4, S.TRANSFERRING: 5}


def can_transition(cur: S, target: S) -> bool:
    return target in TRANSITIONS.get(cur, set())


def validate(cur: S, target: S) -> None:
    if not can_transition(cur, target):
        raise InvalidTransition(cur.value, target.value)


def is_out_of_order(cur: S, target: S) -> bool:
    """True when `target` is a non-terminal progress state at or behind `cur`."""
    if cur in TERMINAL:
        return True
    if target in TERMINAL or target not in RANK:
        return False
    if cur == S.TRANSFERRING and target in (S.BRIDGED, S.ANSWERED):
        return False  # transfer result, handled as a legitimate transition
    return RANK[target] <= RANK[cur]


def classify_hangup(cause: str, *, answered: bool, cancel_requested: bool, timed_out: bool = False) -> tuple[S, str]:
    """Map a FreeSWITCH hangup cause to (terminal state, normalized reason)."""
    cause = (cause or "UNKNOWN").upper()
    if answered:
        if cause == "ALLOTTED_TIMEOUT" or timed_out:
            return S.COMPLETED, "max_duration_reached"
        if cause in {"NORMAL_CLEARING", "NORMAL_UNSPECIFIED", "ORIGINATOR_CANCEL", "NORMAL_CIRCUIT_CONGESTION"}:
            return S.COMPLETED, "normal_clearing" if cause != "ORIGINATOR_CANCEL" else "hangup_requested"
        if cause in {"MEDIA_TIMEOUT", "RECOVERY_ON_TIMER_EXPIRE", "NETWORK_OUT_OF_ORDER", "DESTINATION_OUT_OF_ORDER"}:
            return S.FAILED, f"post_answer_{cause.lower()}"
        return S.COMPLETED, cause.lower()
    if cancel_requested or cause == "ORIGINATOR_CANCEL":
        return S.CANCELLED, "cancelled"
    if cause == "USER_BUSY":
        return S.BUSY, "user_busy"
    if cause in {"NO_ANSWER", "NO_USER_RESPONSE", "ALLOTTED_TIMEOUT", "PROGRESS_TIMEOUT"}:
        return S.NO_ANSWER, cause.lower()
    if cause == "CALL_REJECTED":
        return S.FAILED, "rejected_by_callee"
    if cause in {"UNALLOCATED_NUMBER", "INVALID_NUMBER_FORMAT", "NO_ROUTE_DESTINATION", "NUMBER_CHANGED"}:
        return S.FAILED, "invalid_destination"
    if cause in {"NORMAL_TEMPORARY_FAILURE", "SWITCH_CONGESTION", "NORMAL_CIRCUIT_CONGESTION", "GATEWAY_DOWN",
                 "NETWORK_OUT_OF_ORDER", "DESTINATION_OUT_OF_ORDER", "SERVICE_UNAVAILABLE"}:
        return S.FAILED, "carrier_unavailable"
    if cause in {"NORMAL_CLEARING"}:
        return S.FAILED, "remote_cleared_before_answer"
    return S.FAILED, f"error_{cause.lower()}"
