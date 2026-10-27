# Voice Studio: design (Phase 1, preview only)

Status: offline code and tests only. Nothing here has been deployed, built, restarted or run against a real GPU, and nothing has been paid for.
Everything that touches the GPU is marked UNVERIFIED.

## 1. What it is, in plain words

An administrator opens the dashboard page "Voice Studio", uploads a clip of about 10 seconds of one person's voice, ticks a box that says the
speaker agreed, types a sentence and presses Speak. The service sends the sentence to the TTS worker on RunPod (Chatterbox), which speaks it in
the uploaded voice, and the page plays the result. Later (Phase 2) the same voice can be chosen for calls.

Phase 1 changes **previews only**. The call path (`app/ai/clients.py`, the streaming pipeline, the media bridge) is not touched. The armed state
of the system is not changed. The whole feature is behind the per-tenant flag `voice_studio`, which is **OFF by default**: with the flag off,
every Voice Studio endpoint answers 404 `voice_studio_disabled` and the dashboard shows a short "switched off" message.

## 2. Parts

| Part | Where | What it does |
| --- | --- | --- |
| Worker (RunPod) | `deploy/tts_worker_runpod/handler.py` | New tasks `register_voice` and `voices`; synth with an unknown voice answers `needs_reference`. Not built, not deployed. |
| Clip preparation | `app/voices/clip.py` | Checks and prepares an uploaded clip (mono, 24 kHz, 16 bit, trimmed) and produces the quality report. |
| Text front-end | `app/voices/frontend.py` | The same text normalisation as calls; optional phone quality. |
| Storage | `app/voices/store.py` | Voice records, the clip itself (GridFS), usage events, spend counter. |
| Worker client | `app/voices/worker.py` | Submits RunPod jobs and reports queued / warming / speaking. |
| Studio service | `app/voices/studio.py` | Limits, consent, job lifecycle, audit. |
| API | `app/api/routes_voices.py` | `/v1/voices...` endpoints. |
| Dashboard | `app/static/dashboard/` | The "Voice Studio" page (plain HTML/JS like the rest). |

## 3. The worker (offline change)

* `{"input": {"task": "register_voice", "name": "...", "reference_audio_base64": "..."}}`
  * name: letters, digits, `_`, `-`, at most 40 characters.
  * The audio must be a WAV (the app always sends the prepared 24 kHz mono WAV), at most `MAX_REGISTER_BYTES` (default 8 MB), duration 3 to 30 seconds.
  * The voice is prepared **once** and cached by name **and** checksum (SHA-256 of the audio). Registering the same name with the same checksum again
    is a no-op (`cached: true`); the same name with different audio replaces the voice.
  * Registered voices are kept in a least-recently-used list capped by the environment variable `MAX_REGISTERED_VOICES` (default 8). The baked
    default voice and the voices from `VOICES_DIR` are never evicted and can never be overwritten by a registration (`name_reserved`).
* `{"input": {"task": "voices"}}` lists the names (baked and registered) and the cap.
* A synth request that names a voice the worker does not have answers
  `{"error": "needs_reference", "code": "needs_reference", "voice": "<name>", "voices": [...]}`. It is a normal answer, not a crash. The app reacts by
  registering the voice and asking again (see section 5). (Before, the answer was `Unknown voice`; the call path treats any `error` as a failure, so
  it behaves the same.)
* `ALLOW_REQUEST_REF` stays **off by default**; a reference sent inside a synth request is still ignored.
* `MODEL_ID` (`mmeog/mme_stt_model`) belongs to the STT worker and is untouched.
* UNVERIFIED on a GPU: `prepare_conditionals()` with an arbitrary registered clip, the time it takes, the memory of N prepared voices, and the
  behaviour of the LRU eviction on a real worker. The pure logic (validation, cache, LRU, error shapes) is tested with a fake model.
* The built-in `--self-test` is extended. It needs numpy, scipy and soundfile, which are **not installed** in the WSL venv, so it cannot run here;
  the new checks are one function (`selftest_voice_tasks`) that is also run by a pytest with the heavy imports stubbed.

## 4. Clip preparation (offline, in the app)

Decoder check on the WSL venv (reported, nothing installed): `ffmpeg`, `ffprobe` and `sox` are **not on PATH**; numpy, scipy, soundfile, pydub, librosa,
av are **not installed**. Python 3.12 has the standard `wave` and `audioop` modules. Therefore:

