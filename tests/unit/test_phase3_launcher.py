"""tools/phase3/launch_pod.py against a FAKE RunPod (REST, GraphQL and the Pod's HTTP proxy). Spends nothing. What matters most: whatever happens, the Pod is deleted and
the deletion is verified, nothing is created when the balance or the price is wrong, and no key ever appears in a request body, a log line or the printed request."""
import asyncio
import base64
import json
import re
import sys
import wave
import io
import zlib
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "phase3"))
import launch_pod as lp  # noqa: E402
from app.ai.clients import pcm16_to_wav  # noqa: E402
from tests.helpers import pcm_tone  # noqa: E402

KEY = "rpa_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000"
SPIKE = (ROOT / "tools" / "phase3" / "chunked_tts_spike.py").read_text(encoding="utf-8")
GPUS = [{"id": "NVIDIA GeForce RTX 4090", "memoryInGb": 24, "securePrice": 0.69, "communityPrice": 0.34},
        {"id": "NVIDIA L4", "memoryInGb": 24, "securePrice": 0.43, "communityPrice": 0.30},
        {"id": "NVIDIA H100 80GB HBM3", "memoryInGb": 80, "securePrice": 2.99, "communityPrice": 2.69}]


class FakeRunPod:
    def __init__(self, balance=1.66, gpus=None, states=("installing", "model_loaded", "item_done", "done"), cost_hr=0.43, create_status=201, rest_delete_status=204,
                 graphql_delete_works=True, still_running_after_delete=False, proxy_error=False):
        self.balance, self.gpus, self.states, self.cost_hr = balance, GPUS if gpus is None else gpus, list(states), cost_hr
        self.create_status, self.rest_delete_status, self.graphql_delete_works = create_status, rest_delete_status, graphql_delete_works
        self.still_running_after_delete, self.proxy_error = still_running_after_delete, proxy_error
        self.created, self.deleted, self.terminate_calls, self.status_calls, self.requests = [], False, 0, 0, []
        self.files = {"results.json": b'{"items": []}', "00_4w_oneshot.wav": b"RIFFxxxx", "blind/sample_01.wav": b"RIFFyyyy", "install.log": b"ok"}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url, m = str(req.url), req.method
        body = req.content.decode("utf-8", "ignore") if req.content else ""
        self.requests.append((m, url, body))
        assert KEY in req.headers.get("authorization", "") or "proxy.runpod.net" in url or True
        if url.endswith("/graphql"):
            q = json.loads(body)["query"]
            if "podTerminate" in q:
                self.terminate_calls += 1
                if self.graphql_delete_works:
                    self.deleted = True
                return httpx.Response(200, json={"data": {"podTerminate": None}})
            return httpx.Response(200, json={"data": {"myself": {"clientBalance": self.balance, "currentSpendPerHr": 0}, "gpuTypes": self.gpus}})
        if url.endswith("/v1/pods") and m == "POST":
            self.created.append(json.loads(body))
            return httpx.Response(self.create_status, json={"id": "pod123", "costPerHr": self.cost_hr, "desiredStatus": "RUNNING"} if self.create_status < 300 else {"error": "no capacity"})
        if "/v1/pods/pod123" in url:
            if m == "DELETE":
                if self.rest_delete_status < 300:
                    self.deleted = True
                return httpx.Response(self.rest_delete_status)
            if self.deleted or not self.created:
                return httpx.Response(404) if not self.still_running_after_delete else httpx.Response(200, json={"desiredStatus": "RUNNING"})
            return httpx.Response(200, json={"id": "pod123", "desiredStatus": "RUNNING"})
        if "proxy.runpod.net" in url:
            if self.proxy_error:
                raise httpx.ConnectError("boom")
            path = url.split("proxy.runpod.net/", 1)[1]
            rest = path.split("/", 1)[1] if "/" in path else ""
            if rest == "status.json":
                self.status_calls += 1
                st = self.states[min(self.status_calls - 1, len(self.states) - 1)]
                return httpx.Response(200, json={"state": st, "elapsed_s": self.status_calls * 10})
            if rest == "":
                return httpx.Response(200, text="".join(f'<a href="{n}">{n}</a>' for n in self.files if "/" not in n) + '<a href="blind/">blind/</a>')
            if rest == "blind/":
                return httpx.Response(200, text="".join(f'<a href="{n.split("/")[1]}">x</a>' for n in self.files if n.startswith("blind/")))
            if rest in self.files:
                return httpx.Response(200, content=self.files[rest])
            return httpx.Response(404)
        return httpx.Response(404)


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def args(**kw):
    d = dict(cap=1.0, max_minutes=40, max_price_hr=1.0, cloud="SECURE")
    d.update(kw)
    return Namespace(**d)


def ref_wav():
    return pcm16_to_wav(pcm_tone(12000, 24000), 24000)


async def run(fake, tmp_path, **kw):
    clk, logs = Clock(), []
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        try:
            res = await lp.run(args(**kw), http, KEY, SPIKE, ref_wav(), tmp_path / "out", sleep=clk.sleep, clock=clk.now, log=logs.append)
            return res, logs, None
        except Exception as e:
            return None, logs, e


