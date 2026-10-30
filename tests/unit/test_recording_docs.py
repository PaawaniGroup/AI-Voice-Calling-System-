"""docs/RECORDING_PROTOCOL.md and docs/RECORDING_SHEET.md agree with each other and with the tools (file names, counts, the statistics table, the DRAFT marks)."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import eot_replay as e  # noqa: E402
import recording_intake_check as c  # noqa: E402

PROTOCOL = (ROOT / "docs" / "RECORDING_PROTOCOL.md").read_text(encoding="utf-8")
SHEET = (ROOT / "docs" / "RECORDING_SHEET.md").read_text(encoding="utf-8")


def sheet_files():
    return re.findall(r"\| ☐ \| `([^`]+)\.wav` \|", SHEET)


def test_the_sheet_lists_exactly_the_files_the_intake_check_expects():
    names = sheet_files()
    fixed = {n for n in names if not n.startswith("s")}
    assert fixed == c.expected_names()                                           # 30 scripted + 20 pause lines + 3 room tones
    open_answers = [n for n in names if n.startswith("s")]
    assert len(open_answers) == 30 and len({n.split("_")[0] + n.split("_")[-1] for n in open_answers}) == 30
    assert len(names) - 3 == 80 and len(names) == len(set(names))


def test_every_line_of_the_protocol_is_on_the_sheet_and_the_other_way_round():
    scripted_and_pause = re.findall(r"^\| (?:\d+|p\d\d) \| (?:hi|hg|en) \| (.+?)(?: \| [NSP]*)? \|$", PROTOCOL, re.M)
    questions = re.findall(r"^\| s\d\d \| (.+?) \|$", PROTOCOL, re.M)
    assert (len(scripted_and_pause), len(questions)) == (35, 10)                  # 15 scripted + 20 pause lines, 10 open questions
    texts = [t.strip() for t in scripted_and_pause + questions]
    for t in texts:
        assert t in SHEET, t[:30]


def test_the_statistics_table_matches_the_exact_bound_and_the_150_rule():
    row = re.search(r"\| bound for zero cut-offs \|(.+)\|", PROTOCOL).group(1)
    shown = [x.strip() for x in row.split("|")]
    for n, text in zip((6, 12, 30, 80, 150, 300), shown):
        assert abs(float(text.rstrip("%")) / 100 - e.upper_bound(0, n)) < 0.005, (n, text)         # the table is rounded: within half a percentage point
    assert "about 150 clean utterances" in PROTOCOL and "exactly 149" in PROTOCOL and e.judge(0, 149)["verdict"] == "PASS" and e.judge(0, 148)["verdict"] == "RANGE ONLY"


def test_the_consent_texts_and_the_sheet_are_marked_as_drafts_and_the_true_end_rule_is_stated():
    assert "DRAFTS FOR REVIEW" in PROTOCOL and "DRAFT — समीक्षा के लिए मसौदा" in PROTOCOL and "English (DRAFT)" in PROTOCOL and "DRAFT" in SHEET
    assert "right after your last word" in PROTOCOL and "shorter than 1.3 s" in PROTOCOL and "**2 s after an open answer**" in PROTOCOL
    assert "0.6-1 s" in PROTOCOL and "recording_intake_check.py" in PROTOCOL


def test_the_recording_is_split_into_two_sessions_with_a_break():
    assert "two sessions of about 15 minutes with a break" in PROTOCOL and "at least 10 minutes between them" in PROTOCOL
    for spk in ("S1", "S2", "S3"):
        block = SHEET.split(f"## Speaker {spk}")[1].split("\n---\n")[0]
        s1 = re.findall(r"\| ☐ \| `[^`]+` \|.*\| 1 \|$", block, re.M)
        s2 = re.findall(r"\| ☐ \| `s\d\d_[^`]+` \|.*\| 2 \|$", block, re.M)
        assert len(s2) == 10 and len(s1) >= 17                                      # room tone + scripted + pause lines in session 1, the open answers in session 2