* Accepted without extra tools: **PCM WAV** (8/16/24/32 bit, any rate, mono or stereo). Not accepted: MP3, M4A, OGG, float WAV, compressed WAV.
  They are refused with `clip_unsupported_format` and the message says that only WAV is supported on this server.
* If `ffmpeg` is on PATH at run time, the app uses it to convert any other format first (`decoder_report()` says which is the case; the dashboard shows it).
* Resampling uses `audioop.ratecv` (linear interpolation, no anti-alias filter). Good enough for speech; ffmpeg gives better quality for 44.1/48 kHz
  sources when present. Say so honestly if cloning quality turns out to be affected.

Steps: decode to mono 16-bit, resample to 24 kHz, measure 20 ms frames, find the noise floor, trim leading and trailing silence (keep 0.15 s each side), measure.
The report holds: duration (after trim), original duration, peak (dBFS), noise floor (dBFS), speech seconds in the first 6 s and in the first 10 s, clipped
fraction, estimated SNR, and a list of warnings. The clip is **refused** when: too short (under 3 s after trimming), too long (over 30 s),
clipped (more than 0.5% of samples at full scale), silent (peak below -40 dBFS or under 2 s of speech), unreadable or not a supported format, too large.
Warnings (not refusals): noisy room, little speech in the first 6 s, quiet recording.

## 5. API (admin only, tenant scoped, rate limited)

All endpoints need the admin (or platform) role, the flag `voice_studio` on, and look up records by `(tenant_id, id)`: another tenant's id is a 404.

| Method and path | Purpose |
| --- | --- |
| `POST /v1/voices` | multipart upload: `file`, `name`, `speaker_name`, `consent=true`, `consent_scope` (`preview_only` or `preview_and_calls`). Returns the voice and its quality report. |
| `GET /v1/voices` | list voices, limits and this tenant's usage (previews this hour, uploads today, spend this month and its cap, decoder report). |
| `POST /v1/voices/{id}/disable` | stop the voice being used (clip kept). |
| `DELETE /v1/voices/{id}` | disable and erase the clip. A tombstone (no audio) stays so the audit trail is understandable. |
| `POST /v1/voices/{id}/preview` | `{text, language, phone_quality}`; answers 202 with a job id **at once**. |
| `GET /v1/voices/previews/{job_id}` | status: `queued`, `warming`, `speaking`, `done`, `error`, with `detail`, elapsed seconds, error code. |
| `GET /v1/voices/previews/{job_id}/audio` | the WAV, when `done`. |

The upload is the one route with a larger body limit (the file limit plus 64 KB for the multipart framing; every other route keeps the 64 KB limit) and it **must carry a
Content-Length** (411 otherwise; browsers send one). The route checks the role and the flag **before** it reads the body. The body size is bounded by that Content-Length check, and the file part is read up to the limit (+1 byte, to notice an oversized file).
The dashboard's content-security-policy gains `media-src 'self' blob:` and nothing else.

Preview job (a background task with a deadline, default 180 s):

