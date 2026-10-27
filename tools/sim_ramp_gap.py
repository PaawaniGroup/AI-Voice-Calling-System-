"""SIMULATED gap table for the first-clause ramp (WP2). No network, no audio, no RunPod.

Inputs are the MEASURED voice wall times per clause length (TTS re-measurement of 25 Oct 2026, ulaw8k, named voice: 3 words 2.00 s, 4: 2.63 s, 8: 3.39 s,
12: 4.32 s) and the MEASURED audio lengths (4 words 2.1 s, 8: 3.0 s, 12: 4.2 s). Values between the points are linear interpolations, values outside are
linear extrapolations (ESTIMATE). The simulation ASSUMES the Brain has delivered all clause texts by the time the first one is sent (the Brain is
faster than the voice); the sent jobs run on W identical workers, each job starting as soon as a worker is free; playout is strictly in order.

Run: python tools/sim_ramp_gap.py
"""
from __future__ import annotations

WALL = [(3, 2.00), (4, 2.63), (8, 3.39), (12, 4.32)]      # MEASURED: words -> seconds of voice wall time
AUDIO = [(4, 2.1), (8, 3.0), (12, 4.2)]                    # MEASURED: words -> seconds of audio returned


def _interp(points, x: float) -> float:
    pts = sorted(points)
    if x <= pts[0][0]:
        (x0, y0), (x1, y1) = pts[0], pts[1]
    elif x >= pts[-1][0]:
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
    else:
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= x <= x1:
                break
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def wall(words: int) -> float:
    return _interp(WALL, words)


def audio(words: int) -> float:
    return _interp(AUDIO, words)


def split_today(n: int, first: int = 9) -> list[int]:
    """Today: a first clause of 9 words (MEASURED median), then clauses of up to 14 words."""
    out, left = [min(first, n)], n - min(first, n)
    while left > 0:
        out.append(min(14, left))
        left -= out[-1]
    return out


def split_ramp(n: int) -> list[int]:
    """Ramp: 4 words, then 8, then 12 (the segmenter cuts later clauses at 12-14; 12 is used here), the remainder last."""
    out, left = [], n
    for size in (4, 8):
        k = min(size, left)
        if k:
            out.append(k)
            left -= k
    while left > 0:
        out.append(min(12, left))
        left -= out[-1]
    return out


COLD_READY_S = 49.0           # a cold start of the voice endpoint: MEASURED 48.9 s queue on 27 Oct (cold starts so far: 41 to 52 s)
BASE_QUEUE_S = 0.15           # the queue inside the measured wall times (Phase 0b, back-to-back jobs: MEASURED 0.15 s)
WARM_QUEUE_S = 1.2            # the queue of a warm voice job in the live app path: MEASURED 1.16-1.25 s at idle 300 s (26-27 Oct; 1.10 s at idle 5 s)
WORKER_MODELS = {"1 warm": 1, "2 warm": 2, "1 warm + 1 cold (ready ~49 s later)": [0.0, COLD_READY_S]}


def simulate(clauses: list[int], workers, queue_s: float | None = None) -> dict:
    """`workers`: an int (that many warm workers) or a list of the times (s) at which each worker is free for the first job.
    `queue_s`: the queue each job waits before it runs (None = the 0.15 s that is already inside the measured wall times)."""
    free = [0.0] * workers if isinstance(workers, int) else [float(x) for x in workers]   # all clause texts are available at t = 0
    extra = 0.0 if queue_s is None else queue_s - BASE_QUEUE_S
    ready = []
    for w in clauses:
        i = min(range(len(free)), key=lambda k: free[k])
        free[i] += wall(w) + extra
        ready.append(free[i])
    t_end, gaps = 0.0, []
    for i, w in enumerate(clauses):
        start = ready[i] if i == 0 else max(ready[i], t_end)
        if i:
            gaps.append(round(start - t_end, 2))
        t_end = start + audio(w)
    return {"first_sound_s": round(ready[0], 2), "gap_after_first_s": gaps[0] if gaps else 0.0, "gaps_total_s": round(sum(gaps), 2),
            "last_audio_end_s": round(t_end, 2)}