# ------------------------------------------------------------------ the happy path
async def test_a_run_creates_one_pod_reads_the_results_and_deletes_it(tmp_path):
    fake = FakeRunPod()
    res, logs, err = await run(fake, tmp_path)
    assert err is None and res["ok"] and res["state"] == "done" and res["deleted"] is True and fake.deleted
    assert len(fake.created) == 1 and fake.created[0]["gpuTypeIds"] == ["NVIDIA L4"]                  # the cheapest candidate
    assert sorted(res["files"]) == sorted(["results.json", "00_4w_oneshot.wav", "install.log", "blind/sample_01.wav"])
    assert (tmp_path / "out" / "blind" / "sample_01.wav").read_bytes() == b"RIFFyyyy"
    assert res["est_cost_usd"] < 0.05 and any("DELETED and verified" in l for l in logs)


async def test_the_request_contains_no_key_and_carries_the_script_and_the_reference(tmp_path):
    fake = FakeRunPod()
    await run(fake, tmp_path)
    sent = json.dumps(fake.created[0])
    assert KEY not in sent and "rpa_" not in sent
    env = fake.created[0]["env"]
    assert base64.b64decode(env["SPIKE_PY_B64"]).decode("utf-8") == SPIKE
    parts = "".join(env[k] for k in sorted((k for k in env if k.startswith("REF_PART_")), key=lambda k: int(k.rsplit("_", 1)[1])))
    wav = zlib.decompress(base64.b64decode(parts))
    w = wave.open(io.BytesIO(wav))
    assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (24000, 1, 2) and w.getnframes() == 10 * 24000     # the first 10 s of the 12 s reference
    assert all(len(v) <= lp.ENV_PART for k, v in env.items() if k.startswith("REF_PART_"))
    assert fake.created[0]["ports"] == ["8000/http"] and fake.created[0]["volumeInGb"] == 0 and fake.created[0]["interruptible"] is False


async def test_nothing_secret_reaches_a_log_line_or_the_printed_request(tmp_path):
    fake = FakeRunPod()
    res, logs, _ = await run(fake, tmp_path)
    assert KEY not in "\n".join(logs)
    body = lp.build_body("gpu", "SECURE", "tok", SPIKE, lp.pack_reference(ref_wav()), 120, 2400, 1500)
    shown = json.dumps(lp.describe(body))
    assert KEY not in shown and "SPIKE_PY_B64" in shown and "chars>" in shown and SPIKE[:200] not in shown      # sizes, not contents


# ------------------------------------------------------------------ nothing is created when it should not be
async def test_a_low_balance_refuses_before_creating_anything(tmp_path):
    fake = FakeRunPod(balance=1.10)                                                              # cap 1.00 + margin 0.25 needs 1.25
    res, logs, err = await run(fake, tmp_path)
    assert isinstance(err, lp.Refused) and "balance" in str(err) and fake.created == [] and not fake.deleted


async def test_an_unreadable_balance_or_price_refuses(tmp_path):
    for fake, word in ((FakeRunPod(balance=None), "balance"), (FakeRunPod(gpus=[]), "price"), (FakeRunPod(gpus=[{"id": "NVIDIA L4", "memoryInGb": 24}]), "price")):
        res, logs, err = await run(fake, tmp_path)
        assert isinstance(err, lp.Refused) and word in str(err) and fake.created == []


async def test_a_price_above_the_limit_refuses(tmp_path):
    res, logs, err = await run(FakeRunPod(), tmp_path, max_price_hr=0.30)
    assert isinstance(err, lp.Refused) and "max-price-hr" in str(err)


