#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 3 SPIKE (standalone; runs ON a GPU Pod; no product code): can Chatterbox emit audio in chunks while it is still generating?

What it tries, against the OFFICIAL source (read 2026-10-25, resemble-ai/chatterbox master):
  * T3 (speech tokens) is a plain sampling loop with a KV cache. The script builds a GENERATOR version of the installed `T3.inference` by patching its source
    text (`predicted.append(next_token)` also yields it): the very same sampling code, now producing tokens one at a time. It fails loudly if the source differs.
  * S3Gen (tokens -> audio) has `flow_inference(..., finalize=False)` ("if False, the last 3 tokens will be ignored": a 3-token = 120 ms lookahead) and a vocoder
    `hift_inference`. There is NO flow cache in this version (flow.inference has none), so there are two ways to render a growing token list:
      A  cumulative : re-render ALL tokens so far with finalize=False each time, emit only the new samples (finalize=True at the end). Cost grows with length.
      B  windowed   : render only the last CTX tokens + the new ones, emit only the new samples (bounded cost per chunk).
Both are compared with the one-shot render of the SAME tokens (the speech tokens are sampled randomly, so only the same tokens give a fair comparison).

Measured per text: time to the first audio chunk, total time, audio length, real-time factor, tokens per second, a SIMULATED playback (starts at the first chunk,
plays in real time: how many ms it would stall waiting for a late chunk), a click score at each join, a spectral distance to the one-shot render.
It also times the normal `model.generate()` once per text (different random tokens: latency reference only).

