# Deep Inbound Input Pipeline — Root-Cause Audit
## Plivo audio frame → VAD → STT → transcript → LLM request

> **Repository:** `MME_Telephony`  **Branch:** `realtime-latency`  **Mode:** READ-ONLY diagnosis. No application file was modified; no live call, no RunPod job, no provider request.
> **Second, independent audit.** Evidence below was rebuilt from the current working tree. `docs/DEEP_STT_AUDIO_CONVERSATION_AUDIT.md` was read; where this audit agrees or disagrees with it, that is stated.
> **Evidence labels:** **CONFIRMED-RUN** = reproduced by an offline run in this audit · **CONFIRMED-CODE** = read in the current source, not executed · **SUSPECTED** = plausible, needs evidence · **UNVERIFIED** = cannot be established from the repo.
> **Fixture honesty:** the repository has **no recorded caller audio and no Hindi/Hinglish WAV fixtures**. All VAD scenarios use *synthetic energy envelopes* (a 220 Hz tone at a chosen RMS stands in for "voiced", silence for "pause"). They prove the **segmentation logic**, not recognition quality. No scenario says anything about what Whisper would output.

---

## 1. Executive summary

| Question | Verdict |
|---|---|
| Is the inbound pipeline functioning correctly? | **No.** Transport and codec are sound; the speech-capture/segmentation layer in `serve_loop` and the cancel/turn-boundary handling lose caller audio and caller text. |
| Is audio being lost? | **Yes, confirmed.** (a) the first ~180 ms of **every** utterance; (b) utterances shorter than 200 ms **entirely**; (c) a caller who keeps talking while the reply starts loses up to the whole buffered portion; (d) speech at 450–1000 RMS during playback is discarded; (e) 200 ms after every 6 s cap. |
| Is the audio format correct at every boundary? | **Yes (CONFIRMED-RUN for decode/WAV, CONFIRMED-CODE for worker resample).** μ-law 8 kHz → signed LE PCM16 8 kHz mono → WAV header 8000 Hz (true rate; nothing is mislabelled) → worker polyphase 8→16 kHz. No clipping, no gain stage, no stereo. The worker resample was **not executed** (numpy/scipy absent locally). |
| Is VAD splitting speech incorrectly? | **Yes by design:** fixed 700 ms silence splits at natural pauses (800 ms and 750 ms pauses → 2 STT jobs, 2 LLM turns; 650 ms did not split); fixed 6 s cap splits long sentences (7.5 s → 6.0 s + 1.84 s). Whether real callers pause ≥700 ms that often is **UNVERIFIED** (no call corpus). |
| Is pre-roll functioning correctly? | **There is no pre-roll.** `grep -i pre_?roll` over all `.py/.md/.js` finds nothing outside the previous audit. The previous audit's D-1 is **confirmed**, and is worse than "onset clipping": see F2. |
| Is STT receiving complete audio? | **No** — it receives what the VAD kept (above), plus an untrimmed 700 ms of trailing silence (a 400 ms "haan" arrives as 220 ms speech + 700 ms silence = 920 ms). |
| Is transcript propagation correct? | **Partly.** Text is carried verbatim to the LLM request when it is not touched. Confirmed corruptions: `deduplicate_repetitions` rewrites digits (`98 98 76 76` → `98 76`, `rank 11 11` → `rank 11`); glossary is off by default and has no `result/rank/laakh/jayi mens` mappings; a cancelled turn **erases the caller's utterance from LLM history**. |
| Asynchronous race conditions? | **Yes, confirmed** (see §9): barge-in replaces the buffered caller audio; reply playback starting mid-utterance creates a hole; the stale reply is not cancelled when the caller has already spoken again; a full queue silently drops utterances. **No cross-call / cross-session contamination found.** |
| **Single most important confirmed root cause** | **The speech-capture gate in `serve_loop` (`app/ai/ws.py:376-392`): audio is buffered only after 200 ms of *contiguous* ≥450-RMS audio, with no pre-roll.** It affects 100 % of utterances, silently deletes words <200 ms ("ji", "ha", "ok"), and the same gate is re-armed after every flush. |
| **Fix first** | (1) Pre-roll ring buffer + remove the contiguity requirement; (2) stop replacing/discarding the buffer at barge-in and stop dropping sub-barge speech during playback; (3) keep the caller's text in LLM history when a turn is cancelled. Awaiting authorization; nothing implemented. |

Where I disagree with the previous audit: its Session-2 Turn-4 analysis (transcript reached the LLM; LLM repeated itself) is consistent with the code I read, but I did **not** re-verify the RunPod/LLM-side claims (greedy sampling, anti-repetition bypass). They are outside the input path and are not re-endorsed here.

---

## 2. Actual inbound architecture