1. `queued`: accepted. The limits below were checked and the estimated cost was reserved.
2. The task asks the worker to speak with the voice (the worker's name for it is `v_<voice id>`, unique across tenants).
3. RunPod `IN_QUEUE` is shown as `warming` ("warming up, this can take about a minute"); `IN_PROGRESS` as `speaking`.
4. If the worker answers `needs_reference` (a fresh or restarted worker has no such voice), the app sends the clip **once** (`register_voice`) and asks again. A second
   `needs_reference` ends the job with `voice_unavailable`. Registering also shows as `warming`.
5. `done`: the audio is kept **in memory only** (not in the database, not in logs) for 10 minutes, at most 50 previews per process, then dropped. A restart drops
   them; previews are throw-away. At the deadline the RunPod job is cancelled so it stops billing.

Limits per tenant (all in settings, `MME_TEL_VOICE_STUDIO_*`): previews per hour (20), uploads per day (20), text at most 300 characters, active voices (10),
previews running at once (2), upload size (8 MB). A request over a limit answers 429 `voice_limit` with the name of the limit.

Spend counter and hard cap: a counter per tenant per UTC month in micro-US-dollars. Before a preview starts, an **estimate** is reserved atomically; if the reserve would pass
the cap the preview is refused with 429 `spend_cap_reached` and nothing is sent. After the job, the reservation is replaced by the measured cost
(`delayTime + executionTime` from RunPod times the configured price per GPU second; counting `delayTime` is deliberately conservative). The price and the
cap are settings; **the price default is a placeholder, not RunPod's price: set it from your RunPod bill.** Cap default 5 USD per tenant per month; 0 blocks every preview.

## 6. Text and phone quality

Previews use the same text normalisation as calls: for `auto`/`hi`/`en`/`hinglish` the language tag is detected per sentence and numbers, rupees, dates,
phone numbers and acronyms are written as spoken words (`app/ai/tts_frontend.py`), controlled by the same setting `ai_tts_normalize`. A test checks that the text sent
for a preview equals the text the call path sends for the same input. `phone_quality=true` asks the worker for `ulaw8k` (8 kHz G.711 mu-law, exactly what calls use), decodes it
and returns a WAV at 8 kHz, so the admin hears what the caller would hear. Otherwise the worker returns a 24 kHz WAV.

## 7. Storage design

* **Clip**: MongoDB GridFS, bucket `voice_clips`, in the existing `mme_telephony` database. Never in the repository. Only the **prepared** clip (mono, 24 kHz, 16 bit,
  trimmed) is stored; the original upload is not kept.
* **Voice record** (collection `voices`): `_id` (voice id), `tenant_id`, `name` (display), `name_lower` (unique per tenant), `worker_name`, `status` (`active`,
  `disabled`, `deleted`), `checksum` (SHA-256 of the prepared clip), `gridfs_id`, `duration_s`, `report` (the numbers above, no audio), `consent_ref`
  `{speaker_name, date, scope}`, `created_by`, `created_at`, `updated_at`, `disabled_by`, `disabled_at`.
* **Usage events** (`voice_usage`: tenant, kind `upload` or `preview`, time; expire after 2 days) for the hour and day limits. **Spend** (`voice_spend`: tenant, period, micro-USD).
* **Consent**: an upload without the consent box and a speaker name is refused (422 `consent_required`). A preview of a voice without a `consent_ref` is refused too
  (409 `consent_required`), whatever the way the record got there. The date is set by the server. `preview_only` scope blocks later use for calls (Phase 2 checks it).
* **Audit** (existing `audit` collection): `voice.upload` (ok or the refusal code), `voice.preview`, `voice.disable`, `voice.delete`. Details hold ids, counts and codes
  only: **no audio, no text** (a preview records only the number of characters, the language and the quality flag).
* **Logs**: ids, codes, sizes and seconds only. A test checks that neither the typed text nor audio bytes reach the log output or the audit record.
* **Encryption at rest and retention, honestly.** The application does **not** encrypt clips itself. Whether they are encrypted at rest depends on how MongoDB is run: the local
  development `mongod` has no encryption at rest; disk or volume encryption (or Atlas / MongoDB Enterprise encryption) must be provided by the deployment, and that is **not verified
  here**. Transport encryption to MongoDB depends on the URI settings (also not verified). Generated previews are never stored. **Retention**: a clip stays until an admin deletes the voice;
  there is **no automatic expiry yet**. Audit records follow `retention_audit_days`. A recommended next step is an expiry date on the voice record plus a purge job; it is not built.
  A voice clone of a real person is personal data: the consent text on the page and the `consent_ref` are the only legal basis recorded; this document is not legal advice.

## 8. Dashboard

Page `#/voices` ("Voice Studio", admin only): upload form (file, name, speaker name, scope, consent box with plain wording), the quality report after upload (and the clear reason when refused),
the list of voices (disable, delete), and the Speak panel (voice, text box with a counter to 300, language, phone-quality switch, Speak button, audio player). States shown:
"Queued...", "Warming up the voice server, this can take about a minute...", "Speaking...", done (player), and error messages written for people ("The clip is too noisy", "Daily upload limit reached", ...).
Plain DOM code like the other pages; no framework, no inline script. The page's content-security-policy gains `media-src 'self' blob:` so the player can play the fetched audio.

## 8b. Tests (all with mocks; no GPU, no RunPod, no network)

| File | What it covers |
| --- | --- |
| `tests/unit/test_tts_worker_voice_tasks.py` | worker: register validation (name, size, base64, duration), cache by name + checksum, LRU cap, baked voices protected, `voices`, `needs_reference`, `ALLOW_REQUEST_REF` default off (handler loaded with numpy/scipy/soundfile stubbed; fake model) |
| `tests/unit/test_voice_clip.py` | clip preparation: conversion, trimming, report numbers, refusals (short, silent, clipped, long, not WAV, float WAV), warnings, ffmpeg path with a fake runner |
| `tests/unit/test_voice_frontend.py` | preview text equals the text the real call-path client sends (same normalisation), phone quality = `ulaw8k` |
| `tests/integration/test_voice_studio.py` | API with MongoDB + GridFS and a fake worker: flag, roles, cross-tenant 404, consent, name rules, limits, job lifecycle (queued / warming / speaking / done / error), `needs_reference` retry (clip sent once), deadline + cancel, spend cap, no text or audio in logs / audit / database |
| `tests/e2e/test_voice_studio_browser.py` | the dashboard page in headless Chromium (local): upload, consent, quality report, warming state, audio player from a `blob:` URL, no console errors / CSP violations |
| `tests/unit/test_voice_studio_static.py` | defaults (flag off, limits), dashboard code rules, call path files do not mention Voice Studio, no audio files in code / test / doc folders |

Generated tone fixtures only (`tests/voice_fixtures.py`); no real voice audio was added.

## 8c. What is NOT verified

* Anything on a real GPU or RunPod: `prepare_conditionals` with a registered clip, its time, memory of N voices, cold-start time, real `delayTime` / `executionTime` fields, cancel behaviour, the price.
* The worker's own `--self-test` (needs numpy, scipy, soundfile; not installed here). Its new voice-task part is the same function the pytest runs with stubs; the numpy-dependent rest of the self-test was not run.
* The cloning quality of clips prepared with `audioop.ratecv` instead of ffmpeg.
* Encryption at rest and transport security of the MongoDB deployment (see section 7).
* A real browser other than the bundled headless Chromium; the JavaScript could not be syntax-checked with node (not installed) other than by that browser run.
* The pre-existing audio files in the worktree root (`Utkarsh_voice.wav`, `Utkarsh_voice_ref12s_24k.wav`, `uth-jaa-bkl.mp3`) are not part of this change and were not touched.

## 9. Plan only: measuring on a real GPU (not run, needs APPROVED + the exact action)

Goal: real numbers for register time per extra voice, voice switching, memory and cost. Proposed script (to be written after approval, run on one worker, with a hard cap):
1. One cold start, recording `delayTime` and `executionTime` for a `voices` task (no voice work), to price the cold start.
2. `register_voice` for N = 1, 2, 4, 8 generated-tone clips (3 s, 10 s, 30 s; **no real voices**) recording `prepare_s` for each, and GPU memory after each.
3. Round-robin of 2 voices x 20 short synths, comparing the time with a single-voice run (is switching free, as the design claims?).
4. Eviction: register 10 voices with cap 8, check that the first two are evicted and answer `needs_reference`.
Cost cap: at most about 40 jobs and 10 minutes of GPU; the script stops when its own counter passes `MAX_USD` (given on the command line, no default). Needs: the price per second,
a throw-away endpoint (not the armed one), and an APPROVED line that names the endpoint and the cap.

## 10. Plan only: Phase 2 (calls can use a Studio voice)

* The AI configuration page gets a "call voice" dropdown listing active voices whose consent scope is `preview_and_calls`; choosing one sets `tts_voice` to the voice's worker name (`v_<id>`),
  which the existing validation (1-40 letters, digits, `_`, `-`) already accepts. The built-in voice stays the default.
* Before a call (at warm-up, off the audio path) the app asks the worker `voices` and checks the name is there; if not, it registers it (clip from GridFS) so the first turn is not the one to pay.
* If the voice is missing, disabled, deleted or the worker cannot be asked, the call **falls back to the default voice** and records why (reason code only).
* During a call, a synth answer `needs_reference` is handled by registering the voice and retrying that one sentence once. The cost is one `register_voice` (to be measured in section 9; with
  the cold-start behaviour of a restarted worker it can be several seconds) added to that turn only, so it must stay rare; if it happens twice in one call the call switches to the default voice for the rest of the call.
* The call path will need a small change in `ModelClients._synthesize` (treat `needs_reference` specially). **That is Phase 2 and is not done here.**
