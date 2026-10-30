"""Unit tests for the counselling glossary correction function.

Verifies:
1. Target observed mis-spellings from phone audio are correctly mapped.
2. 'bi si' is turned into a contextual hint, not a blind rewrite.
3. 'eprod' is handled as proposed unconfirmed.
4. Protected conversational words (Hindi and English) are strictly NOT rewritten.
"""
from app.ai.glossary_correction import correct_glossary


def test_observed_stt_misspellings_rewrites():
    # 1. beetaik -> B.Tech
    assert correct_glossary("mujhe beetaik karna hai") == "mujhe B.Tech karna hai"
    assert correct_glossary("beetaik fees kitni hai") == "B.Tech fees kitni hai"

    # 2. je ee men -> JEE Main
    assert correct_glossary("je ee men ka cutoff kya hai") == "JEE Main ka cutoff kya hai"

    # 3. parsentaail -> percentile
    assert correct_glossary("mera 90 parsentaail aaya hai") == "mera 90 percentile aaya hai"

    # 4. nyoo jeelaind -> New Zealand
    assert correct_glossary("nyoo jeelaind mein admission milega") == "New Zealand mein admission milega"


def test_bi_si_hint_not_blind_rewrite():
    # bi si should not be blindly replaced with BCA; it carries a hint
    res = correct_glossary("mujhe bi si ke baare mein janna hai")
    assert "bi si (possible: BCA or BC category)" in res
    assert res == "mujhe bi si (possible: BCA or BC category) ke baare mein janna hai"


def test_eprod_proposed_unconfirmed():
    # eprod marked as proposed unconfirmed
    res = correct_glossary("mujhe eprod study ke liye jana hai")
    assert "eprod (proposed unconfirmed: abroad)" in res


def test_unobserved_terms_not_blindly_altered():
    # Terms not observed in call are not altered
    assert correct_glossary("skolarship aur hostal") == "skolarship aur hostal"
    assert correct_glossary("edmission aur elidjibility") == "edmission aur elidjibility"


def test_protected_cases_must_not_be_rewritten():
    """Negative tests: ordinary Hindi words or English words must NOT be modified."""
    # 'men' in Hindi ('in') must NOT be converted to 'JEE Main'
    hindi_in = "Delhi men ache colleges hain"
    assert correct_glossary(hindi_in) == "Delhi men ache colleges hain"

    # 'bi' or 'si' individually must NOT trigger the hint
    hindi_particles = "kisi bhi student ko admission mil sakta hai"
    assert correct_glossary(hindi_particles) == "kisi bhi student ko admission mil sakta hai"

    # 'fee' / 'fees' / 'free' must stay as is
    assert correct_glossary("application fee free hai ya paid") == "application fee free hai ya paid"

    # 'host' must not become 'hostel'
    assert correct_glossary("event host kaun hai") == "event host kaun hai"

    # 'new' alone must not become 'New Zealand'
    assert correct_glossary("new session kab shuru hoga") == "new session kab shuru hoga"

    # Conversational Hindi words must remain untouched
    hindi_convo = "Aap kaise hain? Mujhe kuch puchna tha, baare mein batao."
    assert correct_glossary(hindi_convo) == "Aap kaise hain? Mujhe kuch puchna tha, baare mein batao."


def test_empty_and_punctuation():
    assert correct_glossary("") == ""
    assert correct_glossary(None) == ""
    assert correct_glossary("beetaik, parsentaail?") == "B.Tech, percentile?"