```mermaid
sequenceDiagram
    autonumber
    participant P as Plivo (WS client)
    participant H as handle_plivo_media (ws.py:513)
    participant IO as PlivoIO.next_item (ws.py:139)
    participant L as serve_loop (ws.py:209) VAD + buffer
    participant Q as utterances Queue(maxsize=3)
    participant W as worker() (ws.py:246)
    participant C as Conversation.stream_turn (pipeline.py:469)
    participant S as ModelClients.transcribe (clients.py:362)
    participant R as RunPod STT worker (handler.py)
    participant A as AiService (service.py)
    participant B as Brain / LLM

    P->>H: HTTP upgrade (v3 signature + nonce)
    P->>H: {"event":"start", mediaFormat: audio/x-mulaw 8000, ticket}
    H->>H: validate ticket/call/session, ai.start + attach_media
    loop every ~20 ms (160 B mu-law)
        P->>IO: {"event":"media", track, chunk, timestamp, payload(b64)}
        IO->>IO: json -> track filter -> b64 strict decode -> chunk>last? -> ulaw2lin
        IO->>L: ("audio", 320 B PCM16 8 kHz)
        L->>L: rms(frame) vs 450 / 1000 ; speech_ms / silence_ms ; buf += frame only if in_speech
    end
    L->>Q: flush_if_due: (pcm, t_speech_end, t_endpoint) when silence>=700 ms or buf>=6 s
    Q->>W: get() (strictly sequential, one worker per call)
    W->>C: stream_turn(pcm, 8000)
    C->>S: transcribe(pcm, 8000, language)
    S->>R: /run {audio_b64 = WAV@8000, language|None, prompt = vocabulary}
    R->>R: soundfile.read -> resample_poly 8k->16k -> Whisper
    R-->>S: {"text": ...}
    S-->>C: {"text","language"}
    C->>C: deduplicate_repetitions -> (optional) glossary -> reply_mode
    C-->>W: TurnChunk(heard) (Phase A)
    W->>A: record_user_utterance (dashboard / Mongo)
    C->>B: messages += user(heard) ; chat_stream
    B-->>C: tokens -> clauses -> TTS -> TurnChunk(audio)
    C-->>W: audio chunks -> io.play -> Plivo playAudio
    Note over L,W: Barge-in: L sets conversation.cancelled, sends clearAudio, REPLACES buf with the 1 triggering frame
```

No component in this diagram is assumed; each box is a function read in the current tree.

---

## 3. Complete file / function trace (31 stages)

`sz` = typical size. "Loss?" = can data be lost / duplicated / truncated / reordered here.

| # | Stage | File : function | In → Out | sz | Config / behaviour | Loss? |
|---|---|---|---|---|---|---|
| 1 | Answer webhook | `app/services/plivo_calls.py:47-57` | Plivo → XML `<Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">` | – | no `audioTrack` attr (Plivo default = inbound) | – |
| 2 | Call acceptance / init | `ws.py:513-642 handle_plivo_media` | signed handshake → ticket → call lookup → `ai.start/attach_media` | – | `start` must arrive ≤5 s; format must be `audio/x-mulaw` 8000 | closes on mismatch |
| 3 | WS connect | `ws.py:523-531` | nonce replay check, capacity cap | – | `plivo_stream_max_concurrent=10` | reject |
| 4 | Message parse | `ws.py:139-185 PlivoIO.next_item` | WS text → event | ≤64 KB | `wait_for(ws.receive(), 0.05)`; non-JSON → `bad++`, skipped silently | malformed ignored (counted) |
| 5 | Media validation | `ws.py:158-167` | `track` must be inbound; `chunk` int; payload str | – | missing `track` ⇒ inbound; missing `chunk` ⇒ last+1 | bad ⇒ dropped (counted) |
| 6 | Base64 decode | `codec.py:52 decode_media_payload` | str → bytes | 160 B / 20 ms | `validate=True`; empty / >4000 B rejected | rejected frames dropped |
| 7 | Payload extraction | same | μ-law bytes | 160 B | no header assumptions | – |
| 8 | Ordering | `ws.py:168-171` | `chunk <= last_chunk` ⇒ drop | – | **drop-only, never reorder; gap not detected** | **late frame lost; counted as "dup"** |
| 9 | `sequenceNumber` | – | **not read anywhere** | – | – | no validation |
| 10 | `timestamp` | – | **not read anywhere** | – | – | no jitter/gap info |
| 11 | Buffering | `ws.py:216,225` | `st["buf"]` bytearray; `utterances` Queue(3) | ≤96 000 B (6 s) | buf grows only while `in_speech` | see F2/F3 |
| 12 | Codec | `codec.py:30 mulaw_to_pcm16` | `audioop.ulaw2lin(data,2)` | 160 B → 320 B | signed LE; range ±32124 | none |
| 13 | Sample-rate conv. | **none inbound** | 8 kHz stays 8 kHz | – | resampling only on outbound TTS (`resample_pcm16`) | – |
| 14 | Channels | – | mono throughout | – | – | – |
| 15 | PCM frame | `ws.py:349 fm` | duration from byte length | 20 ms | `len*500//rate` ⇒ correct for any frame size | – |
| 16 | VAD detect | `ws.py:378` / `clients.py:128 rms` | RMS ≥ `VAD_RMS=450` | – | **fixed absolute threshold, no noise floor, no adaptation** | line noise >450 ⇒ false speech |
| 17 | Onset | `ws.py:381-384` | `speech_ms ≥ MIN_SPEECH_MS=200` | – | **contiguous**: any quiet frame resets `speech_ms=0` (`:387-388`) | **first 180 ms dropped; <200 ms dropped entirely** |
| 18 | Continuation | `ws.py:390-392` | `buf += frame` | – | – | – |
| 19 | Silence | `ws.py:386, 338-344` | `silence_ms += fm` (+50 per idle tick) | – | `SILENCE_MS=700` | splits at ≥700 ms pauses |
| 20 | End-of-utterance | `ws.py:299-308 flush_if_due` | tuple to queue | – | endpoint when silence≥700 ms | trailing 700 ms silence **kept** in audio |
| 21 | Max duration | `ws.py:300` | `len(buf) ≥ 96 000 B` | 6.0 s | flush mid-speech, `in_speech=False` | next 200 ms dropped; fragment becomes its own turn |
| 22 | Final assembly | `ws.py:301` | `bytes(buf)` | ≤96 000 B | – | none |
| 23 | STT request | `clients.py:362-377` | PCM → `pcm16_to_wav(pcm, 8000)` → b64 → `{audio_b64, audio, audio_base64, language, prompt}` | ~1.3× | 3 duplicate b64 copies of the same audio in one body (≈3× upload) | – |
| 24 | STT submit | `clients.py:246-280 _runpod_call` | `/run` → poll `/status` | – | timeout `ai_request_timeout_s=15 s`; **no retry**; cancel on CancelledError | timeout ⇒ turn lost |
| 25 | STT response | `clients.py:378-387` | `output.transcription\|text` | – | worker v2 returns `text` | – |
| 26 | Partial vs final | – | **no partials exist**; every STT result is final | – | – | – |
| 27 | Normalise | `pipeline.py:365-378, 488` | `deduplicate_repetitions` | – | regex collapses repeated ≤4-word phrases | **digits corrupted (F8)** |
| 28 | Glossary / script | `pipeline.py:492-494`; `glossary_correction.py` | optional | – | **off unless tenant `glossary_correction`** | sparse map (F9) |
| 29 | Publish | `pipeline.py:508`, `ws.py:260-262`, `service.py:348 record_user_utterance` | Phase A `TurnChunk(heard)` | – | `ai_store_transcripts` gates text | turn number off-by-one vs Phase B (F11) |
| 30 | LLM request | `pipeline.py:518-536, clients.py:408-458` | `messages += {"user": heard}` (+RAG context, +per-turn hint copy) | – | RunPod: ChatML `prompt` + `messages` both sent | cancelled turn ⇒ user msg deleted (F3) |
| 31 | Back to listening | `ws.py:288-297`; `pipeline.py:472` | `st["playing"]` ends by wall clock `play_until` | – | `cancelled=False` reset at next `stream_turn` start | flag race (theoretical) |