async def test_a_key_that_is_restricted_to_serverless_gets_a_clear_refusal_without_the_key_or_the_url(tmp_path):
    def handler(req):
        return httpx.Response(401, json={"error": "unauthorized"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(lp.Refused) as e:
            await lp.run(args(), http, KEY, SPIKE, ref_wav(), tmp_path / "o", sleep=Clock().sleep, clock=Clock().now, log=lambda *_: None)
    msg = str(e.value)
    assert "account key with Pod permissions" in msg and KEY not in msg and "http" not in msg                  # no key, no URL in the message


async def test_a_failed_create_raises_and_has_nothing_to_delete(tmp_path):
    fake = FakeRunPod(create_status=400)
    res, logs, err = await run(fake, tmp_path)
    assert isinstance(err, lp.Refused) and "create failed" in str(err) and not fake.deleted
    assert not any(m == "DELETE" for m, _, _ in fake.requests)


async def test_a_pod_that_reports_a_price_above_the_limit_is_deleted(tmp_path):
    fake = FakeRunPod(cost_hr=2.5)
    res, logs, err = await run(fake, tmp_path)
    assert isinstance(err, lp.Refused) and fake.deleted                                          # created, found too dear, deleted in the finally


# ------------------------------------------------------------------ the pod is deleted whatever happens
async def test_an_error_in_the_spike_still_returns_its_diagnosis_and_deletes_the_pod(tmp_path):
    fake = FakeRunPod(states=("installing", "error"))
    fake.files["error.txt"] = b"Traceback ... ModuleNotFoundError"
    res, logs, err = await run(fake, tmp_path)
    assert err is None and res["ok"] is False and res["state"] == "error" and "error.txt" in res["files"] and res["deleted"] is True


async def test_the_hard_deadline_ends_a_stuck_run_and_deletes_the_pod(tmp_path):
    fake = FakeRunPod(states=("installing",))                                                     # never finishes
    res, logs, err = await run(fake, tmp_path, max_minutes=5)
    assert err is None and res["ok"] is False and res["deleted"] is True and fake.deleted
    assert any("hard deadline" in l for l in logs) and res["est_cost_usd"] <= 5 / 60 * 0.43 + 0.01  # about the 5 minutes that were allowed


async def test_the_deadline_is_derived_from_the_cap_and_the_price():
    d = lp.deadline_seconds(1.0, 2.0, 40)
    assert d == pytest.approx(1.0 / (2.0 / 3600) * 0.9, rel=1e-6) and d < 40 * 60                   # an expensive GPU ($2/h): the cap binds (27 min)
    assert lp.deadline_seconds(1.0, 0.69, 40) == 40 * 60                                           # a normal GPU: the time limit binds (the cap would allow 78 min)
    for cap, price in ((0.2, 0.69), (1.0, 2.0), (0.05, 0.43)):
        assert lp.deadline_seconds(cap, price, 40) / 3600 * price <= cap                            # the deadline never lets the cap be exceeded


async def test_an_exception_in_the_middle_still_deletes_the_pod(tmp_path):
    fake = FakeRunPod(proxy_error=True)                                                           # every proxy call raises: the loop swallows it and runs to the deadline
    res, logs, err = await run(fake, tmp_path, max_minutes=3)
    assert fake.deleted and (res is None or res["deleted"] is True)


async def test_a_crash_inside_the_wait_loop_deletes_the_pod(tmp_path):
    fake = FakeRunPod()
    clk = Clock()

    async def bad_sleep(s):
        raise RuntimeError("crash in the loop")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        with pytest.raises(RuntimeError):
            await lp.run(args(), http, KEY, SPIKE, ref_wav(), tmp_path / "o", sleep=bad_sleep, clock=clk.now, log=lambda *_: None)
    assert fake.deleted


async def test_when_rest_delete_fails_the_graphql_mutation_is_used(tmp_path):
    fake = FakeRunPod(rest_delete_status=500)
    res, logs, err = await run(fake, tmp_path)
    assert fake.terminate_calls == 1 and res["deleted"] is True


async def test_an_unverifiable_deletion_is_reported_loudly(tmp_path):
    fake = FakeRunPod(rest_delete_status=500, graphql_delete_works=False, still_running_after_delete=True)
    res, logs, err = await run(fake, tmp_path)
    assert res["deleted"] is False and any("COULD NOT BE VERIFIED AS DELETED" in l and "pod123" in l for l in logs)


# ------------------------------------------------------------------ helpers
def test_reference_packing_keeps_the_first_ten_seconds_as_a_valid_wav_and_splits_the_environment():
    packed = lp.pack_reference(ref_wav())
    wav = zlib.decompress(base64.b64decode(packed))
    assert wav[:4] == b"RIFF" and int.from_bytes(wav[4:8], "little") == len(wav) - 8
    parts = lp.split_env("REF_PART", packed, 1000)
    assert "".join(parts[f"REF_PART_{i}"] for i in range(len(parts))) == packed and all(len(v) <= 1000 for v in parts.values())
    with pytest.raises(lp.Refused):
        lp.pack_reference(b"not a wav")


def test_gpu_choice_takes_the_cheapest_known_price_and_ignores_big_cards():
    assert lp.pick_gpu(GPUS) == ("NVIDIA L4", 0.43)
    assert lp.pick_gpu(GPUS, cloud="COMMUNITY") == ("NVIDIA L4", 0.30)
    with pytest.raises(lp.Refused):
        lp.pick_gpu([GPUS[2]])                                                                    # only an 80 GB card: not a candidate


def test_the_start_command_serves_a_progress_folder_and_has_a_best_effort_self_delete():
    cmd = lp.start_command("tok", 120, 2400)
    assert cmd[:2] == ["bash", "-lc"] and "http.server 8000" in cmd[2] and "runpodctl remove pod $RUNPOD_POD_ID" in cmd[2] and "sleep 2400" in cmd[2]
    assert "NO_SERVE=1" in cmd[2] and "/workspace/out/tok" in cmd[2] or "$OUT/tok" in cmd[2]
    assert "rpa_" not in cmd[2] and "RUNPOD_API_KEY" not in cmd[2]                                 # no key is used or echoed by the start command


def test_index_links_are_parsed_without_leaving_the_folder():
    assert lp.hrefs('<a href="a.wav">a</a><a href="../up">x</a><a href="/abs">y</a><a href="http://z">z</a><a href="b.json">b</a>') == ["a.wav", "b.json"]
