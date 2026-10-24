"""Decide whether a request authorizes deletion.

Deterministic first: explicit non-deletion intents (access, export, correction,
marketing opt-out, login help) override generic deletion words, because those
requests often say "do not delete" or "keep my account".
"""

import re

NON_DELETION = [
    r"\bdo not (close|delete|remove)", r"\bwithout (removing|deleting|closing)", r"\bkeep (the|my) account",
    r"\bstill (want|need|use)", r"\bmarketing (lists|emails?) only", r"\bstop the promotional",
    r"\b(send me|need) a copy of", r"\bdownload my\b", r"\bportable format", r"\bwhat personal information",
    r"\binformation request", r"\bcorrect it\b", r"\bmisspelled", r"\bout of date", r"\breplace it with",
    r"\bchange the email address", r"\bresetting the login", r"\bcan't access my profile",
    r"\bleave my profile",
]
DELETION = [
    r"delet", r"eras", r"remove", r"close", r"supprim", r"suppression", r"elimin", r"exclus", r"lösch",
    r"löschen", r"verwijder", r"cancell", r"削除", r"삭제", r"删除", r"удал", r"cancel",
]


def is_deletion_request(text):
    t = (text or "").lower()
    if any(re.search(p, t) for p in NON_DELETION):
        return False
    # Nearly every request in this domain is a deletion request; anything without an
    # explicit non-deletion intent is treated as one.
    return True