---

## 4. Audio-format boundary table

| Stage | Encoding | Rate | Ch | Sample format | Evidence |
|---|---|---:|---:|---|---|
| Plivo incoming audio | G.711 μ-law (`audio/x-mulaw;rate=8000`) | 8000 | 1 | 8-bit companded | XML `plivo_calls.py:55`; start-event check `ws.py:596` rejects anything else |
| WebSocket decoded payload | μ-law bytes | 8000 | 1 | u8 | RUN: 160 B per 20 ms |
| Internal PCM / VAD input | linear PCM | 8000 | 1 | signed int16 LE | RUN: `ulaw2lin` 160 B → 320 B; full-scale decode −32124 (no clipping possible); 20.0 ms/frame |
| STT WAV (client) | RIFF PCM | **8000** (header = true rate) | 1 | int16 | RUN: header rate 8000, 2.0 s of PCM ⇒ 16000 frames, payload bytes == PCM bytes |
| Worker load | `sf.read` → float32 | 8000 | mono | −1..1 | CODE `handler.py:74-95` |
| Whisper input | float32 array | **16000** | 1 | float32 | CODE: `resample_poly(audio,16000,8000)` — **not executed here** (numpy/scipy not installed) |

Checks requested in the brief: incorrect rate declaration **no**; missing/double resampling **no** (single 8→16 in worker); μ-law decode **correct** (G.711 vector 0xFF↔0 passes in the existing test suite and in this audit); signedness/endianness **correct**; WAV header **correct**; duration maths **correct** (`fm` from byte length); stereo **n/a**; clipping **none**; amplitude normalization **none anywhere** (neither app nor worker — fine for Whisper, but it means the absolute VAD thresholds see raw handset levels); μ-law round-trip SNR 39.8 dB on a 300 Hz sine (telephone-band quantisation, expected); truncated final frame **not possible** (flush on frame boundary); concatenation **correct** (bytearray append). Impact on Hindi/Hinglish: none from format. 8 kHz band-limiting is inherent to PSTN and is not a software defect.

---

## 5. WebSocket & frame handling (CONFIRMED-RUN via `PlivoIO.next_item`)

| Case | Result |
|---|---|
| in-order chunks | all accepted |
| chunks 1,2,**5**,6 (3,4 lost) | all accepted; **gap not detected, no silence inserted**, no counter ⇒ audio is silently time-compressed |
| chunk 3 arriving after 4 | dropped, counted as `dup` (**misclassified; permanently lost**) |
| duplicate chunk | dropped, `dup++` ✔ |
| `track=outbound` | skipped ✔ |
| bad base64 / empty / 4001 B | `bad++`, skipped ✔ (counted, not logged per-frame) |
| 80 ms (640 B) frame | accepted; duration computed correctly ✔ |
| no `chunk` field | accepted, auto-numbered |
| chunk numbering restarting at 0 | 0,1 dropped as dup until it exceeds the old max (**SUSPECTED** trigger; no evidence Plivo does this) |
| garbage JSON | `bad++`, skipped ✔ |
| 2nd `start` / `stop` | `end` ✔ |

