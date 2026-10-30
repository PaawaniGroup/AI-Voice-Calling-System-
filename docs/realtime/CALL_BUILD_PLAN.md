# CALL BUILD plan (plan only: nothing here has been done; waits for the operator's "APPROVED: build")

Goal: a branch `callbuild` = `integration` + ONLY the acknowledgement path (and its generator/checker), plus the supervised-call scripts taught about `ack_enabled`. The rt transport, rt workers, probe tools, Pod launcher and the security design stay on `realtime`.

## 1. Worktree
- `git worktree add C:\Users\Admin\Desktop\MME_wt_call -b callbuild 314b168` (read-only check just made: the tip of `integration` is **314b168b0cefb788bd5d04f4bbf8b83d2664a61f**, "scripts: checklist checks flag dependencies ...; rollback reports whether the tenant is still armed"). Branch `callbuild` and the folder do not exist yet. `integration` has no commits since `realtime` branched from it, so the merge base is the same commit: nothing on `integration` moved.
- The uncommitted `tests/unit/test_start_service_sh.py` in `MME_wt_integ` is not part of the commit and is not touched.

## 2. What comes from `realtime` (and what does not)
| take | how | why |
|---|---|---|
| `app/ai/streaming/ack.py`, `tests/unit/test_ack.py`, `tests/unit/test_ack_turn.py` | file copy from `ebbb5ee` | new files, no overlap |
| ack hunks of `turn.py`, `playout.py`, `config.py`, `live.py`, `pipeline.py` | file copy from `ebbb5ee` (these five files are identical to `integration` apart from the ack hunks) | the diff of ebbb5ee on them is ack-only |
| ack hunks of `service.py`, `routes_platform.py`, `settings.py` | **by hand**, not a cherry-pick | ebbb5ee also carries `rt_pins` hunks whose context (`RT_KEYS`, the rt block of `put_config`, `ai_rt_kill`) does not exist on `integration` |
| `tools/realtime/gen_ack_clips.py`, `check_ack_clips.py`, `tests/unit/test_rt_ack_tools.py` | file copy from `7efb32a` | generator and checker |
| NOT taken | `app/ai/rt/*`, `deploy/rt_workers/*`, `tools/realtime/rt_rtt_probe.py`, `launch_probe_pod.py`, `test_rt_*` (except `test_rt_ack_tools.py`), `test_rt_clients.py`, STAGE2/CLOUD docs | stay on `realtime` |
`tools/realtime/` does not exist on `integration` (the Stage 1 tools are on `realtime`); the two ack tools import only `app.*`, so they run there; `tests/helpers.py` and `tests/fakeclock.py` exist there.
In the hand-applied `service.py` hunks: `ack_enabled` is added to `LATENCY_FLAGS`; `from app.ai.streaming.ack import AckSet`; the load of the set at call start. `ack_after_ms` arrives through `STREAM_KEYS` (config.py). `rt_*` keys are NOT present in `callbuild`, so `rt_transport` cannot be switched on there at all.

## 3. Scripts (in `callbuild`, from `integration`'s versions)
- `prepare_supervised_call_tenant.py`: new option `--with-ack` (absent = `ack_enabled` is NOT written; it stays listed as "not set"). With `--with-ack`: first read-only, it reads the clip directory setting (`Settings.ai_ack_dir`, i.e. the process environment `MME_TEL_AI_ACK_DIR`; `.env` is never opened) and loads it through `AckSet.load`; anything but `ok` refuses the option and prints the reason code only. If `ok`, `ack_enabled: True` joins the written flags (`ack_after_ms` is left alone: default 1500). Always listed, never written: `stt_raw_pcm`, `tts_keepalive` / `keepalive_enabled`, `rt_transport`, `faq_grounding`. The dry-run stays the default.
- `rollback_supervised_call_tenant.py`: `ack_enabled` (and `ack_after_ms`) added to `SUPERVISED_KEYS` and to the one-line unset; restored from the backup exactly like the other keys; the "still armed" report is unchanged.
- `pre_call_checklist.py`: new option `--expect-ack`. Without it, `ack_enabled` stays in MUST_BE_OFF (as today). With it: `ack_enabled` must be True through `get_config` (not dropped), `streaming_pipeline` must be on, and `AckSet.load(ai_ack_dir)` must return `ok` (reason code printed, no path content, no phrase); `ack_after_ms` printed (None = 1500). Added to MUST_BE_OFF: `tts_keepalive`, `rt_transport` (off = absent, empty, or every value "queue"). The note that `MME_TEL_AI_ACK_DIR` and `MME_TEL_AI_TRACE_EVIDENCE_PATH` live in the SERVICE environment (not visible from a script) stays.
- Tests: `tests/unit/test_tenant_key_scripts.py` extended on a throwaway database round trip: prepare without/with `--with-ack` (no write / write), refusal when the clip directory is missing or not neutral, rollback restores `ack_enabled`, checklist with and without `--expect-ack`, and that `stt_raw_pcm`, `tts_keepalive`, `rt_transport` fail the checklist when on.

