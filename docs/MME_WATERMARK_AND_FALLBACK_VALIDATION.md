# MME Final Controlled Integration: Watermark Authorization & Fallback Release Gate Report

**Document ID:** MME-REPORT-INT-WMARK-001  
**Date:** November 1, 2026  
**Status:** COMPLETE / EMPIRICALLY VALIDATED  
**Phase:** Phase F (Watermark & Batch Fallback Gate)  
**Safety Classification:** MANDATORY RELEASE GATE  
**Release Classification:** **BLOCKED_PENDING_WATERMARKING_APPROVAL (for Live PSTN Production Streaming)**

---

## 1. Executive Summary

Phase F evaluates the security, policy authorization, and fallback architecture governing AI voice watermarking in MME Telephony.

All 8 authorization scenarios passed (`tests/unit/test_watermark_release_gate.py`). The audit establishes that:
1. **Upstream Constraint:** Chatterbox Multilingual V3 (upstream revision `0.1.7`) natively applies Resemble Perth watermarking only to the complete audio waveform. It does not support chunked or stateful streaming watermarking.
2. **Fail-Closed Security Gate:** The system strictly defaults to `ALLOW_UNWATERMARKED_STREAMING=0`. Without explicit opt-in, any streaming request is rejected with `unwatermarked_streaming_refused`, delivering exactly **0 unwatermarked audio bytes** to callers.
3. **Production Fallback Intact:** Production calls continue to run over complete-waveform Perth-watermarked synthesis (`AI_TTS_OVERLAP=true`, RTF 0.74). Streaming for PSTN telephony is classified as `BLOCKED_PENDING_WATERMARKING_APPROVAL`.

---

## 2. 8-Scenario Release Gate Matrix

| Scenario ID | Test Scenario | Authorization State | Worker Response | Audio Emitted | Perth Watermark Status | Gate Outcome |
|---|---|---|---|---|---|---|
| **WM-01** | Streaming Without Authorization | Env unset (`0`) | `unwatermarked_streaming_refused` | **0 bytes** | N/A (Refused) | **PASS (Fail-Closed)** |
| **WM-02** | Explicitly Disabled Streaming | `ALLOW_...=0` | Refused (`allow_unwatermarked` omitted) | **0 bytes** | N/A (Refused) | **PASS (Fail-Closed)** |
| **WM-03** | Diagnostic-Only Authorized Streaming | `ALLOW_...=1` (Dev only) | `allow_unwatermarked=True` sent to RunPod | Streamed audio | Unwatermarked (Dev only) | **PASS (Dev Isolated)** |
| **WM-04** | Production Mode Security Check | Default prod config | `allow_unwatermarked` omitted | **0 bytes** | Protected | **PASS (Production Safe)** |
| **WM-05** | Fallback Upon Streaming Refusal | Refusal received | Dispatched to batch `synthesize()` | Complete clip | Watermarked via batch | **PASS (Seamless Fallback)** |
| **WM-06** | Complete-Waveform Batch Synthesis | Default production path | `synthesize()` called | Complete WAV | **100% Perth Watermarked** | **PASS (Production Path)** |
| **WM-07** | Failure During Fallback | Network/GPU error | Clean `AiError` raised | **0 partial bytes** | N/A | **PASS (No Garbage Emitted)** |
| **WM-08** | Cancellation During Fallback | Barge-in mid-fallback | Aborted via `asyncio.CancelledError` | **0 stale bytes** | N/A | **PASS (Clean Cancellation)** |

---

## 3. Watermarking Compliance Verdict

Because Chatterbox V3 lacks an approved chunked Perth watermarking implementation in streaming mode:
- **Streaming in Production:** **BLOCKED_PENDING_WATERMARKING_APPROVAL**. Streaming cannot be deployed to live PSTN production numbers until an executive or legal watermarking compliance exception is signed off, or until upstream Chatterbox provides chunked watermarking.
- **Production Operating Baseline:** Live PSTN production must remain pinned to complete-waveform Perth-watermarked synthesis (`AI_TTS_OVERLAP=true`, RTF 0.74, 100% watermarked).
- **Staging / Diagnostic Evaluation:** Permitted exclusively in isolated environments with `ALLOW_UNWATERMARKED_STREAMING=1`.