Concurrency of the receive loop: one `await next_item()` at a time, frames processed strictly in arrival order, no per-frame tasks except `wait_for`'s internal cancel on the 50 ms poll (cancel-safe with Starlette's queue `get`; **SUSPECTED-benign**). `sequenceNumber` and `timestamp` are never read (no jitter, gap or ordering metric). Queues: `utterances` is bounded (3); there is no unbounded queue on the input path. `PLIVO_MAX_MESSAGE=64 KB` terminates the stream if exceeded (`oversize` ⇒ `end`).

---

## 6. Codec & resampling analysis
Covered in §4. One additional point: the request body carries the same base64 WAV **three times** (`audio_b64`, `audio`, `audio_base64`, `clients.py:371-373`) — ≈3× upload per STT job for compatibility with unknown handlers (P2).

---

## 7. VAD state machine (actual)

```
            loud(≥450) & speech_ms<200            loud & speech_ms≥200
 LISTENING ───────────────────────► CANDIDATE ───────────────────────► IN_SPEECH(buf+=frame)
   ▲  ▲        (frame NOT buffered)   │ any quiet frame                     │ quiet: silence_ms+=20
   │  └───────────────────────────────┘ speech_ms=0 (restart gate)          │ loud : silence_ms=0
   │                                                                        ▼
   │                           silence_ms≥700  OR  len(buf)≥96 000 B   FINALIZE
   │                              flush: buf→queue, state reset ◄────────────┘
   └── worker: STT → LLM → TTS → play (st["playing"]=True until wall-clock play_until)
        while PLAYING: inbound frames take a DIFFERENT branch (ws.py:357-375):
           frame ≥ max(450×2.2, 1000)=1000 RMS for 200 ms ⇒ BARGE-IN: cancel(), clearAudio, in_speech=True, buf=bytearray(frame)
           frame < 1000 ⇒ speech_ms=0, silence_ms+=20, frame DISCARDED
```
There is no explicit "cancel/hangup" state: socket end cancels the worker (`finally`, bounded 2 s + 1 s). No noise-floor estimate, no adaptive threshold, `SILENCE_MS`/`MIN_SPEECH_MS`/`MAX_UTTERANCE_MS` are module constants (`ws.py:41-49`); `frame_ms` 20.

### Scenario results (CONFIRMED-RUN; real `serve_loop`, fake IO, virtual clock, mock conversation)
Frames are 20 ms; "kept" = ≥450-RMS audio actually present in the PCM handed to `stream_turn`. Fixture = energy envelope only.

| Scenario (synthetic envelope) | Frames in | Utterances / STT jobs | Audio to STT (ms) | Speech in → kept (ms) |
|---|---:|---:|---|---|
| A  "haan" 400 ms | 90 | 1 | 920 | 400 → **220** |
| A2 "haan" 240 ms | 82 | 1 | 760 | 240 → **60** |
| A3 short word 180 ms | 79 | **0** | – | 180 → **0 (no job, no event, no error)** |
| B  "haan" 400 / pause **800** / "…fees…" 1500 | 205 | **2** | 920, 2020 | 1900 → 1540 |
| B2 same, pause **650** | 197 | 1 | 3060 | 1900 → 1720 |
| C  2 s / pause **750** / 2 s | 307 | **2** | 2520, 2520 | 4000 → 3640 |
| D  0.5 s / 600 / 0.5 s / **800** / 1.2 s | 250 | **2** | 2120, 1720 | 2200 → 1840 |
| E  continuous 7.5 s | 445 | **2** | **6000**, 1840 | 7500 → 7140 (second job is a 1.1 s fragment) |
| E2 continuous 13 s | 720 | 3 | 6000, 6000, 1160 | 13000 → 12460 |
| F  noise 200 RMS 3 s, then 600 RMS (line-noise level) 1 s, then speech | 425 | 1 | 3020 | speech kept; noise <450 ignored ✔; a sustained 600-RMS hiss would be treated as speech |
| F2 noise 200 RMS 5 s then speech | 425 | 1 | 2020 | 1500 → 1320 |
| I  onset with one 20 ms sub-threshold dip at 100 ms | 151 | 1 | 2020 | 1600 → 1320 (gate restarted) |
| I2 dips every 120 ms in first 400 ms | 163 | 1 | 2020 | 1800 → 1320 |
| G  caller talks over a 3 s reply at 2500 RMS | 300 | 2 | 1520, 2020 | barge-in fired once; 3 s reply cancelled |
| G2 caller talks over the reply at **800 RMS** | 300 | **1** | 1520 | **1500 ms of caller speech discarded, no barge, no job** |
| J  caller resumes at **600 RMS** 300 ms after endpoint; reply starts 1.2 s later | 460 | 2 | 1520, **1060** | 3000 ms talk → **1040 kept; 2140 lost** (hole in the middle) |
| K  same but 2500 RMS | 460 | 2 | 1520, 2480 | 3000 ms talk → 1780 kept: **~1040 ms discarded when barge-in replaced the buffer** |
| L  2nd utterance finished while turn 1 still thinking (6 s) | 585 | 2 | 1520, 2020 | reply 1 played at t=7.9 s **after** the caller had already spoken again; no cancel |
| M  five 600 ms utterances while one turn takes 30 s | 560 | queue=3 | – | `inbound_overflow` **twice ⇒ 2 caller utterances silently dropped** |

Scenario G is the existing "caller interruption" design working as intended; G2/J/K are its failure modes. For scenarios A–E the exact timestamps are in the reproduction script (§16); speech onset is always `first loud frame + 180 ms` in buffer terms.

