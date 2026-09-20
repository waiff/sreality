"""Labels for pairs that come from STRUCTURE in the adverts — not from a model, not from the operator.

W6 could not measure any judge's false-merge rate because the human negatives it had were 18
band negatives over 16 listings, 14 of them from one card. A rate measured on one development
is not a rate. This module manufactures the missing axis: pairs whose truth a *fact printed in
the adverts* settles, over as many address blocks as the cohort holds.

Two classes, both conservative — a rule that is merely likely is not here.

POSITIVE (certainly ONE unit). Czech agencies stamp their order number into the body text
("evidenční číslo zakázky N115423", "Ev. číslo: 657349") and the SAME string travels onto every
portal the order is syndicated to, and onto the re-post when the advert expires. The code is the
agency's key for one order, so two adverts carrying it are two views of one order. `pos_unit_in_project`
adds the case with no code: one address block, both adverts naming the same unit number, on the
same stated floor, at the same area.

NEGATIVE (certainly TWO units). Co-liveness alone proves nothing — a broker posting one unit twice
on one portal is a duplicate by the operator's ruling — so every negative rule demands an EXPLICIT
conflicting unit fact: two different unit numbers, two different order codes running side by side,
or a different floor together with a different area.

Nothing here reads an engine feature, a judge verdict or an operator label: the labels must be
independent of the thing they measure. The only shared code is `normalize`'s text folding, which is
parsing, not deciding.

Evidence travels as short strings so a contradiction can be adjudicated by reading it. They are run
through `export.scrub_description` (E28) on the way out, because the context window around a code
can clip a broker's signature.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence

from autodedup.dataset import Listing
from autodedup.export import scrub_description
from autodedup.normalize import fact_text, numeric_facts_folded

LABEL_SAME: str = "same"
LABEL_DIFFERENT: str = "different"

POSITIVE_RULES: tuple[str, ...] = ("pos_ref_cross", "pos_ref_relist", "pos_unit_in_project")
NEGATIVE_RULES: tuple[str, ...] = ("neg_unit_number_conflict", "neg_stated_area_conflict")
RULES: tuple[str, ...] = POSITIVE_RULES + NEGATIVE_RULES

# A code shorter than this, or one carried by a crowd of listings, is a per-broker sequence
# number rather than an order key: it collides across objects and cannot certify anything.
MIN_CODE_LEN: int = 5
MAX_CODE_POPULATION: int = 8

# Two adverts that ran side by side this long are not one advert replaced by its re-post.
MIN_OVERLAP_DAYS: float = 21.0
UNIT_AREA_MAX_REL_DIFF: float = 0.02
# Two areas a Czech advert PRINTS are the same area when they are this close: 47,6 vs 46,6 m²
# is one flat measured twice (adjudicated, pair 16438/92824 — identical text, identical price),
# 50,7 vs 43,3 is two flats. Below 3% the rule abstains rather than guess.
STATED_AREA_MIN_REL_DIFF: float = 0.03
# A body text names the balcony, the plot and the cellar too; anything outside this window is
# not a dwelling's floor area and would make two unrelated adverts look like they agree.
STATED_AREA_MIN_M2: float = 5.0
STATED_AREA_MAX_M2: float = 100000.0

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

def _fold(text: str) -> str:
    return _ACCENTS.sub("", unicodedata.normalize("NFKD", text)).lower()


def reference_codes(text: str | None) -> set[str]:
    """Agency order numbers the body text names explicitly, normalised to one spelling."""
    if not text:
        return set()
    out: set[str] = set()
    folded = _fold(text)
    for pattern in (_REFERENCE, _BRACKET_ID):
        for match in pattern.finditer(folded):
            code = re.sub(r"\s+", "", match.group(1)).upper().strip("/-")
            if len(code) >= MIN_CODE_LEN:
                out.add(code)
    return out


CODE_MASK: str = "[KOD]"


def mask_codes(text: str | None) -> str | None:
    r"""Every agency order code this module can find, replaced by one token.

    The diagnostic W7 needs: `pos_ref_*` certifies a pair BECAUSE both bodies carry one code,
    and 69.3 % of those codes survive into the judge's 1,200-character description window — so
    an arm's recall on structural positives is partly a string match the prompt handed it.
    Masking runs over the ORIGINAL text (the folded copy `reference_codes` matches on is not
    index-aligned with it), joining the code's characters with `\s*` because the capture
    removed the whitespace a body may print inside the number."""
    if not text:
        return text
    out = text
    for code in reference_codes(text):
        pattern = re.compile(r"\s*".join(re.escape(ch) for ch in code), re.IGNORECASE)
        out = pattern.sub(CODE_MASK, out)
    return out


def reference_context(text: str | None, code: str, width: int = 44) -> str:
    """The scrubbed window around `code`'s first mention — what a human adjudicates from."""
    if not text:
        return ""
    folded = _fold(text)
    for pattern in (_REFERENCE, _BRACKET_ID):
        for match in pattern.finditer(folded):
            if re.sub(r"\s+", "", match.group(1)).upper().strip("/-") != code:
                continue
            start = max(0, match.start() - width)
            window = text[start : match.end() + 4]
            return " ".join((scrub_description(window) or "").split())
    return ""


