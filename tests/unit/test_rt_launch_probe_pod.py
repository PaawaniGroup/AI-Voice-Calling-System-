"""The G1 probe-Pod launcher. SIMULATED: a fake RunPod account/Pod API and a fake HTTP client; no key, no network, no Pod is ever created."""
from __future__ import annotations

import argparse
import json

import pytest

from tools.phase3.launch_pod import Refused
from tools.realtime import launch_probe_pod as L

GPUS = [{"id": "NVIDIA RTX A5000", "displayName": "A5000", "memoryInGb": 24, "securePrice": 0.27, "communityPrice": 0.16}]


class FakeResp:
    def __init__(self, code=200, body=None):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


class FakeHttp:
    def __init__(self, serving=True, fp="ab" * 32):
        self.serving, self.fp, self.urls = serving, fp, []

    async def get(self, url, timeout=0):
        self.urls.append(url)
        return FakeResp(200, {"state": "serving", "cert_sha256": self.fp}) if self.serving else FakeResp(404, {})


class FakePods:
    def __init__(self, balance=5.0, delete_ok=True, ip="203.0.113.7", port=13007, cost=0.27, create_error=None):
        self.balance, self.delete_ok, self.ip, self.port, self.cost, self.create_error = balance, delete_ok, ip, port, cost, create_error
        self.created, self.deleted, self.listed = [], [], 0
        self.alive = []

    async def list_pods(self):
        self.listed += 1
        return list(self.alive)

    async def account(self):
        return {"myself": {"clientBalance": self.balance}, "gpuTypes": GPUS}

    async def create(self, body):
        if self.create_error:
            raise self.create_error
        self.created.append(body)
        self.alive.append("pod1")
        return {"id": "pod1", "costPerHr": self.cost}

    async def get(self, pod_id):
        return {"id": pod_id, "publicIp": self.ip, "portMappings": {"8001": self.port}} if self.ip else {"id": pod_id}

    async def delete(self, pod_id):
        self.deleted.append(pod_id)
        if self.delete_ok:
            self.alive.remove(pod_id)
        return self.delete_ok


def ns(**kw):
    base = dict(cap=0.10, max_minutes=7, max_price_hr=1.0, cloud="SECURE")
    base.update(kw)
    return argparse.Namespace(**base)


async def no_sleep(s):
    return None


def clock_factory():
    t = {"now": 0.0}

    def clock():
        t["now"] += 5.0
        return t["now"]
    return clock


async def test_a_run_probes_the_pinned_server_and_deletes_the_pod_with_a_verified_list_before_and_after():
    pods, seen, logs = FakePods(), {}, []

    async def probe(url, secret, pin):
        seen.update(url=url, secret=secret, pin=pin)
        return {"rt_roundtrip_ms": {"median": 12.0, "n": 200}}
    res = await L.run(ns(), FakeHttp(), "key", probe, rp=pods, sleep=no_sleep, clock=clock_factory(), log=logs.append)
    assert res["ok"] and res["deleted"] and pods.deleted == ["pod1"] and pods.alive == [] and res["pods_after"] == 0 and pods.listed == 2
    assert seen["url"] == "wss://203.0.113.7:13007/ws" and seen["pin"] == "ab" * 32 and len(seen["secret"]) > 20
    text = "\n".join(logs)
    assert "203.0.113.7" not in text and seen["secret"].decode() not in text and "13007" not in text      # the address and the secret are never printed
    assert "ACTION NEEDED" not in text and "DELETED and verified" in text


async def test_the_pod_body_has_the_tcp_port_a_private_secret_and_a_deadline_and_describe_hides_values():
    body = L.build_body("NVIDIA RTX A5000", "SECURE", "tok", "s3cr3t-value", 480)
    assert body["ports"] == ["8000/http", "8001/tcp"] and body["gpuCount"] == 1 and body["interruptible"] is False and body["env"]["RT_SECRET"] == "s3cr3t-value"
    assert "runpodctl remove pod" in body["dockerStartCmd"][2] and "sleep 480" in body["dockerStartCmd"][2]
    d = json.dumps(L.describe(body))
    assert "s3cr3t-value" not in d and "chars" in d


async def test_below_the_cap_plus_margin_or_above_the_price_limit_nothing_is_created():
    for pods, kw in ((FakePods(balance=0.2), {}), (FakePods(), {"max_price_hr": 0.1})):
        with pytest.raises(Refused):
            await L.run(ns(**kw), FakeHttp(), "key", lambda *a: None, rp=pods, sleep=no_sleep, clock=clock_factory(), log=lambda *_: None)
        assert pods.created == []


async def test_an_address_that_cannot_be_read_stops_the_run_and_the_pod_is_still_deleted():
    pods = FakePods(ip=None)
    called = []

    async def probe(url, secret, pin):
        called.append(url)
    res = await L.run(ns(), FakeHttp(), "key", probe, rp=pods, sleep=no_sleep, clock=clock_factory(), log=lambda *_: None)
    assert not res["ok"] and res["state"] == "no_server" and called == [] and pods.deleted == ["pod1"]


async def test_a_probe_that_raises_still_deletes_the_pod():
    pods = FakePods()

    async def probe(url, secret, pin):
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await L.run(ns(), FakeHttp(), "key", probe, rp=pods, sleep=no_sleep, clock=clock_factory(), log=lambda *_: None)
    assert pods.deleted == ["pod1"]


async def test_a_pod_that_cannot_be_verified_deleted_prints_the_action_needed_line():
    pods, logs = FakePods(delete_ok=False), []

    async def probe(url, secret, pin):
        return {}
    res = await L.run(ns(), FakeHttp(), "key", probe, rp=pods, sleep=no_sleep, clock=clock_factory(), log=logs.append)
    assert res["deleted"] is False and any("ACTION NEEDED: DELETE POD NOW" in x for x in logs)


async def test_a_pod_reporting_a_price_above_the_limit_is_deleted_without_probing():
    pods, called = FakePods(cost=5.0), []

    async def probe(url, secret, pin):
        called.append(1)
    with pytest.raises(Refused):
        await L.run(ns(), FakeHttp(), "key", probe, rp=pods, sleep=no_sleep, clock=clock_factory(), log=lambda *_: None)
    assert called == [] and pods.deleted == ["pod1"]


def test_endpoint_from_pod_reads_only_a_complete_answer():
    assert L.endpoint_from_pod({"publicIp": "203.0.113.7", "portMappings": {"8001": 13007}}) == ("203.0.113.7", 13007)
    assert L.endpoint_from_pod({"publicIp": "203.0.113.7", "portMappings": {"8001": "13007"}}) == ("203.0.113.7", 13007)
    for bad in (None, {}, {"publicIp": "", "portMappings": {"8001": 1}}, {"publicIp": "1.2.3.4"}, {"publicIp": "1.2.3.4", "portMappings": {"22": 1}}, {"publicIp": "1.2.3.4", "portMappings": {"8001": "x"}}):
        assert L.endpoint_from_pod(bad) is None


def test_main_refuses_a_cap_above_the_request_a_key_inside_the_repository_and_prints_a_dry_run_without_values(capsys, monkeypatch):
    monkeypatch.delenv("RUNPOD_ADMIN_KEY", raising=False)
    with pytest.raises(SystemExit):
        L.main(["--cap", "0.5", "--dry-run"])
    assert L.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert '"RT_SECRET": "<8 chars>"' in out and "<secret>" not in out                 # the value is summarised as a length, never printed
    with pytest.raises(SystemExit) as e:
        L.main(["--key-file", str(L.ROOT / "docs" / "k.txt"), "--check"])
    assert "OUTSIDE" in str(e.value)