---

## 8. Buffer & frame accounting
What exists: `PlivoIO.counters` = `media, bytes, dup, bad, dtmf, cleared, oversize`, written **once**, to the `stream.ended` event (`ws.py:671`).
What cannot be computed today: frames discarded by VAD (pre-gate, playback branch, barge-in replace, queue overflow), audio ms received vs ms sent to STT, gaps, per-utterance STT job count (only the per-call `ModelClients.job_counts["stt"]`, aggregated), frames dropped during cancel. The accounting identity `received = buffered + pre-gate-dropped + playback-dropped + replaced + overflow-dropped` cannot be checked because four of its five terms are not counted.

---

## 9. Async concurrency & turn boundaries

**Verified (CONFIRMED-RUN):**
1. **Barge-in replaces buffered caller speech** — `ws.py:371-372` `st["buf"] = bytearray(frame)` (K: ~1040 ms discarded). Reachable whenever the caller is mid-sentence when the reply's first audio is handed to the transport — plausible given the measured 9–12 s voice-to-voice in the previous report.
2. **Playback starting mid-utterance creates a hole** — frames between 450 and 1000 RMS skip `buf` (`ws.py:373-375`); `flush_if_due` is not evaluated in that branch, so the utterance is flushed later with the middle missing (J).
3. **Stale reply is never cancelled by newer caller speech** unless it is ≥1000 RMS during playback; a fully spoken follow-up utterance is queued and the earlier reply still plays (L).
4. **Queue overflow silently drops caller utterances** (`ws.py:305-308`, M); reported only as `io.error("inbound_overflow")`, which for Plivo is a log line (`ws.py:187-188`).
5. **Cancel erases the caller's words from LLM history** — `pipeline.py:684` (`del self.messages[mark:]` on cancel mid-LLM) and `:821/:827`; a `break` at first audio leaves a **dangling `user` message with no assistant message**, and the next request carries `user,user` (CONFIRMED-RUN, probe: roles after cancel = `['system']`; after break = `['system','user']`; next LLM roles = `['system','user','user']`). `self.turns` is not appended in either case, so the anti-repetition guard and dashboard see a stale last turn.

**Verified absent (CONFIRMED-CODE):** STT submitted twice for the same audio — no (single call site `pipeline.py:484`, no retry); one transcript processed twice — no (sequential worker); late STT overwriting a newer turn — no (the await is in-line in the single worker); another call's audio entering this session — no: `st`, `utterances`, worker, `PlivoIO`, `Runtime`, `Conversation` are all per-`serve_loop`/session closures; `_plivo_streaming` blocks a second stream per session; `turn_trace` uses a ContextVar and tasks copy their context at creation. Pre-roll inheritance — impossible (no pre-roll). Stale VAD state across turns — `flush` resets `buf/in_speech/speech_ms/silence_ms` but **not** `playing/play_until`, which is intended.

**Theoretical (not reproduced):** `stream_turn` resets `self.cancelled=False` at start (`pipeline.py:472`); a barge-in landing between `utterances.get()` and that line would be lost. `cancel()` cancels only TTS tasks, not an in-flight STT (barge-in can only occur during playback, so STT is normally finished). `"idle"` ticks add 50 ms of silence even if frames are merely delayed.

**Correlation IDs:** `call_id`, `session_id` exist on `Runtime`; `trace_id` is minted per utterance (`TurnTrace`); **no STT job id, utterance id or buffer id is attached to the STT request or log**, and `turn_id_val` falls back to `len(turns)+1` without a trace. Nothing is mis-correlated today, but it cannot be proven from logs.

---

## 10. STT client & worker

* Client (`clients.py:362-405`): WAV @ true 8000 Hz; `language` = `None` when the tenant default is `"auto"` (`service.py:67`), `"hi"` for `hinglish`/`hi`; `prompt` = domain vocabulary on every call (including 220 ms clips).
* Worker in repo (`tools/stt_worker_v2/handler.py`, **untracked; deployment UNVERIFIED** — `scripts/save_confirmed_models.py` refers to an endpoint named `mme_stt_model` whose handler is not in the repo):
  * model `Oriserve/Whisper-Hindi2Hinglish-Prime` from env `MODEL_ID`; **`MODEL_REVISION` is defined (`:22`) but never passed to `from_pretrained`** ⇒ weights unpinned (CONFIRMED-CODE).
  * **ignores the request's `language`**; decodes with env `WHISPER_LANGUAGE`, **default `"en"`** (`:23, :131-132`). If this handler is what runs and the env is unset, Hindi speech is decoded under an English language token. Whether the live worker does this is **UNVERIFIED**; the previous audit asserted `language:"hi"` reached the worker, but this handler would not honour it. Needs the live `health` task output (`language` field) — not run.
  * task `transcribe`, greedy (`NUM_BEAMS=1`), `prompt_ids` built from an English/Roman vocabulary string on every request ⇒ **SUSPECTED** hallucination/echo bias on very short clips.
  * audio ≤30 s handled by Whisper's single window; no chunking; no truncation (our cap is 6 s).
  * single job per request; RunPod retry semantics are the platform's and **UNVERIFIED**.
