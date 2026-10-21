# 12-month operations plan

Status: a plan, not a record. Nothing has been operated in production yet. SLO numbers are **proposals to be confirmed against measured data** from verification steps 8-12; they are not guarantees.

## SLOs (proposed)

| SLO | Target | Measured by |
|---|---|---|
| Call-control availability (answer/status callbacks answered correctly) | 99.5 % monthly | `mme_tel_plivo_callbacks_total`, synthetic probe on `/health/ready` |
| Call completion without platform-caused failure | 99 % | `mme_tel_calls_terminal_total` by outcome |
| AI turn latency p95 (end of caller speech -> first reply audio) | <= 4 s (revise after step 8) | `mme_tel_ai_turn_seconds` |
| AI turn error rate | < 5 % | `mme_tel_ai_turn_errors_total` |
| Orphan call (provider billing without platform state) | 0 tolerated beyond the grace window | sweeper + Plivo CDR comparison |
| Data loss | RPO per runbook §3 | restore drills |

## Cadence

| When | Activity |
|---|---|
| Daily | Review alerts; compare Plivo spend and call count to platform records (reconciliation); check backup job succeeded |
| Weekly | Review AI turn error reasons and p95; review rate-limit and signature-failure counts; sample 5 calls (recordings/transcripts per policy) for quality |
| Monthly | `pip-audit` + dependency update through the release checklist; backup **restore drill** with timing; capacity review vs. peak concurrency; cost review; access review (users, tokens) |
| Quarterly | Rotate secrets; failure drills (model down, Mongo restart, service restart under load); review SLOs and tune thresholds; test FreeSWITCH restart and module reload |
| Semi-annually | Review data retention (TTLs) against policy and law; review Plivo account limits (CPS, concurrency) |
| Annually | Full security review incl. TLS, firewall, third-party apps (retire `~/mme-plivo`); Python and OS upgrade plan; disaster-recovery exercise from backup on a clean host |

## Month-by-month outline

* **M1** Close the verification plan (steps 2-10); rotate exposed credentials; load alert rules; first restore drill with measured RTO.
* **M2** Soak and ramp (steps 11-12); fix whatever the real Plivo protocol capture showed; set SLOs from data; decide DID migration (separate approval).
* **M3** First production traffic on a small DID; daily reconciliation discipline; first cost report.
* **M4-M6** Gradual traffic increase gated by SLO burn; tune endpointing/barge-in on real call recordings; first quarterly drills; first secret rotation.
* **M7-M9** Capacity review; decide whether a second host/worker design is needed (single worker is a deliberate limit); evaluate model server scaling and cost.
* **M10-M12** Annual review items; dependency major-version upgrades in a staging venv first; revise this plan.

## Ownership (to fill in by the operator)

On-call owner, backup owner, Plivo account owner, model-server owner, approver for spend and DID changes. No owner is assumed here.
