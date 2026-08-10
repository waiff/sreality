"""S1 — normalization (03 §3.3).

Normalization produces a DERIVED SIDECAR, one row per claim, and is never written back
onto the claim. It is versioned with the resolver (`normalizer_version` is part of
`resolver_version`), so changing anything here is a `RESOLVER_VERSION` bump.

Every transform below is one row of 03 §3.3.1/§3.3.2 and carries its evidence there.
Rejections (PSČ sentinels, town-as-street, malformed numbers) are KEPT as normalization
outcomes rather than dropped — they are QA signal (03 §3.3.3).

`normalize_match_key` is deliberately the same algorithm as the gazetteer loader's
`location_data.name_index.normalize_name` (lower + deaccent + punctuation folded to single
spaces): the resolver matches against `ruian_name_index.name_norm`, so the two keys must
agree exactly. `tests/location_data/test_resolver_normalize.py` asserts that against the
loader's own function whenever that module is present, rather than importing it here — the
resolver's key is resolver-versioned and the loader's is registry-versioned, and a hard
import would make a resolver replay depend on a loader deploy.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence

from location_data.resolver.types import Claim, NormalizedClaim

_PUNCT = re.compile(r"[^0-9a-z]+")
_DASHES = re.compile(r"[‐-―−]")
_WS = re.compile(r"\s+")
# 'MasarykovaNabízíme' — the shared extractor carries the same guard (repo-iss #14).
_GLUE = re.compile(r"(?<=[a-záčďéěíňóřšťúůýž])(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ])")
_STREET_TYPE = re.compile(
    r"^\s*(ulice|ul\.|ul|náměstí|namesti|nám\.|nam\.|třída|trida|tř\.|tr\.|"
    r"nábřeží|nabrezi|nábř\.|sídliště|sidliste)\s+",
    re.IGNORECASE,
)
# '28. října', '17. listopadu', '1. máje' — a LEADING ordinal is part of the street name,
# never a house number (03 §3.3.1, named regression test).
_NUMERIC_LEADING = re.compile(r"^\s*\d{1,3}\.\s*\S")
# čp / čo forms: '128', '128/40', '128/40a', 'ev. č. 12', 'č.ev. 12'
_EVIDENCNI = re.compile(r"(?:ev\.?\s*č\.?|č\.?\s*ev\.?|evidenční\s*číslo)\s*(\d{1,6})", re.IGNORECASE)
_HN = re.compile(r"(\d{1,6})(?:\s*/\s*(\d{1,5})\s*([a-zA-Z])?)?")

# bazos ships two PORTAL BUCKETS in the PSČ field; they are country signals, not postcodes
# (03 §3.3.2, mining-bazos).
PSC_COUNTRY_BUCKETS: dict[str, str] = {"98765": "SK", "98766": "XX"}
PSC_SENTINELS: frozenset[str] = frozenset({"-1", "0", "00000"})


def deaccent(value: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch))


def normalize_match_key(value: str) -> str:
    """lower(unaccent(x)) with punctuation folded to single spaces — the join key against
    `ruian_name_index.name_norm` / `ruian_streets.name_norm`."""
    return _PUNCT.sub(" ", deaccent(value).lower()).strip()


def case_fold(value: str) -> str:
    """`value_cf`: case-folded, diacritics PRESERVED (display + Czech-aware compare)."""
    return _WS.sub(" ", _DASHES.sub("-", value)).strip().casefold()


def split_glue(value: str) -> str:
    return _GLUE.sub(" ", value)


def split_street_type(value: str) -> tuple[str, str | None]:
    """Strip a generic leading street word into its own token. The rest is NOT stripped —
    a Czech street name carries its own type word ('náměstí Míru' is not 'Míru')."""
    match = _STREET_TYPE.match(value)
    if not match:
        return value.strip(), None
    return value[match.end() :].strip(), match.group(1).lower().rstrip(".")


def normalize_psc(raw: str) -> tuple[str | None, str | None, str | None]:
    """-> (psc, country_hint, rejection). Five digits or nothing."""
    digits = re.sub(r"\s+", "", raw or "")
    if digits in PSC_SENTINELS:
        return None, None, "psc_sentinel"
    if digits in PSC_COUNTRY_BUCKETS:
        return None, PSC_COUNTRY_BUCKETS[digits], "psc_portal_bucket"
    if not re.fullmatch(r"\d{5}", digits):
        return None, None, "psc_malformed"
    return digits, None, None


def normalize_house_number(raw: str) -> dict[str, object]:
    """Three typed slots, never collapsed: čp, (čo + znak), evidenční (03 §3.3.2)."""
    slots: dict[str, object] = {}
    text = (raw or "").strip()
    if not text:
        return slots
    ev = _EVIDENCNI.search(text)
    if ev:
        slots["evidencni"] = ev.group(1)
        text = text[: ev.start()] + text[ev.end() :]
    hn = _HN.search(text)
    if hn:
        slots["cislo_domovni"] = hn.group(1)
        if hn.group(2):
            slots["cislo_orientacni"] = hn.group(2)
        if hn.group(3):
            slots["znak_orientacniho"] = hn.group(3).lower()
    return slots


def split_street_and_number(raw: str) -> tuple[str, dict[str, object]]:
    """Split a street claim that carries its own number. A LEADING ordinal stays with the
    name — bazos's regex loses '28. října' exactly here."""
    text = split_glue((raw or "").strip())
    text = _WS.sub(" ", _DASHES.sub("-", text))
    if _NUMERIC_LEADING.match(text):
        head, sep, tail = text.partition(" ")
        rest = tail
        prefix = head + sep
    else:
        prefix, rest = "", text
    match = re.search(r"\s+(\d{1,6}(?:\s*/\s*\d{1,5}[a-zA-Z]?)?)\s*$", rest)
    if not match:
        return (prefix + rest).strip(), {}
    return (prefix + rest[: match.start()]).strip(), normalize_house_number(match.group(1))