* Timeouts/cancel: 15 s default; on timeout the job is cancelled and `AiTimeout` raised ⇒ `stt_timeout` turn error. `chunk.error` ⇒ `m.AI_TURN_ERRORS`, `io.error` (a log line for Plivo) and `break` ⇒ **the caller hears silence and the utterance is gone** (F7). Partial transcripts cannot be produced (non-streaming).

---

## 11. Transcript propagation (CONFIRMED-RUN, mocked STT/LLM)
Input transcript `Mera JEE Main result aa chuka hai, meri rank ek lakh hai.` (what an ideal STT would return; real STT output was **not** obtained):

| Stage | Result |
|---|---|
| Raw STT → `deduplicate_repetitions` | unchanged |
| Glossary (on/off) | unchanged (`JEE Main`, `result`, `rank`, `ek lakh` all preserved) |
| `reply_mode` / hint | hint appended only to a **copy**; history untouched |
| Dashboard `ai.heard` | identical text (when `ai_store_transcripts`) |
| LLM user message | identical + `[Instruction: …]` suffix when `script_enforcement` |

Where information **does** change:
* `deduplicate_repetitions` (`pipeline.py:365-378`): `meri rank ek ek lakh hai`→`ek lakh` (benign); `98 98 76 76`→`98 76`; `rank 11 11`→`rank 11`; `nahi nahi mujhe nahi chahiye`→`nahi mujhe nahi chahiye` (emphatic negation weakened). It runs **always**, regardless of any flag. (Devanagari repeats are *not* collapsed because `\b`/`\w` split on combining marks — inconsistent.)
* Hypothetical Hindi2Hinglish-style phonetic strings (the previous audit observed `rijalt`, `raink`, `jayi mens`): `mera jee main rijalt aa chuka hai meri raink ek laakh hai` and `mera jayi mens result…` pass through **unchanged**; only `je ee main` is mapped. These strings are illustrative, not captured transcripts.
* Cancellation removes the caller's text from the LLM history (F3).
No semantic-understanding claim is made from any spelling normalisation.

---

## 12. Latency / measurement gaps
Available: `t_speech_end` (wall time of last loud frame), `t_endpoint`, `endpoint_silence_ms`, `t_stt_sent/final`, `t_llm_*`, `t_tts_*`, `t_first_frame_to_plivo`, RunPod `delayTime/executionTime`.
Missing: frame arrival delay / jitter (`timestamp` unused), receive+decode time, buffer wait (partly = silence window), pre-roll, utterance **audio duration**, assembly time, STT queue time on the app side (RunPod delay only), normalisation time, transcript→LLM handoff, VAD-dropped ms, gap count.
Smallest additions (instrumentation only, none made): in `serve_loop` four integers per utterance (`pre_gate_dropped_ms`, `playback_dropped_ms`, `replaced_ms`, `utterance_ms`/`trailing_silence_ms`) copied into `TurnTrace.meta` at `flush_if_due`; in `PlivoIO` `gap_frames` (chunk jump) and `late_frames` (separate from `dup`); an `utterance_id` passed to `transcribe` and logged with RunPod's job id; counters exported per stream not only at end.

---

## 13. Confirmed root causes

| ID | Pri | Finding | Evidence / location | Reproduction | Impact | Confidence |
|---|---|---|---|---|---|---|
| **F2** | **P0** | No pre-roll + 200 ms **contiguous** gate: first 180 ms of every utterance dropped, utterances <200 ms deleted with no job/event, gate restarts on any sub-threshold frame | `ws.py:378-392` | A, A2, A3, I, I2 | Word onsets (Hindi plosives, "toh/kitni/batao") clipped; "ji/ha/ok" vanish; after the 6 s cap another 200 ms is lost (E) | CONFIRMED-RUN |
| **F1** | **P0** | Barge-in replaces the buffer; sub-barge speech during playback discarded; hole mid-utterance | `ws.py:357-375` (esp. `:372`) | K, J, G2 | Up to the whole caller phrase lost when the reply starts while they are still talking | CONFIRMED-RUN |
| **F3** | **P0** | Cancel/break loses caller's turn from LLM history, leaves dangling `user`, skips `self.turns` | `pipeline.py:684, 821-832`; `ws.py:257-258` | probe3 | LLM never sees what the caller said before interrupting; consecutive `user` messages | CONFIRMED-RUN |
| **F6** | **P1** | Queue is FIFO with drop-oldest at 3; stale reply plays after newer speech; overflow silent | `ws.py:216, 305-308`, `187-188` | L, M | Wrong-order conversation; lost utterances under slow STT/LLM | CONFIRMED-RUN |
| **F4** | **P1** | Fixed 700 ms silence endpoint | `ws.py:44` | B, C, D | 800/750 ms pauses ⇒ 2 STT jobs + 2 LLM replies for one thought | CONFIRMED-RUN (logic); real pause rates UNVERIFIED |
| **F5** | **P1** | 6 s hard cap splits mid-speech (+200 ms hole) | `ws.py:47, 300` | E, E2 | 7.5 s sentence ⇒ 6.0 s + 1.1 s fragment answered as a separate turn | CONFIRMED-RUN |
| **F7** | **P1** | STT timeout/empty/error ⇒ silent drop, no retry, no "please repeat" | `ws.py:272-277`; `ws.py:187`; `pipeline.py:489-490`; `clients.py` | code | Caller hears nothing; intent unrecoverable | CONFIRMED-CODE |
| **F8** | **P1** | `deduplicate_repetitions` alters digits/emphasis | `pipeline.py:365-378, 488` | probe3 | Phone numbers, ranks, percentages dictated with repeated groups corrupted | CONFIRMED-RUN |
| **F9** | **P1** | Glossary default-off, no `result/rank/laakh/jayi mens` forms; `NEVER_REWRITE` constant unused | `pipeline.py:385,492`; `glossary_correction.py:12-21, 38` | probe3 | Phonetic Roman spellings reach LLM verbatim | CONFIRMED-RUN |
| **F12** | P1/P2 | Untrimmed trailing 700 ms silence in every STT clip | `ws.py:300-301` | A (220 ms speech in 920 ms) | Short words are ~75 % silence ⇒ **SUSPECTED** hallucination ("haan haan…" that `deduplicate` was written to hide) | CONFIRMED-RUN (ratio); effect SUSPECTED |

