# Script Policy Measurement Report **[STUB ONLY - SIMULATION]**

> [!IMPORTANT]
> **STUB NOTICE**: Brain endpoint was evaluated using the local simulation stub. All figures below reflect stub results. Real measurements must be run against the live RunPod endpoint once reachable.

### 1. Condition Comparison Across 3 Runs (Mean ± Spread)

| Metric | OLD Prompt (Baseline) | NEW Prompt (Enforced) | Difference (New - Old) | Significant? |
|---|---|---|---|---|
| First-gen checker pass share | 59.0% (±0.0%) | 100.0% (±0.0%) | +41.0% | Yes |
| Roman Hindi token share | 19.2% (±0.0%) | 0.0% (±0.0%) | -19.2% | Yes |
| Wrong-language reply share | 41.0% (±0.0%) | 0.0% (±0.0%) | -41.0% | Yes |
| Spelled-out numbers share | 0.0% (±0.0%) | 0.0% (±0.0%) | +0.0% | No (inside spread) |
| Mean reply length (words) | 13.8 (±0.0) | 15.5 (±0.0) | +1.7 | Yes |
| P95 reply length (words) | 16.0 (±0.0) | 16.0 (±0.0) | +0.0 | No (inside spread) |
| First-token latency (ms) | 54.5 (±5.0) | 54.5 (±5.0) | +0.0 | No (inside spread) |
| First-clause latency (ms) | 114.6 (±31.0) | 114.6 (±31.0) | +0.0 | No (inside spread) |

### 2. 20 Worst Replies (Ranked by Violations)

| ID | Style | Input Caller Utterance | Reply | Issues |
|---|---|---|---|---|
| u001 | roman | Mujhe B.Tech ki fees janni hai. | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u002 | roman | Aapka admission process kya hai? | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u003 | roman | Kya hostel facility campus ke andar available hai? | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u004 | roman | Form bharne ki last date kab tak hai? | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u005 | roman | B.Tech admission ke liye documents kaun se lagenge? | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u006 | roman | 12th mein 60 percent marks hain, kya direct admission mil sakta hai? | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u007 | roman | Scholarship ke liye eligibility criteria kya hota hai? | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u008 | roman | Computer Science engineering branch ki cut-off kitni jaati hai? | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u009 | roman | Kya fees installment mein pay kar sakte hain? | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u010 | roman | Education loan ke liye college help karta hai kya? | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u011 | roman | College ka placement record kaisa hai aur average package kitna milta hai? | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u012 | roman | Entrance exam JEE Main dena compulsory hai ya 12th marks par ho jayega? | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u013 | roman | Mujhe MBA marketing course ke baare mein poochna tha. | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u014 | roman | Kya aapke yahan Artificial Intelligence aur Data Science branch hai? | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u015 | roman | Biotechnology engineering ki fees aur seats check kijiye. | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u016 | roman | Mera naam Rohit Sharma hai, mujhe BCA course mein admission lena hai. | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u017 | roman | Mera naam Anjali Gupta hai aur main B.Tech CSE ke liye interested hoon. | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u018 | roman | Haan | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u019 | roman | Ok | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |
| u020 | roman | Theek hai | Ji, B.Tech ki fees Rs 1,25,000 hai. Aapko online form 30 June tak bharna hoga. | no_devanagari_in_hi_mode, roman_hindi_word:hai, roman_hindi_word:aapko, roman_hindi_word:tak, roman_hindi_word:bharna, roman_hindi_word:hoga |