def unit_numbers(text: str | None) -> set[str]:
    """Unit designators the body text names: `byt (č.3)`, `jednotka č. 12`, `označením B36`."""
    if not text:
        return set()
    folded = _fold(text)
    return {
        match.group(1).upper()
        for pattern in _UNIT_PATTERNS
        for match in pattern.finditer(folded)
    }


def stated_areas(text: str | None) -> set[float]:
    """Every floor area the BODY prints, in m².

    Deliberately not `listings.area_m2`: that column is a per-portal parse, and one flat can
    carry 70 on one portal and 72 on another off the same sentence ("byt 3+1 o velikosti 72m2",
    adjudicated pair 469862/15290511). What the two texts SAY is the same fact on both sides."""
    if not text:
        return set()
    return {
        value
        for slot, value in numeric_facts_folded(fact_text(text))
        if slot == "m2" and STATED_AREA_MIN_M2 <= value <= STATED_AREA_MAX_M2
    }


def areas_disjoint(a: Iterable[float], b: Iterable[float]) -> bool:
    """No area either text prints is within `STATED_AREA_MIN_REL_DIFF` of one the other prints."""
    left, right = list(a), list(b)
    if not left or not right:
        return False
    return not any(
        abs(x - y) <= STATED_AREA_MIN_REL_DIFF * max(x, y) for x in left for y in right
    )


def address_block_key(listing: Listing) -> str:
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


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)


def overlap_days(a: Listing, b: Listing) -> float:
    """Days both adverts were live at once; a live advert ends at +infinity, not at `last_seen_at`."""
    start_a, start_b = _parse(a.first_seen_at), _parse(b.first_seen_at)
    if start_a is None or start_b is None:
        return 0.0
    end_a = _parse(a.inactive_at) or _parse(a.last_seen_at) or (_FAR_FUTURE if a.is_active else None)
    end_b = _parse(b.inactive_at) or _parse(b.last_seen_at) or (_FAR_FUTURE if b.is_active else None)
    if end_a is None or end_b is None:
        return 0.0
    span = min(end_a, end_b) - max(start_a, start_b)
    return max(0.0, span / timedelta(days=1))


def _rel_diff(x: float | None, y: float | None) -> float | None:
    if x is None or y is None:
        return None
    scale = max(abs(x), abs(y))
    return abs(x - y) / scale if scale else 0.0


@dataclass(slots=True, frozen=True)
class StructuralLabel:
    lo: int
    hi: int
    label: str
    rule: str
    block: str
    evidence: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "lo": self.lo,
            "hi": self.hi,
            "label": self.label,
            "rule": self.rule,
            "block": self.block,
            "evidence": dict(self.evidence),
        }


