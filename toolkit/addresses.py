"""Czech address normalization + similarity for the Tier-2 dedup sweep.

Pure functions, no I/O, hermetically testable. Properties carry coarse location
(locality + district, occasionally a PSČ in raw), so this normalizes those into
a comparable token form and scores two of them. It is one corroborating signal
in the sweep's confidence ladder (alongside geo / price / area / pHash / vision)
— never used alone to merge.
"""

from __future__ import annotations

import re
import unicodedata

# Common Czech address abbreviations -> canonical word. Applied after lowering
# + diacritic stripping, so keys are ascii.
_ABBREV: dict[str, str] = {
    "nam": "namesti",
    "nam.": "namesti",
    "ul": "ulice",
    "ul.": "ulice",
    "tr": "trida",
    "tr.": "trida",
    "nabr": "nabrezi",
    "sidl": "sidliste",
    "sv": "svateho",
}

_PSC_RE = re.compile(r"\b(\d{3})\s?(\d{2})\b")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_address(*parts: str | None) -> str:
    """Lowercase, strip diacritics, expand abbreviations; join the parts.

    Accepts the coarse fields a property has (locality, district, …); None /
    empty parts are skipped. Returns a normalized whitespace-joined string.
    """
    raw = " ".join(p for p in parts if p)
    ascii_text = _strip_diacritics(raw).lower()
    tokens = [_ABBREV.get(t, t) for t in _TOKEN_RE.findall(ascii_text)]
    return " ".join(tokens)


def extract_psc(text: str | None) -> str | None:
    """Pull a Czech postal code (PSČ, 'NNN NN') out of free text, normalized."""
    if not text:
        return None
    m = _PSC_RE.search(text)
    return f"{m.group(1)}{m.group(2)}" if m else None


def address_similarity(a: str | None, b: str | None) -> float:
    """Token-set Jaccard of two normalized addresses, in [0, 1].

    Hand-rolled (no fuzzy-match dependency). 0 when either side is empty. A PSČ
    or rare street token shared between two coarse locality strings pushes the
    score up; two identical locality strings score 1.0.
    """
    na, nb = normalize_address(a), normalize_address(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0
