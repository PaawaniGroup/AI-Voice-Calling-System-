# SIMULATED, NOT A MEASUREMENT: delays are scripted

> [!IMPORTANT]
> **THIS IS A SIMULATION ONLY, NOT A MEASUREMENT OF REAL LATENCY.**
> All model endpoints (STT, Brain, TTS) ran via in-process `httpx.MockTransport` with programmed `asyncio.sleep()` delays.
> No real RunPod serverless endpoints were called. Zero spend was incurred. No real GPU inference occurred.

**Test Timestamp:** `2026-10-25T16:14:29Z`  
**Execution Mode:** Simulated Control-Flow Test (`SimulatedModels` in-memory mock transport)  
**Conversation Scope:** 10 Scripted Turns (Dialogue text from static test array)  

---

## 1. Summary of Simulated Runs

| Metric Dimension | Simulated Cold Run | Simulated Warm Run | Simulated Delta |
|---|:---:|:---:|:---:|
| **Turn 1 Voice-to-Voice** | **1187.0 ms** | **563.0 ms** | **-624.0 ms** |
| **10-Turn Average Voice-to-Voice** | **865.6 ms** | **751.6 ms** | **-114.0 ms** |
| **10-Turn Average STT Delay** | 183.1 ms | 151.2 ms | -31.9 ms |
| **10-Turn Average LLM TTFT** | 259.5 ms | 228.0 ms | -31.5 ms |
| **10-Turn Average TTS TTFB** | 217.0 ms | 171.8 ms | -45.2 ms |

---

## 2. Scripted Delay Parameters in Code

The delays observed above come directly from the following hardcoded values in `SimulatedModels` in `tests/e2e/test_simulated_10_turn_cold_warm.py`:
```python
# STT Delays (_handle_stt):
if self.mode == 'cold' and is_first_turn:
    stt_delay = 0.320   # 320 ms
elif self.mode == 'cold':
    stt_delay = 0.170   # 170 ms
else:
    stt_delay = 0.150 if is_first_turn else 0.145

# LLM TTFT Delays (_handle_brain):
if self.mode == 'cold' and is_first_turn:
    ttft_delay = 0.380  # 380 ms
    clause_delay = 0.080
elif self.mode == 'cold':
    ttft_delay = 0.190 + 0.010 * self.current_turn
    clause_delay = 0.065
else:
    ttft_delay = 0.180 + 0.010 * self.current_turn
    clause_delay = 0.060

# TTS Delays (_handle_tts):
if self.mode == 'cold' and is_first_turn and is_first_tts_of_turn:
    tts_delay = 0.350   # 350 ms
elif self.mode == 'cold':
    tts_delay = 0.190   # 190 ms
else:
    tts_delay = 0.170 if is_first_tts_of_turn else 0.160
```

---

## 3. Simulated Turn-by-Turn Data Table

| Turn | Run | Scripted Input Text | Scripted Reply Text | VAD (ms) | STT (ms) | LLM TTFT | LLM TTFC | TTS Chunk 1 | Voice-to-Voice | Turn End |
|:---:|:---:|---|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **#1** | **Cold** | Namaste, mujhe BTech admission ke baar... | जी, Make My Education में आपका स्वागत ... | 700 | 329 | 390 | 94 | 343 | **1187** | 1281 |
| | **Warm** | (same text) | (same text) | 700 | 140 | 172 | 63 | 172 | **563** | 813 |
| **#2** | **Cold** | Eligibility criteria kya hai 12th stan... | जी, B.Tech Computer Science के लिए 12t... | 700 | 156 | 203 | 79 | 218 | **687** | 953 |
| | **Warm** | (same text) | (same text) | 700 | 156 | 187 | 63 | 156 | **750** | 969 |
| **#3** | **Cold** | Fee structure kya hai per semester aur... | जी, B.Tech की annual tuition fee ₹1,20... | 700 | 156 | 235 | 93 | 188 | **860** | 938 |
| | **Warm** | (same text) | (same text) | 700 | 156 | 203 | 63 | 187 | **750** | 985 |
| **#4** | **Cold** | Hostel facility available hai kya camp... | जी हाँ, campus में AC और non-AC hostel... | 700 | 172 | 219 | 140 | 203 | **765** | 953 |
| | **Warm** | (same text) | (same text) | 700 | 140 | 219 | 125 | 172 | **828** | 1000 |
| **#5** | **Cold** | Kya koi merit-based scholarship ya con... | जी, 12th में 85% से अधिक अंक होने पर 2... | 700 | 172 | 235 | 140 | 188 | **875** | 875 |
| | **Warm** | (same text) | (same text) | 700 | 140 | 219 | 110 | 187 | **766** | 766 |
| **#6** | **Cold** | JEE Main score compulsory hai ya direc... | जी, 12th merit के आधार पर direct admis... | 700 | 172 | 235 | 62 | 188 | **657** | 953 |
| | **Warm** | (same text) | (same text) | 700 | 156 | 234 | 47 | 172 | **609** | 828 |
| **#7** | **Cold** | Computer Science aur AI branch ki cut-... | जी, Computer Science और AI branch के ल... | 700 | 157 | 250 | 125 | 218 | **968** | 968 |
| | **Warm** | (same text) | (same text) | 700 | 156 | 250 | 125 | 172 | **875** | 875 |
| **#8** | **Cold** | Average placement package kitna gaya t... | जी, CSE branch का average placement pa... | 700 | 172 | 282 | 125 | 203 | **813** | 1000 |
| | **Warm** | (same text) | (same text) | 700 | 156 | 265 | 110 | 172 | **719** | 875 |
| **#9** | **Cold** | Admission ke time kon-kon se documents... | जी, admission के लिए 10th और 12th की m... | 700 | 157 | 265 | 63 | 218 | **828** | 1078 |
| | **Warm** | (same text) | (same text) | 700 | 156 | 265 | 63 | 156 | **765** | 984 |
| **#10** | **Cold** | Theek hai, mujhe campus visit ke liye ... | जी, कल सुबह 11 बजे आपका campus visit s... | 700 | 188 | 281 | 125 | 203 | **1016** | 1078 |
| | **Warm** | (same text) | (same text) | 700 | 156 | 266 | 125 | 172 | **891** | 984 |

---
*SIMULATED, NOT A MEASUREMENT: delays are scripted.*