@dataclass(slots=True)
class Facts:
    """Everything the rules read off ONE listing, computed once."""

    listing_id: int
    codes: frozenset[str]
    units: frozenset[str]
    areas: frozenset[float]
    block: str


@dataclass(slots=True)
class Index:
    facts: dict[int, Facts]
    code_population: dict[str, int]

    def rare_codes(self, listing_id: int) -> set[str]:
        codes = self.facts[listing_id].codes
        return {c for c in codes if self.code_population.get(c, 0) <= MAX_CODE_POPULATION}


def build_index(listings: Mapping[int, Listing]) -> Index:
    facts: dict[int, Facts] = {}
    population: dict[str, int] = defaultdict(int)
    for listing_id, listing in listings.items():
        codes = reference_codes(listing.description)
        facts[listing_id] = Facts(
            listing_id=listing_id,
            codes=frozenset(codes),
            units=frozenset(unit_numbers(listing.description)),
            areas=frozenset(stated_areas(listing.description)),
            block=address_block_key(listing),
        )
        for code in codes:
            population[code] += 1
    return Index(facts=facts, code_population=dict(population))


def _comparable(a: Listing, b: Listing) -> bool:
    """The standing ruling: never across deal types, never byt with komerční."""
    return a.category_main == b.category_main and a.category_type == b.category_type


def _one_project(a: Listing, b: Listing, index: Index) -> bool:
    """Two adverts a conflicting unit fact can be read across: one address block, or one
    agency's two orders in one municipality (a code conflict needs the agency, not the house)."""
    if index.facts[a.id].block == index.facts[b.id].block:
        return True
    same_place = a.location.obec_kod is not None and a.location.obec_kod == b.location.obec_kod
    if not same_place:
        return False
    if a.broker_identity_id is not None and a.broker_identity_id == b.broker_identity_id:
        return True
    return a.source == b.source and a.broker_key is not None and a.broker_key == b.broker_key


def _code_family(code: str) -> str:
    """`N115423` -> `N999999`: the shape an agency numbers its orders in.

    A code CONFLICT only means something inside one numbering scheme — two agencies selling
    one unit carry two unrelated codes, and calling that a conflict would be a false negative."""
    return re.sub(r"\d", "9", code)


def _positive(a: Listing, b: Listing, index: Index) -> tuple[str, dict[str, str]] | None:
    shared = index.rare_codes(a.id) & index.rare_codes(b.id)
    if shared:
        code = sorted(shared)[0]
        evidence = {
            "code": code,
            "code_population": str(index.code_population.get(code, 0)),
            "context_lo": reference_context(a.description, code),
            "context_hi": reference_context(b.description, code),
        }
        if a.source != b.source:
            return "pos_ref_cross", evidence | {"sources": f"{a.source}|{b.source}"}
        if a.source_id_native != b.source_id_native:
            return "pos_ref_relist", evidence | {
                "source": str(a.source),
                "native": f"{a.source_id_native}|{b.source_id_native}",
                "overlap_days": f"{overlap_days(a, b):.1f}",
            }
        return None
    units_a, units_b = index.facts[a.id].units, index.facts[b.id].units
    if len(units_a) != 1 or len(units_b) != 1 or units_a != units_b:
        return None
    if index.facts[a.id].block != index.facts[b.id].block:
        return None
    area = _rel_diff(a.area_m2, b.area_m2)
    floors_agree = a.floor is not None and a.floor == b.floor
    if not floors_agree and (area is None or area > UNIT_AREA_MAX_REL_DIFF):
        return None
    return "pos_unit_in_project", {
        "unit": sorted(units_a)[0],
        "floor": f"{a.floor}|{b.floor}",
        "area_m2": f"{a.area_m2}|{b.area_m2}",
        "block": index.facts[a.id].block,
    }


