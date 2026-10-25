# TTS worker for RunPod (Chatterbox Multilingual), version v3

`handler.py` in this folder is the RunPod Serverless handler of the TTS worker `mme-tts-runpod`. It is **not** part of the telephony service: it is
built separately from the worker's own GitHub repo (`aitechteammme-dev`) into the image `ghcr.io/aitechteammme-dev/mme-tts-runpod`. It is kept here so
the source is not lost.

What v3 changes against the version in `handler (5).py`:

* every voice is prepared **once per worker, at start-up**, and kept by **name** (the baked `/app/ref_voice.wav` is the voice `utkarsh`; any
  `/app/voices/<name>.wav` is added too). A request chooses it with `"voice"`;
* a reference sent in the request is **ignored** (old clients keep working and use the baked voice); `ALLOW_REQUEST_REF=1` re-enables the old, slow path;
* new `"output_format": "ulaw8k"`: 8 kHz G.711 mu-law, 8000 bytes per second of speech (`wav` stays the default, `pcm16` is unchanged);
* the start-up log names each voice and how long it took to prepare;
* `python handler.py --self-test` runs 23 checks with a fake model: no GPU, no torch, no chatterbox, no network.

Request: `{"input": {"text": "...", "language": "hi", "voice": "utkarsh", "output_format": "ulaw8k"}}`
Response: `{"audio_base64", "format", "sample_rate", "language", "voice", "timing": {"generate_s", "total_s", "audio_s"}}` or `{"error": "..."}`.

## Build v3 and test it WITHOUT touching the live endpoint

The live endpoint (`93tqgnxb5dyhvx`) must not change. The existing workflow pushes the tags `latest` and `v2` on every push to **main**, so **never push this
to main for the test**: work on a branch, build only the tag `v3`, and point a NEW test endpoint at it.

1. **Branch.** In your clone of the worker repo: `git checkout -b tts-v3`. (Do not merge it to main until the test is accepted.)
2. **Replace `handler.py`** in the repo root with the `handler.py` of this folder (the whole file).
3. **`requirements.txt`:** add one line, `scipy>=1.11` (it is needed for the 24 kHz -> 8 kHz resample; it normally arrives with `librosa`, but only
   indirectly). If the build log reports a numpy conflict with chatterbox, use `scipy>=1.11,<1.16` instead.
4. **Replace the `Dockerfile` with the one of this folder, and put `ref_voice.wav` in the repo root.** The Dockerfile of the repo at the time of writing had NO
   bake lines: it did not copy a reference voice into the image and did not download the model weights at build time. With it, v3 would have no voice
   (every request fails with `Unknown voice`), and every cold worker would download the weights from Hugging Face. The v3 Dockerfile adds exactly five lines:
   `ENV HF_HOME=/app/hf_cache`, `RUN python -c "...snapshot_download('ResembleAI/chatterbox')"`, `COPY ref_voice.wav /app/ref_voice.wav`,
   `ENV HF_HUB_OFFLINE=1` and `ENV DEFAULT_REF_WAV=/app/ref_voice.wav`; everything else is yours unchanged.
   `ref_voice.wav` is the 12-second, 24 kHz, mono, 16-bit recording of the voice (the same file the telephony service sends today with every request, so the voice
   stays the same). It must be committed to the worker repo. Optional: `COPY voices/ /app/voices/` only if you want more voices.
5. **Optional check on your own machine (no GPU):** `python -m venv v && v/bin/pip install numpy scipy soundfile && v/bin/python handler.py --self-test`
   (on Windows: `v\Scripts\...`). Expected last line: `23/23 checks passed`. Do not push if it says FAILED.
6. **Add the separate workflow:** copy `build-image-v3-test.yml` of this folder to `.github/workflows/build-image-v3-test.yml` on the branch. Do not edit
   or delete the existing `build-image.yml`. The new workflow runs only for pushes to `tts-v3` and pushes only `ghcr.io/aitechteammme-dev/mme-tts-runpod:v3`.
7. **Push:** `git add handler.py Dockerfile ref_voice.wav requirements.txt .github/workflows/build-image-v3-test.yml && git commit -m "TTS worker v3" && git push -u origin tts-v3`.
   GitHub -> Actions -> "Build TTS v3 test image" must finish green. The build now downloads the model weights into the image: allow 20 to 40 minutes, and
   expect a bigger image (several GB more), which is what makes cold starts faster.