Environment (all optional): REF_WAV, OUT_DIR, RESULT_TOKEN, LANGUAGE (hi), CONFIGS ("10:20,16:40" = first chunk tokens : later chunk tokens), CTX (25),
MAX_RUN_S (1500), SERVE_S (600), SPIKE_TEXTS (a JSON list; default: 8 test clauses of 4/8/12/16 words), SEED (7).
Run `python chunked_tts_spike.py --self-check` anywhere (needs numpy only) to test the stitching logic with a fake model.
The script writes status.json and results.json under OUT_DIR/RESULT_TOKEN and serves OUT_DIR over HTTP (port 8000) afterwards. It logs numbers, never keys.
"""
import functools
import http.server
import inspect
import json
import math
import os
import re
import socketserver
import sys
import textwrap
import threading
import time
import traceback

TOKEN_SAMPLES = 960          # 24 kHz / 25 tokens per second: one speech token is 40 ms of audio
LOOKAHEAD = 3                # flow_inference(finalize=False) ignores the last 3 tokens
SPEECH_VOCAB = 6561          # token ids at or above this are special (start / stop)
SR = 24000

DEFAULT_TEXTS = [
    "जी, ज़रूर बताइए ना।", "कौन सा course चाहिए?",
    "सालाना फ़ीस लगभग एक लाख बीस हज़ार है।", "counselling सोमवार से शुक्रवार तक online होती है।",
    "B.Tech चार साल का course है, admission entrance exam से होता है।", "आपको ये ज़रूरी documents लाने होंगे: marksheet, ID proof और दो photographs।",
    "अगर आपके बारहवीं में साठ प्रतिशत से ज़्यादा नंबर हैं तो आप apply कर सकते हैं।",
    "आप online form भर दीजिए, फिर हमारी team आपको counselling के लिए जल्दी ही call करेगी।",
]


def now():
    return time.perf_counter()


# ------------------------------------------------------------------------------------------------ pure logic (tested offline with a fake model)
def patch_t3_source(src: str) -> str:
    """Turn the source of `T3.inference` into a GENERATOR `inference_stream` that yields every sampled token. Raises if the upstream code is not as expected."""
    src = textwrap.dedent(src)
    if "def inference(" not in src:
        raise RuntimeError("T3.inference source not found")
    pattern = re.compile(r"^(\s*)predicted\.append\(next_token\)\s*$", re.M)
    if len(pattern.findall(src)) != 1:
        raise RuntimeError("the line `predicted.append(next_token)` was not found exactly once: the upstream loop changed, the spike needs a new patch")
    out = pattern.sub(lambda m: f"{m.group(1)}predicted.append(next_token)\n{m.group(1)}yield next_token", src, count=1)
    out = out.replace("def inference(", "def inference_stream(", 1)
    if "return predicted_tokens" not in out:
        raise RuntimeError("unexpected end of T3.inference")
    return out


class ChunkPlanner:
    """When to render: the first time after `first` tokens, then after every `chunk` more."""

    def __init__(self, first: int, chunk: int):
        self.next_at, self.chunk = max(first, LOOKAHEAD + 1), max(chunk, 1)

    def due(self, n_tokens: int) -> bool:
        if n_tokens >= self.next_at:
            self.next_at = n_tokens + self.chunk
            return True
        return False


def stream_render(token_iter, vocode, mode: str, first: int, chunk: int, ctx: int, fade, clock=now):
    """Drive the chunked rendering. `token_iter` yields valid speech tokens (ints); `vocode(tokens, finalize) -> np.ndarray` renders a token list at 24 kHz.
    Returns (audio, events, joins, tokens): events = [(seconds since start, cumulative samples)], joins = sample index where one piece meets the next."""
    import numpy as np
    t0 = clock()
    tokens, pieces, events, joins = [], [], [], []
    planner = ChunkPlanner(first, chunk)
    emitted = 0          # samples already handed out
    done = 0             # tokens whose audio has been handed out (mode B)

    def emit(piece):
        nonlocal emitted
        if len(piece) == 0:
            return
        if emitted == 0 and fade is not None:
            n = min(len(fade), len(piece))
            piece = piece.copy()
            piece[:n] = piece[:n] * fade[:n]                         # what S3Gen.inference does to the start of a render (reduces spill-over of the reference)
        if emitted > 0:
            joins.append(emitted)
        pieces.append(piece)
        emitted += len(piece)
        events.append((clock() - t0, emitted))

    for tok in token_iter:
        tokens.append(int(tok))
        if not planner.due(len(tokens)):
            continue
        n = len(tokens)
        if mode == "A":
            wav = vocode(tokens, False)                              # (n - 3) tokens of audio
            emit(wav[emitted:(n - LOOKAHEAD) * TOKEN_SAMPLES])
        else:
            start = max(0, done - ctx)
            wav = vocode(tokens[start:], False)
            lo, hi = (done - start) * TOKEN_SAMPLES, (n - LOOKAHEAD - start) * TOKEN_SAMPLES
            emit(wav[lo:hi])
            done = n - LOOKAHEAD
    # the end: finalize, and drop the last token's audio (it decodes to ~40 ms of noise; generate() does the same)
    n = len(tokens)
    end_samples = max(1, n - 1) * TOKEN_SAMPLES
    if mode == "A":
        wav = vocode(tokens, True)
        emit(wav[emitted:end_samples])
    else:
        start = max(0, done - ctx)
        wav = vocode(tokens[start:], True)
        emit(wav[(done - start) * TOKEN_SAMPLES:end_samples - start * TOKEN_SAMPLES])
    audio = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
    return audio, events, joins, tokens


def simulate_playback(events, sr=SR):
    """Playback starts when the first chunk is ready and runs in real time. Returns the total and the longest stall (ms) caused by chunks that arrive late."""
    if not events:
        return {"stall_ms_total": 0.0, "stall_ms_max": 0.0, "ttfa_s": None}
    t_start = events[0][0]
    clock_shift = 0.0                                   # accumulated stalls push the playback timeline back
    stalls, played_before = [], events[0][1] / sr
    for t, cum in events[1:]:
        due = t_start + clock_shift + played_before     # the moment the audio handed out so far runs out
        if t > due:
            stalls.append(t - due)
            clock_shift += t - due
        played_before = cum / sr
    return {"stall_ms_total": round(sum(stalls) * 1000, 1), "stall_ms_max": round(max(stalls, default=0.0) * 1000, 1), "ttfa_s": round(t_start, 3)}


def click_scores(audio, joins, sr=SR, half_ms=5):
    """At each join: the largest sample-to-sample step within +-2 samples divided by the typical step in the +-5 ms around it (a smooth join is ~1-3, a click is 8+)."""
    import numpy as np
    d = np.abs(np.diff(audio.astype(np.float64)))
    h = max(8, int(sr * half_ms / 1000))
    out = []
    for j in joins:
        if j < 3 or j + 3 >= len(d):
            continue
        around = np.concatenate([d[max(0, j - h):j - 2], d[j + 2:j + h]])
        base = np.median(around) if len(around) else 0.0
        out.append(round(float(d[j - 2:j + 2].max() / max(base, 1e-6)), 2))
    return out


def mel_l1(a, b, sr=SR):
    """Mean absolute log-mel difference between two renders of the same tokens (0 = identical). Needs librosa (installed with chatterbox)."""
    import librosa
    import numpy as np
    n = min(len(a), len(b))
    ma = librosa.feature.melspectrogram(y=a[:n].astype(np.float32), sr=sr, n_fft=1024, hop_length=256, n_mels=80)
    mb = librosa.feature.melspectrogram(y=b[:n].astype(np.float32), sr=sr, n_fft=1024, hop_length=256, n_mels=80)
    return float(np.mean(np.abs(np.log(ma + 1e-5) - np.log(mb + 1e-5))))


# ------------------------------------------------------------------------------------------------ the GPU side
def load_model(ref_wav):
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    if not torch.cuda.is_available():
        raise RuntimeError("no CUDA GPU")
    t = now()
    model = ChatterboxMultilingualTTS.from_pretrained(device="cuda")
    t_load = now() - t
    t = now()
    model.prepare_conditionals(ref_wav)
    return model, {"model_load_s": round(t_load, 2), "prepare_voice_s": round(now() - t, 2)}


def build_token_stream(model):
    """Patch the installed T3.inference into a generator (same code, one `yield` added)."""
    from chatterbox.models.t3 import T3
    ns = dict(vars(sys.modules[T3.__module__]))
    exec(compile(patch_t3_source(inspect.getsource(T3.inference)), "<t3_inference_stream>", "exec"), ns)
    return ns["inference_stream"]


def token_iter_factory(model, text, language, stream_fn, params):
    import torch
    import torch.nn.functional as F
    from chatterbox.mtl_tts import punc_norm
    text = punc_norm(text)
    tt = model.tokenizer.text_to_tokens(text, language_id=language.lower()).to(model.device)
    tt = torch.cat([tt, tt], dim=0)
    tt = F.pad(tt, (1, 0), value=model.t3.hp.start_text_token)
    tt = F.pad(tt, (0, 1), value=model.t3.hp.stop_text_token)
    stop = model.t3.hp.stop_speech_token
    stamps = []

    def gen():
        with torch.inference_mode():
            for tok in stream_fn(model.t3, t3_cond=model.conds.t3, text_tokens=tt, max_new_tokens=1000, **params):
                tid = int(tok.view(-1)[0])
                stamps.append(now())
                if tid == stop:
                    return
                if tid < SPEECH_VOCAB:
                    yield tid
    return gen(), stamps


def make_vocoder(model):
    import torch
    s3 = model.s3gen

    def vocode(tokens, finalize):
        t = torch.tensor([tokens], dtype=torch.long, device=model.device)
        with torch.inference_mode():
            mel = s3.flow_inference(t, ref_dict=model.conds.gen, finalize=finalize).to(dtype=s3.dtype)
            wav, _ = s3.hift_inference(mel, None)
        torch.cuda.synchronize()
        return wav.squeeze(0).float().cpu().numpy()
    return vocode, s3.trim_fade.float().cpu().numpy()


def write_wav(path, audio, sr=SR):
    import numpy as np
    import soundfile as sf
    sf.write(path, np.clip(audio, -1.0, 1.0).astype(np.float32), sr, subtype="PCM_16")


def run_experiment(env, out_dir, status, deadline=float("inf")):
    import numpy as np
    import torch
    seed = int(env.get("SEED", 7))
    torch.manual_seed(seed)
    ref = env.get("REF_WAV", "/workspace/ref_voice.wav")
    language = env.get("LANGUAGE", "hi")
    texts = json.loads(env["SPIKE_TEXTS"]) if env.get("SPIKE_TEXTS") else DEFAULT_TEXTS
    configs = [tuple(int(x) for x in c.split(":")) for c in env.get("CONFIGS", "10:20,16:40").split(",")]
    ctx = int(env.get("CTX", 25))
    res = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "gpu": torch.cuda.get_device_name(0), "torch": torch.__version__, "configs": configs, "ctx": ctx, "items": []}
    try:
        import chatterbox
        res["chatterbox_version"] = getattr(chatterbox, "__version__", None)
    except Exception:
        pass
    model, load = load_model(ref)
    res.update(load, sr=model.sr)
    status("model_loaded", **load)
    from chatterbox.models.s3gen import S3GEN_SR
    assert model.sr == S3GEN_SR == SR, f"unexpected sample rate {model.sr}"
    import inspect as _i
    res["signatures"] = {"flow_inference": str(_i.signature(model.s3gen.flow_inference)), "hift_inference": str(_i.signature(model.s3gen.hift_inference)),
                         "flow.inference": str(_i.signature(model.s3gen.flow.inference))}
    stream_fn = build_token_stream(model)
    vocode, fade = make_vocoder(model)
    params = dict(temperature=0.8, cfg_weight=0.5, repetition_penalty=1.2, min_p=0.05, top_p=1.0)
    blind = []
    # warm-up (CUDA kernels): not measured
    it, _ = token_iter_factory(model, texts[0], language, stream_fn, params)
    toks = list(it)
    vocode(toks, True)
    for i, text in enumerate(texts):
        if now() > deadline:
            res["stopped_early"] = f"MAX_RUN_S reached before text {i}"
            status("deadline", at_text=i)
            break
        item = {"i": i, "words": len(text.split()), "configs": {}}
        # latency reference: the normal one-shot generate() (own random tokens)
        torch.cuda.synchronize()
        t = now()
        wav_gen = model.generate(text, language_id=language).squeeze(0).numpy()
        torch.cuda.synchronize()
        item["generate"] = {"total_s": round(now() - t, 3), "audio_s": round(len(wav_gen) / SR, 3)}
        item["generate"]["rtf"] = round(item["generate"]["total_s"] / max(item["generate"]["audio_s"], 1e-6), 3)
        write_wav(os.path.join(out_dir, f"{i:02d}_{item['words']}w_generate.wav"), wav_gen)
        # the tokens of this item: sampled ONCE, rendered every way
        it, stamps = token_iter_factory(model, text, language, stream_fn, params)
        t = now()
        tokens = list(it)
        torch.cuda.synchronize()
        item["tokens"] = len(tokens)
        item["token_loop_s"] = round(now() - t, 3)
        item["tokens_per_s"] = round((len(stamps) - 1) / max(stamps[-1] - stamps[0], 1e-6), 1) if len(stamps) > 1 else None
        t = now()
        oneshot = vocode(tokens, True)
        oneshot = oneshot.copy()
        oneshot[:len(fade)] *= fade
        oneshot = oneshot[:max(1, len(tokens) - 1) * TOKEN_SAMPLES]
        item["render_oneshot_s"] = round(now() - t, 3)
        item["audio_s"] = round(len(oneshot) / SR, 3)
        write_wav(os.path.join(out_dir, f"{i:02d}_{item['words']}w_oneshot.wav"), oneshot)
        for (first, chunk) in configs:
            for mode in ("A", "B"):
                # the REAL streaming run: tokens are generated and rendered in an interleaved way (that is what a service would do); the speech tokens
                # are sampled afresh, so audio/similarity comparisons below use `replay` (the same tokens as the one-shot above)
                it, _ = token_iter_factory(model, text, language, stream_fn, params)
                t = now()
                audio_live, ev, joins, live_tokens = stream_render(it, vocode, mode, first, chunk, ctx, fade)   # events: seconds since the start of this run
                live_total = now() - t
                # replay: same tokens as the one-shot, to compare the renderings fairly
                audio, _, rjoins, _ = stream_render(iter(tokens), vocode, mode, first, chunk, ctx, fade)
                n = min(len(audio), len(oneshot))
                name = f"{i:02d}_{item['words']}w_{mode}_f{first}_c{chunk}"
                write_wav(os.path.join(out_dir, name + ".wav"), audio)
                sim = simulate_playback(ev)
                item["configs"][f"{mode}_f{first}_c{chunk}"] = {
                    "live_total_s": round(live_total, 3), "live_audio_s": round(len(audio_live) / SR, 3), "live_rtf": round(live_total / max(len(audio_live) / SR, 1e-6), 3),
                    "ttfa_s": sim["ttfa_s"], "stall_ms_total": sim["stall_ms_total"], "stall_ms_max": sim["stall_ms_max"], "chunks": len(ev),
                    "chunk_times_s": [round(e[0], 3) for e in ev][:12],
                    "length_vs_oneshot_samples": len(audio) - len(oneshot), "joins": len(rjoins), "click_scores": click_scores(audio, rjoins),
                    "click_score_oneshot_same_places": click_scores(oneshot, [j for j in rjoins if j < len(oneshot)]),
                    "mel_l1_vs_oneshot": round(mel_l1(audio[:n], oneshot[:n]), 4)}
                blind.append(name)
        res["items"].append(item)
        status("item_done", i=i)
        with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    # a blind listening set: random names, the key in a separate file
    import random
    rnd = random.Random(seed)
    files = []
    for i in range(min(4, len(texts))):
        files += [f"{i:02d}_{len(texts[i].split())}w_oneshot", f"{i:02d}_{len(texts[i].split())}w_A_f{configs[0][0]}_c{configs[0][1]}",
                  f"{i:02d}_{len(texts[i].split())}w_B_f{configs[0][0]}_c{configs[0][1]}"]
    order = files[:]
    rnd.shuffle(order)
    os.makedirs(os.path.join(out_dir, "blind"), exist_ok=True)
    key = {}
    for k, f in enumerate(order, 1):
        src = os.path.join(out_dir, f + ".wav")
        if os.path.exists(src):
            dst = f"blind/sample_{k:02d}.wav"
            with open(src, "rb") as a, open(os.path.join(out_dir, dst), "wb") as b:
                b.write(a.read())
            key[dst] = f
    with open(os.path.join(out_dir, "blind_key_DO_NOT_OPEN_BEFORE_LISTENING.json"), "w") as f:
        json.dump(key, f, indent=1)
    res["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    res["peak_gpu_mem_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 2)
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    return res


# ------------------------------------------------------------------------------------------------ the shell: status, deadline, serving
def main():
    env = os.environ
    out_parent = env.get("OUT_DIR", "/workspace/out")
    token = env.get("RESULT_TOKEN", "result")
    out_dir = os.path.join(out_parent, token)
    os.makedirs(out_dir, exist_ok=True)
    t_start = now()

    def status(state, **kw):
        with open(os.path.join(out_dir, "status.json"), "w") as f:
            json.dump({"state": state, "elapsed_s": round(now() - t_start, 1), **kw}, f)
        print(f"[spike] {state} {kw}", flush=True)

    status("starting")
    deadline = t_start + float(env.get("MAX_RUN_S", 1500))     # checked between texts; a hung GPU call is stopped by the launcher's own deadline (it deletes the pod)
    try:
        run_experiment(env, out_dir, status, deadline)
        status("done")
    except BaseException as e:                                   # the diagnosis is the result: a failure must be readable without a log viewer
        tb = traceback.format_exc()
        with open(os.path.join(out_dir, "error.txt"), "w") as f:
            f.write(tb)
        status("error", error=f"{type(e).__name__}: {str(e)[:300]}")
    if env.get("NO_SERVE") == "1":                               # the launcher's start command already serves the output folder
        return
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=out_parent)
    with socketserver.ThreadingTCPServer(("0.0.0.0", 8000), handler) as srv:
        srv.timeout = 1
        end = now() + float(env.get("SERVE_S", 600))
        while now() < end:
            srv.handle_request()


# ------------------------------------------------------------------------------------------------ self-check with a fake model (no GPU, no torch)
def self_check():
    import numpy as np
    tone = lambda n: np.sin(np.arange(n) * 0.05).astype(np.float32)   # noqa: E731

    def fake_vocode(tokens, finalize):
        """Context-independent: the audio of token k depends on k only, so every way of cutting must reproduce the one-shot exactly."""
        n = len(tokens) if finalize else max(0, len(tokens) - LOOKAHEAD)
        return np.concatenate([tone(TOKEN_SAMPLES) * (1 + (t % 7)) / 8 for t in tokens[:n]]) if n else np.zeros(0, np.float32)

    toks = list(range(1, 131))
    one = fake_vocode(toks, True)[:max(1, len(toks) - 1) * TOKEN_SAMPLES]
    class Tick:                                         # a clock that advances 0.1 s per reading
        t = 0.0

        def __call__(self):
            Tick.t += 0.1
            return Tick.t
    for mode in ("A", "B"):
        audio, events, joins, got = stream_render(iter(toks), fake_vocode, mode, 10, 20, 25, None, clock=Tick())
        assert got == toks, mode
        assert len(audio) == len(one) and np.allclose(audio, one), f"mode {mode}: chunked != one-shot"
        assert joins == sorted(joins) and len(joins) == len(events) - 1 and all(0 < j < len(audio) for j in joins)
    print("self-check OK: A and B reproduce the one-shot render sample for sample with a context-free vocoder;", len(events), "chunks,", len(joins), "joins")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        self_check()
    else:
        main()