def normalize_claim(
    claim: Claim,
    *,
    is_place_name: Callable[[str], bool] | None = None,
    street_exists: Callable[[str], bool] | None = None,
) -> NormalizedClaim:
    """One claim -> its S1 sidecar.

    `is_place_name` / `street_exists` implement the TOWN-AS-STREET rejection at S1 rather
    than at S3 (03 §3.3.1): a candidate street that resolves to an obec/část-obce name and
    does not exist as a street in the constraining obec "poisons both the display and the
    match key worse than a NULL would".
    """
    verbatim = claim.value_text
    rejections: list[str] = []
    slots: dict[str, object] = {}

    if verbatim is None:
        return NormalizedClaim(claim.id, claim.claim_type, None, None, None, slots, ())

    cf = case_fold(verbatim)
    ascii_key = normalize_match_key(verbatim)

    if claim.claim_type == "street_name":
        name, number_slots = split_street_and_number(verbatim)
        name, street_type = split_street_type(name)
        slots.update(number_slots)
        if street_type:
            slots["street_type"] = street_type
        slots["street"] = name
        ascii_key = normalize_match_key(name)
        cf = case_fold(name)
        if not ascii_key:
            rejections.append("street_empty_after_normalization")
        elif is_place_name is not None and is_place_name(ascii_key):
            if street_exists is None or not street_exists(ascii_key):
                rejections.append("town_as_street")
    elif claim.claim_type == "psc":
        psc, country_hint, rejection = normalize_psc(verbatim)
        if psc:
            slots["psc"] = psc
        if country_hint:
            slots["country_hint"] = country_hint
        if rejection:
            rejections.append(rejection)
        ascii_key = psc or ""
    elif claim.claim_type in ("house_number_cp", "house_number_co", "evidencni", "house_unit"):
        slots.update(normalize_house_number(verbatim))
        if not slots:
            rejections.append("house_number_unparsed")
    elif claim.claim_type in (
        "obec_name",
        "cast_obce_name",
        "quarter_name",
        "mestsky_obvod_name",
        "okres_name",
        "orp_name",
        "kraj_name",
        "cadastral_territory_name",
        "postal_town",
        "homonym_qualifier",
        "landmark",
        "development_name",
        "country",
    ):
        cleaned = split_glue(verbatim.strip())
        ascii_key = normalize_match_key(cleaned)
        cf = case_fold(cleaned)
        if not ascii_key:
            rejections.append("name_empty_after_normalization")

    return NormalizedClaim(
        claim_id=claim.id,
        claim_type=claim.claim_type,
        value_verbatim=verbatim,
        value_cf=cf or None,
        value_ascii=ascii_key or None,
        typed_slots=slots,
        rejections=tuple(rejections),
    )


def normalize_all(
    claims: Sequence[Claim],
    *,
    is_place_name: Callable[[str], bool] | None = None,
    street_exists: Callable[[str], bool] | None = None,
) -> dict[int, NormalizedClaim]:
    return {
        c.id: normalize_claim(c, is_place_name=is_place_name, street_exists=street_exists)
        for c in claims
    }
