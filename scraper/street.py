"""Shared street-name extraction + cleaning for the HTML/crawler portals.

sreality reads a structured street field from its detail JSON; the other portals
must mine it from a free-text locality string — and they disagree on where the
street sits:

  - idnes / remax:  "Street, City - Quarter[, okres X]"   (street is FIRST)
  - maxima:         "City[, Quarter], Street"             (street is LAST)
  - bazos:          a regex capture off the title/description, prefix-decorated
                    ("ul. Koterovská") and prone to bleeding a description word
                    ("ul. Teplého Nabízíme").

The dominant failure mode is FABRICATION, not absence: a town name written as a
street ("Brno", a foreign "Estepona, Španělsko", a "Town - Quarter" tail, a
village last-segment) poisons the dedup street-key and Browse worse than a NULL
does. So every extractor routes through the ONE don't-fabricate guard here
(`reject_as_town`) rather than re-implementing it five times. `clean_street` is
the matching ONE cleaner. The stored value stays human-readable (Browse displays
it); `toolkit.dedup_engine._street_name_key` owns the separate match-time
grouping key.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal, Sequence

# Czech-bbox guard: a street candidate whose listing coordinate lands abroad is
# never a Czech street (idnes carries ~37% foreign listings). Mirrors the bbox
# the portal coord parsers already use.
_CZ_LAT_MIN, _CZ_LAT_MAX = 48.0, 51.5
_CZ_LON_MIN, _CZ_LON_MAX = 12.0, 19.0

_CZ_LOWER = "a-záčďéěíňóřšťúůýž"
_CZ_UPPER = "A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ"

# A description word glued onto the street with no space ("MasarykovaNabízíme");
# Czech street names are space-separated, so a lower->Upper boundary is always a
# token break we can safely re-insert.
_GLUE_RE = re.compile(rf"([{_CZ_LOWER}])([{_CZ_UPPER}])")

# "ul."/"ulice"/"ulici" is pure decoration before the real name -> strip so
# "ul. Koterovská" stores as "Koterovská" (uniform with sreality's bare names).
# "náměstí"/"třída"/"nábřeží" are frequently INTEGRAL to the proper name
# ("náměstí Míru", "Vinohradská třída") -> kept; the dedup key folds them anyway.
_LEAD_GENERIC_RE = re.compile(r"^(?:ulice|ulici|ul\.?)\s+", re.IGNORECASE)

# A trailing house-number token (12, 12a, 123/45) — stored street is the bare
# name; bazos never resolves a reliable house_number off its free-text capture.
_TRAIL_HOUSE_NO_RE = re.compile(r"\s+\d{1,4}[a-z]?(?:/\d{1,4}[a-z]?)?$", re.IGNORECASE)

# Real-estate boilerplate that bleeds into bazos title/description captures.
# Diacritics-folded, lowercase; trailing tokens matching these are trimmed.
_DESC_STOPWORDS: frozenset[str] = frozenset({
    "nabizime", "nabizim", "nabizi", "nabidka", "prodej", "prodava",
    "prodavame", "pronajem", "pronajmu", "pronajima", "vam", "vas", "penb",
    "energeticka", "spolecnost", "investice", "podnikani", "kombinace",
    "zlevneno", "zleva", "sleva", "klimatizace", "certifikaty", "lokalita",
    "maklerka", "makler", "novostavba", "exkluzivne", "exkluzivni", "cena",
})

# Country names (folded) that mark a non-Czech locality. Backstop for when the
# coordinate is absent; the bbox check is the primary foreign guard.
_FOREIGN_COUNTRIES: frozenset[str] = frozenset({
    "spanelsko", "slovensko", "italie", "chorvatsko", "nemecko", "rakousko",
    "madarsko", "polsko", "francie", "portugalsko", "recko", "turecko",
    "bulharsko", "svycarsko", "kypr", "egypt", "thajsko", "slovinsko",
    "cerna hora", "spojene arabske emiraty", "spojene staty", "usa",
})

# Prepositional street prefixes ("Na Výsluní", "U Hotelu", "Pod Lipami").
_STREET_PREPOSITIONS: frozenset[str] = frozenset({
    "na", "nad", "pod", "za", "u", "v", "ve", "k", "ke", "do", "po", "pri",
    "mezi", "kolem", "okolo", "nam", "namesti",
})

# Explicit street-type keywords anywhere in the name.
_STREET_KEYWORDS: frozenset[str] = frozenset({
    "ulice", "ul", "namesti", "nam", "trida", "tr", "nabrezi", "nabr",
    "sidliste", "alej", "sady", "park", "vrch",
})

# Adjective/possessive endings that mark a Czech street name (folded). Village
# endings (-ice/-ce/-ka/-in) are deliberately excluded.
_STREET_SUFFIXES: tuple[str, ...] = (
    "ova", "ska", "cka", "eho", "ich", "ni", "nou", "skou", "ckou",
)

_OKRES_RE = re.compile(r"(?i)^okr(?:es|\.)\b")


def _fold(text: str | None) -> str:
    """Diacritics-stripped, lowercase, whitespace-collapsed comparison form."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(ascii_text.split())


