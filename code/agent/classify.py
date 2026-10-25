"""Decide whether a request authorizes deletion.

Deterministic and sentence-based, because both kinds of request use the same words:
"please remove me from marketing lists only" is not a deletion, while "I don't want
to keep my account anymore, please delete it" is.

1. An explicit refusal of deletion ("do not close", "not asking for deletion",
   "no quiero eliminar", "nichts löschen", "ne supprimez pas") means NOT a deletion.
2. A sentence that asks for deletion without negating it and without a narrower
   purpose (marketing only, a copy, a correction) means a deletion.
3. Otherwise any other stated purpose (access, export, correction, opt-out, login
   help, keeping the account) means NOT a deletion.
4. Anything left is treated as a deletion: nearly every request in this domain is one.
"""

import re

DELETE_VERB = (r"(delet|eras|remov|clos|cancel|wipe|purg|supprim|suppression|elimin|borr|exclu|apag|lösch|"
               r"verwijder|cancell|削除|삭제|删除|刪除|удал)")

REFUSAL = [
    r"\b(do not|don't|dont|please don't|never)\s+(\w+\s+){0,3}" + DELETE_VERB,
    r"\bnot (asking|a request|requesting) (for|to)\s+(\w+\s+){0,2}" + DELETE_VERB,
    r"\bwithout (removing|deleting|closing|erasing)",
    r"\bno (quiero|deseo)\s+(\w+\s+){0,2}" + DELETE_VERB, r"\bno (eliminen|borren|cierren|supriman)",
    r"\bnicht(s)?\s+(\w+\s+){0,2}" + DELETE_VERB, r"\bkein(e|en)? löschung",
    r"\bne (\w+\s+){0,2}(supprimez|supprimer|fermez|effacez) pas", r"\bne pas (supprimer|fermer|effacer)",
    r"\bnão (\w+\s+){0,2}(apaguem|excluam|apagar|excluir)",
]

OTHER_PURPOSE = [
    r"\bmarketing (lists?|emails?) only", r"\bstop the promotional", r"\bunsubscribe", r"\bnewsletter",
    r"\b(send me|need|get|receive) a copy of", r"\bdownload my\b", r"\bportable format",
    r"\bwhat personal information", r"\binformation request", r"\bcorrect it\b", r"\bmisspelled",
    r"\bout of date", r"\breplace it with", r"\bchange the email address", r"\bresetting the login",
    r"\bcan't access my profile", r"\bupdate my (address|phone|email|name)",
    # other languages
    r"\bactualic", r"\bcorrij", r"\bcopia de mis datos", r"\bkopie meiner", r"\baktualisier",
    r"\bberichtig", r"\bcorriger\b", r"\borthographe", r"\bcopie de mes",
]

KEEP_ACCOUNT = [
    r"(?<!want to )(?<!wish to )(?<!need to )\bkeep (using )?(the|my) account(?! anymore)",
    r"\bkeep the account as it is", r"\bleave (the|my) (account|profile)", r"\bcontinue using",
    r"\bstill (want|need|use)\b", r"\bkeep using\b",
]

NEGATED_BEFORE = re.compile(r"\b(not|no|don't|dont|never|nicht|nichts|ne|não|without)\b")


def _any(patterns, text):
    return any(re.search(p, text) for p in patterns)


def _asks_for_deletion(sentence):
    m = re.search(DELETE_VERB, sentence)
    if not m:
        return False
    if NEGATED_BEFORE.search(sentence[:m.start()]):
        return False
    return not _any(OTHER_PURPOSE, sentence)


def is_deletion_request(text):
    t = (text or "").lower().replace("’", "'")
    if _any(REFUSAL, t):
        return False
    sentences = [s for s in re.split(r"[.;!?。！？\n]+", t) if s.strip()]
    if any(_asks_for_deletion(s) for s in sentences):
        return True
    if _any(OTHER_PURPOSE, t) or _any(KEEP_ACCOUNT, t):
        return False
    return True
