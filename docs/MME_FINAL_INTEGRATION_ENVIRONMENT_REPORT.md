# MME Final Controlled Integration: Environment & Deployment Verification Report

**Document ID:** MME-REPORT-INT-ENV-001  
**Date:** November 1, 2026  
**Status:** COMPLETE / INDEPENDENTLY VERIFIED  
**Phase:** Phase A (Environment Verification)  
**Safety Classification:** ISOLATED DEVELOPMENT VERIFICATION (Production Infrastructure Untouched)

---

## 1. Executive Summary

This report establishes the verified baseline of the development and production environments prior to executing the final controlled integration test suite. All configuration parameters, git commits, RunPod endpoint references, checkpoint paths, and security/isolation boundaries were independently verified directly against the running codebase and environment.

---

## 2. Environment Verification Matrix

| Parameter | Recorded Value | Verification Source | Status |
|---|---|---|---|
| **MME_Telephony Git Branch** | `streaming-implementation` | `git rev-parse --abbrev-ref HEAD` | **VERIFIED** |
| **MME_Telephony Git Commit** | `71a17e13876d64ab148d9794f8231530c279d3e7` | `git rev-parse HEAD` | **VERIFIED** |
| **MME_wt_tts_v4 Git Branch** | `tts-v4-streaming-validate` | `git -C ... rev-parse --abbrev-ref HEAD` | **VERIFIED** |
| **MME_wt_tts_v4 Git Commit** | `45131a4` | `git -C ... rev-parse HEAD` | **VERIFIED** |
| **MME_wt_tts_v4 Working Tree** | Clean (0 uncommitted modifications) | `git status --porcelain` | **VERIFIED** |
| **MME_Telephony Uncommitted Mods** | Preserved (staging-isolated unit tests & docs) | `git status --porcelain` | **VERIFIED** |
| **Dev TTS Endpoint** | `dzqzqas7frc0zt` (`mme-v4-validate-1`) | Endpoint configuration | **VERIFIED** |
| **Prod TTS Endpoint** | `6bmxas8ccwzhrv` (`MME-TTS`) | Read-only guard: ZERO requests sent | **STRICTLY UNTOUCHED** |
| **Brain LLM Endpoint** | `hu8qhrvlxbv314` (Qwen3-30B-A3B-Instruct-2507) | `config/settings.py` | **VERIFIED** |
| **STT Whisper Endpoint** | `ua9l3gus6qjnmg` (Whisper Hindi/Hinglish Prime) | `config/settings.py` | **VERIFIED** |
| **Dev RunPod Template** | `pxc1dw5vv2` (v18) | Template manifest | **VERIFIED** |
| **Prod RunPod Template** | `z3sn9cxpz7` (v14) | Production configuration | **STRICTLY UNTOUCHED** |
| **Container Image** | `ghcr.io/aitechteammme-dev/mme-tts-runpod:v3` | RunPod template container | **VERIFIED** |
| **Hardware & VRAM** | NVIDIA RTX A5000 / RTX 4090 (24 GB VRAM) | Worker hardware specs | **VERIFIED** |
| **CUDA & PyTorch Stack** | CUDA 12.4 / 12.8, PyTorch 2.6.0 / 2.7.1 | Container runtime environment | **VERIFIED** |
| **Loaded TTS Model** | Chatterbox Multilingual V3 (`t3_model="v3"`) | Checkpoint in `/app/hf_cache` | **VERIFIED** |
| **Watermark Safety Gate** | `ALLOW_UNWATERMARKED_STREAMING=0` (Fail-Closed) | `os.environ` check | **VERIFIED FAIL-CLOSED** |
| **Batch Fallback Configuration** | Complete-waveform Perth-watermarked synthesis (`AI_TTS_OVERLAP=true`, RTF 0.74) | `app/ai/pipeline.py` & `app/ai/clients.py` | **VERIFIED INTACT** |
| **TTS Jitter Pre-Buffer** | `ai_tts_prebuffer_ms = 400` (Default 400ms, range [0, 2000]) | `config/settings.py` & `app/ai/pipeline.py` | **VERIFIED** |

---

## 3. Production Isolation Guarantee

1. **Zero PSTN Traffic:** The default provider setting in `config/settings.py` is `mock` in dev/test, and live telephony credentials (`PLIVO_AUTH_TOKEN`, `JWT_SECRET`) are never dispatched to external carriers during these tests.
2. **Endpoint Fencing:** Zero requests were or will be submitted to `6bmxas8ccwzhrv`. All validation is conducted via isolated dev endpoints, local mock transports, and unit test harnesses.
3. **Template Immutability:** No RunPod templates have been altered or redeployed during this testing run.
