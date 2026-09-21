"""Facts a Czech advert PRINTS, parsed once for both the benchmark and the engine (E60/E61).

`structural_truth` manufactured these parsers to LABEL pairs. W7 then showed the strongest of
them — a shared agency order code — is evidence the engine itself never sees: there is no
reference-code slot in `FEATURE_ORDER`, and `rare_token_overlap` is 0.0 on 62.8 % of the pairs
one certifies. Moving the parsing here gives the rule floor and the benchmark ONE spelling of
each fact instead of two that can drift apart.

What stays in `structural_truth` is the RULES — which combination of facts settles a pair — so
the benchmark's verdicts are still stated in one place. What lives here is only parsing: no
rule, no threshold that decides a pair, nothing that reads an engine feature.

The measurement consequence is stated once, here, so nobody forgets it: the engine's K-R
certificate and the benchmark's `pos_ref_*` labels now read the SAME string, so scoring K-R
against those labels is scoring a rule against itself. K-R is measured against the operator
and judge labels; `pos_ref_*` coverage is reported, never counted as accuracy.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Iterator, Mapping, TYPE_CHECKING

from autodedup.normalize import fact_text

if TYPE_CHECKING:  # pragma: no cover
    from autodedup.dataset import Listing

# A code shorter than this, or one carried by a crowd of listings, is a per-broker sequence
# number rather than an order key: it collides across objects and cannot certify anything.
MIN_CODE_LEN: int = 5
MAX_CODE_POPULATION: int = 8

CODE_MASK: str = "[KOD]"

_ACCENTS = re.compile(r"[̀-ͯ]")

# `ev. číslo: 657349`, `evidenční číslo zakázky N115423`, `evidenční číslo / ID zakázky: R2256`.
# The keyword is mandatory: a bare number in a body is a price, a year or a house number.
_REFERENCE = re.compile(
    r"(?:ev\.?\s*c(?:\.|islo)?"
    r"|evidencni\s+cislo(?:\s*/\s*id\s+zakazky)?(?:\s+zakazky)?"
    r"|cislo\s+zakazky|id\s+zakazky|kod\s+zakazky|zakazka\s*c\.?"
    r"|ref\.?\s*c(?:\.|islo)?|referencni\s+cislo"
    r"|cislo\s+nabidky|kod\s+nabidky|nabidka\s*c\.?)"
    r"\s*[:\s]\s*([a-z]{0,3}\s?\d[\d/-]{2,20})(?![\w/-])"
)
# One agency echoes its own key into the bazos headline as `[ID 84553]` rather than a Czech
# keyword; the bracket IS the keyword, so the token is as anchored as the others.
_BRACKET_ID = re.compile(r"\[\s*id\s*(\d{4,9})\s*\]")

# The unit DESIGNATOR — a name for one flat inside one building. `normalize`'s `unit` slot only
# reads `jednotka 12` / `byt č. 12`; the developer adverts this benchmark lives on write
# `byt (č.3)`, `označením B36`, `apartmán č. A311`, which that slot misses entirely. A trailing
# `+` or digit is excluded so `jednotku 4+kk` (a disposition) can never become unit 4.
_UNIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"byt\w*\s*[\(\[]?\s*c\.?\s*(\d{1,4})(?![\d+]|[.,]\d)"),
    re.compile(r"jednotk\w*\s*(?:c\.|cislo)\s*([a-z]?\d{1,4})(?![\d+]|[.,]\d)"),
    re.compile(r"cisl\w*\s+bytu\s*[:\-]?\s*(\d{1,4})(?![\d+]|[.,]\d)"),
    re.compile(r"oznacen\w{0,3}\s+([a-z]\d{1,3})\b"),
    re.compile(r"apartman\w*\s*c\.?\s*([a-z]?\d{1,4})\b"),
)

# --- printed floor areas ------------------------------------------------------------------
# A body text names the balcony, the plot and the cellar too; anything outside this window is
# not a dwelling's floor area and would make two unrelated adverts look like they agree.
STATED_AREA_MIN_M2: float = 5.0
STATED_AREA_MAX_M2: float = 100000.0
# W8: an area more than this many times the listing's OWN stored area (or that many times
# smaller) is not this unit's floor area — it is the building, the plot or a cellar. The stored
# column is used as a SANITY WINDOW only, never as the value: a cross-portal parse that prints
# 70 on one side and 72 on the other must still read as one fact (W7, pair 469862/15290511).
STATED_AREA_STORED_RATIO: float = 3.0
# Mentions this close together are one enumeration — `10 m2, 20 m2, 30 m2, …` is a size MENU of
# what the landlord can offer, not a statement about the advertised unit.
MENU_MAX_GAP_CHARS: int = 12
MENU_MIN_RUN: int = 3

# Spaced and dot-grouped thousands, then the plain form. Without the grouped alternative first,
# `10 500 m2` matches at `500` and fabricates an area the advert never printed (it did, on
# ceskereality 412654) — the same thousands truncation the portal parsers carry. Fixed HERE and
# not in `normalize`, whose `m2` slot feeds scored features that this wave must not move.
_AREA_NUMBER = r"(\d{1,3}(?:[  .]\d{3})+|\d+(?:[.,]\d+)?)"
_M2_MENTION = re.compile(_AREA_NUMBER + r"\s*m2\b")
# `od 55 m2 do 900 m2`, `55 m2 az 900 m2`, `55 - 900 m2`: a RANGE of what is available.
_AREA_RANGE = re.compile(
    r"\bod\s+" + _AREA_NUMBER + r"\s*(?:m2)?\s*(?:do|az|-|–)\s*" + _AREA_NUMBER + r"\s*m2"
    r"|" + _AREA_NUMBER + r"\s*m2\s*(?:az|do|-|–)\s*" + _AREA_NUMBER + r"\s*m2"
)
# The building's own size, named as such: `budova o velikosti 10.500 m2`, `objekt disponuje
# celkovou plochou cca 10 500 m2`. Adjacency is strict on purpose — `cihlové budovy. s užitnou
# plochou 80 m2` (listing 464483) names the UNIT, and a looser window would eat it.
_BUILDING_TOTAL = re.compile(
    r"(?:budov|objekt|areal|komplex)\w*\s+(?:o\s+)?(?:celkov\w+\s+)?"
    r"(?:velikosti|rozloze|ploche|plochou|vymere|vymera)\s+(?:cca\s+)?" + _AREA_NUMBER + r"\s*m2"
    r"|celkov\w+\s+(?:plochou|plocha|vymer\w+)\s+(?:cca\s+)?" + _AREA_NUMBER + r"\s*m2"
)


def fold(text: str) -> str:
    """Deaccented lowercase — the folding every pattern here is written against."""
    return _ACCENTS.sub("", unicodedata.normalize("NFKD", text)).lower()


def reference_codes(text: str | None) -> set[str]:
    """Agency order numbers the body text names explicitly, normalised to one spelling."""
    if not text:
        return set()
    out: set[str] = set()
    folded = fold(text)
    for pattern in (_REFERENCE, _BRACKET_ID):
        for match in pattern.finditer(folded):
            code = re.sub(r"\s+", "", match.group(1)).upper().strip("/-")
            if len(code) >= MIN_CODE_LEN:
                out.add(code)
    return out


def mask_codes(text: str | None) -> str | None:
    r"""Every agency order code this module can find, replaced by one token.

    Masking runs over the ORIGINAL text (the folded copy `reference_codes` matches on is not
    index-aligned with it), joining the code's characters with `\s*` because the capture removed
    the whitespace a body may print inside the number."""
    if not text:
        return text
    out = text
    for code in reference_codes(text):
        pattern = re.compile(r"\s*".join(re.escape(ch) for ch in code), re.IGNORECASE)
        out = pattern.sub(CODE_MASK, out)
    return out


def iter_reference_matches(folded: str) -> Iterator[tuple[str, re.Match[str]]]:
    """Every reference-code match in already-folded text, as `(code, match)` pairs."""
    for pattern in (_REFERENCE, _BRACKET_ID):
        for match in pattern.finditer(folded):
            yield re.sub(r"\s+", "", match.group(1)).upper().strip("/-"), match


def unit_designators(text: str | None) -> set[str]:
    """Unit designators the body text names: `byt (č.3)`, `jednotka č. 12`, `označením B36`."""
    if not text:
        return set()
    folded = fold(text)
    return {
        match.group(1).upper()
        for pattern in _UNIT_PATTERNS
        for match in pattern.finditer(folded)
    }


# The compass direction a body PRINTS as this unit's: `Orientace je na východ`, `byt je
# orientovaný na jihozápad`, `situovaný na sever`. Longest stem first, because `jihovýchod`
# contains `východ` and a shorter match would read one flat's south-east as another's east.
# The keyword is mandatory — a body also names the direction of the motorway and of the park.
_COMPASS_STEMS: tuple[str, ...] = (
    "severovychod", "severozapad", "jihovychod", "jihozapad",
    "sever", "jih", "vychod", "zapad",
)
_ORIENTATION_KEYWORD = re.compile(r"orientac\w*|orientovan\w*|situovan\w*")
_ORIENTATION_WINDOW: int = 44
_COMPASS_WORD = re.compile(
    r"\b(severovychod\w*|severozapad\w*|jihovychod\w*|jihozapad\w*"
    r"|sever\w*|jizni|jih|vychod\w*|zapad\w*)\b"
)


def orientations(text: str | None) -> set[str]:
    """Every compass direction the body states as this unit's, normalised to one stem.

    The clause after the keyword is read WHOLE, not to its first direction: `orientaci na jih
    i na sever` is a through-flat naming two, and a rule that stopped at `jih` would read it
    as one. Abbreviations (`na J/Z`) are deliberately not read — a two-letter token is a coin
    flip against street names and room labels, and the rule would rather abstain than guess."""
    if not text:
        return set()
    folded = fold(text)
    out: set[str] = set()
    for keyword in _ORIENTATION_KEYWORD.finditer(folded):
        clause = folded[keyword.end(): keyword.end() + _ORIENTATION_WINDOW]
        clause = re.split(r"[.;!?]", clause, maxsplit=1)[0]
        for match in _COMPASS_WORD.finditer(clause):
            word = match.group(1)
            if word == "jizni":
                out.add("jih")
                continue
            for stem in _COMPASS_STEMS:
                if word.startswith(stem):
                    out.add(stem)
                    break
    return out


def _area_value(raw: str) -> float | None:
    cleaned = raw.replace(" ", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:[.]\d{3})+", cleaned):  # 10.500 = ten and a half thousand
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned.replace(",", "."))
    except ValueError:
        return None


def _menu_spans(folded: str, mentions: list[tuple[int, int, float]]) -> list[tuple[int, int]]:
    """Character spans that hold a size MENU rather than one unit's area."""
    spans: list[tuple[int, int]] = [match.span() for match in _AREA_RANGE.finditer(folded)]
    run: list[tuple[int, int, float]] = []
    for mention in mentions:
        if run and mention[0] - run[-1][1] <= MENU_MAX_GAP_CHARS:
            run.append(mention)
            continue
        if len(run) >= MENU_MIN_RUN:
            spans.append((run[0][0], run[-1][1]))
        run = [mention]
    if len(run) >= MENU_MIN_RUN:
        spans.append((run[0][0], run[-1][1]))
    return spans