## 4. Config allowlist keys and a survival test for each flag of the call
All six are in `AiService.get_config`'s allowlist through `LATENCY_FLAGS` (tuple in `app/ai/service.py`: `trace_evidence, stream_ramp, stt_raw_pcm, tts_adaptive_poll, glossary_correction, strict_grounding` + `ack_enabled`), and `ack_after_ms` through `STREAM_KEYS`. Existing survival test: `tests/unit/test_tenant_key_scripts.py::test_every_flag_the_prepare_script_sets_survives_get_config_into_the_runtime_config` (one test looping over every flag the prepare script sets: the failing flag is named in the message but all share one node id). Plan: split it into a parametrised test so each flag has its OWN node id and its own failure: `stream_ramp`, `tts_adaptive_poll`, `trace_evidence`, `glossary_correction`, `strict_grounding`, `ack_enabled` (+ `ack_after_ms`); the ack ones also have `test_ack.py::test_ack_enabled_is_off_by_default_and_survives_get_config_together_with_the_wait` and `test_put_config_validates_the_ack_keys`. No threshold or assertion is loosened by the split.

## 5. Conflicts expected
- `integration` vs `realtime` as branches: **none**; `integration` has no commit since the merge base, so `realtime` would fast-forward onto it. (The operator decides about any merge; I do none.)
- Taking only the ack work onto `callbuild`: clean for `turn.py`, `playout.py`, `config.py`, `live.py`, `pipeline.py` (identical to the base outside the ack hunks); **conflict by design if cherry-picked** in `service.py` (rt_pins / `rt_protocol` import / `put_config` rt block / `RT_KEYS` context), `routes_platform.py` (`rt_pins` and `rt_secret_env` lines next to the ack fields) and `settings.py` (the `ai_ack_dir` field sits beside `ai_rt_kill`, which `callbuild` does not have). Hence the hand application in section 2.
- Later (if the rt work is ever added to `callbuild`): the same three files, in the same places, in the other direction.

## 6. What the service start needs, and what must be restarted
- Code: the service must run from `MME_wt_call` (its own checkout), not from `MME_wt_integ`; that means stopping the running service (port 8800) and starting it from the new folder: a restart the operator approves.
- `MME_TEL_AI_ACK_DIR=/mnt/c/mme_ack_audio` in the service environment (read at process start; the clip set is loaded at each call start from that directory, so replacing a clip later needs no restart, changing the setting does).
- The `.env` of the service in the new worktree: the operator copies it (I never open it); the key files `/mnt/c/mme_keys/runpod_stt.txt`, `runpod_brain.txt`, `runpod_tts.txt` are read into the environment variables named by the tenant's `key_refs` at service start; `MME_TEL_AI_TRACE_EVIDENCE_PATH` for `trace_evidence`.
- Not restarted: Mongo, ngrok, the RunPod endpoints. Tenant flags (`ack_enabled`, the others) are read from Mongo at each call start through `get_config`: changing them needs no restart, but the code that understands `ack_enabled` does.
- Nothing is set, started or switched before "APPROVED: build" (and a separate approval for any restart).

## 7. Order of work after "APPROVED: build" (each test run needs its own "APPROVED: run")
1. create the worktree and branch; confirm the hash again. 2. copy/apply the ack work and the generator/checker. 3. update the three scripts and `test_tenant_key_scripts.py`. 4. compile and import checks (free). 5. ask for the run of: `test_ack.py`, `test_ack_turn.py`, `test_rt_ack_tools.py`, `test_tenant_key_scripts.py`, `test_streaming_turn.py`, `test_playout.py`, `test_key_refs_regression.py`. 6. commit in small steps (guard, secret scan zero hits, `--cached --stat`). 7. report; the operator decides about the service restart and `MME_TEL_AI_ACK_DIR`.
