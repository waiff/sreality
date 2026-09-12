"""CHECK — step 4 of 4: which country this is, and whether the row disagrees with itself.

The eight reconciler rules, the contradiction ledger, the disposition table and the
auto-close machinery collapse to ONE comparison and ONE output column. `disputed` is a
single nullable text column whose value IS the reason (NULL = clean), so a reader never has
to join a ledger to find out whether to trust the row.

Three reasons ship:

* `pin_outside_obec` — the published pin is not inside the town the row names. The pin is
  KEPT (throwing it away loses the only position we have) but the granularity drops to the
  admin level, because the address identity is the half that is now in doubt.
* `pin_outside_cz` — the pin is outside the Czech state polygon while the row names a Czech
  town. Almost always a geocoder artifact; the town is the trustworthy half.
* `country_conflict` — text says another country, the registry says Czech. 3 of the 5
  corpus `foreign_suspect` rows were unambiguously Czech artifacts, so trusting either side
  unconditionally is wrong in both directions.

**Foreign is a determination, never a default** (rule 25). A listing with no Czech town and
no foreign signal is `undetermined` at `unknown` granularity — the state that says "we have
nothing", which is the one thing "foreign" must never be allowed to mean. The §3.4.2
false-positive rejections below are mandatory and are enforced twice, structurally (a
`subject_scoped=false` claim is inadmissible at all) and by content: a naive keyword
detector flags every listing on two portals — `Zahraniční nemovitosti` is site nav on 100 %
of mmreality and ceskereality pages, the REMAX footer lists twelve countries, EUR is
standard practice on CZ commercial rent, and the Regus boilerplate advertises a "global
network".
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from location_data.resolver.normalize import normalize_match_key
from location_data.resolver.types import (
    Claim,
    Fill,
    GranularityRank,
    NormalizedClaim,
    Position,
    RegistryView,
    Verdict,
)

# The ONE Czech bounding box (lon_min, lat_min, lon_max, lat_max). It was the single row in
# `location_constants` and is a code constant now that the table is going. It is a TRIGGER,
# never a determination: the Wisła hotel sits 0.008° outside it.
CZ_BBOX: tuple[float, float, float, float] = (12.0, 48.0, 19.0, 51.5)

# The closed Czech-language country gazetteer. Deliberately closed: an open-ended detector
# is what produces the false positives.
COUNTRY_GAZETTEER: dict[str, str] = {
    "cesko": "CZ", "ceska republika": "CZ", "cr": "CZ", "ceske republice": "CZ",
    "slovensko": "SK", "slovenska republika": "SK", "slovensku": "SK",
    "spanelsko": "ES", "spanelsku": "ES", "spanelska": "ES",
    "chorvatsko": "HR", "chorvatsku": "HR",
    "italie": "IT", "italii": "IT", "italsko": "IT",
    "polsko": "PL", "polsku": "PL",
    "nemecko": "DE", "nemecku": "DE",
    "rakousko": "AT", "rakousku": "AT",
    "madarsko": "HU", "madarsku": "HU",
    "bulharsko": "BG", "bulharsku": "BG",
    "recko": "GR", "recku": "GR",
    "francie": "FR", "francii": "FR",
    "portugalsko": "PT", "portugalsku": "PT",
    "turecko": "TR", "turecku": "TR",
    "kypr": "CY", "kypru": "CY",
    "slovinsko": "SI", "slovinsku": "SI",
    "cerna hora": "ME", "cerne hore": "ME",
    "srbsko": "RS", "albanie": "AL", "rumunsko": "RO", "ukrajina": "UA",
    "svycarsko": "CH", "svycarsku": "CH",
    "nizozemsko": "NL", "belgie": "BE", "lucembursko": "LU",
    "velka britanie": "GB", "anglie": "GB", "irsko": "IE",
    "dansko": "DK", "svedsko": "SE", "norsko": "NO", "finsko": "FI",
    "estonsko": "EE", "lotyssko": "LV", "litva": "LT",
    "malta": "MT", "monako": "MC", "andorra": "AD",
    "bosna a hercegovina": "BA", "severni makedonie": "MK",
    "egypt": "EG", "maroko": "MA", "tunisko": "TN", "izrael": "IL",
    "spojene arabske emiraty": "AE", "usa": "US", "spojene staty": "US",
    "kanada": "CA", "mexiko": "MX", "brazilie": "BR",
    "thajsko": "TH", "vietnam": "VN", "filipiny": "PH", "indonesie": "ID",
    "japonsko": "JP", "cina": "CN", "australie": "AU", "novy zeland": "NZ",
    "gruzie": "GE", "rusko": "RU", "dominikanska republika": "DO",
}

_NAV_ZAHRANICNI = re.compile(r"zahranicni nemovitosti", re.IGNORECASE)
_EUR = re.compile(r"(?:\bEUR\b|€)")
_REGUS = re.compile(r"\b(regus|iwg)\b|globalni sit|global network", re.IGNORECASE)
# The REMAX footer is a bare RUN of country names — and it is in ENGLISH on all 12 sampled
# pages, so the footer detector needs its own token set. These are never a determination
# vocabulary: they only recognise the trap.
_FOOTER_TOKENS_EN = frozenset(
    {
        "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia", "denmark",
        "estonia", "finland", "france", "germany", "greece", "hungary", "ireland", "italy",
        "latvia", "lithuania", "luxembourg", "malta", "netherlands", "norway", "poland",
        "portugal", "romania", "serbia", "slovakia", "slovenia", "spain", "sweden",
        "switzerland", "turkey", "ukraine",
    }
)
_FOOTER_MIN_COUNTRIES = 5
_FOREIGN_BUCKET_TOKENS = frozenset({"zahranici", "zahranicni"})
# The bazos `Zahraničí` bucket: foreign, country unknown. It never reaches the answer table
# as a code — it is `status='foreign'` with a NULL `country_code`.
UNKNOWN_FOREIGN = "XX"

_TEXT_COUNTRY_TYPES = ("address_line_verbatim", "postal_town", "obec_name", "landmark")


def in_cz_bbox(lat: float, lon: float) -> bool:
    lon_min, lat_min, lon_max, lat_max = CZ_BBOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def is_rejected_country_evidence(text: str | None) -> str | None:
    """-> the name of the trap this text is, or None. Mandatory on every text-borne country
    signal, on top of the structural `subject_scoped` gate."""
    if not text:
        return None
    key = normalize_match_key(text)
    if _NAV_ZAHRANICNI.search(key):
        return "site_nav_zahranicni_nemovitosti"
    if _EUR.search(text):
        return "eur_denomination"
    if _REGUS.search(key) or _REGUS.search(text):
        return "regus_boilerplate"
    hits: set[str] = {code for token, code in COUNTRY_GAZETTEER.items() if _token_in(key, token)}
    hits.update(token for token in _FOOTER_TOKENS_EN if _token_in(key, token))
    if len(hits) >= _FOOTER_MIN_COUNTRIES:
        return "country_list_footer"
    return None


def _token_in(key: str, token: str) -> bool:
    return re.search(rf"(?:^|\s){re.escape(token)}(?:\s|$)", key) is not None


def country_from_text(text: str | None) -> str | None:
    """A TRAILING country token in a locality/address text (`"Benahavís, Španělsko"`).
    Trailing only — a country named mid-sentence is prose, not an address tail."""
    if not text:
        return None
    parts = [p for p in re.split(r"[,;]", text) if p.strip()]
    if not parts:
        return None
    tail = normalize_match_key(parts[-1])
    if tail in COUNTRY_GAZETTEER:
        return COUNTRY_GAZETTEER[tail]
    if tail in _FOREIGN_BUCKET_TOKENS:
        return UNKNOWN_FOREIGN
    return None


def country_codes(
    claims: Sequence[Claim], normalized: dict[int, NormalizedClaim]
) -> set[str]:
    """Every country code the ADMISSIBLE, trap-filtered claims assert."""
    codes: set[str] = set()
    for claim in claims:
        if not _admissible(claim):
            continue
        if claim.claim_type == "country":
            code = _code_of(claim, normalized)
            if code:
                codes.add(code)
        elif claim.claim_type in _TEXT_COUNTRY_TYPES:
            code = country_from_text(claim.value_text)
            if code:
                codes.add(code)
        if claim.claim_type == "foreign_indicator":
            codes.add(_code_of(claim, normalized) or UNKNOWN_FOREIGN)
        norm = normalized.get(claim.id)
        hint = norm.typed_slots.get("country_hint") if norm else None
        if isinstance(hint, str):
            codes.add(hint)
        elif claim.claim_type in ("obec_name", "postal_town") and normalize_match_key(
            claim.value_text or ""
        ) in _FOREIGN_BUCKET_TOKENS:
            codes.add(UNKNOWN_FOREIGN)
    return codes


def check(
    claims: Sequence[Claim],
    normalized: dict[int, NormalizedClaim],
    filled: Fill,
    position: Position,
    granularity: str,
    *,
    registry: RegistryView,
    rank: GranularityRank,
) -> Verdict:
    """-> (country_status, country_code, disputed, granularity)."""
    codes = country_codes(claims, normalized)
    foreign = {c for c in codes if c != "CZ"}
    has_town = filled.obec_kod is not None
    # Only a PORTAL PIN can be somewhere the rest of the row denies. A registry point and an
    # admin centroid both come OUT of the RÚIAN mirror, so asking whether they are in Czechia
    # or inside their own obec is a round trip whose answer is fixed by construction — and it
    # would be a round trip the slice warm cannot serve, because the key is not a claim's
    # coordinate. Restricting both reads to the pin is what keeps CHECK at zero cold reads.
    pin = (position.lat, position.lon) if position.origin == "portal_pin" else None
    in_cz = registry.in_czechia_polygon(*pin) if pin else None

    if not has_town:
        if foreign:
            code = next(iter(sorted(foreign))) if len(foreign) == 1 else None
            return Verdict(
                country_status="foreign",
                country_code=None if code == UNKNOWN_FOREIGN else code,
                granularity="country" if code and code != UNKNOWN_FOREIGN else "unknown",
            )
        if in_cz is False:
            # A pin outside the state polygon with nothing Czech to anchor it. The pin is
            # the only evidence there is, and it says "not here".
            return Verdict(country_status="foreign", country_code=None, granularity="unknown")
        if in_cz is True or "CZ" in codes or (pin is not None and in_cz is None and in_cz_bbox(*pin)):
            return Verdict(country_status="cz", country_code="CZ", granularity=granularity)
        return Verdict(country_status="undetermined", country_code=None, granularity="unknown")

    # ---- a Czech town. Everything below decides whether the row agrees with itself.
    if foreign:
        return Verdict("cz", "CZ", disputed="country_conflict", granularity=granularity)
    if in_cz is False:
        return Verdict("cz", "CZ", disputed="pin_outside_cz", granularity=granularity)
    if pin is not None:
        covering = registry.containing_obec(*pin)
        if covering is None or covering.code != filled.obec_kod:
            # Keep the pin — it is the only position we have — but say so, and drop to the
            # admin level: it is the ADDRESS identity that the disagreement puts in doubt.
            return Verdict(
                "cz", "CZ", disputed="pin_outside_obec",
                granularity=rank.coarser_of(granularity, "obec"),
            )
    return Verdict("cz", "CZ", granularity=granularity)


def _admissible(claim: Claim) -> bool:
    """A nav block, a footer or a similar-listings carousel is stored `subject_scoped=false`
    and is inadmissible here by construction."""
    if claim.subject_scoped is False:
        return False
    return is_rejected_country_evidence(claim.value_text) is None


def _code_of(claim: Claim, normalized: dict[int, NormalizedClaim]) -> str | None:
    raw = (claim.value_text or "").strip()
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    norm = normalized.get(claim.id)
    key = (norm.value_ascii if norm else None) or normalize_match_key(raw)
    if key in COUNTRY_GAZETTEER:
        return COUNTRY_GAZETTEER[key]
    if key in _FOREIGN_BUCKET_TOKENS:
        return UNKNOWN_FOREIGN
    return None
