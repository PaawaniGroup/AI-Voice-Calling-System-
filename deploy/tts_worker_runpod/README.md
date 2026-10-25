# TTS worker for RunPod (Chatterbox Multilingual), version v4

`handler.py` in this folder is the RunPod Serverless handler of the TTS worker `mme-tts-runpod`. It is **not** part of the telephony service: it is
built separately from the worker's own GitHub repo (`aitechteammme-dev`) into the image `ghcr.io/aitechteammme-dev/mme-tts-runpod`. It is kept here so
the source is not lost.

## v3 had a start-up bug, v4 fixes it
The first v3 image **crashed every worker at start-up**, and requests then hung until the 300 s execution timeout. The log line is:

```
RuntimeError: Only Tensors created explicitly by the user (graph leaves) support the deepcopy protocol at the moment
```

Cause: v3 kept each prepared voice with `copy.deepcopy(...)`. Chatterbox's prepared voice (`model.conds`) holds tensors that are results of computations (`prepare_conditionals`
has no `no_grad`), and PyTorch refuses to deep-copy those. v4 copies the voice object **shallowly** (the object and its small reference dict; the tensors are shared and
never edited). The v3 self-test did not catch it because its fake voice object was a plain dict; the v4 self-test uses a fake whose `__deepcopy__` raises the same error,
and **fails on the v3 code**.

What v4 does (everything v3 promised, plus `t3_model`):
* every voice is prepared **once per worker, at start-up**, and kept by **name** (the baked `/app/ref_voice.wav` is the voice `utkarsh`; any
  `/app/voices/<name>.wav` is added too). A request chooses it with `"voice"`;
* a reference sent in the request is **ignored** (old clients keep working and use the baked voice); `ALLOW_REQUEST_REF=1` re-enables the old, slow path;
* new `"output_format": "ulaw8k"`: 8 kHz G.711 mu-law, 8000 bytes per second of speech (`wav` stays the default, `pcm16` is unchanged);
* `t3_model` (environment variable `T3_MODEL`, default `v3`) is passed to `from_pretrained` **only if** the installed Chatterbox names that parameter; otherwise a
  `WARNING` is logged. The "model loaded" line shows the one used: `[TTS] model loaded in 12.34s (t3_model=v3)`;
* the start-up log names each voice and how long it took to prepare;
* `python handler.py --self-test` runs **30 checks** with a fake model: no GPU, no torch, no chatterbox, no network.

Request: `{"input": {"text": "...", "language": "hi", "voice": "utkarsh", "output_format": "ulaw8k"}}`
Response: `{"audio_base64", "format", "sample_rate", "language", "voice", "timing": {"generate_s", "total_s", "audio_s"}}` or `{"error": "..."}`.

## Build v4 and test it WITHOUT touching the live endpoint

**Never test on the live endpoint.** The existing workflow pushes the tags `latest` and `v2` on every push to **main**, so **never push this to main**: work on the branch
`tts-v3` (the name stays), build only the tag `v4`, and point a NEW test endpoint at it. The live endpoint stays on its rolled-back release until v4 has passed.

1. **Branch.** `git checkout tts-v3` (it exists from the v3 attempt; otherwise `git checkout -b tts-v3`). Do not merge it to main until the test is accepted.
2. **Replace `handler.py`** in the repo root with the `handler.py` of this folder (the whole file).
3. **`requirements.txt`:** it needs the line `scipy>=1.11` (for the 24 kHz -> 8 kHz resample). If you added it for v3 it is already there. If the build log reports a numpy
   conflict with chatterbox, use `scipy>=1.11,<1.16` instead.
4. **`Dockerfile` and `ref_voice.wav`:** the same as for v3 (the Dockerfile of this folder; `ref_voice.wav` in the repo root). No change for v4.
5. **Optional check on your own machine (no GPU):** `python -m venv v && v/bin/pip install numpy scipy soundfile && v/bin/python handler.py --self-test`
   (on Windows: `v\Scripts\...`). Expected last line: **`30/30 checks passed`**. Do not push if it says FAILED.
6. **The workflow:** replace the content of your existing `.github/workflows/build-image-v3-test.yml` with the file of this folder (same name, same trigger: pushes to
   `tts-v3` or a manual run). Only the tag changes: it now pushes `ghcr.io/aitechteammme-dev/mme-tts-runpod:v4` and nothing else. Do not edit `build-image.yml`.
7. **Push:** `git add handler.py .github/workflows/build-image-v3-test.yml && git commit -m "TTS worker v4: shallow-copy the voice" && git push origin tts-v3`.
   GitHub -> Actions -> "Build TTS v4 test image" must finish green (the model weights are baked in: allow 20 to 40 minutes).
8. **Check the package:** GitHub -> Packages -> `mme-tts-runpod`: the tag `v4` exists, and the digests of `latest` and `v2` did not change. (The tag `v3` is the broken
   image: do not use it.)
9. **RunPod: a NEW template and a NEW endpoint (never edit the live ones).**
   * Templates -> clone the template of the live TTS endpoint as `mme-tts-v4-test`; change only the image to `ghcr.io/aitechteammme-dev/mme-tts-runpod:v4`.
   * Serverless -> **New Endpoint** from that template, name `mme-tts-v4-test`: 24 GB GPU, FlashBoot on, execution timeout 300 s, scaling type request count with value 1,
     **Active workers 0**, **Max workers 2** (3 for the concurrency probe), **idle timeout 60 s** (10 s for the probe).
   * Send me the new **endpoint ID**.
