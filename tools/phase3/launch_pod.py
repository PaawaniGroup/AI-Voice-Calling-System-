# -*- coding: utf-8 -*-
"""
tools/phase3/launch_pod.py - run the Phase 3 spike (tools/phase3/chunked_tts_spike.py) on a RunPod GPU Pod, fetch the results, DELETE the pod. CHARGEABLE.

    python tools/phase3/launch_pod.py --check                                   # FREE: balance and GPU prices only (read-only GraphQL)
    python tools/phase3/launch_pod.py --dry-run                                 # FREE: prints what would be sent (no key, no network)
    python tools/phase3/launch_pod.py --cap 1.00 --max-minutes 40 --out-dir <folder OUTSIDE the repo> --yes

The API key: a RunPod ACCOUNT key with Pod permissions, from the environment variable RUNPOD_ADMIN_KEY or --key-file (a file OUTSIDE the repo). It is never printed,
logged or put into the pod. The endpoint keys stored for the tenant do NOT work here (they are restricted to serverless: RunPod answers 401 on the Pod API).
Money safety (each is tested offline with a fake RunPod):
  * refuses to start when the balance is below the cap + a margin, or when no GPU price can be read;
  * picks the cheapest of a short list of 24 GB class GPUs, and refuses a Pod whose reported cost per hour is above --max-price-hr;
  * a hard deadline = min(--max-minutes, cap / price): when it is reached the Pod is deleted whatever state the experiment is in;
  * the Pod is deleted in EVERY path (success, error, timeout, Ctrl-C, any exception) and the deletion is verified; if the REST delete fails the GraphQL
    mutation is tried; if both fail the Pod id is printed loudly so it can be removed by hand in the console;
  * the Pod also runs a best-effort self-delete timer (`runpodctl remove pod`); that is NOT relied on (RunPod's documentation does not say what the pod key may do).
The results (audio + metrics) are downloaded to --out-dir, which must be outside the repository (the commit guard refuses audio).
"""
import argparse, asyncio, base64, json, re, secrets, sys, time, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
REST = "https://rest.runpod.io/v1"
GRAPHQL = "https://api.runpod.io/graphql"
IMAGE = "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime"           # the image family of the TTS worker, so the CUDA / torch stack is comparable
GPU_CANDIDATES = ["NVIDIA GeForce RTX 4090", "NVIDIA RTX A5000", "NVIDIA L4", "NVIDIA GeForce RTX 3090", "NVIDIA RTX A4500", "NVIDIA RTX A6000"]
ENV_PART = 90_000                                                  # characters of base64 per environment variable (one variable is limited to ~128 KB)
BALANCE_MARGIN = 0.25                                              # keep at least this much on the account beyond the cap


class Refused(Exception):
    pass


# ------------------------------------------------------------------------------------------------ pure helpers (unit-tested)
def split_env(name: str, data: str, size: int = ENV_PART) -> dict:
    return {f"{name}_{i}": data[off:off + size] for i, off in enumerate(range(0, len(data), size))}


def pack_reference(wav_bytes: bytes, seconds: float = 10.0, rate: int = 24000) -> str:
    """The first `seconds` of a 16-bit mono PCM WAV (Chatterbox only uses the first 10 s for the decoder conditioning, 6 s for the encoder), zlib + base64."""
    i = wav_bytes.find(b"data")
    if i < 0:
        raise Refused("the reference is not a PCM WAV (no data chunk)")
    pcm = wav_bytes[i + 8:i + 8 + int(seconds * rate) * 2]
    header = wav_bytes[:i + 4] + len(pcm).to_bytes(4, "little")
    riff = bytearray(header + pcm)
    riff[4:8] = (len(riff) - 8).to_bytes(4, "little")
    return base64.b64encode(zlib.compress(bytes(riff), 9)).decode("ascii")


def pick_gpu(gpu_types: list, candidates=GPU_CANDIDATES, cloud="SECURE"):
    """The cheapest candidate with a known price. Returns (id, price per hour) or raises Refused."""
    key = "securePrice" if cloud == "SECURE" else "communityPrice"
    best = None
    for g in gpu_types or []:
        if g.get("id") in candidates and isinstance(g.get(key), (int, float)) and g[key] > 0 and (g.get("memoryInGb") or 0) >= 20:
            if best is None or g[key] < best[1]:
                best = (g["id"], float(g[key]))
    if best is None:
        raise Refused(f"no price could be read for any of {len(candidates)} candidate GPUs on the {cloud} cloud: nothing is started")
    return best


