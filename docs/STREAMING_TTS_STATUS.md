# Streaming TTS: status, 2026-10-28

Goal: genuinely incremental Chatterbox Multilingual V3 speech (T3 token streaming + incremental S3Gen decoding), progressive transport,
Qwen -> TTS -> telephony overlap, barge-in safe. **No latency, quality, GPU-memory or concurrency number exists yet: this machine has no GPU,
and nothing was measured against a real worker.** Labels: **[TESTED]** implemented and tested here (fakes/mocks), **[NOT VALIDATED]** implemented but
not run in the target environment, **[BLOCKED]** needs a missing resource, **[NOT IMPLEMENTED]**.

## Environment inventory (checked 2026-10-28)

| Component | Available? | Source | Testable here? |
|---|---|---|---|
| TTS worker source | Yes, outside this tree | newest: `C:\Users\Admin\Desktop\MME_wt_turns\deploy\tts_worker_runpod\handler.py` (turn-cancel worktree another session edits; not touched). Also `Desktop\tts_v4_update\`, git commits 05e7663/671fa01/d80d69b | Its `--self-test` needs numpy/scipy/soundfile; not run |
| Chatterbox source | No local checkout | public repo read via web (summaries only, no verbatim code) | No |
| Checkpoint | No | | No |
| torch / CUDA / GPU | No (`torch` missing in the WSL venv, no `nvidia-smi`) | | No |
| Progressive transport | Documented by RunPod (generator handlers, `/stream`) | docs.runpod.io (handler-generator page) | Client side only, with a mock |
| Telephony test env | Plivo media-stream mock + FreeSWITCH test stack | repo tests | Mock only; no real call |

Worker facts (read from `handler.py`): `ChatterboxMultilingualTTS.from_pretrained(device="cuda", t3_model=...)`, `MODEL.generate(text, language_id=..., exaggeration, cfg_weight, temperature)`,
voices prepared once by name (`prepare_conditionals`, cloned shallowly per request), `output_format` `wav|pcm16|ulaw8k`, a plain (non-generator) handler,
`runpod.serverless.start({"handler": handler})`. The app client still sends `reference_audio_base64`, which v3/v4 workers ignore.

## What the Chatterbox source shows (secondhand: summaries of the public repo, master branch; the DEPLOYED revision is not pinned/known)

- `T3.inference` is a manual KV-cache loop, sampling per step (repetition penalty, temperature, min-p, top-p, CFG), stop on EOS, returns one tensor at the end.
  Streaming it = turning the loop into a generator that yields each sampled token. **[NOT IMPLEMENTED]**: I only have a summary, not verbatim code, so I cannot copy the loop faithfully.
- `S3Token2Wav.flow_inference(..., finalize=False)` already ignores the last 3 tokens (look-ahead). Tokens are 25 Hz, 2 mel frames per token, 24 kHz audio.
  `hift_inference(speech_feat, cache_source)` carries vocoder source state. Every decode applies a 40 ms fade-in (`trim_fade`).
- PR #528 (Turbo) `S3GenStreamer` buffers tokens, holds back `pre_lookahead_len`, reuses noise, carries `hift_cache_source`, cross-fades 12 ms, and **re-decodes the whole
  prefix on every flush** (emits only samples past `emitted_samples`): quadratic work. It needed a `flow.py` mask/trim fix. Its streamed audio is **not watermarked** (Perth needs the full waveform).
- Not verified: whether the V3 decoder keeps quality when each decode sees only a bounded window of earlier tokens instead of the whole prefix.

## Built

| Piece | Files | Status |
|---|---|---|
| Clause overlap: TTS of clause N runs while the LLM streams; ordered; <=2 concurrent; cancel on error/barge-in. Off by default, tenant config `tts_overlap` | [pipeline.py](../app/ai/pipeline.py), [service.py](../app/ai/service.py), [routes_platform.py](../app/api/routes_platform.py), [test_tts_overlap.py](../tests/unit/test_tts_overlap.py) | **[TESTED]** (fakes) |
| Lost-turn root cause + regression test | [test_tts_overlap_lifecycle.py](../tests/e2e/test_tts_overlap_lifecycle.py) | **[TESTED]** (simulated call) |
| Bounded-window stream decoder (look-ahead, context, cross-fade, flush, cancel); also the quadratic full-prefix mode for quality A/B | [stream_core.py](../tools/tts_stream_worker/stream_core.py), [test_tts_stream_core.py](../tests/unit/test_tts_stream_core.py) | logic **[TESTED]** on a synthetic decoder; real S3Gen **[NOT VALIDATED]** |
| Worker generator: tokens -> ordered `ulaw8k` chunks, stateful 24->8 kHz decimator, cancel, per-request state | [worker_stream.py](../tools/tts_stream_worker/worker_stream.py), [test_tts_worker_stream.py](../tests/unit/test_tts_worker_stream.py) | **[TESTED]** with a fake token source; RunPod wiring and the T3/S3Gen adaptors **[BLOCKED]** |
| Client `synthesize_stream` (`/run` + `/stream/{id}` polling, ordered, dedupe, gap = error, cancel on early close, non-streaming-worker fallback) | [clients.py](../app/ai/clients.py), [test_tts_stream_client.py](../tests/unit/test_tts_stream_client.py) | **[TESTED]** against a mock; the `/stream` JSON shape is an **assumption** (not in the pages read) **[NOT VALIDATED]** |
| Pipeline streaming: each audio chunk goes to playback as it arrives; bounded queue per clause (backpressure); barge-in closes the stream. Off by default, tenant config `tts_stream` | [pipeline.py](../app/ai/pipeline.py), [test_tts_stream_pipeline.py](../tests/unit/test_tts_stream_pipeline.py) | **[TESTED]** (fakes) |
| Lifecycle marks: `t_llm_done`, `t_tts_done`, `t_turn_end` (delivered), `t_playback_end_expected` (computed, **not observed**) | [pipeline.py](../app/ai/pipeline.py), [ws.py](../app/ai/ws.py), [turn_trace.py](../app/ai/turn_trace.py) | **[TESTED]** (existing e2e still green) |
| Benchmark harness (complete vs stream, p50/p95/p99, first 20/100 ms, RTF) | [bench_tts_stream.py](../scripts/bench_tts_stream.py) | harness **[TESTED]** on a mock; real run **[BLOCKED]** |

## Root cause of "2 of 10 turns lost" with overlap on (traced)

Instrumented a copy of the tree (not this one). The server emits one `checkpoint` per clause. With overlap, clause 2 of turn N is ready right after clause 1, so its checkpoint is
already queued when the harness has read the first one. `test_simulated_10_turn_cold_warm.py` treats the first checkpoint as end-of-turn: it read turn N's leftover checkpoint
(`utt-2`) as the answer to turn N+1 and spoke turn N+2 while N+1 had produced no audio. The server then did its designed thing: newer caller speech while audio was "playing" =
barge-in, which cancels the in-flight turn; a turn cancelled before its first audio chunk is never recorded (`record_turn` runs on the first chunk). Counts: 10 utterances flushed, 10 turns
started, 7 reached their final chunk, 8 recorded. Nothing was dropped by the pipeline. The new test waits for turn finalization (`t_turn_end` stored) and passes with overlap on (5/5 turns, checkpoints
`utt-1..utt-N` contiguous). The old test is unchanged and still passes with the default (off).
Also found and fixed: the config API rejected unknown keys (422), so the `tts_overlap` opt-in reported earlier was unreachable; it is now in `AiConfigIn`/`default_config`/`get_config`/`put_config`.
Open product question: `tts_overlap` still makes a caller who replies after the first clause supersede an unanswered turn; that is correct barge-in behaviour but should be confirmed live before enabling.

## Streaming contract (worker -> client)

Worker is a generator handler (RunPod `yield`) and the request carries `input.stream=true`, `input.output_format="ulaw8k"`. Each item:
`{"seq": 0.., "audio_base64": "...", "format": "ulaw8k", "sample_rate": 8000, "final": bool}`, last item `final=true` (may be empty). RunPod JSON only: base64 is unavoidable on this transport
(binary frames would need a persistent WebSocket worker; **[NOT IMPLEMENTED]**). `/run` accepts up to 10 MB; per-chunk limit on `/stream` is not documented (client caps one item at ~1 MB).
Cancel: client `POST /cancel/{id}`; whether the RunPod runtime then closes the generator (so `stream_synth`'s `finally` runs and T3 stops) is **unverified**; a non-cooperative GPU loop may keep running until it
checks `should_cancel`. Audio from a cancelled job cannot leak: the client stops reading and drops its generator.
Audio path: 24 kHz model audio -> stateful decimator (no filter restart per chunk) -> mu-law at the worker. The app plays it via `PlivoIO.play`, which decodes to PCM16 8 kHz and re-encodes mu-law (a second G.711 pass; avoidable later by a pass-through).
Watermark: streamed audio is not watermarked. **Product/policy decision needed before enabling.**

## Test results (WSL venv, `-m "not freeswitch"`)

- Before this session's second part, full `tests/unit tests/integration`: 631 passed, 1 skipped, 2 failed (the two pre-existing ones below).
- e2e (3 files) + overlap + AI integration: 26 passed. New unit files: stream core 23, client 15, worker stream 11, pipeline streaming 7, bench 2.
- Final full `tests/unit tests/integration` run (all edits through the pipeline streaming + marks, before the adaptor files): **688 passed, 1 skipped, 3 failed**: the two pre-existing ones plus
  `tests/integration/test_api_calls.py::test_pagination_filters_usage`, which **passes alone (29/29 in that file)**: not reproduced, unrelated to these files; suspected the known WSL wall-clock stepping (unproven).
  Later additions: adaptor pure tests 5 passed. The command is `pytest tests/unit tests/integration -m "not freeswitch"` in the WSL venv.
- Pre-existing failures, untouched and not part of this work: `test_listening_tools.py::test_default_prompt_is_byte_for_byte_the_committed_one_when_devanagari_is_off` (`SYSTEM_PROMPT` edited, uncommitted)
  and `test_strict_grounding.py::test_tenant_config_defaults_and_put` (`strict_grounding` missing from `default_config`; `service.py` also never passes `glossary_correction`/`strict_grounding` to `Conversation`).
- numpy was installed into the dev venv for the decoder tests; it is not an app dependency (worker image already has it). Add to `requirements-dev.txt` if you want it pinned.

## Real-model source findings (VERBATIM source of resemble-ai/chatterbox master @ 5de7a54, read 2026-10-28; the deployed revision is still unknown)

- `T3.inference`: manual loop over a KV cache; CFG combine -> repetition penalty -> temperature -> min-p -> top-p -> `multinomial`; EOS is appended then breaks; `max_new_tokens=None` would crash; `generate()` passes 1000 and top_p=1.0.
- **Master's `finalize=False` flow path is broken**: `CausalMaskedDiffWithXvec.inference` trims `h` by `pre_lookahead_len*token_mel_ratio` (3 tokens = 6 mel frames) but computes `h_lengths` from the untrimmed mask, so `mu` and `mask` disagree. PR #528 fixes this in `flow.py`; the adaptor carries the same fix (`flow_inference_nonfinal`).
- Every flow decode is `[reference prompt tokens + tokens]` with the reference mel as condition; already-generated context gets NO mel conditioning in a stateless window decode. Quality risk, to be measured, with a possible CosyVoice-style variant (feed emitted context tokens + their mels as extra prompt) if the plain window sounds worse.
- CFM noise is `torch.randn_like` per call: two decodes of identical tokens are never sample-identical. Streaming-vs-full comparison therefore uses a spectral distance judged against a measured noise floor, not equality. `noised_mels` lets a caller inject noise.
- Every decode fades in the first 40 ms (`trim_fade`); `generate()` drops the last token's audio and then applies the Perth watermark to the whole wav (so streaming audio is unwatermarked).
- The tree's `S3GenStreamer` referenced in `forward()`'s docstring does not exist in master.

## Built for the GPU host, never run (IMPLEMENTED, NOT VALIDATED)

- [chatterbox_adapter.py](../tools/tts_stream_worker/chatterbox_adapter.py): `verify_installed_source()` (AST fingerprints of every copied upstream function: the worker must refuse to stream on a mismatch), `t3_token_stream`, `hold_back_last_token`,
  `flow_inference_nonfinal`, `make_window_decoder`. Only its torch-free parts have tests ([test_chatterbox_adapter_pure.py](../tests/unit/test_chatterbox_adapter_pure.py), 5 passed).
- [gpu_validate_streaming.py](../scripts/gpu_validate_streaming.py): stage 1 token equivalence (fixed seeds), stage 2 decoder sweep (full vs full-prefix vs bounded windows, noise floor, click ratio, peak memory, WAVs for listening), stage 3 latency/RTF/cancel.
  It refuses to run without CUDA, stops if the installed source differs, and stops if tokens differ. It decides nothing: acceptance needs human listening.

## GPU-host runbook (exact order)

1. Pin: record `pip freeze`, the Chatterbox git SHA (or `chatterbox-tts` version), the HF checkpoint revision (`T3_MODEL`), CUDA/driver. Compare with the worker image. If the SHA is not `5de7a54`, expect `verify_installed_source()` to list the differing functions: re-read those functions and update the adaptor and the fingerprints deliberately.
2. `python -m scripts.gpu_validate_streaming --voice-wav <ref.wav> --out /tmp/stream_val --seeds 5` (add `--quick` first). Read `report.json`; listen to `wav/*_full.wav` vs `*_win_*.wav` and `*_fullprefix.wav`.
3. Choose `context/hop/first_hop/crossfade` by sound first, speed second. If plain windows sound worse than full-prefix, try the prompt-extension variant before accepting full-prefix cost; if nothing sounds acceptable, keep full-prefix with a measured cost or stay on complete clips.
4. Worker: in the worker repo (not this tree) add a generator handler around `stream_synth` selected by `input.stream`; keep the existing `handler` as the fallback; call `require_matching_revision()` at start-up and fall back to the non-streaming path if it raises. Keep voice names and `ulaw8k` unchanged.
5. Isolated live test (NOT production): a separate RunPod endpoint/worker version, `BENCH_TTS_URL`/`BENCH_TTS_KEY` from a secret store, `python -m scripts.bench_tts_stream --runs 20`. Confirm `/stream` JSON shape, that the first chunk arrives before generation ends (worker log timestamps vs client), and what `/cancel` does to the generator.
6. Then app-side: enable `tts_stream` for ONE test tenant, run a supervised test call (needs explicit approval for any chargeable call), compare with the complete-clip baseline.

## Deployment and rollback (do not deploy automatically)

Build a NEW worker image tag with the generator handler; never retag the current one. Smoke test: `{"input":{"warmup":true}}`, one non-streaming synth (must equal today's output format), one `stream:true` synth (chunks `seq` 0..n, last `final`).
Rollout: new endpoint/worker version -> tenant config `tts_url` for a test tenant -> `tts_stream=true` for that tenant -> widen only after acceptance numbers pass. Rollback: set `tts_stream=false` (instant, app-side; the worker still answers complete clips) and, if needed, point `tts_url` back to the current endpoint. No data migration.
Product decision required before any production use: streamed audio is **not watermarked** (Perth needs the whole waveform).

## Blocked: needs a GPU host with the pinned Chatterbox checkout and the worker repo

1. T3 token generator: WRITTEN (`t3_token_stream`), never run. Run stage 1 of `gpu_validate_streaming` (token-for-token vs `T3.inference` with fixed seeds) and explain any mismatch.
2. S3Gen window decoder: WRITTEN (`make_window_decoder` + the `finalize=False` mask fix), never run. Run stage 2; decide `context_tokens` etc. by listening, and evaluate the `hift` `cache_source` / prompt-extension variants if boundaries are audible.
3. RunPod generator handler around `stream_synth` (`return_aggregate_stream` optional) in the worker repo; confirm the `/stream` JSON shape and cancel behaviour live.
4. Measurements: `python -m scripts.bench_tts_stream` (needs `BENCH_TTS_URL`/`BENCH_TTS_KEY`), warm vs cold, concurrency, GPU memory/utilisation, Hindi/English/Hinglish + voice-cloning listening tests.
5. Barge-in against a real Plivo call; a real-call check of `tts_overlap` / `tts_stream`.

## Not done / not feasible to claim

Audio was **not** observed playing before generation completed on real hardware. Genuine T3 streaming and incremental S3Gen decoding are **not implemented against the real model**. No production deployment, endpoint, image or credential was touched.

## Enabling and rollback

Both flags default to off (`tts_overlap`, `tts_stream` in the tenant `PUT /v1/ai/config`). Enable `tts_stream` only after the worker yields chunks (the benchmark shows `chunks_p50 > 1`). Rollback: set both to `false`; no migration, the
non-streaming path is the default code path.
