"""Behaviour of the ported Hindi/Hinglish TTS text front-end. The Hindi number-word table is UNVERIFIED by a native speaker."""
from app.ai.tts_frontend import detect_lang, int_words, normalize


def test_indian_number_words():
    assert int_words(0) == "शून्य" and int_words(7) == "सात" and int_words(99) == "निन्यानवे"
    assert int_words(125000) == "एक लाख पच्चीस हज़ार"
    assert int_words(250) == "दो सौ पचास"


def test_money_phone_percent_and_acronyms_are_written_as_words():
    assert normalize("फीस ₹1,25,000 है") == "फीस एक लाख पच्चीस हज़ार रुपये है"
    assert normalize("Aap call kijiye +91 98765 43210") == "Aap call kijiye प्लस नौ एक, नौ आठ सात छह पाँच, चार तीन दो एक शून्य"
    assert "प्रतिशत" in normalize("Aapko 75% marks chahiye")
    assert normalize("Aap IIT mein jayenge") == "Aap आई आई टी mein jayenge"
    assert normalize("12वीं के बाद") == "बारहवीं के बाद"


def test_dates_and_times():
    assert normalize("15 November 2026 ko aana") == "पंद्रह नवंबर दो हज़ार छब्बीस ko aana"
    assert normalize("Kal 3:30 PM par") == "Kal दोपहर तीन बजकर तीस मिनट par"


def test_plain_english_is_untouched_and_language_detection():
    s = "Your fees are Rs 5000 and the IIT counsellor will call."
    assert normalize(s) == s
    assert detect_lang("Aapka admission kaise hoga") == "hi"
    assert detect_lang("आपका admission") == "hi"
    assert detect_lang("Hello, how are you") == "en"
    assert normalize("") == ""
