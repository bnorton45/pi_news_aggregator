"""Wire-service attribution detection (PLAN §6.5 syndication collapse) — pure.

Local stations and mainstream outlets overwhelmingly carry wire copy (AP, Reuters,
AFP) and network content services (CNN Newsource, Gray, Scripps, Nexstar). Counting
five stations running one AP story as five independent origins would hollow out the
corroboration gate, so items that credit a wire service collapse into one origin per
service (see ProvNode.origin_key).

Error asymmetry drives the matching posture: a MISS overcounts origins → premature
corroboration (unsafe); a FALSE POSITIVE merely folds an original report into a wire
origin → undercounts (trust-conservative). So full org names match anywhere in the
text, and the terse parenthesized tags — too collision-prone globally — match only in
the dateline window at the head of the item. Bare "AP" is never matched (initials).

All input is untrusted feed text: patterns are literal/anchored (no backtracking),
text is treated as opaque beyond these matches.
"""

from __future__ import annotations

import re

# "WICHITA, Kan. (AP) — ..." — the tag form appears at the head of wire copy.
_DATELINE_WINDOW = 160

# Terse parenthesized tags, dateline window only. Case-sensitive: (AP) is a wire
# mark, (ap)/(Ap) are not.
_DATELINE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ap", re.compile(r"\(AP\)")),
    ("reuters", re.compile(r"\(Reuters\)")),
    ("afp", re.compile(r"\(AFP\)")),
    ("cnn", re.compile(r"\(CNN\)")),
    ("gray", re.compile(r"\(Gray News\)")),
    ("scripps", re.compile(r"\(Scripps News\)")),
    ("nexstar", re.compile(r"\(NEXSTAR\)")),
)

# Full org names / credit lines, anywhere in the text (recall-first, see above).
_CREDIT: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ap", re.compile(r"\bAssociated Press\b")),
    ("reuters", re.compile(r"\bReuters\b")),
    ("afp", re.compile(r"\bAgence France-Presse\b|\bAFP\b")),
    ("cnn", re.compile(r"\bCNN Newsource\b")),
    ("gray", re.compile(r"\bGray News\b")),
    ("scripps", re.compile(r"\bScripps News\b")),
    ("nexstar", re.compile(r"\bNexstar\b")),
)


def wire_ref(text: str) -> str:
    """Canonical wire-service slug ('ap', 'reuters', …) credited by `text`, or ''.

    Deterministic priority: dateline tags (strongest signal) before credit names,
    each in declaration order — an AP dateline wins over a body mention of Reuters.
    """
    if not text:
        return ""
    head = text[:_DATELINE_WINDOW]
    for slug, pat in _DATELINE:
        if pat.search(head):
            return slug
    for slug, pat in _CREDIT:
        if pat.search(text):
            return slug
    return ""