def in_cz_bbox(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    return _CZ_LAT_MIN <= lat <= _CZ_LAT_MAX and _CZ_LON_MIN <= lon <= _CZ_LON_MAX


def clean_street(raw: str | None) -> str | None:
    """Trim a raw street candidate to a bare, stored-ready name, or None.

    Strips the "ul."/"ulice" decoration prefix, splits a glued description word,
    drops trailing boilerplate + a trailing house number. Idempotent and safe on
    an already-clean structured value ("Jesenická" -> "Jesenická")."""
    if not raw:
        return None
    s = _GLUE_RE.sub(r"\1 \2", raw)
    s = re.sub(r"\s+", " ", s).strip(" ,.;:\"'()[]")
    if not s:
        return None
    s = _LEAD_GENERIC_RE.sub("", s).strip()
    s = _TRAIL_HOUSE_NO_RE.sub("", s).strip()
    tokens = s.split()
    while len(tokens) > 1 and _fold(tokens[-1]) in _DESC_STOPWORDS:
        tokens.pop()
    s = " ".join(tokens).strip(" ,.;:")
    return s or None


def looks_like_czech_street(name: str | None) -> bool:
    """A conservative positive test: prepositional prefix, an explicit street
    keyword, or a street-adjective ending. Used to gate ambiguous last-segment
    extraction (maxima) where a village name would otherwise be fabricated."""
    folded = _fold(name)
    if not folded:
        return False
    tokens = folded.split()
    if tokens[0] in _STREET_PREPOSITIONS:
        return True
    if any(t.rstrip(".") in _STREET_KEYWORDS for t in tokens):
        return True
    return tokens[-1].endswith(_STREET_SUFFIXES)


def reject_as_town(
    candidate: str | None,
    *,
    geo_names: Sequence[str | None] = (),
    lat: float | None = None,
    lon: float | None = None,
) -> bool:
    """True when `candidate` must NOT be stored as a street.

    Fires on: a coordinate abroad, a "Town - Quarter" form, a known foreign
    country, an "okres ..." admin qualifier, a digits-only token, or an exact
    match to one of `geo_names` (the listing's own town/district/region — a town
    masquerading as a street). The geo cross-check is the strongest signal: it
    catches town-as-street cases the structural rules miss."""
    folded = _fold(candidate)
    if not folded:
        return True
    if (lat is not None or lon is not None) and not in_cz_bbox(lat, lon):
        return True
    if " - " in (candidate or ""):
        return True
    if folded in _FOREIGN_COUNTRIES:
        return True
    if _OKRES_RE.match(candidate or ""):
        return True
    if not re.search(rf"[{_CZ_LOWER}{_CZ_UPPER}]", candidate or ""):
        return True
    return any(_fold(g) == folded for g in geo_names if g)


def street_from_locality(
    locality: str | None,
    *,
    position: Literal["first", "last"],
    geo_names: Sequence[str | None] = (),
    require_morphology: bool = False,
    lat: float | None = None,
    lon: float | None = None,
) -> str | None:
    """Extract a street from a comma-separated locality/address string.

    `position` picks the street segment ('first' for idnes/remax, 'last' for
    maxima). A single segment is treated as town-only (no street). Every
    candidate passes `clean_street` + `reject_as_town`; cross-checked against the
    other segments and the supplied `geo_names`. `require_morphology` additionally
    demands the candidate look like a Czech street (for the ambiguous last
    segment). Returns None whenever the result would risk a fabricated street."""
    if not locality:
        return None
    parts = [p.strip() for p in locality.split(",") if p.strip()]
    # Drop a leading area token ("114 m², Praha 6, …" on some index strings).
    if parts and re.match(r"^\d[\d\s.,]*m", _fold(parts[0])):
        parts = parts[1:]
    if len(parts) < 2:
        return None
    if _fold(parts[-1]) in _FOREIGN_COUNTRIES:
        return None

    idx = 0 if position == "first" else len(parts) - 1
    candidate = parts[idx]
    # For a first-segment street, an immediately-following "okres X" means the
    # first segment IS the town ("Studénka, okres Nový Jičín"), not a street.
    if position == "first" and len(parts) >= 2 and _OKRES_RE.match(parts[1]):
        return None

    cleaned = clean_street(candidate)
    if cleaned is None:
        return None
    context = tuple(geo_names) + tuple(p for i, p in enumerate(parts) if i != idx)
    if reject_as_town(cleaned, geo_names=context, lat=lat, lon=lon):
        return None
    # A 3+-segment locality is "City, Quarter, Street" — the last segment is
    # reliably a street, so the morphology gate is only needed for the ambiguous
    # 2-segment case ("City, Street" vs "Village, Village/Quarter").
    if require_morphology and len(parts) < 3 and not looks_like_czech_street(cleaned):
        return None
    return cleaned
