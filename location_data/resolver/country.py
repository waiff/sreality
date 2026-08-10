"""S2 — country determination (03 §3.4). Runs before everything else.

Country is a first-class determination with a status, a confidence and a method, and
non-CZ is a POSITIVE state rather than a resolution failure: 16 833 active idnes rows sit
outside the CZ bbox today and carry no obec — invisible to every admin filter and still on
the map.

Two rules dominate the implementation:

* **Cheap and deterministic first** (§3.4.5). The two portals that carry the problem are
  solved free: idnes ships the country *in Czech, in a stored text column* (signal 1b) and
  bazos ships it as a PSČ bucket (signal 3). The model lane is residual-only, so a
  free-tier configuration still determines them.
* **The bbox is a TRIGGER, never a determination** (§3.4.1 signal 5). The Wisła hotel sits
  0.008° outside it; the Italian row's coordinates are geographically correct for Scalea —
  those pins are *uncountried, not wrong*. The one bbox is read from `location_constants`
  (01 §2.2); this module never spells the numbers.

The §3.4.2 false-positive rejections are MANDATORY and are enforced twice: structurally
(a `subject_scoped=false` claim is inadmissible at all) and by content
(`is_rejected_country_evidence`), because a keyword detector flags every listing on two
portals — `Zahraniční nemovitosti` is site nav on 100 % of mmreality and ceskereality
pages, the REMAX footer lists twelve countries, EUR is standard practice on CZ commercial
rent, and the Regus boilerplate advertises a "global network".
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from location_data.resolver.normalize import normalize_match_key
from location_data.resolver.types import (
    Claim,
    CountryDetermination,
    LocationConstants,
    NormalizedClaim,
    RegistryView,
)

# The closed Czech-language country gazetteer signal 1b needs. Keys are match keys
# (lower + unaccented). Deliberately closed: an open-ended detector is what produces the
# §3.4.2 false positives.
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

# The four §3.4.2 traps, verified present in the corpus. Each one flags EVERY listing on
# at least one portal if a naive keyword detector is allowed to see it.
_NAV_ZAHRANICNI = re.compile(r"zahranicni nemovitosti", re.IGNORECASE)
_EUR = re.compile(r"(?:\bEUR\b|€)")
_REGUS = re.compile(r"\b(regus|iwg)\b|globalni sit|global network", re.IGNORECASE)
# The REMAX footer is a bare RUN of country names — and it is in ENGLISH ("Austria Belgium
# Bulgaria … Spain" on all 12 sampled pages), so the footer detector needs its own token
# set. These are never a determination vocabulary: they only recognise the trap.
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


def is_rejected_country_evidence(text: str | None) -> str | None:
    """-> the name of the §3.4.2 trap this text is, or None. Mandatory on every text-borne
    country signal, on top of the structural `subject_scoped` gate."""
    if not text:
        return None
    key = normalize_match_key(text)
    if _NAV_ZAHRANICNI.search(key):
        return "site_nav_zahranicni_nemovitosti"
    if _EUR.search(text):
        return "eur_denomination"
    if _REGUS.search(key) or _REGUS.search(text):
        return "regus_boilerplate"
    hits: set[str] = {
        code for token, code in COUNTRY_GAZETTEER.items() if _token_in(key, token)
    }
    hits.update(token for token in _FOOTER_TOKENS_EN if _token_in(key, token))
    if len(hits) >= _FOOTER_MIN_COUNTRIES:
        return "country_list_footer"
    return None


def _token_in(key: str, token: str) -> bool:
    return re.search(rf"(?:^|\s){re.escape(token)}(?:\s|$)", key) is not None


def country_from_text(text: str | None) -> str | None:
    """Signal 1b: a TRAILING country token in a locality/address text
    (`"Benahavís, Španělsko"`, `"…, 180 00, Česko"`). Trailing only — a country named
    mid-sentence is prose, not an address tail."""
    if not text:
        return None
    parts = [p for p in re.split(r"[,;]", text) if p.strip()]
    if not parts:
        return None
    tail = normalize_match_key(parts[-1])
    if tail in COUNTRY_GAZETTEER:
        return COUNTRY_GAZETTEER[tail]
    if tail in _FOREIGN_BUCKET_TOKENS:
        return "XX"
    return None


def _admissible(claim: Claim) -> bool:
    """§3.2 rule 4 + §3.4.2: nav, footers and similar-listing blocks are stored
    `subject_scoped=false` and are inadmissible to S2 by construction."""
    if claim.subject_scoped is False:
        return False
    return is_rejected_country_evidence(claim.value_text) is None


def determine_country(
    claims: Sequence[Claim],
    normalized: dict[int, NormalizedClaim],
    *,
    registry: RegistryView,
    constants: LocationConstants,
    pin: tuple[float, float] | None,
) -> CountryDetermination:
    """The §3.4.1 signals in evaluation order, with §3.4.3's conflict rule.

    `XX` is the code the bazos `Zahraničí` bucket produces: foreign, country unknown. It
    never reaches the projection as a code — it is carried as `status='foreign'` with a
    NULL `country_code`.
    """
    votes: list[tuple[str, str, int, str]] = []  # (code, method, claim_id, strength)

    # ---- 1: an explicit country claim from a portal-structured field.
    for claim in claims:
        if claim.claim_type != "country" or not _admissible(claim):
            continue
        code = _code_of(claim, normalized)
        if not code:
            continue
        if claim.extraction_method in ("portal_structured_field", "portal_declared_quality"):
            votes.append((code, "portal_field", claim.id, "strong"))
        else:
            votes.append((code, "text_claim", claim.id, "medium"))

    # ---- 1b: trailing country token in a subject-scoped locality / address text.
    for claim in claims:
        if claim.claim_type not in ("address_line_verbatim", "postal_town", "obec_name", "landmark"):
            continue
        if not _admissible(claim):
            continue
        code = country_from_text(claim.value_text)
        if not code:
            continue
        method = (
            "portal_field"
            if claim.extraction_method == "portal_structured_field"
            else "text_claim"
        )
        votes.append((code, method, claim.id, "strong"))

    # ---- 3: portal bucket sentinels (bazos PSČ 987 65 / 987 66, the `Zahraničí` bucket).
    for claim in claims:
        norm = normalized.get(claim.id)
        if norm is None or not _admissible(claim):
            continue
        hint = norm.typed_slots.get("country_hint")
        if isinstance(hint, str):
            votes.append((hint, "portal_bucket", claim.id, "strong"))
        elif claim.claim_type in ("obec_name", "postal_town") and normalize_match_key(
            claim.value_text or ""
        ) in _FOREIGN_BUCKET_TOKENS:
            votes.append(("XX", "portal_bucket", claim.id, "strong"))

    # ---- 4: text-mined foreign indicators (residual; decisive where nothing else is).
    for claim in claims:
        if claim.claim_type != "foreign_indicator" or not _admissible(claim):
            continue
        code = _code_of(claim, normalized) or "XX"
        votes.append((code, "text_claim", claim.id, "medium"))

    # ---- 2: registry containment. Authoritative for CZ; non-containment is a trigger only.
    registry_cz: bool | None = None
    if pin is not None:
        registry_cz = registry.in_czechia_polygon(pin[0], pin[1])
        if registry_cz is True:
            votes.append(("CZ", "registry_containment", 0, "strong"))

    distinct = {code for code, _, _, _ in votes}
    foreign_votes = {c for c in distinct if c != "CZ"}

    if not votes:
        return _from_pin_only(pin, registry_cz, constants)

    # §3.4.3: text beats pin when subject-scoped and validated — but the result is
    # `disputed`, never a silent flip. 3 of the 5 corpus `foreign_suspect` rows are
    # unambiguously Czech geocoder artifacts, so trusting either side unconditionally is
    # wrong in both directions.
    if "CZ" in distinct and foreign_votes:
        driving = tuple(sorted(cid for code, _, cid, _ in votes if cid))
        conflicting = tuple(
            {"code": code, "method": method, "claim_id": cid}
            for code, method, cid, _ in sorted(votes, key=lambda v: (v[0], v[1], v[2]))
        )
        return CountryDetermination(
            country_code=None,
            status="disputed",
            confidence="medium",
            method="text_claim",
            driving_claim_ids=driving,
            conflicting=conflicting,
        )

    if len(foreign_votes) > 1:
        # Two different foreign countries claimed: still foreign, but the code is disputed.
        driving = tuple(sorted(cid for _, _, cid, _ in votes if cid))
        return CountryDetermination(
            country_code=None,
            status="disputed",
            confidence="low",
            method="text_claim",
            driving_claim_ids=driving,
            conflicting=tuple(
                {"code": code, "method": method, "claim_id": cid}
                for code, method, cid, _ in sorted(votes, key=lambda v: (v[0], v[1], v[2]))
            ),
        )

    code = next(iter(distinct))
    strongest = _strongest(votes, code)
    driving = tuple(sorted(cid for c, _, cid, _ in votes if c == code and cid))
    status = "cz" if code == "CZ" else "foreign"
    confidence = "high" if strongest[3] == "strong" else "medium"
    return CountryDetermination(
        country_code=None if code == "XX" else code,
        status=status,
        confidence=confidence,
        method=strongest[1],
        driving_claim_ids=driving,
    )


def _strongest(votes: list[tuple[str, str, int, str]], code: str) -> tuple[str, str, int, str]:
    order = {"portal_field": 0, "portal_bucket": 1, "registry_containment": 2, "text_claim": 3}
    return min(
        (v for v in votes if v[0] == code),
        key=lambda v: (0 if v[3] == "strong" else 1, order.get(v[1], 9), v[2]),
    )


def _code_of(claim: Claim, normalized: dict[int, NormalizedClaim]) -> str | None:
    raw = (claim.value_text or "").strip()
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    norm = normalized.get(claim.id)
    key = (norm.value_ascii if norm else None) or normalize_match_key(raw)
    if key in COUNTRY_GAZETTEER:
        return COUNTRY_GAZETTEER[key]
    if key in _FOREIGN_BUCKET_TOKENS:
        return "XX"
    return None


def _from_pin_only(
    pin: tuple[float, float] | None,
    registry_cz: bool | None,
    constants: LocationConstants,
) -> CountryDetermination:
    """No claim said anything. The pin alone may only produce a CZ determination (registry
    containment) or `undetermined` — never a foreign determination (§3.4.1 signal 5)."""
    if pin is None:
        return CountryDetermination(None, "undetermined", "low", "unknown")
    if registry_cz is True:
        return CountryDetermination("CZ", "cz", "high", "registry_containment")
    if registry_cz is None and constants.in_bbox(pin[0], pin[1]):
        # bbox alone: a weak, explicitly non-authoritative CZ assumption.
        return CountryDetermination("CZ", "cz", "low", "assumed_default")
    return CountryDetermination(None, "undetermined", "low", "unknown")
