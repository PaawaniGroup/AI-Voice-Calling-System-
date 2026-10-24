# Roman Hinglish pronunciation: status, tools, open items

Problem: the TTS reads Roman-letter Hindi with English sounds (`main`, `hoon`, `hai`, `aapko`, `chaiya`, `baare`, ...). Four approaches are built to be compared **by native listeners**:
A (Brain writes Devanagari), B (lexicon), C (English words inside Hindi sentences), D (language tag for Roman-only sentences).

**Status: everything is built and unit-tested. Nothing is listened to, nothing is verified by a native speaker, no approach has been chosen, nothing is wired into the call path (default stays as before).**

## Files

| What | Where |
|---|---|
| Lexicon (1,257 entries, all `unreviewed`) | `lexicon/hindi_lexicon.csv` (roman, variants, devanagari, notes, context_rule, status, reviewed_by) |
| English respelling proposals (C2), English word list | `lexicon/english_respell.csv`, `lexicon/english_known.txt` |
| Module | `mme_lexicon.py` (`convert(text)`, `Lexicon(...)`, `split_by_script`, `sentence_lang`) |
| Tests | `tests/unit/test_mme_lexicon.py` (variants, homographs, main/ki/to/hai rules, gate, idempotency, speed, front-end, regression of the carrier sentences), `tests/unit/test_listening_tools.py` |
| Review sheet for native speakers | `lexicon/review_sheet.csv` (Hindi), `lexicon/review_sheet_english.csv`; apply returned sheets with `tools/apply_review.py` |
| Coverage / frequency | `tools/lexicon_coverage.py`, output in `lexicon/reports/` |
| Approach A | `SYSTEM_PROMPT_DEVANAGARI` in `app/ai/pipeline.py` (opt-in `Conversation(devanagari_hindi=True)`), `tools/measure_roman_hindi.py`, `lexicon/brain_test_utterances.jsonl` (100 synthetic utterances) |
| Carrier sentences (reported words) | `lexicon/carrier_sentences.jsonl` (41 sentences incl. English controls) |
| Per-condition TTS inputs (text only) | `python tools/make_condition_inputs.py` -> `lexicon/listening_inputs/` |
| Blind N-condition page + tally | `tools/make_abn_page.py`, `tools/tally_abn.py` |
| Seeding scripts (one-time) | `tools/build_lexicon.py` (refuses to overwrite review statuses without `--force`), `tools/seed_english_words.py` (needs `pip install wordfreq`, build time only) |

## How the module decides (summary)

* Runs after `tts_frontend.normalize` and only touches Latin words. Variants (chahiye/chaiya/chaiye/chahie..., hoon/hun/hu, baare/bare/baarey, han/haan, aapka/apka, ...) share one row.
* English homographs (main, to, hi, me, us, is, do, per, par, are, man, bas, ban, ho, mat, tab, log, hum) convert only next to a Hindi word; `so`, `pan` never convert. "main reason", "main page", "course name", "to do list" stay Latin.
* `main/me` (मैं or में), `ki` (कि or की): neighbour rules; when no rule is sure the fallback is applied **and listed in `Result.decisions`**. `hai` after a plural subject is kept as written and logged.
* Sentence gate (default: 90% of Latin words known): otherwise the whole sentence is returned unchanged and its unknown words are counted (`Lexicon.write_unknown_report`). `min_known=0` turns the gate off (the "convert every Hindi-looking token" condition).
* Deterministic, no network, idempotent (iterated to a fixed point), 0.15 ms per sentence on average (measured on the 342-sentence corpus). `only_verified=True` converts only `verified` rows (today: none).

## What could NOT be verified (honest list)

1. **Every Devanagari spelling** is a proposal (written from memory, plus 537 rows generated from verb stems and suffixes). No native speaker has confirmed any. Status stays `unreviewed` until `tools/apply_review.py` is run on returned sheets.
2. **Listening results, preference shares, per-word error, the 95% target and the choice between A, B, C, D**: not measured. No audio was generated. The pack tools are tested with silent synthetic wavs only.
3. **Approach A** (how often the Brain still writes Roman Hindi): not measured, the Brain endpoint is not deployed. The prompt wording is untested on Qwen3-30B-A3B-Instruct-2507.
4. **Coverage target (98% of Hindi tokens by frequency)**: the only corpus available (342 sentences) is the one the old table was written from, so its 98.5% is circular. Re-run `tools/lexicon_coverage.py` on real Brain replies.
5. **Rule heuristics** for main/me/ki/to/is/do (word lists in `mme_lexicon.py`) are plausible, unit-tested on written examples, not tested on real replies. Verb inflections are covered only for the generated stems; unknown inflections make the gate skip the sentence (safe, but it lowers the converted share).
6. The 12 problem sentences and their audio, the TTS endpoint and the reviewers are not supplied yet.
7. `tests/unit/test_configuration.py::test_validate_config_script_exit_codes` fails (Windows Python 3.11 and the WSL venv, `tests/unit`: 280 passed, 1 failed). The message is the JWT-secret length check; the test imports nothing changed here. It was not run against a clean checkout, so "unrelated" is an inference, not a measurement. Integration tests were not run (they need FreeSWITCH/browser).

## Next steps (need you)

1. Send the reviewer sheets (`lexicon/review_sheet.csv`) to at least 3 native speakers; run `tools/apply_review.py` on the returns.
2. Build conditions: `python tools/make_condition_inputs.py`, then run the Colab notebook once per condition with the same fixed seed and reference clip (`baseline_hi`, `baseline_en`, `lex_gated`, `lex_nogate`, `lex_respell`, `lex_segments`); add the 12 problem sentences and real Brain replies to the input file first (at least 30 sentences).
3. `python tools/make_abn_page.py --cond baseline_hi=... --cond lex_gated=... --baseline baseline_hi --texts lexicon/listening_inputs/listening_texts.jsonl --out listening_pack --listeners 3`, zip `listening_pack/`, keep `listening_pack_KEY.json` private; then `python tools/tally_abn.py listening_pack_KEY.json answers_*.json`.
4. When the Brain is deployed: `python tools/measure_roman_hindi.py live --base-url ... --key-env ...`.
5. Only after a winner exists: integrate behind a setting (default OFF) with token-only logging.