## 14. Suspected / unverified

| ID | Pri | Item | Why not confirmed |
|---|---|---|---|
| S1 | P1 | Worker decodes with `WHISPER_LANGUAGE` default `en`, ignores request language | handler is untracked; live worker's env unknown — run the `health` task |
| S2 | P1 | Weights unpinned (`MODEL_REVISION` unused) | confirmed in code; whether a revision matters is unknown |
| S3 | P1 | English `prompt_ids` on every call biases/echoes on short clips | needs A/B on real audio |
| S4 | P2 | Absolute VAD threshold 450 mis-fires on loud-line or quiet-handset callers | no real level distribution in repo |
| S5 | P2 | Plivo `chunk` restart / out-of-order delivery frequency | no Plivo capture |
| S6 | P2 | `cancelled=False` reset swallows a barge-in landing between dequeue and `stream_turn` | needs scheduler-level test |
| F11 | P2 | `record_user_utterance` numbers the turn `len(turns)+1`, `record_turn` `len(turns)` (`service.py` ≈ l.351 / 389) ⇒ same turn shows two numbers on the dashboard | CONFIRMED-CODE, not run (needs Mongo) |
| F10 | P2 | `PlivoIO` gap/late/restart handling, `timestamp`/`sequenceNumber` unused; 3× duplicated base64; per-frame `wait_for` task | efficiency/observability only |

---

## 15. Prioritised remediation plan (proposal — nothing applied)

**P0**
1. `serve_loop`: keep a ring of the last N (≈300 ms = 15) pre-gate frames and prepend on onset; make the gate tolerant (e.g. count loud frames in a sliding window, not strictly contiguous); do not reset the gate on a single quiet frame; after a flush at the 6 s cap, **continue** in speech (carry pre-roll) instead of re-arming the 200 ms gate.
2. Barge-in: **extend** `buf` with the pre-barge frames instead of `bytearray(frame)`; during playback keep (not discard) loud-above-VAD frames in a side ring so that when playback ends or barge fires they are recovered; evaluate `flush_if_due` in both branches.
3. `stream_turn` cancel paths: on cancel after STT, **keep** the heard text (append `user` and, if interrupted, a truncated `assistant` marker or merge into the next user turn); always append to `self.turns`; avoid leaving `user,user`.

**P1**
4. Endpointing: longer/adaptive silence (e.g. 700 → ~1000 ms for utterances <1 s, shorter after sentence-final intonation), merge a fragment that follows within a window; raise or remove the 6 s cap with continuation instead of cutting.
5. Cancel stale reply and merge utterances queued during "thinking"; make overflow merge-oldest rather than drop, and surface it as a metric.
6. STT failure: bounded retry, and a spoken "could you repeat" on timeout/empty (needs authorization — new caller-facing behaviour).
7. `deduplicate_repetitions`: never touch tokens containing digits; limit to ≥3 repetitions; keep behind a flag.
8. Glossary: enable and extend from observed STT logs; wire up or delete `NEVER_REWRITE`.
9. Trim trailing silence to ≈150–200 ms before STT (after confirming A/B on real audio).
10. Verify the live worker (`health` task: model, revision, `language`); pin `MODEL_REVISION`.

**P2** gap/late/restart counters in `PlivoIO`; send audio once in the STT body; per-utterance accounting in `TurnTrace.meta`; STT job/utterance ids; turn-number consistency.

## 16. Safe offline tests (actual commands and results)

All run from the repo root with `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider` (no `.pyc`/cache written).

1. `python -m pytest -p no:cacheprovider -q tests/unit/test_plivo_stream.py tests/unit/test_pipeline_cancellation.py tests/unit/test_stt_upload.py tests/unit/test_glossary_correction.py tests/unit/test_turn_trace.py tests/unit/test_script_enforcement.py tests/unit/test_logs_no_words.py tests/unit/test_stt_domain_vocabulary.py tests/unit/test_inbound_conversation_relevance.py tests/unit/test_runpod_clients.py`
   → **`82 passed, 1 warning in 4.20s`** (warning: `audioop` deprecated). These pass **despite** every defect above: none of them exercises `serve_loop` endpointing, barge-in buffers, overflow, or cancel-history behaviour.
