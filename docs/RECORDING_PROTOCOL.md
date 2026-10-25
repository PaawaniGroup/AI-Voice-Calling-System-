# Recording protocol for the end-of-turn and rolling-STT spikes

**Nobody and nothing records automatically.** You record, with the consent below; the assistant (Claude) records nothing and only reads the WAV files you point it to.
Status: **prepared, not run.** No spike runs until you send the folder path and say go.

## 1. What is recorded and why

30 short scripted utterances, used for two offline tests (see `docs/LATENCY_FEASIBILITY_PLAN.md`):

- **End-of-turn replay** (`tools/eot_replay.py`): how often a speaker is cut off at a natural pause when the silence window is 700 / 550 / 450 / 350 ms. Offline, no network, $0.
- **Rolling-window STT** (`tools/rolling_stt_spike.py`, cap $0.20): the audio is sent to the project's speech-to-text service on RunPod. Only numbers and file ids are written down; transcripts are not kept.

**Every recording is run in two versions, reported separately:**
- `clean8k`: the recording converted to 8 kHz mono.
- `phone`: the same, then passed through the telephone chain: 8 kHz sampling, a 300–3400 Hz band-pass (`tools/telephone_chain.py`) and G.711 mu-law encoding and decoding.

The phone version is a model of the line, not the line: it has no packet loss, mobile-network codec or echo. The first supervised call shows the real thing.

## 2. Who

- 3 speakers (S1, S2, S3). Mix of voices and accents. Speakers are **codes only**: the table "code → person" stays with you and is never given to the assistant, put in the repo or in a database.
- Each speaker is an adult team member or acquaintance who has agreed. No customers, no callers.

## 3. Consent (read it out or hand it over; keep the signed or ticked sheet yourself, outside the repo)

Both texts are **DRAFTS FOR REVIEW**. The Hindi text in particular needs a Hindi-speaking reviewer, and both need a data-protection adviser, before use. They are a practical draft, **not legal advice** (the DPDP Act 2023 asks for a clear notice, a specific purpose and a way to withdraw).

**English (DRAFT)**

> I agree that my voice is recorded to test a telephone assistant. The recording is used only to measure how fast and how accurately speech is recognised. It may be processed by the cloud speech service used for this test. It will not be shared outside the project team and will be deleted by **[DELETE-BY DATE]**. I can withdraw my consent at any time and my recordings will then be deleted. I will not say my name, phone number, address or any other personal information in the recording.

**हिन्दी (DRAFT — समीक्षा के लिए मसौदा)**

> मैं सहमति देता/देती हूँ कि फ़ोन असिस्टेंट की जाँच के लिए मेरी आवाज़ रिकॉर्ड की जाए। रिकॉर्डिंग का उपयोग केवल यह नापने के लिए होगा कि बोली कितनी तेज़ी और कितनी सटीकता से पहचानी जाती है। इस जाँच के लिए इस्तेमाल होने वाली क्लाउड स्पीच सेवा इसे प्रोसेस कर सकती है। इसे प्रोजेक्ट टीम के बाहर साझा नहीं किया जाएगा और **[हटाने की तारीख]** तक हटा दिया जाएगा। मैं किसी भी समय अपनी सहमति वापस ले सकता/सकती हूँ; तब मेरी रिकॉर्डिंग हटा दी जाएँगी। मैं रिकॉर्डिंग में अपना नाम, फ़ोन नंबर, पता या कोई और निजी जानकारी नहीं बोलूँगा/बोलूँगी।

## 4. The lines (no personal data: read exactly as written, no ad-libbing)

Lines 1–12 are the owner's caller lines. Lines 13–15 are the pause lines: leave a pause of **0.6–1 s in the middle of the sentence** where "…" is marked (they test the end-of-turn rule).
`N` = record with low background noise (a TV or street sound in the next room). `SP` = record on speakerphone: phone on a table about 1 m away, the line played or spoken at normal volume.

| Id | Language code | Line | Flag |
|---|---|---|---|
| 1 | hi | नमस्ते, मुझे B.Tech के बारे में जानकारी चाहिए। | |
| 2 | hg (Hinglish) | Mujhe B.Tech ki fees janni hai. | |
| 3 | hi | B.Tech की फीस कितनी है? | N |
| 4 | hg | Aapka admission process kya hai? | |
| 5 | en | What is the eligibility for B.Tech? | |
| 6 | hi | कोर्स की अवधि कितनी होती है? | |
| 7 | hg | Meri counselling ki next date kab hai? | N |
| 8 | hi | क्या मैं किश्तों में फीस जमा कर सकता हूँ? | |
| 9 | en | Can I pay the fees in two installments? | SP |
| 10 | hg | Hostel ki facility hai kya? | N |
| 11 | hi | कृपया मुझे कल शाम को कॉल कर लीजिए। | |
| 12 | hg | Thanks, bas itna hi. Bye. | |
| 13 | hi | मैं पूछना चाहता था… हम्म… कि प्लेसमेंट कैसा रहता है? | P |
| 14 | hg | Actually… mujhe ye puchna tha… ki online classes hoti hain kya? | P |
| 15 | en | I wanted to ask… um… whether there is an evening batch. | P |

**Who records what (30 files, every line twice, by two different speakers):**

| Speaker | Lines |
|---|---|
| S1 | 1–10 |
| S2 | 6–15 |
| S3 | 1–5 and 11–15 |

Each speaker also records 5 seconds of the room with nobody talking (`roomtone_S1.wav` …): not counted, used to check the noise level.

## 5. How to record

- Any phone voice recorder or PC recorder, WAV or any common format; the tools convert to 8 kHz mono. Speak at normal phone distance (except the `SP` line), one line per file, 0.5 s of silence before and after.
- Re-record a file if you stumble or say anything personal; delete the bad take.

## 6. Names and storage

- File name: `u{line}_{lang}_{speaker}.wav`, with `_N` or `_SP` appended for the noise and speakerphone lines. Examples: `u02_hg_S1.wav`, `u03_hi_S3_N.wav`, `u09_en_S2_SP.wav`, `u14_hg_S2.wav`, `roomtone_S1.wav`. No names, no dates of birth, no places.
- Folder **outside the repository**, with the deletion date in its name, for example `C:\Users\Admin\Desktop\mme_recordings\eot_<recording-date>_DELETE-BY-<yyyy-mm-dd>\`. Suggested deletion date: 30 days after recording, or earlier once both spikes are done.
- The repository's audio guard (`scripts/check_commit_guard.py`, `.gitignore`) rejects audio files, so a recording cannot be committed by accident. The tools never write audio: the phone version exists only in memory.
- Put a one-line `DELETION.txt` in the folder with the date. On that date delete the folder and tell the assistant to confirm it is gone.

## 7. After the recordings exist

1. You tell the assistant the folder path **and say go**. Nothing is copied into the repository.
2. **Free first:** `python tools/eot_replay.py <folder> --out <folder>\eot_report.json` (offline). It prints two tables, `clean8k` and `phone`.
3. **Then, only with your go:** `python tools/rolling_stt_spike.py <folder> --budget 0.20 --yes --out <folder>\stt_report.json`. Both versions share the one $0.20 budget (about 180 decodes); if it is reached the tool stops and reports what it measured. It needs the STT endpoint key (from the tenant config or `RUNPOD_STT_KEY`).
4. The reports contain numbers and file ids only.