def _negative(a: Listing, b: Listing, index: Index) -> tuple[str, dict[str, str]] | None:
    """The two facts a Czech advert prints that settle "different unit" — and nothing softer.

    `neg_ref_code_conflict` was BUILT and DROPPED here: two different order codes in one
    building are not two units, because an agency re-numbers per portal and sometimes per
    re-post. Adjudicated 395722/486034 and 395722/496635 (identical text, identical price
    7 974 910 Kč, codes N115815 vs N118731) — both one flat. Two of the nine band pairs the
    rule produced were false, so it is not ground truth. A shared code stays a POSITIVE."""
    facts_a, facts_b = index.facts[a.id], index.facts[b.id]
    if facts_a.block != facts_b.block:
        return None
    units_a, units_b = facts_a.units, facts_b.units
    if len(units_a) == 1 and len(units_b) == 1 and units_a != units_b:
        return "neg_unit_number_conflict", {
            "unit_lo": sorted(units_a)[0],
            "unit_hi": sorted(units_b)[0],
            "block": facts_a.block,
            "area_m2": f"{a.area_m2}|{b.area_m2}",
        }
    overlap = overlap_days(a, b)
    # An identical asking price, to the koruna, is the developer pricing one flat twice far
    # more often than two flats alike: the K Botiči block prints 27,2 m² and 28,6 m² both at
    # 5 499 000 Kč and the cohort cannot say which is the typo, so the rule abstains there.
    price_identical = a.price is not None and a.price == b.price
    if (
        overlap >= MIN_OVERLAP_DAYS
        and not price_identical
        and areas_disjoint(facts_a.areas, facts_b.areas)
    ):
        return "neg_stated_area_conflict", {
            "areas_lo": ",".join(f"{v:g}" for v in sorted(facts_a.areas)[:6]),
            "areas_hi": ",".join(f"{v:g}" for v in sorted(facts_b.areas)[:6]),
            "min_rel_gap": f"{min(abs(x - y) / max(x, y) for x in facts_a.areas for y in facts_b.areas):.3f}",
            "overlap_days": f"{overlap:.1f}",
            "price": f"{a.price}|{b.price}",
            "block": facts_a.block,
        }
    return None


def label_pair(a: Listing, b: Listing, index: Index) -> StructuralLabel | None:
    """The structural verdict on one pair, or None when no rule fires.

    A pair a positive AND a negative rule both claim is returned as None: the two facts
    contradict each other, and a benchmark may not guess which one lied."""
    if a.id > b.id:
        a, b = b, a
    if not _comparable(a, b):
        return None
    positive = _positive(a, b, index)
    negative = _negative(a, b, index)
    if positive is not None and negative is not None:
        return None
    block = index.facts[a.id].block
    if positive is not None:
        return StructuralLabel(a.id, b.id, LABEL_SAME, positive[0], block, positive[1])
    if negative is not None:
        return StructuralLabel(a.id, b.id, LABEL_DIFFERENT, negative[0], block, negative[1])
    return None


def label_pairs(
    listings: Mapping[int, Listing],
    pairs: Iterable[tuple[int, int]],
    index: Index | None = None,
) -> list[StructuralLabel]:
    index = index or build_index(listings)
    out: list[StructuralLabel] = []
    for lo, hi in pairs:
        a, b = listings.get(lo), listings.get(hi)
        if a is None or b is None:
            continue
        label = label_pair(a, b, index)
        if label is not None:
            out.append(label)
    return out


def stratify(
    labels: Sequence[StructuralLabel], cap_per_block: int, limit: int
) -> list[StructuralLabel]:
    """Round-robin over blocks so no development can dominate the benchmark.

    Blocks are visited in a rotation ordered by `(block, lo, hi)`, taking one pair from each
    before any block gets a second — deterministic, and it maximises block coverage at every
    prefix, so a cap that bites still spreads the pairs it keeps."""
    by_block: dict[str, list[StructuralLabel]] = defaultdict(list)
    for label in sorted(labels, key=lambda item: (item.block, item.lo, item.hi)):
        by_block[label.block].append(label)
    out: list[StructuralLabel] = []
    for round_index in range(cap_per_block):
        for block in sorted(by_block):
            bucket = by_block[block]
            if round_index < len(bucket):
                out.append(bucket[round_index])
                if len(out) >= limit:
                    return out
    return out
