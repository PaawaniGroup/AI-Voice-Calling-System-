# MME Telephony Counselling Glossary & Phonetic Normalisation

This glossary defines canonical academic and admissions terminology for Make My Education (MME) voice calling, along with known Speech-to-Text (STT) mis-transcriptions observed in phone audio.

---

## 1. Observed STT Mis-Transcriptions & Canonical Mappings

During live telephone calls over 8 kHz narrow-band audio, Whisper/ASR frequently transliterates Indian English technical and academic terms into colloquial Roman-Hinglish or phonetic approximations:

| Observed STT Mis-Spelling | Canonical Term | Category | Notes / Context |
| :--- | :--- | :--- | :--- |
| `beetaik`, `bee tek`, `btek` | **B.Tech** | Degree | Bachelor of Technology |
| `bi si`, `bee see ay` | **BCA** | Degree | Bachelor of Computer Applications |
| `je ee men`, `jee mane` | **JEE Main** | Entrance Exam | Engineering Joint Entrance Examination |
| `je ee edvaans` | **JEE Advanced** | Entrance Exam | IIT Joint Entrance Examination |
| `parsentaail`, `percentail` | **percentile** | Score Metric | Entrance score cutoff (e.g. 90 percentile) |
| `nyoo jeelaind` | **New Zealand** | Study Destination | Country name |
| `eprod`, `ebrod` | **abroad** | Study Category | "Study abroad" inquiry misinterpreted as unknown acronym |

---

## 2. Core Counselling Terms Glossary

### Degrees & Programs
- **B.Tech** (Bachelor of Technology): `beetaik`, `btech`, `b tek`, `bee tek`, `beetek`, `बीटेक`
- **BCA** (Bachelor of Computer Applications): `bi si`, `b c a`, `bee see ay`, `बीसीए`
- **MBA** (Master of Business Administration): `em bee ay`, `emba`, `mba`, `एमबीए`
- **BBA** (Bachelor of Business Administration): `bee bee ay`, `bba`, `बीबीए`
- **MBBS** (Bachelor of Medicine): `em bee bee es`, `mbbs`, `एमबीबीएस`
- **B.Sc** (Bachelor of Science): `bee es see`, `bsc`, `बीएससी`

### Entrance Examinations
- **NEET** (UG / PG): `neet`, `nit`, `neet ug`, `नीट`
- **JEE Main**: `je ee men`, `jee main`, `jee mains`, `jee mane`, `जेईई मेन`
- **JEE Advanced**: `jee advanced`, `je ee edvaans`, `jee advance`, `जेईई एडवांस्ड`
- **CUET**: `si yu ee tee`, `cuet`, `सीयूईटी`
- **CAT**: `cat exam`, `kat`, `कैट`
- **GATE**: `gate exam`, `गेट्स`, `गेट`

### Admissions & Campus Terminology
- **percentile**: `parsentaail`, `parsentaile`, `percentail`, `persentile`, `परसेंटाइल`
- **scholarship**: `skolarship`, `scolarship`, `skolership`, `स्कॉलरशिप`
- **admission**: `edmission`, `admn`, `admisn`, `एडमिशन`
- **eligibility**: `elidjibility`, `ilijibility`, `eligiblity`, `एलिजिबिलिटी`
- **hostel**: `hostal`, `haustel`, `हॉस्टल`
- **fees**: `fee`, `fis`, `fees`, `फीस`
- **placement**: `plesment`, `plesemint`, `प्लेसमेंट`
- **counsellor**: `kaunslar`, `counselor`, `काउंसलर`
- **abroad**: `eprod`, `ebrod`, `abrod`, `एब्रॉड`

### Study Abroad Destinations
- **New Zealand**: `nyoo jeelaind`, `new zeeland`, `nyu ziland`, `न्यूजीलैंड`
- **Canada**: `kainada`, `caneda`, `कनाडा`
- **Australia**: `ostreliya`, `ostrelia`, `ऑस्ट्रेलिया`
- **United Kingdom / UK**: `yu ke`, `uk`, `यूके`
- **USA**: `yu es ay`, `usa`, `यूएसए`
- **Germany**: `jarmani`, `germeny`, `जर्मनी`
- **Russia**: `roos`, `rashiya`, `रूस`
- **Ireland**: `aairlaind`, `ireland`, `आयरलैंड`

---

## 3. Pipeline Placement Architecture

The glossary correction layer is a lightweight, zero-latency, deterministic pre-processing stage. It sits **immediately between STT output and downstream consumers (RAG and LLM Brain)**:

```
[Inbound Audio 8kHz PCM]
          │
          ▼
   [STT Service] ──────────► Raw Transcript: "mujhe beetaik aur nyoo jeelaind ke baare mein puchna hai"
          │
          ▼
┌───────────────────────────────┐
│  Glossary Correction Layer    │  ◄── [docs/counselling_glossary.md rules]
│  (app/ai/glossary_correction) │
└───────────────────────────────┘
          │
          ├────────────────────────────────────────┬─────────────────────────────────────┐
          ▼                                        ▼                                     ▼
[RAG Retrieval Query]                     [LLM Brain Prompt]                   [Dashboard Activity Feed]
"mujhe B.Tech aur New Zealand..."         "STUDENT: mujhe B.Tech aur..."        "🗣️ Caller: mujhe B.Tech..."
```

### Why Place It Here:
1. **RAG Vector Search Precision:** Embeddings and keywords for `B.Tech` and `New Zealand` match college knowledge bases, whereas `beetaik` and `nyoo jeelaind` produce 0 retrieval matches.
2. **Brain Hallucination Prevention:** The LLM does not have to guess what "eprod" or "beetaik" means, eliminating invented explanations.
3. **Zero Audio Lag:** Correction is a regular-expression and dictionary match taking `< 0.2 ms` CPU time, adding no perceptual latency.