def deadline_seconds(cap_usd: float, price_hr: float, max_minutes: float) -> float:
    return min(max_minutes * 60.0, cap_usd / (price_hr / 3600.0) * 0.9)


def start_command(token: str, serve_s: int, hard_stop_s: int) -> list:
    """What the Pod runs: unpack the script and the reference from environment variables, serve a progress folder over HTTP, install chatterbox, run the spike."""
    script = f"""set -u
OUT=/workspace/out; mkdir -p $OUT/{token}
python3 -m http.server 8000 --directory $OUT >/dev/null 2>&1 &
( sleep {hard_stop_s}; runpodctl remove pod $RUNPOD_POD_ID >/dev/null 2>&1 || true ) &
echo "$SPIKE_PY_B64" | base64 -d > /workspace/spike.py
python3 - <<'PY'
import os, base64, zlib
parts = [os.environ[k] for k in sorted((k for k in os.environ if k.startswith("REF_PART_")), key=lambda k: int(k.rsplit("_", 1)[1]))]
open("/workspace/ref_voice.wav", "wb").write(zlib.decompress(base64.b64decode("".join(parts))))
PY
echo '{{"state": "installing"}}' > $OUT/{token}/status.json
( apt-get update -qq && apt-get install -y -qq git libsndfile1 ffmpeg ) > $OUT/{token}/install.log 2>&1
( pip install -q chatterbox-tts || pip install -q git+https://github.com/resemble-ai/chatterbox.git ) >> $OUT/{token}/install.log 2>&1
pip list 2>/dev/null | grep -i -E "^(chatterbox|torch|torchaudio|numpy|scipy|librosa|transformers) " >> $OUT/{token}/install.log
NO_SERVE=1 python3 /workspace/spike.py
sleep {serve_s}
runpodctl remove pod $RUNPOD_POD_ID >/dev/null 2>&1 || true
"""
    return ["bash", "-lc", script]


def build_body(gpu_id, cloud, token, script_py: str, ref_b64: str, serve_s: int, hard_stop_s: int, max_run_s: int) -> dict:
    env = {"SPIKE_PY_B64": base64.b64encode(script_py.encode("utf-8")).decode("ascii"), "RESULT_TOKEN": token, "OUT_DIR": "/workspace/out", "REF_WAV": "/workspace/ref_voice.wav",
           "MAX_RUN_S": str(max_run_s), "SERVE_S": str(serve_s), "HF_HUB_DISABLE_TELEMETRY": "1", **split_env("REF_PART", ref_b64)}
    return {"name": "mme-phase3-spike", "imageName": IMAGE, "gpuTypeIds": [gpu_id], "gpuCount": 1, "cloudType": cloud, "containerDiskInGb": 40, "volumeInGb": 0,
            "ports": ["8000/http"], "env": env, "dockerStartCmd": start_command(token, serve_s, hard_stop_s), "interruptible": False}


def describe(body: dict) -> dict:
    """The request with every value summarised (sizes, not contents): safe to print."""
    b = dict(body)
    b["env"] = {k: f"<{len(v)} chars>" for k, v in body["env"].items()}
    b["dockerStartCmd"] = [body["dockerStartCmd"][0], body["dockerStartCmd"][1], f"<script of {len(body['dockerStartCmd'][2])} chars>"]
    return b


def proxy_url(pod_id: str, token: str, name: str = "") -> str:
    return f"https://{pod_id}-8000.proxy.runpod.net/{token}/{name}"


def hrefs(html: str) -> list:
    return [h for h in re.findall(r'href="([^"?#]+)"', html) if not h.startswith(("/", "..", "http"))]


