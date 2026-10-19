# Call state machine

Implemented in `app/telephony/state_machine.py` (pure) and applied by `Repos.transition` (compare-and-swap on `state_version`).

```
queued ──► initiating ──► ringing ──► answered ──► bridged ──► completed
  │            │  │          │  │         │  ▲        │  ▲
  │            │  │          │  │         ▼  │        ▼  │
  │            │  │          │  │      transferring ◄──┘  │ (transfer ok => bridged,
  │            │  │          │  │         │               │  rejected/timeout => answered)
  ▼            ▼  ▼          ▼  ▼         ▼
cancelled   busy/no_answer/failed/cancelled (from initiating|ringing)      failed (from any connected state)
```

Terminal: `completed, failed, cancelled, busy, no_answer` (immutable; later events are recorded but ignored).

| From | Allowed targets |
|---|---|
| queued | initiating, cancelled, failed |
| initiating | ringing, answered, bridged, busy, no_answer, cancelled, failed |
| ringing | answered, bridged, busy, no_answer, cancelled, failed |
| answered | bridged, transferring, completed, failed |
| bridged | transferring, completed, failed |
| transferring | bridged, answered, completed, failed |

## Event handling rules
1. Every event is first written to `call_events` under a unique `(call_id, dedup_id)` index: replays return `duplicate` and change nothing.
2. A non-terminal target at or behind the current progress rank is `out_of_order` (recorded, ignored), e.g. late ringing after answer.
3. Events on a terminal call are `terminal_ignored`.
4. A move that the table forbids (e.g. `queued -> answered`) raises `InvalidTransition`; the controller records an `invalid_transition` event and keeps the state.
5. Concurrent writers use CAS on `state_version`; the loser re-reads and re-evaluates, so racing hangup/bridge events converge on one terminal state.
6. `connected_duration_s = ended_at - answered_at`, `ring_duration_s = (answered_at or ended_at) - ringing_at`; a negative value (clock step) is stored as 0 with `clock_anomaly=true`.

## Hangup cause mapping (`classify_hangup`)
| Condition | Result |
|---|---|
| answered + NORMAL_CLEARING / ORIGINATOR_CANCEL | completed |
| answered + ALLOTTED_TIMEOUT (or controller-forced) | completed, `max_duration_reached` |
| answered + MEDIA_TIMEOUT / RECOVERY_ON_TIMER_EXPIRE / network errors | failed `post_answer_*` |
| not answered + cancel requested / ORIGINATOR_CANCEL | cancelled |
| USER_BUSY | busy |
| NO_ANSWER / NO_USER_RESPONSE / PROGRESS_TIMEOUT / ALLOTTED_TIMEOUT | no_answer |
| CALL_REJECTED | failed `rejected_by_callee` |
| UNALLOCATED_NUMBER / NO_ROUTE_DESTINATION / INVALID_NUMBER_FORMAT | failed `invalid_destination` |
| gateway/congestion causes | failed `carrier_unavailable` |
| NORMAL_CLEARING before answer (not requested) | failed `remote_cleared_before_answer` |

## Timeouts (controller sweeps, every 2 s)
* `queued` older than `QUEUE_MAX_WAIT_S` → failed `queue_timeout`.
* `initiating` older than `INITIATING_TIMEOUT_S` → hangup `PROGRESS_TIMEOUT` (uncertain originations are left to reconciliation).
* connected longer than `max_duration_s + 10 s` → `ALLOTTED_TIMEOUT` hangup (FreeSWITCH also schedules `sched_hangup` on answer).
* `transferring` longer than `TRANSFER_TIMEOUT_S` → attempt closed `timeout`, state reverts to `answered`.

## Reconciliation (`Controller.reconcile`, on every ESL (re)connect and every `RECONCILE_INTERVAL_S`)
* Source of truth for "channel exists" is `show channels as json`. If FreeSWITCH cannot be queried, **nothing changes**.
* A non-terminal call whose `fs_uuid` has no channel is finalized only when older than the grace period; for `initiating`/uncertain calls the grace is at least `RING_TIMEOUT_S + 10 s` (a load-test race with grace 0 motivated this).
  Answered → `completed`/`reconciled_channel_missing`; otherwise `failed`/`lost_before_answer` or `origination_uncertain_no_channel`.
* A live channel whose call is already terminal is an orphan and is hung up (`orphans_killed`).
* Origination is never retried automatically: the UUID is persisted before dispatch, so an uncertain outcome is resolved by looking, not by re-dialing.
