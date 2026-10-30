# Recording protocol for the end-of-turn and rolling-STT spikes

**Nobody and nothing records automatically.** You record, with the consent below; the assistant (Claude) records nothing and only reads the WAV files you point it to.
Status: **prepared, not run.** No spike runs until you send the folder path and say go. A one-page sheet per speaker is in `docs/RECORDING_SHEET.md`.

## 1. What is recorded and why

**80 short recordings plus 3 room-tone files** (a different set from any earlier list; the owner's 12 caller lines are lines 1-12 below):
- 30 scripted lines (the owner's 12 caller lines, 3 pause lines, 3 of them with noise and 1 on speakerphone), every line recorded twice by two different speakers;
- 20 extra pause lines (pauses at commas and before numbers or course names);
- 30 open answers (10 open questions per speaker, answered without reading, about 5 minutes per speaker).

They feed two offline tests (see `docs/LATENCY_FEASIBILITY_PLAN.md`):
- **End-of-turn replay** (`tools/eot_replay.py`): how often a speaker is cut off at a pause when the silence window is 700 / 550 / 450 / 350 ms. Offline, no network, $0.
- **Rolling-window STT** (`tools/rolling_stt_spike.py`, cap $0.20): the audio is sent to the project's speech-to-text service on RunPod. Only numbers and file ids are written down; transcripts are not kept.

**Every recording is run in two versions, reported separately:**
- `clean8k`: the recording converted to 8 kHz mono.
- `phone`: the same, then passed through the telephone chain: 8 kHz sampling, a 300-3400 Hz band-pass (`tools/telephone_chain.py`) and G.711 mu-law encoding and decoding.

The phone version is a model of the line, not the line: it has no packet loss, mobile-network codec or echo. The first supervised call shows the real thing.

## 2. Who

- 3 speakers (S1, S2, S3). Mix of voices and accents. Speakers are **codes only**: the table "code to person" stays with you and is never given to the assistant, put in the repo or in a database.
- Each speaker is an adult team member or acquaintance who has agreed. No customers, no callers.

## 3. Consent (read it out or hand it over; keep the signed or ticked sheet yourself, outside the repo)

Both texts are **DRAFTS FOR REVIEW**. The Hindi text in particular needs a Hindi-speaking reviewer, and both need a data-protection adviser, before use. They are a practical draft, **not legal advice** (the DPDP Act 2023 asks for a clear notice, a specific purpose and a way to withdraw).

**English (DRAFT)**

> I agree that my voice is recorded to test a telephone assistant. The recording is used only to measure how fast and how accurately speech is recognised. It may be processed by the cloud speech service used for this test. It will not be shared outside the project team and will be deleted by **[DELETE-BY DATE]**. I can withdraw my consent at any time and my recordings will then be deleted. I will not say my name, phone number, address or any other personal information in the recording.

**हिन्दी (DRAFT — समीक्षा के लिए मसौदा)**

> मैं सहमति देता/देती हूँ कि फ़ोन असिस्टेंट की जाँच के लिए मेरी आवाज़ रिकॉर्ड की जाए। रिकॉर्डिंग का उपयोग केवल यह नापने के लिए होगा कि बोली कितनी तेज़ी और कितनी सटीकता से पहचानी जाती है। इस जाँच के लिए इस्तेमाल होने वाली क्लाउड स्पीच सेवा इसे प्रोसेस कर सकती है। इसे प्रोजेक्ट टीम के बाहर साझा नहीं किया जाएगा और **[हटाने की तारीख]** तक हटा दिया जाएगा। मैं किसी भी समय अपनी सहमति वापस ले सकता/सकती हूँ; तब मेरी रिकॉर्डिंग हटा दी जाएँगी। मैं रिकॉर्डिंग में अपना नाम, फ़ोन नंबर, पता या कोई और निजी जानकारी नहीं बोलूँगा/बोलूँगी।

## 4. What is said (no personal data: no real names, numbers or addresses)

### 4a. Scripted lines 1-15 (read exactly as written)

Lines 1-12 are the owner's caller lines. Lines 13-15 are pause lines: leave a pause of **0.6-1 s in the middle of the sentence** where "..." is marked.
`N` = record with low background noise (a TV or street sound in the next room). `SP` = record on speakerphone: phone on a table about 1 m away.

| Id | Language code | Line | Flag |
|---|---|---|---|
| 1 | hi | नमस्ते, मुझे B.Tech के बारे में जानकारी चाहिए। |  |
| 2 | hg | Mujhe B.Tech ki fees janni hai. |  |
| 3 | hi | B.Tech की फीस कितनी है? | N |
| 4 | hg | Aapka admission process kya hai? |  |
| 5 | en | What is the eligibility for B.Tech? |  |
| 6 | hi | कोर्स की अवधि कितनी होती है? |  |
| 7 | hg | Meri counselling ki next date kab hai? | N |
| 8 | hi | क्या मैं किश्तों में फीस जमा कर सकता हूँ? |  |
| 9 | en | Can I pay the fees in two installments? | SP |
| 10 | hg | Hostel ki facility hai kya? | N |
| 11 | hi | कृपया मुझे कल शाम को कॉल कर लीजिए। |  |
| 12 | hg | Thanks, bas itna hi. Bye. |  |
| 13 | hi | मैं पूछना चाहता था… हम्म… कि प्लेसमेंट कैसा रहता है? |  |
| 14 | hg | Actually… mujhe ye puchna tha… ki online classes hoti hain kya? |  |
| 15 | en | I wanted to ask… um… whether there is an evening batch. |  |

### 4b. Extra pause lines p01-p20 (read exactly as written)

Pauses of **0.6-1 s** at every "..." (at commas and just before numbers or course names). Say the line once, naturally; do not exaggerate the pause beyond 1 s (a longer pause counts as the end of the turn).

| Id | Language code | Line |
|---|---|---|
| p01 | hi | मुझे जानना है… कि B.Tech की फीस, पहले साल की, कितनी है? |
| p02 | hi | क्या आप बता सकते हैं… कि MBA में, एडमिशन के लिए, कितने नंबर चाहिए? |
| p03 | hi | हॉस्टल की फीस, खाना मिलाकर… कितनी पड़ेगी? |
| p04 | hi | कोर्स की अवधि… चार साल है, या… तीन साल? |
| p05 | hi | मुझे BCA के बारे में, और उसकी… फीस के बारे में बताइए। |
| p06 | hi | फीस दो किश्तों में… या चार किश्तों में, जमा हो सकती है? |
| p07 | hi | आखिरी तारीख… पंद्रह तारीख है, ना? |
| p08 | hg | Mujhe batao… ki B.Tech ki fees, per semester, kitni hai? |
| p09 | hg | Kya MBA mein, admission ke liye… entrance exam dena padta hai? |
| p10 | hg | Hostel ki fees, mess ke saath… kitni hogi? |
| p11 | hg | Course ka duration… char saal hai, ya… teen saal? |
| p12 | hg | Mujhe BCA ke baare mein, aur uske… placement ke baare mein batao. |
| p13 | hg | Fees do installments mein… ya char installments mein, de sakte hain? |
| p14 | hg | Last date… pandrah tareekh hai, na? |
| p15 | en | I want to know… the fees for B.Tech, first year, in total. |
| p16 | en | Can you tell me… whether MBA, after graduation, needs an entrance exam? |
| p17 | en | What is the hostel fee, including meals… per year? |
| p18 | en | Is the course… four years, or… three years? |
| p19 | en | Can I pay the fees… in two installments, or… four installments? |
| p20 | en | The last date… is the fifteenth, right? |

### 4c. Open questions s01-s10 (answered without reading)

Answer each in your own words, one to three sentences (about 30 s), as you would on a call. Do not prepare. Talk about imagined situations and general opinions: **no real names, numbers, addresses or personal details**. Choose the language you naturally use and put its code in the file name (`hi`, `hg` or `en`).

| Id | Question |
|---|---|
| s01 | अगर आप किसी कॉलेज के बारे में जानकारी लेना चाहें तो सबसे पहले क्या पूछेंगे? |
| s02 | Ek achha college chunte waqt aapke liye sabse zaroori cheez kya hogi? |
| s03 | How would you decide between two courses that both look interesting? |
| s04 | फीस के बारे में बात करते समय आपको किन बातों की चिंता रहती है? |
| s05 | Hostel aur college ki location ke baare mein aap kya sochte hain? |
| s06 | What would make a phone call with an assistant feel natural to you? |
| s07 | जब कोई फ़ोन पर जवाब देने में देर करे तो आपको कैसा लगता है? |
| s08 | Kisi ko fees installments mein dene ke baare mein apne vichar batayein. |
| s09 | Describe how you would explain an admission process to a friend. |
| s10 | आप अपने किसी दोस्त को पढ़ाई की सलाह कैसे देंगे? |

## 5. The rule that marks where each turn truly ends

- **One file = one turn:** one scripted line, or one answer to one open question.
- The turn ends where you would expect the assistant to start speaking: **right after your last word**. Stay silent for **0.5 s** after it (**2 s after an open answer**), then stop the recording. Nothing may follow the last word: no "umm", no "thank you", no clearing the throat.
- A pause inside the turn must be **shorter than 1.3 s**. If you stop to think for longer, you have ended the turn: stop and record a new file.
- Sentence breaks inside an open answer are normal pauses inside one turn. If the replay cuts the speaker off at one, that is a **real false cut-off**: the assistant would have started talking over a speaker who was not finished.
- The tools take the last frame of speech in the file as the true end. Decisions forced by the 6 s utterance cap of the app do not depend on the silence threshold and are counted separately, never as cut-offs.

## 6. What the results can and cannot prove

Zero cut-offs in n utterances only bounds the true rate: with 95% confidence it is below 1 - 0.05^(1/n).

| n (utterances) | 6 | 12 | 30 | 80 | 150 | 300 |
|---|---|---|---|---|---|---|
| bound for zero cut-offs | 39% | 22% | 9.5% | 3.7% | 2.0% | 1.0% |

- The plan's go/no-go threshold of 2% therefore needs **about 150 clean utterances** (exactly 149 with zero cut-offs). This set gives n = 80 per version, so it can show a clear **NO-GO** or a **range**, not a PASS of the 2% threshold. About 70 more clean utterances would be needed for that.
- With fewer than 150 utterances the tools report **a range** (the exact binomial interval), never a bare pass or fail, and say so in their output.
- Recordings from the same speaker are not independent, so the real uncertainty is larger than the table.
- Word-error results from the STT spike are means over n files: below about 20 files the p95 is close to the maximum and is only indicative.

## 7. How to record: two sessions of about 15 minutes, with a break

Each speaker records in **two sessions of about 15 minutes with a break of at least 10 minutes between them** (water, a short walk, silence). Open answers need a relaxed voice, and a tired voice changes the pauses the test measures. The times are my estimate: about 25 s per scripted file and about 50 s per open answer, including starting, stopping and a re-take now and then.

| Session | What | Files per speaker | Time (estimate) |
|---|---|---|---|
| 1 | Consent and setup (3 min), 5 s of room tone, the scripted lines `u..` including the noise (`N`) and speakerphone (`SP`) lines (set the room up once for each), the speaker's extra pause lines `p..` | 16-17 plus the room tone | about 15 min |
| break | at least 10 min | | |
| 2 | A warm-up question (not recorded), then the 10 open answers `s01`-`s10` without reading; a helper runs the intake check on the files of session 1 meanwhile | 10 | about 15 min |

- Any phone voice recorder or PC recorder, WAV or any common format; the tools convert to 8 kHz mono. Speak at normal phone distance (except the `SP` line), one turn per file, 0.5 s of silence before and the tail described in section 5.
- Re-record a file if you stumble or say anything personal; delete the bad take.
- Each speaker also records 5 seconds of the room with nobody talking (`roomtone_S1.wav`, ...): not counted, used to check the noise level.

## 8. Names and storage

- File names are in the sheet, for example `u02_hg_S1.wav`, `u03_hi_S3_N.wav`, `u09_en_S2_SP.wav`, `p04_hi_S1.wav`, `s07_hg_S2.wav`, `roomtone_S1.wav`. No names, no dates of birth, no places.
- Folder **outside the repository**, with the deletion date in its name, for example `C:\Users\Admin\Desktop\mme_recordings\eot_<recording-date>_DELETE-BY-<yyyy-mm-dd>\`. Suggested deletion date: 30 days after recording, or earlier once both spikes are done.
- The repository's audio guard (`scripts/check_commit_guard.py`, `.gitignore`) rejects audio files, so a recording cannot be committed by accident. The tools never write audio: the phone version exists only in memory.
- Put a one-line `DELETION.txt` in the folder with the date. On that date delete the folder and tell the assistant to confirm it is gone.

## 9. After the recordings exist

1. You tell the assistant the folder path **and say go**. Nothing is copied into the repository.
2. **Free intake check first:** `python tools/recording_intake_check.py <folder>`. Per file: name, duration (at least 3 s; open answers 5-60 s), sample rate, clipping, silence, lead and tail silence, and for pause lines an internal pause of 0.6-1.0 s. It prints counts and file ids only, and lists missing and unexpected files. Files with errors are re-recorded before any spike.
3. **Free:** `python tools/eot_replay.py <folder> --out <folder>\eot_report.json` (offline). It prints two tables, `clean8k` and `phone`, with a range and a reading for each threshold.
4. **Then, only with your go:** `python tools/rolling_stt_spike.py <folder> --budget 0.20 --yes --out <folder>\stt_report.json`. Both versions share the one $0.20 budget; if it is reached the tool stops and reports what it measured. It needs the STT endpoint key (from the tenant config or `RUNPOD_STT_KEY`).
5. The reports contain numbers and file ids only.