def table() -> str:
    rows = ["| reply words | plan | clauses (words) | workers | first sound (s) | gap after clause 1 (s) | all gaps (s) | reply ends (s) |",
            "|---|---|---|---|---|---|---|---|"]
    for n in (15, 23, 28):
        for name, split in (("today", split_today), ("ramp", split_ramp)):
            for w in (1, 2):
                c = split(n)
                r = simulate(c, w)
                rows.append(f"| {n} | {name} | {'+'.join(map(str, c))} | {w} | {r['first_sound_s']} | {r['gap_after_first_s']} | {r['gaps_total_s']} | {r['last_audio_end_s']} |")
    return "\n".join(rows)


def best_plan(n: int, workers, max_first_sound_s: float = 3.0, min_part: int = 3, max_part: int = 14) -> dict | None:
    """Exhaustive search over clause plans of n words (every clause but the last has min_part..max_part words) with first sound <= max_first_sound_s:
    least total silence first, then the earliest end of the reply. A plan is only as realistic as the segmenter's ability to hit its sizes (ESTIMATE)."""
    best: list = [None]

    def rec(prefix: list[int], left: int) -> None:
        if left == 0:
            r = simulate(prefix, workers)
            if r["first_sound_s"] <= max_first_sound_s:
                key = (r["gaps_total_s"], r["last_audio_end_s"])
                if best[0] is None or key < best[0][0]:
                    best[0] = (key, list(prefix), r)
            return
        for s in range(min_part, max_part + 1):
            if s <= left:
                rec(prefix + [s], left - s)
        if left < min_part:
            rec(prefix + [left], 0)                                    # a short tail is a clause of its own

    rec([], n)
    return None if best[0] is None else {"clauses": best[0][1], **best[0][2]}


def table2() -> str:
    rows = ["| reply words | voice workers | plan | clauses (words) | first sound (s) | gap after clause 1 (s) | all gaps (s) | reply ends (s) |",
            "|---|---|---|---|---|---|---|---|"]
    for n in (15, 23, 28):
        for label, w in WORKER_MODELS.items():
            for name, c in (("today", split_today(n)), ("ramp 4+8+12", split_ramp(n))):
                r = simulate(c, w)
                rows.append(f"| {n} | {label} | {name} | {'+'.join(map(str, c))} | {r['first_sound_s']} | {r['gap_after_first_s']} | {r['gaps_total_s']} | {r['last_audio_end_s']} |")
            b = best_plan(n, w)
            if b:
                rows.append(f"| {n} | {label} | **least silence, first sound <= 3.0 s** | {'+'.join(map(str, b['clauses']))} | {b['first_sound_s']} | {b['gap_after_first_s']} | {b['gaps_total_s']} | {b['last_audio_end_s']} |")
    return "\n".join(rows)


QUEUE_MODELS = {
    "1 worker, queue 0.15 s (Phase 0b back-to-back)": (1, BASE_QUEUE_S),
    "1 worker, warm queue 1.2 s": (1, WARM_QUEUE_S),
    "2 workers, 2nd restored in ~1.2 s": (2, WARM_QUEUE_S),
    "2 workers, 2nd cold ~49 s": ([0.0, COLD_READY_S], WARM_QUEUE_S),
}


def table3() -> str:
    """The measured warm queue (1.2 s per job) and the two cases for a second voice worker: restored in about 1.2 s, or a cold start of about 49 s."""
    rows = ["| reply words | voice workers | plan | clauses (words) | first sound (s) | gap after clause 1 (s) | all gaps (s) | reply ends (s) |",
            "|---|---|---|---|---|---|---|---|"]
    for n in (15, 23, 28):
        for label, (w, q) in QUEUE_MODELS.items():
            for name, c in (("today", split_today(n)), ("ramp 4+8+12", split_ramp(n))):
                r = simulate(c, w, q)
                rows.append(f"| {n} | {label} | {name} | {'+'.join(map(str, c))} | {r['first_sound_s']} | {r['gap_after_first_s']} | {r['gaps_total_s']} | {r['last_audio_end_s']} |")
    return "\n".join(rows)


if __name__ == "__main__":
    print("SIMULATED (inputs MEASURED, interpolation/extrapolation ESTIMATE; assumes the Brain is faster than the voice)\n")
    print(table())
    print("\nWorker models (SIMULATED; voice endpoint REAL settings: Max 2, Active 0; a cold start takes 41-52 s, MEASURED 48.9 s on 27 Oct):\n")
    print(table2())
    print("\nWith the MEASURED warm queue of 1.2 s per job (SIMULATED):\n")
    print(table3())