def stated_areas(text: str | None, stored_area_m2: float | None = None) -> set[float]:
    """Every floor area the BODY prints AS THIS UNIT'S, in m².

    Deliberately not `listings.area_m2`: that column is a per-portal parse, and one flat can
    carry 70 on one portal and 72 on another off the same sentence. What the two texts SAY is
    the same fact on both sides.

    W8 narrows what counts as "prints": a commercial advert also prints the BUILDING it sits in
    (`Polygon je moderní budova o velikosti 10.500 m²`) and a landlord prints a MENU of what is
    available (`od 55 m² do 900 m²`, `10 m², 20 m², 30 m², …`). Neither is a statement about the
    advertised unit, and comparing one side's building with the other side's unit mislabelled
    three same-unit cross-broker pairs as different (W7 benchmark, root cause AREA SCOPE)."""
    if not text:
        return set()
    folded = fact_text(text)
    mentions: list[tuple[int, int, float]] = []
    for match in _M2_MENTION.finditer(folded):
        value = _area_value(match.group(1))
        if value is not None:
            mentions.append((match.start(), match.end(), value))
    if not mentions:
        return set()
    skip = _menu_spans(folded, mentions) + [
        match.span() for match in _BUILDING_TOTAL.finditer(folded)
    ]
    low, high = STATED_AREA_MIN_M2, STATED_AREA_MAX_M2
    if stored_area_m2 is not None and stored_area_m2 > 0.0:
        low = max(low, stored_area_m2 / STATED_AREA_STORED_RATIO)
        high = min(high, stored_area_m2 * STATED_AREA_STORED_RATIO)
    out: set[float] = set()
    for start, end, value in mentions:
        if any(span[0] <= start and end <= span[1] for span in skip):
            continue
        if low <= value <= high:
            out.add(value)
    return out


