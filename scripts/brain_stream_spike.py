"""Brain streaming spike against the REAL RunPod vLLM endpoint (never mocks). CHARGEABLE, tiny: needs --yes and a --budget.

    python scripts/brain_stream_spike.py --yes --budget 0.05 --idle-s 60 --price 0.000486 --out docs/evidence/brain_stream_spike.json

Question: can the Brain stream tokens so the first clause reaches TTS before the whole reply exists? Three routes, same prompt
(the app's real system prompt + one short caller line), small max_tokens:
  run_nonstream POST /run + poll /status whole reply at once (what the app effectively does today: baseline; /run so the id is known)
  openai       POST /openai/v1/chat/completions, stream=true (OpenAI-compatible SSE)
  run_stream   POST /run {"stream": true}, then poll GET /stream/{id} (RunPod native streaming)
Measured per call (client clock): time to first content (TTFT), total time, chunks received, plus RunPod delay/execution time when the
route reports them. Output holds timings and counts only: no prompt, no generated text, no key. Spend is estimated from client time x
price, plus one idle tail; the run halts at the budget, and an open /run job is cancelled on abort.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import e2e_latency_bench as eb  # noqa: E402  (Ledger)
import live_latency as ll  # noqa: E402  (tenant config loader)
from app.ai.pipeline import build_system_prompt  # noqa: E402
from config.settings import Settings  # noqa: E402

CALLER_LINE = "Mujhe B.Tech ki eligibility aur fees batao."
MODEL_SERVED = "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8"   # the model name shown on the endpoint (the OpenAI route needs the served name)


def messages() -> list[dict]:
    return [{"role": "system", "content": build_system_prompt()}, {"role": "user", "content": CALLER_LINE}]


class Halt(Exception):
    pass


class Spike:
    def __init__(self, base: str, key: str, ledger: eb.Ledger, max_tokens: int, max_wait_s: float = 25.0):
        self.base, self.hdr, self.ledger, self.max_tokens, self.max_wait_s = base, {"Authorization": f"Bearer {key}"}, ledger, max_tokens, max_wait_s
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=max_wait_s + 5, write=30, pool=30))
        self.open_job: str | None = None
        self.consec_errors = 0

    def _gate(self) -> None:
        try:
            self.ledger.before("brain")
        except eb.StopBench as e:
            raise Halt(str(e)) from None
        if self.consec_errors >= 3:
            raise Halt("3 consecutive errors: stopped, no retries")

    def _bill(self, t0: float, t1: float, note_delay_ms: float | None = None, note_exec_ms: float | None = None) -> None:
        self.ledger.begin("brain", t0)
        billed = ((note_delay_ms or 0) + (note_exec_ms or 0)) / 1000 if note_exec_ms is not None else (t1 - t0)
        self.ledger.end("brain", t1, billed)

    async def runsync(self) -> dict:
        """Non-streamed baseline. Submitted with /run (the job id is known at once) and polled on /status with a hard wait cap, because a
        blocking /runsync hides the id for up to 90 s: an unfinished job is cancelled and the spike halts (cold start), never left billing."""
        self._gate()
        body = {"input": {"messages": messages(), "sampling_params": {"temperature": 0.3, "max_tokens": self.max_tokens}}}
        t0 = time.perf_counter()
        r = await self.http.post(f"{self.base}/run", json=body, headers=self.hdr)
        j = r.json() if r.status_code < 400 else {}
        jid = j.get("id")
        if not jid:
            self.consec_errors += 1
            self._bill(t0, time.perf_counter())
            return {"route": "run_nonstream", "http": r.status_code, "ok": False, "error": str(j)[:200]}
        self.open_job = jid
        st: dict = {}
        while time.perf_counter() - t0 < self.max_wait_s:
            st = (await self.http.get(f"{self.base}/status/{jid}", headers=self.hdr)).json()
            if st.get("status") not in ("IN_QUEUE", "IN_PROGRESS"):
                break
            await asyncio.sleep(0.1)
        t1 = time.perf_counter()
        if st.get("status") in ("IN_QUEUE", "IN_PROGRESS"):
            await self.cancel_open()
            self._bill(t0, t1)
            raise Halt(f"job still {st.get('status')} after {self.max_wait_s:.0f} s (cold start): cancelled; stopped to protect the budget")
        self.open_job = None
        ok = st.get("status") == "COMPLETED"
        self.consec_errors = 0 if ok else self.consec_errors + 1
        self._bill(t0, t1, st.get("delayTime"), st.get("executionTime"))
        out = st.get("output")
        out = out[0] if isinstance(out, list) and out else out
        usage = out.get("usage") if isinstance(out, dict) else None
        return {"route": "run_nonstream", "http": r.status_code, "ok": ok, "ttft_ms": None, "total_ms": round((t1 - t0) * 1000, 1),
                "note": "whole reply arrives at once: time to first content == total", "delay_ms": st.get("delayTime"),
                "exec_ms": st.get("executionTime"), "completion_tokens": (usage or {}).get("completion_tokens"),
                "prompt_tokens": (usage or {}).get("prompt_tokens"), "request_id": jid}

    async def openai_stream(self) -> dict:
        self._gate()
        body = {"model": MODEL_SERVED, "messages": messages(), "temperature": 0.3, "max_tokens": self.max_tokens, "stream": True,
                "stream_options": {"include_usage": True}}
        t0 = time.perf_counter()
        ttft = None
        chunks = 0
        usage = None
        status = None
        err = None
        async with self.http.stream("POST", f"{self.base}/openai/v1/chat/completions", json=body, headers=self.hdr) as r:
            status = r.status_code
            ctype = r.headers.get("content-type", "")
            if status >= 400:
                err = (await r.aread()).decode(errors="replace")[:200]
            else:
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        ch = json.loads(data)
                    except ValueError:
                        continue
                    if ch.get("usage"):
                        usage = ch["usage"]
                    delta = ((ch.get("choices") or [{}])[0].get("delta") or {}).get("content")
                    if delta:
                        chunks += 1
                        if ttft is None:
                            ttft = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        ok = status < 400 and ttft is not None
        self.consec_errors = 0 if ok else self.consec_errors + 1
        self._bill(t0, t1)
        return {"route": "openai_stream", "http": status, "ok": ok, "content_type": ctype if status < 400 else None,
                "ttft_ms": round(ttft, 1) if ttft is not None else None, "total_ms": round((t1 - t0) * 1000, 1), "content_chunks": chunks,
                "prompt_tokens": (usage or {}).get("prompt_tokens"), "completion_tokens": (usage or {}).get("completion_tokens"),
                "error": err}

    async def run_stream(self) -> dict:
        self._gate()
        body = {"input": {"messages": messages(), "stream": True, "sampling_params": {"temperature": 0.3, "max_tokens": self.max_tokens}}}
        t0 = time.perf_counter()
        r = await self.http.post(f"{self.base}/run", json=body, headers=self.hdr)
        t_submit = (time.perf_counter() - t0) * 1000
        j = r.json() if r.status_code < 400 else {}
        jid = j.get("id")
        if not jid:
            self.consec_errors += 1
            self._bill(t0, time.perf_counter())
            return {"route": "run_stream", "http": r.status_code, "ok": False, "error": str(j)[:200]}
        self.open_job = jid
        ttft = None
        polls = 0
        batches = []   # (ms since submit, tokens-ish items in this poll) of non-empty stream polls
        status = "IN_QUEUE"
        while status in ("IN_QUEUE", "IN_PROGRESS"):
            if time.perf_counter() - t0 > 90:
                await self.http.post(f"{self.base}/cancel/{jid}", headers=self.hdr)
                self.open_job = None
                raise Halt("run_stream job did not finish in 90 s: cancelled")
            sr = await self.http.get(f"{self.base}/stream/{jid}", headers=self.hdr)
            polls += 1
            sj = sr.json() if sr.status_code < 400 else {}
            status = sj.get("status", "ERROR")
            items = sj.get("stream") or []
            if items:
                now = (time.perf_counter() - t0) * 1000
                if ttft is None:
                    ttft = now
                batches.append((round(now, 1), len(items)))
            if status in ("IN_QUEUE", "IN_PROGRESS"):
                await asyncio.sleep(0.05)
        t1 = time.perf_counter()
        self.open_job = None
        st = (await self.http.get(f"{self.base}/status/{jid}", headers=self.hdr)).json()
        ok = status == "COMPLETED" and ttft is not None
        self.consec_errors = 0 if ok else self.consec_errors + 1
        self._bill(t0, t1, st.get("delayTime"), st.get("executionTime"))
        return {"route": "run_stream", "http": r.status_code, "ok": ok, "status": status, "ttft_ms": round(ttft, 1) if ttft is not None else None,
                "total_ms": round((t1 - t0) * 1000, 1), "submit_ms": round(t_submit, 1), "polls": polls, "stream_batches": batches[:12],
                "delay_ms": st.get("delayTime"), "exec_ms": st.get("executionTime"), "request_id": jid}

    async def cancel_open(self) -> None:
        if self.open_job:
            try:
                await self.http.post(f"{self.base}/cancel/{self.open_job}", headers=self.hdr, timeout=15)
            finally:
                self.open_job = None

    async def aclose(self) -> None:
        await self.http.aclose()


async def run(args) -> dict:
    s = Settings()
    cfg = ll.load_cfg(s, args.tenant)
    key = os.environ.get("RUNPOD_BRAIN_KEY") or cfg["keys"]["brain"]
    base = cfg["brain_url"].rsplit("/", 1)[0]
    ledger = eb.Ledger({"stt": 0, "brain": args.price, "tts": 0}, {"stt": 0, "brain": args.idle_s, "tts": 0}, args.budget, args.cold_reserve_s)
    sp = Spike(base, key, ledger, args.max_tokens, args.max_wait_s)
    results, halted = [], None
    plan = [sp.runsync, sp.openai_stream, sp.run_stream, sp.openai_stream, sp.run_stream]
    try:
        for fn in plan:
            res = await fn()
            results.append(res)
            print(f"{res['route']:<14} ok={res['ok']} ttft_ms={res.get('ttft_ms')} total_ms={res.get('total_ms')} est_spend=${ledger.spent():.5f}", flush=True)
            d = res.get("delay_ms")
            if isinstance(d, (int, float)) and d > eb.COLD_DELAY_MS:
                halted = f"{res['route']}: RunPod delay {d:.0f} ms = cold start; stopped to protect the budget"
                break
    except Halt as e:
        halted = str(e)
    except httpx.TimeoutException as e:   # the OpenAI route gives no job id: the request is abandoned, not cancellable
        halted = f"{type(e).__name__}: no data within the wait cap (cold start?); request abandoned (the OpenAI route exposes no job id to cancel)"
    finally:
        await sp.cancel_open()
        await sp.aclose()
    return {"started_at": datetime.now(timezone.utc).isoformat(), "endpoint": base.rsplit("/", 1)[-1], "model_served": MODEL_SERVED,
            "max_tokens": args.max_tokens, "prompt": "app system prompt + one short caller line (text not stored)", "results": results,
            "halted": halted, "cost": ledger.report()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", default="mme-test")
    ap.add_argument("--budget", type=float, required=True, help="hard cap in USD (estimated)")
    ap.add_argument("--idle-s", type=float, required=True, help="Brain idle timeout in seconds (billed once as a tail)")
    ap.add_argument("--price", type=float, required=True, help="Brain USD per second")
    ap.add_argument("--cold-reserve-s", type=float, default=15.0, help="seconds reserved per call for an unexpected cold start (workers are ready)")
    ap.add_argument("--max-tokens", type=int, default=60)
    ap.add_argument("--max-wait-s", type=float, default=25.0, help="hard cap on waiting for any single job; an unfinished job is cancelled and the run halts")
    ap.add_argument("--out")
    ap.add_argument("--yes", action="store_true", help="required: this makes chargeable RunPod calls")
    args = ap.parse_args(argv)
    if not args.yes:
        print(f"refusing to send anything: 5 tiny Brain requests (runsync, 2x openai stream, 2x run+stream), budget ${args.budget:.2f}. Re-run with --yes.")
        return 2
    res = asyncio.run(run(args))
    text = json.dumps(res, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 1 if res["halted"] else 0


if __name__ == "__main__":
    sys.exit(main())