8. **Check the package:** GitHub -> your organisation -> Packages -> `mme-tts-runpod`: the tag `v3` exists, and the digests of `latest` and `v2` did not change.
9. **RunPod: a NEW template and a NEW endpoint (never edit the live ones).**
   * Serverless -> Templates -> open the template of the live TTS endpoint -> **Clone**. Name it `mme-tts-v3-test`. Change only the image to
     `ghcr.io/aitechteammme-dev/mme-tts-runpod:v3`. Keep the container disk, registry credential and environment variables of the original.
   * Serverless -> **New Endpoint** -> select `mme-tts-v3-test`. Name `mme-tts-v3-test`. Same GPU type, FlashBoot and execution timeout as the live endpoint
     (so the numbers are comparable). **Active workers 0, Max workers 3** (three, so the concurrency probe can run 3 jobs at once), idle timeout 60 s.
   * Send me the new **endpoint ID**. My tools read the API key from the tenant configuration; nothing needs to be pasted.
10. **First start.** The first worker has to pull the image (several GB): it can take 5 to 15 minutes. In the endpoint's **Requests** tab send
    `{"input": {"warmup": true}}` to start a worker without synthesising anything. Then open Workers -> the worker -> **Logs**.

### The first log lines to read (in this order)

```
[TTS] model loaded in 12.34s
[TTS] voice 'utkarsh' prepared in 1.20s (source /app/ref_voice.wav)
[TTS] warmup done in 3.10s
[TTS] ready: voices=['utkarsh'] default=utkarsh sample_rate=24000 formats=wav,pcm16,ulaw8k request_reference=ignored
```

(The times are examples.) Then a console test request,
`{"input": {"text": "नमस्ते, कैसे हैं आप?", "language": "hi", "output_format": "ulaw8k"}}`, must answer with `"format": "ulaw8k"`, `"sample_rate": 8000`,
`"voice": "utkarsh"` and log `[TTS_TIMING] ... ref_s=0.000 voice=utkarsh ... fmt=ulaw8k`. **`ref_s=0.000` on every request is the point of v3.**

### What each failure means

| What you see | Meaning | What to do |
|---|---|---|
| Image pull fails, `unauthorized` / `not found` | The GHCR package is private and the template has no registry credential, or the tag `v3` was not built | Same credential as the live template; check step 8 |
| `ModuleNotFoundError: No module named 'scipy'` | The `scipy` line is missing from `requirements.txt`, or the image was built before it was added | Add the line, push again, rebuild |
| `ModuleNotFoundError: ... chatterbox` / `soundfile` / `runpod` | `requirements.txt` of the repo lacks it (the old handler needed the same) | Add it |
| `RuntimeError: CUDA GPU is not available` | The endpoint got a worker without a GPU or the wrong GPU type | Use the same GPU type as the live endpoint |
| The GitHub build fails with `COPY failed: ... ref_voice.wav: not found` | `ref_voice.wav` is not in the repo root of the branch | Commit it (step 4) and push again |
| `[TTS] WARNING: default reference not found at /app/ref_voice.wav` and later `Unknown voice` | The image was built from the OLD Dockerfile (no `COPY ref_voice.wav`) | Use the v3 Dockerfile (step 4) |
| At start-up an `OSError` / `LocalEntryNotFoundError` about "offline mode" or a file "not found in the cache" | `HF_HUB_OFFLINE=1` is set but the build-time download did not include a file the model asks for | Remove the line `ENV HF_HUB_OFFLINE=1` from the Dockerfile and rebuild (the worker then fetches the missing file at start-up); tell me the file name |
| The build step `snapshot_download` fails (network / rate limit) | Hugging Face was unreachable from GitHub's runner | Re-run the workflow |
| `RuntimeError: the model has no .conds after prepare_conditionals()` | The installed Chatterbox keeps the prepared voice under a different attribute | Send me this line and the Chatterbox version; do not use this image |
| `[TTS] voice 'utkarsh' prepared in` takes much longer than 3 s | The GPU is slow or the model is still loading from disk | Note the time; it is a one-off per worker |
| response `{"error": "Unknown voice: 'x'", "voices": [...]}` | The request named a voice that is not baked | Use a name from `voices` |
| response `{"error": "output_format must be 'wav', 'pcm16' or 'ulaw8k'"}` | The request reached an OLD worker (v2): the endpoint still runs the old image | Check the template image |
| response `{"error": "ulaw8k needs a model rate that is a multiple of 8000 ..."}` | The model's sample rate is not 24000 | Send me the rate in the message |
| worker restarts in a loop | An exception at start-up: read the first traceback in the log | Send me the first 20 log lines |

## Rolling back / promoting

* The test costs nothing to undo: delete the endpoint `mme-tts-v3-test` and the template `mme-tts-v3-test`. The live endpoint and the tenant configuration
  were never touched; the image tag `v3` is harmless.
* Promotion comes later, after the re-measurement and a listening test: change the image of the LIVE template to `:v3` (or point the tenant's
  `tts_url` at the new endpoint) and turn on the tenant flag `tts_ulaw8k` of the telephony service. Not before.