2. `vad_sim.py` — drives the real `app.ai.ws.serve_loop` with a fake `io`, a virtual monotonic clock and a mock conversation; produced the §7 table.
3. `probe2.py` — `PlivoIO.next_item` event matrix, codec, WAV header, μ-law SNR (§4–§5).
4. `probe3.py` — dedupe, glossary, `Conversation.stream_turn` propagation and cancel/break history (§9, §11) with mocked STT/LLM.
   Scripts 2–4 are in `C:\Users\Admin\AppData\Local\Temp\claude\c--Users-Admin-Desktop-MME-Telephony\ad0505d5-1d6c-4a1f-b815-953a6894252f\scratchpad\` (outside the repo, not committed).

**NOT RUN:** worker inference and `resample_poly` (no numpy/scipy; no GPU), live `health` task, any Plivo call, any RunPod/LLM/TTS request, Mongo-backed integration tests (`service.py` turn numbering, transcript privacy end-to-end), dashboard. Existing `tests/integration/test_streaming_transcript_privacy.py` was not run (needs the DB).

## 17. Files and functions that would need modification (after authorization)
* `app/ai/ws.py` — `serve_loop` (VAD branches, `flush_if_due`, barge-in block `:357-375`, queue overflow `:305-308`), `PlivoIO.next_item` (gap/late counters), `PlivoIO.error`.
* `app/ai/pipeline.py` — `stream_turn` cancel/except paths (`:684, :821-832`), `deduplicate_repetitions`.
* `app/ai/clients.py` — `transcribe` (single audio copy, utterance id, retry policy).
* `app/ai/turn_trace.py` — `TurnTrace.meta` for accounting.
* `app/ai/glossary_correction.py` — mappings / enable path.
* `app/ai/service.py` — turn numbering in `record_user_utterance` / `record_turn`.
* `tools/stt_worker_v2/handler.py` — honour request `language`, pin revision, prompt policy (only if S1–S3 are confirmed).
* Tests to add: `serve_loop` scenario tests (the §7 harness is a starting point), `PlivoIO` gap/late tests, cancel-history tests, digit-preserving dedupe tests. **Fixtures missing:** recorded 8 kHz μ-law Hindi/Hinglish call audio with reference transcripts (A–G in real speech), and a captured Plivo `media` stream with real `chunk/timestamp/sequenceNumber`.

## 18. Working-tree safety confirmation
Start: branch `realtime-latency`, 22 modified + 38 untracked entries (`git status --short` = 60 lines), `git diff --stat` = 22 files, +1113/−259. No reset, stash, clean, checkout, commit or push was run; no application, config, test or tool file was edited; the Uvicorn process was not touched; no live provider was contacted; no credentials, phone numbers, caller data or real transcripts appear in this document (all strings are synthetic or quoted from the previous report). The only new tracked-path file is this document.

---

## 19. Addendum — the working tree changed while this audit ran (not by this audit)

**Baseline.** §1–§18 describe `app/ai/ws.py`, `clients.py` and `pipeline.py` as read at the start of the audit (before ~09:36 IST), when `ws.py` had no pre-roll, `SILENCE_MS=700`, `MAX_UTTERANCE_MS=6000`. Line numbers in this document refer to that snapshot.

**What changed underneath.** Between my reads and this addendum, files were modified by another process/session — I ran no write command: `app/ai/ws.py` (mtime 09:40–09:41, still moving), `app/ai/clients.py` (09:38), `mme_hinglish.py` and `config/script_allowlist_terms.txt` (newly modified). `git status --short` went from 60 to 63 entries; `git diff --stat` from 22 files / +1113 −259 to 24 files / +1226 −273 (before this document was added). The in-flight `ws.py` now contains `PRE_ROLL_MS=200`, `SILENCE_MS=850`, `SHORT_SILENCE_MS=1000`, `MAX_UTTERANCE_MS=8000`, a `pre_roll` deque in `serve_loop`, and a barge-in block that seeds `buf` from the pre-roll instead of `bytearray(frame)`.

**Provisional re-run (same read-only harness, unmodified, against the in-flight file, 09:41:45):**

| Scenario | Baseline: speech kept / jobs | In-flight tree: speech kept / jobs |
|---|---|---|
| A "haan" 400 ms | 220 / 1 | **400 / 1** |
| A2 240 ms | 60 / 1 | **240 / 1** |
| A3 180 ms | 0 / **0 jobs** | 0 / **0 jobs** (still deleted: gate unchanged) |
| B 800 ms pause | 1540 / 2 jobs | **1900 / 1 job** (silence window now 850/1000 ms) |
| C 750 ms pause | 3640 / 2 jobs | **4000 / 1 job** |
| E 7.5 s continuous | 7140 / 2 jobs (6.0 s + 1.8 s) | **7500 / 1 job** |

An earlier run of the same harness at 09:38–09:40 against a *different intermediate* version of `ws.py` still lost 180 ms per utterance, and J/K/G2 (playback-time loss) were still reproduced then. I did **not** re-verify J, K, G2, L, M, the cancel/history defects (F3), dedupe (F8) or the STT-worker findings against the newest file. Treat them as **confirmed against the baseline snapshot only**.

**Consequences for the verdict.** F2 (onset clipping) and F4/F5 (silence/cap splitting) appear to be **already being addressed** by the concurrent change; that work is unreviewed here and the file is still being edited, so no claim is made that it is correct. Items that do not depend on `ws.py` (F3, F7, F8, F9, S1–S3) are unaffected by that change. Re-run `vad_sim.py` once the edit settles before relying on either column.

**Safety statement correction (supersedes §18).** This audit changed no tracked file; the only file it added is this document. It cannot attest that the tree is "unchanged since start" because other writers were active. Pre-existing and concurrent modifications were left exactly as found.