# ------------------------------------------------------------------------------------------------ the RunPod calls
class RunPod:
    def __init__(self, http, key: str):
        self.http, self._h = http, {"Authorization": f"Bearer {key}"}

    async def graphql(self, query: str) -> dict:
        r = await self.http.post(GRAPHQL, json={"query": query}, headers=self._h, timeout=30)
        if r.status_code in (401, 403):
            raise Refused("RunPod refused this key for the account API (HTTP %d). The endpoint keys stored for the tenant are restricted to serverless and cannot do this: "
                          "use an account key with Pod permissions (--key-file)." % r.status_code)
        r.raise_for_status()
        j = r.json()
        if j.get("errors"):
            raise Refused("GraphQL: " + str(j["errors"][0].get("message"))[:120])
        return j["data"]

    async def account(self) -> dict:
        return await self.graphql("query { myself { clientBalance currentSpendPerHr } gpuTypes { id displayName memoryInGb securePrice communityPrice } }")

    async def create(self, body: dict) -> dict:
        r = await self.http.post(f"{REST}/pods", json=body, headers=self._h, timeout=60)
        if r.status_code in (401, 403):
            raise Refused("RunPod refused this key for creating a Pod (HTTP %d): it needs Pod write permission." % r.status_code)
        if r.status_code >= 400:
            raise Refused(f"create failed: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    async def get(self, pod_id: str):
        r = await self.http.get(f"{REST}/pods/{pod_id}", headers=self._h, timeout=30)
        return None if r.status_code == 404 else r.json()

    async def delete(self, pod_id: str) -> bool:
        """Delete and VERIFY. REST first, the GraphQL mutation as a fallback."""
        for attempt in range(3):
            try:
                r = await self.http.delete(f"{REST}/pods/{pod_id}", headers=self._h, timeout=30)
                if r.status_code < 300 or r.status_code == 404:
                    break
            except Exception:
                pass
            await asyncio.sleep(1 + attempt)
        else:
            try:
                await self.graphql('mutation { podTerminate(input: {podId: "%s"}) }' % pod_id)
            except Exception:
                pass
        try:
            p = await self.get(pod_id)
            return p is None or p.get("desiredStatus") in ("TERMINATED", "EXITED")
        except Exception:
            return False


# ------------------------------------------------------------------------------------------------ the run
async def run(a, http, key: str, script_py: str, ref_wav: bytes, out_dir: Path, sleep=asyncio.sleep, clock=time.monotonic, log=print):
    rp = RunPod(http, key)
    acct = await rp.account()
    me = acct.get("myself") or {}
    balance = me.get("clientBalance")
    if not isinstance(balance, (int, float)):
        raise Refused("the account balance could not be read: nothing is started")
    if balance < a.cap + BALANCE_MARGIN:
        raise Refused(f"balance ${balance:.2f} is below the cap ${a.cap:.2f} + margin ${BALANCE_MARGIN:.2f}: nothing is started")
    gpu_id, price = pick_gpu(acct.get("gpuTypes"), cloud=a.cloud)
    if price > a.max_price_hr:
        raise Refused(f"the cheapest candidate costs ${price:.2f}/h, above --max-price-hr ${a.max_price_hr:.2f}: nothing is started")
    limit = deadline_seconds(a.cap, price, a.max_minutes)
    log(f"balance ${balance:.2f} | GPU {gpu_id} at ${price:.3f}/h ({a.cloud}) | hard deadline {limit / 60:.1f} min = at most ${price * limit / 3600:.2f} (cap ${a.cap:.2f})")
    token = secrets.token_urlsafe(12)
    max_run, serve_s = int(limit - 300), 120
    body = build_body(gpu_id, a.cloud, token, script_py, pack_reference(ref_wav), serve_s, int(limit) + 60, max(max_run, 300))
    pod_id, t_created, result = None, None, {"ok": False}
    try:
        pod = await rp.create(body)
        pod_id = pod.get("id")
        t_created = clock()
        cost_hr = pod.get("costPerHr")
        log(f"pod {pod_id} created (reported cost ${cost_hr}/h)" if cost_hr is not None else f"pod {pod_id} created")
        if isinstance(cost_hr, (int, float)) and cost_hr > a.max_price_hr:
            raise Refused(f"the pod reports ${cost_hr}/h, above --max-price-hr: deleting it")
        state = None
        while clock() - t_created < limit:
            await sleep(10)
            try:
                import httpx
                r = await http.get(proxy_url(pod_id, token, "status.json"), timeout=20)
                if r.status_code == 200:
                    st = r.json()
                    if st.get("state") != state:
                        state = st.get("state")
                        log(f"[{(clock() - t_created) / 60:4.1f} min] spike state: {state} {({k: v for k, v in st.items() if k not in ('state', 'elapsed_s')} or '')}")
                    if state in ("done", "error", "deadline"):
                        break
            except Exception:
                pass
        else:
            log(f"hard deadline reached after {limit / 60:.1f} min")
        out_dir.mkdir(parents=True, exist_ok=True)
        got = []
        try:
            idx = await http.get(proxy_url(pod_id, token), timeout=30)
            listing = list(dict.fromkeys(hrefs(idx.text))) if idx.status_code == 200 else []      # each link once
            for name in listing:
                if name.endswith("/"):                                                              # a sub-folder (the blind listening set): its files, one level
                    sub = await http.get(proxy_url(pod_id, token, name), timeout=30)
                    names = [name + h for h in dict.fromkeys(hrefs(sub.text))] if sub.status_code == 200 else []
                else:
                    names = [name]
                for n in names:
                    f = await http.get(proxy_url(pod_id, token, n), timeout=120)
                    if f.status_code == 200:
                        (out_dir / n).parent.mkdir(parents=True, exist_ok=True)
                        (out_dir / n).write_bytes(f.content)
                        got.append(n)
        except Exception as e:
            log(f"download problem: {type(e).__name__}")
        result.update(ok=state == "done", state=state, files=got)
    finally:
        if pod_id:
            deleted = await rp.delete(pod_id)
            spent = (clock() - t_created) / 3600 * price if t_created else 0.0
            result.update(pod_id=pod_id, deleted=deleted, est_cost_usd=round(spent, 3))
            if deleted:
                log(f"pod {pod_id} DELETED and verified. Estimated cost ${spent:.3f} (up to the delete call).")
            else:
                log("!" * 78 + f"\n!! THE POD {pod_id} COULD NOT BE VERIFIED AS DELETED. REMOVE IT NOW in the RunPod console (Pods). It bills by the hour.\n" + "!" * 78)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", default="mme-test"); ap.add_argument("--cap", type=float, default=1.00); ap.add_argument("--max-minutes", type=float, default=40)
    ap.add_argument("--max-price-hr", type=float, default=1.00); ap.add_argument("--cloud", choices=("SECURE", "COMMUNITY"), default="SECURE")
    ap.add_argument("--key-file", default="", help="file (outside the repo) holding a RunPod ACCOUNT key with Pod permissions; or set RUNPOD_ADMIN_KEY")
    ap.add_argument("--out-dir", default=""); ap.add_argument("--check", action="store_true"); ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
    from config.settings import Settings
    s = Settings()
    script_py = (ROOT / "tools" / "phase3" / "chunked_tts_spike.py").read_text(encoding="utf-8")
    ref = Path(s.ai_tts_reference_wav)
    if a.dry_run:
        body = build_body("<gpu>", a.cloud, "<token>", script_py, pack_reference(ref.read_bytes()), 120, 2400, 1500)
        print(json.dumps(describe(body), indent=1))
        return
    import httpx
    import os
    # A SEPARATE account key with Pod permissions (create a temporary one in the RunPod console, put it in a file OUTSIDE the repository, delete it afterwards).
    # The endpoint keys stored for the tenant are restricted to serverless: RunPod answers them with 401 on the account and Pod APIs.
    key = os.environ.get("RUNPOD_ADMIN_KEY", "").strip()
    if not key and a.key_file:
        kf = Path(a.key_file).resolve()
        if ROOT in kf.parents:
            raise SystemExit("--key-file must be OUTSIDE the repository")
        key = kf.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit("no key: set the environment variable RUNPOD_ADMIN_KEY or pass --key-file (a file outside the repo holding an account key with Pod permissions)")

    async def go():
        async with httpx.AsyncClient() as http:
            if a.check:
                acct = await RunPod(http, key).account()
                me = acct["myself"]
                print(f"balance ${me['clientBalance']:.2f} | current spend ${me.get('currentSpendPerHr')}/h")
                for cloud in ("SECURE", "COMMUNITY"):
                    try:
                        g, p = pick_gpu(acct["gpuTypes"], cloud=cloud)
                        print(f"cheapest candidate on {cloud}: {g} ${p:.3f}/h")
                    except Refused as e:
                        print(cloud, "->", e)
                return
            if not a.yes:
                raise SystemExit("--yes required: chargeable")
            if not a.out_dir or ROOT in Path(a.out_dir).resolve().parents or Path(a.out_dir).resolve() == ROOT:
                raise SystemExit("--out-dir is required and must be OUTSIDE the repository (the commit guard refuses audio)")
            res = await run(a, http, key, script_py, ref.read_bytes(), Path(a.out_dir))
            print(json.dumps({k: v for k, v in res.items() if k != "files"} | {"files": len(res.get("files", []))}))
    asyncio.run(go())


if __name__ == "__main__":
    main()