10. **First start.** Allow 5 to 15 minutes for the image pull. In the **Requests** tab send `{"input": {"warmup": true}}` to start a worker without synthesising anything,
    then open Workers -> the worker -> **Logs**.

### The first log lines to read (in this order)

```
[TTS] model loaded in 12.34s (t3_model=v3)
[TTS] voice 'utkarsh' prepared in 1.20s (source /app/ref_voice.wav)
[TTS] warmup done in 3.10s
[TTS] ready: voices=['utkarsh'] default=utkarsh sample_rate=24000 formats=wav,pcm16,ulaw8k request_reference=ignored
```

(The times are examples.) Then a console test request,
`{"input": {"text": "नमस्ते, कैसे हैं आप?", "language": "hi", "output_format": "ulaw8k"}}`, must answer with `"format": "ulaw8k"`, `"sample_rate": 8000`,
`"voice": "utkarsh"` and log `[TTS_TIMING] ... ref_s=0.000 voice=utkarsh ... fmt=ulaw8k`. **`ref_s=0.000` on every request is the point of v3/v4.**

### What each failure means

| What you see | Meaning | What to do |
|---|---|---|
| **`RuntimeError: Only Tensors created explicitly by the user (graph leaves) support the deepcopy protocol at the moment`**, at start-up, right after `[TTS] model loaded` and before any `voice ... prepared` line | **The v3 image.** It deep-copied the prepared voice, which PyTorch refuses for Chatterbox's tensors. Every worker crashes in a loop; requests wait until the 300 s execution timeout and get no answer | Never use the `v3` tag. Roll the endpoint back to its previous release, build **v4** (this README) and test it on a NEW endpoint |
| `[TTS] WARNING: this Chatterbox version's from_pretrained has no t3_model parameter: T3_MODEL='v3' is ignored` | The installed Chatterbox is older: it has no `t3_model` choice. The library's default model is used (the "model loaded" line then says `t3_model=not-supported`) | Fine if that default is the model you want; otherwise install a newer chatterbox in `requirements.txt` |
| `ValueError: Unknown multilingual T3 model '...'` at start-up | `T3_MODEL` is set to a name Chatterbox does not know (`v3`, `t3_mtl23ls_v3` or a `.safetensors` file name are valid) | Fix or remove the `T3_MODEL` environment variable of the template |
| Image pull fails, `unauthorized` / `not found` | The GHCR package is private and the template has no registry credential, or the tag `v4` was not built | Same credential as the live template; check step 8 |
| `ModuleNotFoundError: No module named 'scipy'` | The `scipy` line is missing from `requirements.txt`, or the image was built before it was added | Add the line, push again, rebuild |
| `ModuleNotFoundError: ... chatterbox` / `soundfile` / `runpod` | `requirements.txt` of the repo lacks it (the old handler needed the same) | Add it |
| `RuntimeError: CUDA GPU is not available` | The endpoint got a worker without a GPU or the wrong GPU type | Use the same GPU type as the live endpoint |
| The GitHub build fails with `COPY failed: ... ref_voice.wav: not found` | `ref_voice.wav` is not in the repo root of the branch | Commit it and push again |
| `[TTS] WARNING: default reference not found at /app/ref_voice.wav` and later `Unknown voice` | The image was built from the OLD Dockerfile (no `COPY ref_voice.wav`) | Use the Dockerfile of this folder |
| At start-up an `OSError` / `LocalEntryNotFoundError` about "offline mode" or a file "not found in the cache" | `HF_HUB_OFFLINE=1` is set but the build-time download did not include a file the model asks for | Remove the line `ENV HF_HUB_OFFLINE=1` from the Dockerfile and rebuild (the worker then fetches the missing file at start-up); tell me the file name |
| The build step `snapshot_download` fails (network / rate limit) | Hugging Face was unreachable from GitHub's runner | Re-run the workflow |
| `RuntimeError: the model has no .conds after prepare_conditionals()` | The installed Chatterbox keeps the prepared voice under a different attribute | Send me this line and the Chatterbox version; do not use this image |
| `[TTS] voice 'utkarsh' prepared in` takes much longer than 3 s | The GPU is slow or the model is still loading from disk | Note the time; it is a one-off per worker |
| response `{"error": "Unknown voice: 'x'", "voices": [...]}` | The request named a voice that is not baked | Use a name from `voices` |
| response `{"error": "output_format must be 'wav' or 'pcm16'"}` | The request reached an OLD worker (the handler without ulaw8k): the endpoint still runs the old image | Check the template image |
| response `{"error": "ulaw8k needs a model rate that is a multiple of 8000 ..."}` | The model's sample rate is not 24000 | Send me the rate in the message |
| worker restarts in a loop, or a request hangs until the execution timeout | An exception at start-up: read the first traceback in the worker log | Send me the first 20 log lines |

## Rolling back / promoting

* The test costs nothing to undo: delete the endpoint `mme-tts-v4-test` and the template `mme-tts-v4-test`. The live endpoint and the tenant configuration are never
  touched by the test.
* Promotion comes later, after the re-measurement and a listening test: change the image of the LIVE template to `:v4` (or point the tenant's `tts_url` at the new
  endpoint) and turn on the tenant flag `tts_ulaw8k` of the telephony service. Not before.