# A price the advert states as a STARTING price — "ceny od 5 499 000 Kč", "již od 2 750 000".
# It is the printed admission that the number belongs to a CATALOGUE of units rather than to
# this one, which is why a price tie between two such adverts says nothing about identity.
_FROM_PRICE = re.compile(
    r"(?:cen\w*\s+(?:jiz\s+)?od|jiz\s+od|prodej\s+od|pronajem\s+od|k\s+dispozici\s+od)"
    r"\s*(?:cca\s*)?\d[\d\s\u00a0]{3,}"
)


def states_from_price(text: str | None) -> bool:
    """Does the body quote a FROM price — the advert's own word for a price list?"""
    return bool(text) and bool(_FROM_PRICE.search(fold(text)))


def address_block_key(listing: "Listing") -> str:
    """The finest place key the listing carries — the stratification unit and the "one project" test.

    RÚIAN address code first (one entrance), then obec+street+house number, then obec+street,
    then a 3-decimal pin (~110 m), then the obec. Never empty, so every pair has a stratum."""
    loc = listing.location
    if loc.ruian_adm_kod:
        return f"ruian:{loc.ruian_adm_kod}"
    obec = loc.obec_kod or 0
    if loc.street_key and loc.house_number:
        return f"addr:{obec}:{loc.street_key}:{loc.house_number}"
    if loc.street_key:
        return f"street:{obec}:{loc.street_key}"
    if loc.lat is not None and loc.lon is not None:
        return f"pin:{obec}:{loc.lat:.3f}:{loc.lon:.3f}"
    return f"obec:{obec}"


def code_population(descriptions: Mapping[int, str | None]) -> dict[str, int]:
    """How many listings of the corpus print each code — the purity cap's denominator."""
    population: dict[str, int] = {}
    for text in descriptions.values():
        for code in reference_codes(text):
            population[code] = population.get(code, 0) + 1
    return population


def rare_codes(codes: Iterable[str], population: Mapping[str, int]) -> set[str]:
    """The codes of one listing that a crowd does not share (E60's population cap)."""
    return {code for code in codes if population.get(code, 0) <= MAX_CODE_POPULATION}
