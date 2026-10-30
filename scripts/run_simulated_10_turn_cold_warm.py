#!/usr/bin/env python3
"""SIMULATED, NOT A MEASUREMENT: delays are scripted.

Run simulated 10-Turn Cold vs Warm Latency Test and Generate Markdown Report.
Uses in-process mock transports (SimulatedModels) with scripted sleep delays.
Does NOT call real RunPod or cloud endpoints.

Usage:
    python scripts/run_simulated_10_turn_cold_warm.py [--out docs/evidence/simulated_10_turn_cold_warm_report.md]
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def run_test():
    print("=" * 80)
    print("Running SIMULATED 10-Turn Cold and Warm Test (Delays are scripted)...")
    print("=" * 80)
    cmd = [sys.executable, "-m", "pytest", "tests/e2e/test_simulated_10_turn_cold_warm.py", "-s", "-v"]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"Pytest run failed with code {proc.returncode}")
    print("[OK] Test completed successfully!")


def generate_markdown_report(result_json_path: Path, out_path: Path):
    if not result_json_path.exists():
        raise FileNotFoundError(f"Result JSON not found at {result_json_path}")

    with open(result_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    cold = data["cold_metrics"]
    warm = data["warm_metrics"]
    turns_count = len(cold)

    def stats(metric_list, key):
        vals = [m[key] for m in metric_list]
        vals_sorted = sorted(vals)
        mean_val = sum(vals) / len(vals)
        p50 = vals_sorted[len(vals_sorted) // 2]
        p90 = vals_sorted[int(len(vals_sorted) * 0.9)]
        return {
            "min": round(min(vals), 1),
            "max": round(max(vals), 1),
            "mean": round(mean_val, 1),
            "p50": round(p50, 1),
            "p90": round(p90, 1),
        }

    stages = [
        ("VAD Silence Window", "vad_silence_ms"),
        ("STT Transcription", "stt_ms"),
        ("LLM Time to First Token (TTFT)", "llm_ttft_ms"),
        ("LLM Time to First Clause (TTFC)", "llm_ttfc_ms"),
        ("TTS First Chunk Synthesis (TTFB)", "tts_first_chunk_ms"),
        ("Voice-to-Voice Latency", "voice_to_voice_ms"),
        ("Total Turn Duration", "turn_end_ms"),
    ]

    L = []
    L.append("# SIMULATED, NOT A MEASUREMENT: delays are scripted")
    L.append("")
    L.append("> [!IMPORTANT]")
    L.append("> **THIS IS A SIMULATION ONLY, NOT A MEASUREMENT OF REAL LATENCY.**")
    L.append("> All model endpoints (STT, Brain, TTS) ran via in-process `httpx.MockTransport` with programmed `asyncio.sleep()` delays.")
    L.append("> No real RunPod serverless endpoints were called. Zero spend was incurred. No real GPU inference occurred.")
    L.append("")
    L.append(f"**Test Timestamp:** `{data.get('test_timestamp')}`  ")
    L.append(f"**Execution Mode:** Simulated Control-Flow Test (`SimulatedModels` in-memory mock transport)  ")
    L.append(f"**Conversation Scope:** 10 Scripted Turns (Dialogue text from static test array)  ")
    L.append("")
    L.append("---")
    L.append("")

    c1_v2v = cold[0]["voice_to_voice_ms"]
    w1_v2v = warm[0]["voice_to_voice_ms"]
    t1_diff = round(c1_v2v - w1_v2v, 1)

    c_avg_v2v = stats(cold, "voice_to_voice_ms")["mean"]
    w_avg_v2v = stats(warm, "voice_to_voice_ms")["mean"]
    avg_diff = round(c_avg_v2v - w_avg_v2v, 1)

    L.append("## 1. Summary of Simulated Runs")
    L.append("")
    L.append("| Metric Dimension | Simulated Cold Run | Simulated Warm Run | Simulated Delta |")
    L.append("|---|:---:|:---:|:---:|")
    L.append(f"| **Turn 1 Voice-to-Voice** | **{c1_v2v:.1f} ms** | **{w1_v2v:.1f} ms** | **-{t1_diff:.1f} ms** |")
    L.append(f"| **10-Turn Average Voice-to-Voice** | **{c_avg_v2v:.1f} ms** | **{w_avg_v2v:.1f} ms** | **-{avg_diff:.1f} ms** |")
    L.append(f"| **10-Turn Average STT Delay** | {stats(cold, 'stt_ms')['mean']:.1f} ms | {stats(warm, 'stt_ms')['mean']:.1f} ms | -{stats(cold, 'stt_ms')['mean'] - stats(warm, 'stt_ms')['mean']:.1f} ms |")
    L.append(f"| **10-Turn Average LLM TTFT** | {stats(cold, 'llm_ttft_ms')['mean']:.1f} ms | {stats(warm, 'llm_ttft_ms')['mean']:.1f} ms | -{stats(cold, 'llm_ttft_ms')['mean'] - stats(warm, 'llm_ttft_ms')['mean']:.1f} ms |")
    L.append(f"| **10-Turn Average TTS TTFB** | {stats(cold, 'tts_first_chunk_ms')['mean']:.1f} ms | {stats(warm, 'tts_first_chunk_ms')['mean']:.1f} ms | -{stats(cold, 'tts_first_chunk_ms')['mean'] - stats(warm, 'tts_first_chunk_ms')['mean']:.1f} ms |")
    L.append("")
    L.append("---")
    L.append("")

    L.append("## 2. Scripted Delay Parameters in Code")
    L.append("")
    L.append("The delays observed above come directly from the following hardcoded values in `SimulatedModels` in `tests/e2e/test_simulated_10_turn_cold_warm.py`:")
    L.append("```python")
    L.append("# STT Delays (_handle_stt):")
    L.append("if self.mode == 'cold' and is_first_turn:")
    L.append("    stt_delay = 0.320   # 320 ms")
    L.append("elif self.mode == 'cold':")
    L.append("    stt_delay = 0.170   # 170 ms")
    L.append("else:")
    L.append("    stt_delay = 0.150 if is_first_turn else 0.145")
    L.append("")
    L.append("# LLM TTFT Delays (_handle_brain):")
    L.append("if self.mode == 'cold' and is_first_turn:")
    L.append("    ttft_delay = 0.380  # 380 ms")
    L.append("    clause_delay = 0.080")
    L.append("elif self.mode == 'cold':")
    L.append("    ttft_delay = 0.190 + 0.010 * self.current_turn")
    L.append("    clause_delay = 0.065")
    L.append("else:")
    L.append("    ttft_delay = 0.180 + 0.010 * self.current_turn")
    L.append("    clause_delay = 0.060")
    L.append("")
    L.append("# TTS Delays (_handle_tts):")
    L.append("if self.mode == 'cold' and is_first_turn and is_first_tts_of_turn:")
    L.append("    tts_delay = 0.350   # 350 ms")
    L.append("elif self.mode == 'cold':")
    L.append("    tts_delay = 0.190   # 190 ms")
    L.append("else:")
    L.append("    tts_delay = 0.170 if is_first_tts_of_turn else 0.160")
    L.append("```")
    L.append("")
    L.append("---")
    L.append("")

    L.append("## 3. Simulated Turn-by-Turn Data Table")
    L.append("")
    L.append("| Turn | Run | Scripted Input Text | Scripted Reply Text | VAD (ms) | STT (ms) | LLM TTFT | LLM TTFC | TTS Chunk 1 | Voice-to-Voice | Turn End |")
    L.append("|:---:|:---:|---|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")

    for i in range(turns_count):
        c = cold[i]
        w = warm[i]
        heard = c["heard"][:38] + ("..." if len(c["heard"]) > 38 else "")
        reply = c["reply"][:38] + ("..." if len(c["reply"]) > 38 else "")

        L.append(f"| **#{c['turn']}** | **Cold** | {heard} | {reply} | {c['vad_silence_ms']:.0f} | {c['stt_ms']:.0f} | {c['llm_ttft_ms']:.0f} | {c['llm_ttfc_ms']:.0f} | {c['tts_first_chunk_ms']:.0f} | **{c['voice_to_voice_ms']:.0f}** | {c['turn_end_ms']:.0f} |")
        L.append(f"| | **Warm** | (same text) | (same text) | {w['vad_silence_ms']:.0f} | {w['stt_ms']:.0f} | {w['llm_ttft_ms']:.0f} | {w['llm_ttfc_ms']:.0f} | {w['tts_first_chunk_ms']:.0f} | **{w['voice_to_voice_ms']:.0f}** | {w['turn_end_ms']:.0f} |")

    L.append("")
    L.append("---")
    L.append("*SIMULATED, NOT A MEASUREMENT: delays are scripted.*")

    content = "\n".join(L)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(f"[OK] Report written to {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Run simulated 10-turn benchmark and generate report")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "evidence" / "simulated_10_turn_cold_warm_report.md",
                        help="Path to output markdown report")
    parser.add_argument("--json", type=Path, default=ROOT / "docs" / "evidence" / "simulated_10_turn_cold_warm_result.json",
                        help="Path to input/output result JSON")
    parser.add_argument("--skip-run", action="store_true", help="Skip test execution and generate report from existing JSON")
    args = parser.parse_args()

    if not args.skip_run:
        run_test()

    generate_markdown_report(args.json, args.out)


if __name__ == "__main__":
    main()
