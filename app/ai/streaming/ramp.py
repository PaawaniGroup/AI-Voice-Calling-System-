"""Brain side of the clause-size ramp (tenant flag `stream_ramp`, default OFF).

The block is APPENDED to the system message of a call whose tenant has the flag on; the base system prompt (app/ai/pipeline.py) is not changed.
It asks the Brain to open with a short complete phrase, so the first clause is short and ends on a natural boundary (the segmenter then needs no hard cut)."""
from __future__ import annotations

RAMP_BRAIN_RULE = (
    "\n\nOPENING RULE: start every reply with a short complete phrase of 2 to 4 words that ends in punctuation (a comma, a full stop or a danda), "
    "then continue the answer in the same language and script. The opening phrase must be neutral: it must not agree, deny, confirm or promise anything, "
    "so never open with हाँ, हां, नहीं, बिल्कुल, ज़रूर, जरूर, yes, no, sure, of course or any similar word, and it must not contain a fact; it may only point at the "
    "topic of the question. "
    "If nothing was asked about a topic (a greeting, unclear speech or a request to repeat), open instead with a short polite phrase that invites the caller "
    "to continue or to say it again; that phrase must never promise a callback or any other action. "
    "Never state fees, cut-offs or dates unless they come from the provided knowledge."
)

# Openers proposed for the operator's approval (NOT in the rule yet: they are examples a tenant may add after approval). Gender-free, no agreement,
# denial or promise, 2-4 words ending in punctuation.
PROPOSED_OPENERS = (
    "देखिए, इस बारे में,",
    "आपके प्रश्न पर,",
    "इस विषय पर,",
    "Regarding your question,",
    "On that point,",
)
# For turns with no topic (a greeting, unclear speech, a request to repeat). They invite the caller to go on; they never agree, deny or promise anything.
NON_TOPICAL_OPENERS = (
    "आप कहिए,",
    "Please go on,",
)
FORBIDDEN_OPENER_WORDS = ("हाँ", "हां", "नहीं", "बिल्कुल", "ज़रूर", "जरूर", "yes", "no", "sure", "of course")


def apply_to_conversation(conv) -> bool:
    """Append the rule to the conversation's system message (once). Returns True when it was added."""
    msgs = getattr(conv, "messages", None)
    if not msgs or msgs[0].get("role") != "system":
        return False
    if RAMP_BRAIN_RULE in msgs[0].get("content", ""):
        return False
    msgs[0]["content"] = msgs[0]["content"] + RAMP_BRAIN_RULE
    return True
