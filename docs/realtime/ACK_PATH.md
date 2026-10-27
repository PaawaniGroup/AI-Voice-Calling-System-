# Acknowledgement path ("first sound": priority 1; flag `ack_enabled`, default OFF)

Status: ported offline from quality-work 9cdb32b (read-only) into this worktree: `app/ai/streaming/ack.py` (unchanged copy), the ack hunks of `turn.py`, `playout.py`, `config.py`, `live.py`,
`pipeline.py` (`conv.ack`), `service.py` (flag in the config allowlist + load at call start), `settings.py` (`ai_ack_dir`, env `MME_TEL_AI_ACK_DIR`), the API model (`ack_enabled`, `ack_after_ms`).
Not ported (not needed here): the keep-alive flag set of that commit. Tests: `test_ack.py` (phrase set, neutrality, loading, flag survives `get_config`, default 1500 ms), `test_ack_turn.py` (virtual time),
`test_rt_ack_tools.py`. All SIMULATED; NONE RUN YET (waiting for "APPROVED: run"). First sound is BLOCKED in `gates.json` until the clips exist and a MEASURED trace shows it.

Behaviour (what the tests assert): played only when no real clause has audio `ack_after_ms` (default 1500) after the speech-to-text final; on the SAME playout timeline, so it never overlaps real
speech (a clause that becomes ready meanwhile waits for it, at most the phrase's length, about 1 s); cancelled the moment the first real clause is handed over; cleared by barge-in like any audio;
never part of `clause_texts`, so never in the history, the reply or the "heard" text; it does not make a turn a success when no real audio follows; skipped silently when the directory or any file is
missing, unreadable, of the wrong length (0.3-2.0 s) or has a phrase on the agree/disagree deny-list (the whole set is refused). "First audio" in the trace means the first REAL frame; the phrase's
frame is `t_ack_first_frame`. Approved phrases: Hindi "एक सेकंड।", "एक पल।", "बस एक क्षण।"; English "One moment.", "Just a second.".

## REQUEST: generate the five clips (chargeable, NOT approved, NOT run)
- Action: `RUNPOD_TTS_KEY=<content of /mnt/c/mme_keys/runpod_tts.txt, in this process only> python tools/realtime/gen_ack_clips.py --out-dir <folder you name, outside every repo> --cap-usd 0.15 --approved "<id>"`.
  1 warm-up voice job (wakes the endpoint, audio discarded) + 5 voice jobs. Active workers are NOT changed. The voice endpoint must be reachable; it will cold-start (41-52 s MEASURED earlier).
- Cost (ESTIMATE; the endpoint stays billed for its idle timeout, 300 s, after the last job): about (50 s cold + 6 jobs x 3 s + 300 s idle) = 370 s: $0.070 at $0.00019/s, $0.111 at $0.0003/s. If the endpoint is
  already warm: about 320 s: $0.061 / $0.096; the jobs alone (6 x 3 s = 18 s): $0.003 / $0.005. Cap $0.15 total (projection counts cold start and idle tail at the higher price: 380 s x 0.0003 = $0.114).
- Stops: the projected cost reaches the cap, the first 401/403, 3 consecutive errors. Output is numbers only.
- Then (free): `python tools/realtime/check_ack_clips.py <folder>` (sample rate fixed by the format, duration 0.3-2.0 s, clipping share <= 0.001, no silent clip, loads through `AckSet.load`);
  you listen to the five clips yourself (I cannot judge a voice), then set `MME_TEL_AI_ACK_DIR`; the service must be restarted to read the setting (a restart is yours to approve).
- Undo: delete the folder; the flag stays OFF; nothing else changed.
- Minimum you must do: name the output folder (outside every repo) and reply `APPROVED: run gen_ack_clips.py once, cap $0.15, key file /mnt/c/mme_keys/runpod_tts.txt, out-dir <folder>`.
