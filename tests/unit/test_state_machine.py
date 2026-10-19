import itertools

import pytest

from app.core.errors import InvalidTransition
from app.telephony import state_machine as sm
from app.telephony.state_machine import S


def test_terminal_states_have_no_exits():
    for t in sm.TERMINAL:
        for tgt in S:
            assert not sm.can_transition(t, tgt)


def test_happy_path():
    for a, b in [(S.QUEUED, S.INITIATING), (S.INITIATING, S.RINGING), (S.RINGING, S.ANSWERED), (S.ANSWERED, S.BRIDGED),
                 (S.BRIDGED, S.TRANSFERRING), (S.TRANSFERRING, S.BRIDGED), (S.BRIDGED, S.COMPLETED)]:
        sm.validate(a, b)


@pytest.mark.parametrize("a,b", [(S.QUEUED, S.ANSWERED), (S.QUEUED, S.RINGING), (S.RINGING, S.TRANSFERRING),
                                 (S.INITIATING, S.COMPLETED), (S.RINGING, S.COMPLETED), (S.COMPLETED, S.FAILED)])
def test_invalid(a, b):
    with pytest.raises(InvalidTransition):
        sm.validate(a, b)


def test_out_of_order_detection():
    assert sm.is_out_of_order(S.ANSWERED, S.RINGING)
    assert sm.is_out_of_order(S.BRIDGED, S.ANSWERED)
    assert sm.is_out_of_order(S.RINGING, S.RINGING)
    assert not sm.is_out_of_order(S.RINGING, S.ANSWERED)
    assert not sm.is_out_of_order(S.TRANSFERRING, S.BRIDGED)
    assert sm.is_out_of_order(S.COMPLETED, S.RINGING)


def test_every_state_reachable_or_terminal():
    reach = {S.QUEUED}
    for _ in range(len(S)):
        reach |= set(itertools.chain.from_iterable(sm.TRANSITIONS[s] for s in list(reach)))
    assert reach == set(S)


@pytest.mark.parametrize("cause,answered,cancel,state", [
    ("NORMAL_CLEARING", True, False, S.COMPLETED), ("USER_BUSY", False, False, S.BUSY),
    ("NO_ANSWER", False, False, S.NO_ANSWER), ("NO_USER_RESPONSE", False, False, S.NO_ANSWER),
    ("ORIGINATOR_CANCEL", False, True, S.CANCELLED), ("NORMAL_CLEARING", False, True, S.CANCELLED),
    ("CALL_REJECTED", False, False, S.FAILED), ("UNALLOCATED_NUMBER", False, False, S.FAILED),
    ("GATEWAY_DOWN", False, False, S.FAILED), ("NORMAL_CLEARING", False, False, S.FAILED),
    ("MEDIA_TIMEOUT", True, False, S.FAILED), ("ALLOTTED_TIMEOUT", True, False, S.COMPLETED),
    ("WEIRD", False, False, S.FAILED)])
def test_hangup_mapping(cause, answered, cancel, state):
    assert sm.classify_hangup(cause, answered=answered, cancel_requested=cancel)[0] == state


def test_classified_state_always_reachable_from_connected_or_preconnect():
    for cause in ["NORMAL_CLEARING", "USER_BUSY", "NO_ANSWER", "MEDIA_TIMEOUT", "X"]:
        st, _ = sm.classify_hangup(cause, answered=True, cancel_requested=False)
        assert sm.can_transition(S.ANSWERED, st)
        st, _ = sm.classify_hangup(cause, answered=False, cancel_requested=False)
        assert sm.can_transition(S.RINGING, st)
