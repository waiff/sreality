"""Pairwise features over the exported cohort (PROGRAM.md §5; E12, E9/E10, E19, E20).

Every feature is a `(value, present)` pair — E12: missing data is never a mismatch, it is its
own signal, so the model consumes `value·present` and `present` separately. Nothing here reads
the database; all inputs come from `autodedup.dataset` records plus the per-listing
`Fingerprint`, and the per-listing halves of the work (token document frequencies, tf-idf
vectors, rare-token sets, attribute rarity, price-change events, pin populations, the decoded
CLIP anchors) are precomputed ONCE in `FeatureContext`, never per pair.
"""

from __future__ import annotations

import math
import re
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from autodedup.dataset import Dataset, Image, Listing, cosine_norm, hamming64
from autodedup.normalize import canonical_attr

if TYPE_CHECKING:  # the sibling modules are imported for their types only, never at runtime
    from autodedup.fingerprint import Fingerprint
    from autodedup.settings import Settings

Feats = dict[str, tuple[float, bool]]

ABSENT: tuple[float, bool] = (0.0, False)

# `location_granularity_rank.rank` (migration 380): street = 60 is the precision gate of E16.
STREET_GRAIN_RANK: int = 60
RADIUS_BY_RANK: tuple[tuple[int, float], ...] = (
    (100, 10.0),
    (90, 20.0),
    (80, 40.0),
    (70, 60.0),
    (60, 120.0),
    (50, 900.0),
    (40, 2500.0),
    (0, 20000.0),
)
DIST_FLOOR_M: float = 25.0

# `FEATURE_ORDER` is a closed vocabulary; this is its version, bumped whenever the tuple changes
# shape. v2 appends `unit_number_shared` (E45); v3 appends the two plot-area slots (W4e). The
# lane's `score_lane.FEATURE_VERSION` stamp on `autodedup.pairs` must follow this number.
FEATURE_VERSION: int = 3

# Comparable attribute slots: `attrs` keys (autodedup/export_sql.ATTR_COLUMNS) that carry a
# categorical value, plus the listing-level subtype. Numeric side-areas and timestamps are not
# equality-comparable and are excluded. `Settings.vocabulary_attr_keys` subtracts the slots that
# are portal VOCABULARY rather than property fact (default `price_unit`, `area_basis`).
ATTR_KEYS: tuple[str, ...] = (
    "building_type",
    "condition",
    "energy_rating",
    "ownership",
    "furnished",
    "terrace",
    "cellar",
    "garage",
    "parking_lots",
    "has_lift",
    "area_basis",
    "category_sub_cb",
    "price_unit",
    "subtype",
)
# Legacy conflated booleans (balcony-or-loggia, parking-or-garage) carry half weight.
CONFLATED_ATTR_KEYS: tuple[str, ...] = ("has_balcony", "has_parking")
# THE default of `Settings.vocabulary_attr_keys` — settings.py imports this tuple rather than
# restating it, so a settings-less caller (the judge digest) can never drop a different set.
# The dependency runs one way only: features.py must not import settings at runtime.
DEFAULT_VOCABULARY_ATTR_KEYS: tuple[str, ...] = ("price_unit", "area_basis")
CONFLATED_WEIGHT: float = 0.5

AREA_EXACT_REL: float = 0.01
AREA_EXACT_ABS: float = 0.5

# The PLOT (parcel) area, measured on the W4 cohort (export 35096363646, 2026-09-16):
#   * `estate_area` IS the plot everywhere it appears — on `dum` rows its median is 3.56x the
#     headline `area_m2` (295 rows carrying both), i.e. the headline is the BUILDING;
#   * on `pozemek` rows the headline area is the plot: `estate_area == area_m2` on 177 of 177
#     rows that carry both, so the headline is the fallback carrier for land and only for land;
#   * `garden_area` is NOT a plot fallback — on the 89 rows carrying both it is a median 0.84 of
#     `estate_area` and matches it exactly on only 8% (within 2% on 11%), so reading it as the
#     parcel would manufacture contradictions on true duplicates.
LAND_CATEGORY: str = "pozemek"
PLOT_EXACT_REL: float = 0.02

# Five portals parse an area with `(\d+(?:[.,]\d+)?)\s*m2`, which reads "5 870 m²" as 870 — a
# thousands-separator truncation that is INVISIBLE in the stored number:
# `scraper/{bazos,ceskereality,realitymix,remax,maxima}_parser.py` all carry the same regex
# (bazos_parser.py:76, ceskereality_parser.py:89, realitymix_parser.py:91, remax_parser.py:164,
# maxima_parser.py:89). Census of the carriers `plot_area` below actually SELECTS (n above the
# PLOT_MIN_M2 floor / share >= 1000 / how many the per-value test below flags):
#   idnes        estate    195 / 43% /  0      clean
#   ceskereality estate    173 /  0% / 19      <- truncating, gated per SOURCE
#   sreality     estate    168 / 45% /  0      clean
#   realitymix   estate     50 / 28% /  0      clean (a different code path)
#   bazos        headline   40 / 32% /  6      <- truncating, gated per VALUE
#   realitymix   headline   20 /  0% /  6      <- truncating, gated per SOURCE
#   bezrealitky  estate     18 / 55% /  0      clean
#   mmreality    headline    9 / 44% /  0      clean
#   mmreality    estate      4 / 50% /  0      too thin to census, left trusted
#   maxima       estate      2 /  0% /  2      too thin to census, left trusted
#   remax        headline    1 /  0% /  0      too thin to census, left trusted
# TWO guards, because the portals fail in two different shapes.
#
# Per SOURCE: ceskereality's 173 estate values never reach 1,000 where sreality and idnes reach
# it on 43-45%, so its truncation is SYSTEMATIC and no threshold can separate a real 870 from a
# cut 5,870 — the carrier is read as ABSENT wholesale. Same for realitymix's land headline (0 of
# 20). `("ceskereality", "headline")` is dead in this cohort — all 29 ceskereality land rows also
# carry an `estate_area`, so the fallback is never reached — and is kept as a forward guard only:
# the same parser cuts that column too (0 of its 709 headline areas reaches 1,000).
PLOT_TRUNCATING_SOURCES: frozenset[tuple[str, str]] = frozenset({
    ("ceskereality", "estate"),
    ("ceskereality", "headline"),
    ("realitymix", "headline"),
})
# Per VALUE: bazos is the largest headline carrier (40 of the 70 rows the code reads through it)
# and carries the same regex, but it DOES reach 1,000 on 13 of 40 — its truncation depends on
# whether the seller typed "1 500" or "1500", so the population has no signature and only the
# individual advert can convict the number. The test: the stored plot is a three-digit tail of a
# space-grouped number the listing's OWN description writes ("11 197 m²" stored as 197). It flags
# 6 of bazos' 40 and 0 of the 431 values on the four clean carriers, so it costs nothing to run
# everywhere — and it survives the re-census the source table owes once the parsers are fixed.
_GROUPED_THOUSANDS = re.compile(r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+")
# The same artefact between two carriers neither guard convicts: a plot that is the last THREE
# digits of the other side's plot is a cut number, not a different parcel (870 of 5,870; 400 of
# 3,400). Exactly three: a thousands cut leaves the whole final group, where a shorter residue
# would match a coincidental digit tail (a 1-digit low matches ~1 high in 10). Such a pair is read
# as ABSENT rather than as a contradiction.
PLOT_TRUNCATION_GROUP_DIGITS: int = 3
# A parcel below this is a data-entry artefact, not land: 36 rows in the cohort carry
# `estate_area = 1`. Such a value must never reach the comparison — via the truncation guard above
# it would read two real disagreements as absence.
PLOT_MIN_M2: float = 10.0

# False-by-omission (same cohort, 2026-09-16). Per (source, boolean slot): the portals that emit
# `true` or NOTHING and never `false`, over at least 30 observed trues —
#   bezrealitky cellar 110 / terrace 39 / has_balcony 152, ceskereality has_balcony 180,
#   idnes cellar 336 / terrace 108 / has_lift 374 / has_balcony 345 / has_parking 355,
#   mmreality cellar 36 / has_parking 59, realitymix has_balcony 36, all with ZERO falses.
# For such a portal "no cellar" is not a fact it publishes, so a `false` in that slot is a parser
# default, and a default may not contradict the other side's `true`. This is a FORWARD guard: no
# row in the measured cohort trips it (that is what "never emits false" means), and it fires the
# day a parser starts writing its default into the column.
# The broader reading — suppress every cross-portal `false` — is REFUTED by the same labels: on
# judged pairs a sreality `cellar=false` is the discriminator on 34 non-duplicates against 2 true
# duplicates, so a portal that DOES publish the negative must keep contradicting.
FALSE_BY_OMISSION: frozenset[tuple[str, str]] = frozenset({
    ("bezrealitky", "cellar"),
    ("bezrealitky", "terrace"),
    ("bezrealitky", "has_balcony"),
    ("ceskereality", "has_balcony"),
    ("idnes", "cellar"),
    ("idnes", "terrace"),
    ("idnes", "has_lift"),
    ("idnes", "has_balcony"),
    ("idnes", "has_parking"),
    ("mmreality", "cellar"),
    ("mmreality", "has_parking"),
    ("realitymix", "has_balcony"),
})

PRICE_EVENT_MIN_REL: float = 0.001
PRICE_EVENT_DAY_TOL: float = 3.0
PRICE_EVENT_DELTA_TOL: float = 0.01

# Per-unit tolerance for numeric-fact agreement/conflict (relative; 0.0 means exact).
NUMERAL_TOLERANCE: dict[str, float] = {
    "m2": 0.02,
    "kc": 0.01,
    "floor": 0.0,
    "rooms": 0.0,
    "unit": 0.0,
}

# CLIP is the only quadratic cost that is not a popcount, so the cross-product is capped — but
# over an INTERIOR-FIRST ordering (§4's anchor rule), never in gallery order, which would bias the
# lane toward cover shots. `Settings.clip_sample` / `Settings.phash_sample` override when present.
# The 8 is measured: at cohort scale (5,400 listings x 15 images, 150k pairs) the pass costs 92 s
# at 8 and ~245 s at 12, against a <5 min budget for the whole run.
CLIP_SAMPLE: int = 8
PHASH_SAMPLE: int = 30

# E20's "rare" means df <= rare_token_df in a corpus big enough for that to mean anything: in a
# two-document block every shared boilerplate token is rare. Below this many in-block documents
# the feature is ABSENT, and the overlap is capped so one long shared template cannot outvote
# every guard in the model.
MIN_RARE_BLOCK_DOCS: int = 20
RARE_TOKEN_CAP: float = 5.0

# `numeral_conflict` is a terminal auto-reject in decide.py, so only IDENTITY slots may raise it:
# a price cut (kc) or a rounded area (m2) is what an E6 re-listing looks like, not a different unit.
# `Settings.numeral_conflict_units` is the swept row; this is its default.
CONFLICT_UNITS: frozenset[str] = frozenset({"floor", "rooms", "unit"})
UNIT_SLOT: str = "unit"

# Thresholds of the E11 evidence rules below, named because each one is an argued bar.
ATTR_EXACT_AREA_REL: float = 0.01
ATTR_UNIT_AREA_REL: float = 0.02
ATTR_RARE_EVIDENCE: float = 4.0
TXT_RARE_TOKENS: float = 2.0
TXT_NUMERIC_FACTS: float = 2.0
IMG_INTERIOR_MIN: float = 0.01
IMG_MATCH_RATIO: float = 0.30
IMG_GALLERY_MATCHES: float = 4.0
IMG_GALLERY_MONOTONE: float = 0.80
IMG_CLIP_COS: float = 0.95
IMG_CATALOG_MAX: float = 0.20

SECONDS_PER_DAY: float = 86400.0

FEATURE_ORDER: tuple[str, ...] = (
    # ATTR
    "area_rel_diff",
    "area_exact",
    "dispo_equal",
    "floor_diff",
    "total_floors_equal",
    "attr_agreements",
    "attr_contradictions",
    "attr_agreements_rare",
    # PRICE
    "price_last_ratio",
    "ppm2_rel_diff",
    "price_path_event_match",
    # TXT
    "jaccard_shingle",
    "containment_max",
    "tfidf_cos",
    "simhash_hamming",
    "len_ratio",
    "rare_token_overlap",
    "numeric_fact_overlap",
    "numeral_conflict",
    # BRK
    "same_broker_key",
    "same_broker_identity",
    "broker_known_both",
    # LOC
    "same_ruian_adm_kod",
    "same_street_key",
    "same_house_number",
    "same_psc",
    "dist_norm",
    "same_exact_pin",
    "pin_pop",
    # IMG
    "phash_tight_matches",
    "phash_loose_matches",
    "phash_match_ratio",
    "clip_max_cos",
    "clip_mean_top3_cos",
    "seq_monotone_ratio",
    "interior_match_ratio",
    "exterior_match_ratio",
    "plan_match_ratio",
    "catalog_ratio_max",
    "n_images_min",
    # TIME
    "gap_days",
    "overlap_days",
    "both_active",
    "same_source",
    # v2 append-only tail (E45): the unit-number fact the "same building, different unit" class
    # is missing. FEATURE_ORDER grows at the END only, so a stored vector stays readable.
    "unit_number_shared",
    # v3 append-only tail (W4e): the PARCEL area, which no ATTR slot compared — `estate_area` and
    # `garden_area` are numeric, so they sit outside ATTR_KEYS and nothing looked at them.
    "plot_area_rel_diff",
    "plot_area_exact",
)

FAMILY_OF: dict[str, str] = {
    "area_rel_diff": "ATTR",
    "area_exact": "ATTR",
    "dispo_equal": "ATTR",
    "floor_diff": "ATTR",
    "total_floors_equal": "ATTR",
    "attr_agreements": "ATTR",
    "attr_contradictions": "ATTR",
    "attr_agreements_rare": "ATTR",
    "price_last_ratio": "PRICE",
    "ppm2_rel_diff": "PRICE",
    "price_path_event_match": "PRICE",
    "jaccard_shingle": "TXT",
    "containment_max": "TXT",
    "tfidf_cos": "TXT",
    "simhash_hamming": "TXT",
    "len_ratio": "TXT",
    "rare_token_overlap": "TXT",
    "numeric_fact_overlap": "TXT",
    "numeral_conflict": "TXT",
    "same_broker_key": "BRK",
    "same_broker_identity": "BRK",
    "broker_known_both": "BRK",
    "same_ruian_adm_kod": "LOC",
    "same_street_key": "LOC",
    "same_house_number": "LOC",
    "same_psc": "LOC",
    "dist_norm": "LOC",
    "same_exact_pin": "LOC",
    "pin_pop": "LOC",
    "phash_tight_matches": "IMG",
    "phash_loose_matches": "IMG",
    "phash_match_ratio": "IMG",
    "clip_max_cos": "IMG",
    "clip_mean_top3_cos": "IMG",
    "seq_monotone_ratio": "IMG",
    "interior_match_ratio": "IMG",
    "exterior_match_ratio": "IMG",
    "plan_match_ratio": "IMG",
    "catalog_ratio_max": "IMG",
    "n_images_min": "IMG",
    "gap_days": "TIME",
    "overlap_days": "TIME",
    "both_active": "TIME",
    "same_source": "TIME",
    "unit_number_shared": "TXT",
    "plot_area_rel_diff": "ATTR",
    "plot_area_exact": "ATTR",
}

# E11 counts corroboration over five families only; PRICE and TIME are scored, never counted as
# an independent family. A rule is an AND of `(feature, lo, hi)` conditions on PRESENT values, and
# a family fires when ANY of its rules holds. Every rule must be UNIT-grade: E20 says template
# similarity is the thing that has to be separated from unit identity, and E10 says a shared facade
# or site plan is a BUILDING signal — so bare template similarity, a bare shared photo and a bare
# disposition match are scored features, never corroboration.
Condition = tuple[str, float, float]
EVIDENCE_FAMILIES: tuple[str, ...] = ("IMG", "TXT", "LOC", "BRK", "ATTR")
EVIDENCE_RULES: dict[str, tuple[tuple[Condition, ...], ...]] = {
    "ATTR": (
        (("area_exact", 1.0, 1.0),),
        (("area_rel_diff", 0.0, ATTR_EXACT_AREA_REL),),
        (("area_rel_diff", 0.0, ATTR_UNIT_AREA_REL), ("dispo_equal", 1.0, 1.0)),
        (("attr_agreements_rare", ATTR_RARE_EVIDENCE, math.inf),),
        # A parcel area is a CADASTRAL fact, not a describable one — but the labelled evidence for
        # that is THIN and does not yet separate: 40 of 42 judged duplicates that carry two trusted
        # plots agree within 2%, against 3 of the 9 — the whole labelled negative set — that carry
        # them. Two tightenings were measured and REFUTED: the threshold buys nothing (all three
        # false fires are EXACT equalities, and 0.5% scores the same 40/3 as 2%), and rarity buys
        # less than nothing (the false fires sit at corpus df 8-11, but so do 8 of the true
        # duplicates — a df gate that drops them drops 19 positives). The failure mode is a modal
        # parcel: 740 m² is the median `dum` estate on both sreality and idnes and appears 11 times
        # in the cohort. The rule is kept because the mechanism is sound and it decides NOTHING
        # today (measured: removing it moves 0 pairs between zones, 0 reasons, 0 certificates — it
        # only shows in the recorded family bitmask of 27 pairs); it is owed a labelled draw of
        # plot-carrying negatives before the refit can lean on it.
        (("plot_area_exact", 1.0, 1.0),),
    ),
    # E20: only the unit-specific half of the text corroborates. Template similarity — jaccard,
    # tf-idf, containment — stays a scored feature; a broker's reused boilerplate is exactly what
    # high containment looks like between two units of one development.
    "TXT": (
        (("rare_token_overlap", TXT_RARE_TOKENS, math.inf),),
        (("numeric_fact_overlap", TXT_NUMERIC_FACTS, math.inf),),
    ),
    "BRK": (
        (("same_broker_identity", 1.0, 1.0),),
        (("same_broker_key", 1.0, 1.0),),
    ),
    "LOC": (
        (("same_ruian_adm_kod", 1.0, 1.0),),
        (("same_street_key", 1.0, 1.0),),
        (("same_house_number", 1.0, 1.0),),
        (("same_exact_pin", 1.0, 1.0),),
        (("dist_norm", 0.0, 1.0),),
    ),
    "IMG": (
        (("interior_match_ratio", IMG_INTERIOR_MIN, math.inf),),
        (
            ("phash_match_ratio", IMG_MATCH_RATIO, math.inf),
            ("catalog_ratio_max", 0.0, IMG_CATALOG_MAX),
        ),
        (
            ("phash_tight_matches", IMG_GALLERY_MATCHES, math.inf),
            ("seq_monotone_ratio", IMG_GALLERY_MONOTONE, math.inf),
            ("catalog_ratio_max", 0.0, IMG_CATALOG_MAX),
        ),
        (
            ("clip_max_cos", IMG_CLIP_COS, math.inf),
            ("catalog_ratio_max", 0.0, IMG_CATALOG_MAX),
        ),
    ),
}


def _known(value: float | int | None) -> bool:
    return value is not None


def _eq(a: Any, b: Any) -> tuple[float, bool]:
    if a is None or b is None:
        return ABSENT
    return (1.0 if a == b else 0.0, True)


def rel_diff(a: float, b: float) -> float:
    """Symmetric relative difference; 0.0 when both sides are zero."""
    scale = max(abs(a), abs(b))
    return 0.0 if scale == 0.0 else abs(a - b) / scale


def plot_area(listing: Listing) -> float | None:
    """The parcel area of one side, or None when the portal's number cannot be trusted.

    `estate_area` is the plot; on land the headline area is (measured identical on 177/177 rows
    that carry both). `garden_area` is a different fact and is never read here."""
    attrs = listing.attrs or {}
    value = attrs.get("estate_area")
    carrier = "estate"
    if value is None and listing.category_main == LAND_CATEGORY:
        value = listing.area_m2
        carrier = "headline"
    if value is None or (listing.source, carrier) in PLOT_TRUNCATING_SOURCES:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < PLOT_MIN_M2 or plot_truncated_in_text(number, listing.description):
        return None
    return number


def plot_truncated_in_text(value: float, text: str | None) -> bool:
    """Is this stored plot a three-digit tail of a space-grouped number the advert itself writes?

    That is a thousands cut ("11 197 m²" stored as 197), not a parcel. Measured: 0 false flags
    over the 431 values on the four carriers whose populations look clean."""
    if not text or value <= 0.0 or value != int(value):
        return False
    digits = str(int(value))
    if len(digits) != PLOT_TRUNCATION_GROUP_DIGITS:
        return False
    for match in _GROUPED_THOUSANDS.finditer(text):
        joined = re.sub(r"[\s\u00a0\u202f]", "", match.group(0))
        if len(joined) > len(digits) and joined.endswith(digits):
            return True
    return False


def thousands_truncation_suspect(a: float, b: float) -> bool:
    """Is the smaller plot the last THREE digits of the larger one — a cut number, not a parcel?"""
    low, high = sorted((a, b))
    if low <= 0.0 or low == high or low != int(low) or high != int(high):
        return False
    low_text, high_text = str(int(low)), str(int(high))
    if len(low_text) != PLOT_TRUNCATION_GROUP_DIGITS or len(high_text) <= len(low_text):
        return False
    return high_text.endswith(low_text)


def haversine_m(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    phi_a = math.radians(lat_a)
    phi_b = math.radians(lat_b)
    d_phi = phi_b - phi_a
    d_lam = math.radians(lon_b - lon_a)
    h = math.sin(d_phi / 2.0) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lam / 2.0) ** 2
    return 2.0 * 6371008.8 * math.asin(min(1.0, math.sqrt(h)))


def uncertainty_radius_m(rank: int | None, declared: float | None) -> float:
    """The exported radius when the resolver supplied one, else the rung's nominal radius."""
    if declared is not None and declared > 0.0:
        return declared
    if rank is None:
        return RADIUS_BY_RANK[-1][1]
    for threshold, radius in RADIUS_BY_RANK:
        if rank >= threshold:
            return radius
    return RADIUS_BY_RANK[-1][1]


def parse_ts(value: str | None) -> float | None:
    """ISO timestamp -> epoch days; naive stamps are read as UTC."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp() / SECONDS_PER_DAY


def price_change_events(history: Sequence[tuple[str, float | None]]) -> list[tuple[float, float]]:
    """Price path -> `(epoch_day, relative delta)` for each real change (E19)."""
    points: list[tuple[float, float]] = []
    for stamp, price in history:
        day = parse_ts(stamp)
        if day is None or price is None or price <= 0.0:
            continue
        points.append((day, float(price)))
    points.sort(key=lambda point: point[0])
    events: list[tuple[float, float]] = []
    for index in range(1, len(points)):
        previous = points[index - 1][1]
        current = points[index][1]
        delta = (current - previous) / previous
        if abs(delta) >= PRICE_EVENT_MIN_REL:
            events.append((points[index][0], delta))
    return events


def price_path_event_match(
    events_a: Sequence[tuple[float, float]], events_b: Sequence[tuple[float, float]]
) -> float:
    """Fraction of the SMALLER side's change events matched within ±3 days and ±1% of Δ."""
    small, large = (events_a, events_b) if len(events_a) <= len(events_b) else (events_b, events_a)
    if not small:
        return 0.0
    taken: set[int] = set()
    matched = 0
    for day, delta in small:
        for index, (other_day, other_delta) in enumerate(large):
            if index in taken:
                continue
            if abs(day - other_day) <= PRICE_EVENT_DAY_TOL and abs(delta - other_delta) <= PRICE_EVENT_DELTA_TOL:
                taken.add(index)
                matched += 1
                break
    return matched / len(small)


def vocabulary_attr_keys(settings: "Settings | None") -> frozenset[str]:
    """The attribute slots that are portal VOCABULARY, excluded from agreement AND contradiction.

    Gold eval 2026-09-16: `price_unit` differs on 55% of cross-portal TRUE duplicates (221/401)
    and on 0% of same-portal ones — the portals spell one fact two ways — and `area_basis`
    differs on 31% of cross-portal positives against 14% of negatives. Both are anti-signal."""
    if settings is None:
        return frozenset(DEFAULT_VOCABULARY_ATTR_KEYS)
    return frozenset(settings.vocabulary_attr_keys)


def _emitted(listing: Listing, key: str, value: object) -> bool:
    """Is this a value the portal PUBLISHES? A `false` from a portal that only ever publishes the
    positive is a parser default (`FALSE_BY_OMISSION`), and a default is absence, not a fact."""
    if value is None:
        return False
    return not (value is False and (listing.source, key) in FALSE_BY_OMISSION)


def _attr_raw(listing: Listing, skip: frozenset[str] = frozenset()) -> dict[str, object]:
    out: dict[str, object] = {}
    attrs = listing.attrs or {}
    for key in ATTR_KEYS:
        if key in skip:
            continue
        value = listing.subtype if key == "subtype" else attrs.get(key)
        if _emitted(listing, key, value):
            out[key] = value
    for key in CONFLATED_ATTR_KEYS:
        if key in skip:
            continue
        value = attrs.get(key)
        if _emitted(listing, key, value):
            out[key] = value
    return out


def _attr_map(listing: Listing, skip: frozenset[str] = frozenset()) -> dict[str, str]:
    """The slots as the comparison sees them: one canonical token per value, `None` dropped."""
    out: dict[str, str] = {}
    for key, value in _attr_raw(listing, skip).items():
        token = canonical_attr(key, value)
        if token is not None:
            out[key] = token
    return out


def attribute_conflicts(
    la: Listing, lb: Listing, settings: "Settings | None" = None
) -> list[tuple[str, object, object]]:
    """The slots behind the `attr_contradictions` COUNT, as `(field, value A, value B)`.

    Same field set and same equality as the feature (normalised, case-folded, vocabulary slots
    dropped), because a prompt that named a conflict the model cannot see in the feature vector
    would be arguing with it — the values are returned RAW so the digest can print
    `energy_rating A=B vs B=C` rather than the lower-cased form the comparison runs on."""
    skip = vocabulary_attr_keys(settings)
    raw_a = _attr_raw(la, skip)
    raw_b = _attr_raw(lb, skip)
    norm_a = _attr_map(la, skip)
    norm_b = _attr_map(lb, skip)
    return [
        (key, raw_a[key], raw_b[key])
        for key, value_a in norm_a.items()
        if key in norm_b and norm_b[key] != value_a
    ]


def _numeral_agreement(
    a: Iterable[tuple[str, float]],
    b: Iterable[tuple[str, float]],
    conflict_units: frozenset[str] = CONFLICT_UNITS,
) -> tuple[float, float]:
    """`(agreements, conflicts)`: a one-to-one sorted sweep per unit, so the count is the same in
    either argument order, and only an IDENTITY slot (`conflict_units`) can raise a conflict —
    never a Kč or m² slot, which prices and areas already have their own features for."""
    by_unit_a: dict[str, list[float]] = {}
    by_unit_b: dict[str, list[float]] = {}
    for unit, value in a:
        by_unit_a.setdefault(unit, []).append(value)
    for unit, value in b:
        by_unit_b.setdefault(unit, []).append(value)
    agreements = 0.0
    conflicts = 0.0
    for unit, raw_a in by_unit_a.items():
        raw_b = by_unit_b.get(unit)
        if not raw_b:
            continue
        tolerance = NUMERAL_TOLERANCE.get(unit, 0.0)
        values_a = sorted(raw_a)
        values_b = sorted(raw_b)
        index_a = index_b = 0
        hits = 0
        while index_a < len(values_a) and index_b < len(values_b):
            left = values_a[index_a]
            right = values_b[index_b]
            if rel_diff(left, right) <= tolerance:
                hits += 1
                index_a += 1
                index_b += 1
            elif left < right:
                index_a += 1
            else:
                index_b += 1
        agreements += hits
        if hits == 0 and unit in conflict_units:
            conflicts += 1.0
    return agreements, conflicts


def unit_number_shared(
    a: Iterable[tuple[str, float]], b: Iterable[tuple[str, float]]
) -> tuple[float, bool]:
    """E45: do the two adverts name the SAME unit number? Present only when both name one.

    `normalize.numeric_facts` already isolates the `unit` slot (`jednotka 12`, `č. bytu 12`); a
    shared unit number is the one text fact the developer-unit false merges never had."""
    units_a = {value for slot, value in a if slot == UNIT_SLOT}
    units_b = {value for slot, value in b if slot == UNIT_SLOT}
    if not units_a or not units_b:
        return ABSENT
    return (1.0 if units_a & units_b else 0.0, True)


@dataclass(slots=True)
class FeatureContext:
    """Per-run, per-listing precomputation: everything a pair should never recompute."""

    settings: "Settings"
    block_docs: dict[str, int] = field(default_factory=dict)
    block_token_df: dict[str, dict[str, int]] = field(default_factory=dict)
    block_attr_df: dict[str, dict[tuple[str, str], int]] = field(default_factory=dict)
    corpus_docs: int = 0
    corpus_token_df: dict[str, int] = field(default_factory=dict)
    tfidf: dict[int, dict[str, float]] = field(default_factory=dict)
    tfidf_corpus: dict[int, dict[str, float]] = field(default_factory=dict)
    rare: dict[int, set[str]] = field(default_factory=dict)
    pin_pop: dict[str, int] = field(default_factory=dict)
    dataset: Dataset | None = None
    _events: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    _clip: dict[tuple[int, int], list[tuple["array[float]", float]]] = field(default_factory=dict)
    _phash: dict[tuple[int, int], list[tuple[int, int, int]]] = field(default_factory=dict)
    _live: dict[tuple[int, int], int] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        fps: Mapping[int, "Fingerprint"],
        settings: "Settings",
        dataset: Dataset | None = None,
    ) -> "FeatureContext":
        ctx = cls(settings=settings, dataset=dataset)
        listings_by_block: dict[str, list[int]] = {}
        for listing_id, fp in fps.items():
            block = _block_of(fp)
            listings_by_block.setdefault(block, []).append(listing_id)
            df = ctx.block_token_df.setdefault(block, {})
            for token in set(fp.desc_tokens or ()):
                df[token] = df.get(token, 0) + 1
                ctx.corpus_token_df[token] = ctx.corpus_token_df.get(token, 0) + 1
            if fp.pin_key:
                ctx.pin_pop[fp.pin_key] = ctx.pin_pop.get(fp.pin_key, 0) + 1
        for block, members in listings_by_block.items():
            ctx.block_docs[block] = len(members)
        ctx.corpus_docs = len(fps)
        for listing_id, fp in fps.items():
            block = _block_of(fp)
            df = ctx.block_token_df[block]
            n_docs = max(1, ctx.block_docs.get(block, 1))
            counts: dict[str, int] = {}
            for token in fp.desc_tokens or ():
                counts[token] = counts.get(token, 0) + 1
            ctx.tfidf[listing_id] = _tfidf_vector(counts, df, n_docs)
            # A cross-block pair has no shared block corpus, so K3/K4/K5 pairs are compared in the
            # corpus-wide space instead (§4: those probes are deliberately location-free).
            ctx.tfidf_corpus[listing_id] = _tfidf_vector(
                counts, ctx.corpus_token_df, max(1, ctx.corpus_docs)
            )
            ctx.rare[listing_id] = (
                {token for token in counts if df.get(token, 0) <= settings.rare_token_df}
                if n_docs >= MIN_RARE_BLOCK_DOCS
                else set()
            )
        return ctx

    def attr_rarity(self, block: str, key: str, value: str) -> float:
        """Self-information of an attribute value inside its block: rare agreements score more."""
        df = self.block_attr_df.get(block, {}).get((key, value), 0)
        n_docs = self.block_docs.get(block, 0)
        if df <= 0 or n_docs <= 0:
            return 1.0
        return max(0.0, -math.log(min(1.0, df / n_docs)))

    def index_attrs(self, fps: Mapping[int, "Fingerprint"], listings: Mapping[int, Listing]) -> None:
        """Count in-block attribute-value frequencies; optional, and `attr_rarity` degrades to 1.0."""
        for listing_id, fp in fps.items():
            listing = listings.get(listing_id)
            if listing is None:
                continue
            bucket = self.block_attr_df.setdefault(_block_of(fp), {})
            for key, value in _attr_map(listing, vocabulary_attr_keys(self.settings)).items():
                bucket[(key, value)] = bucket.get((key, value), 0) + 1

    def events(self, listing: Listing) -> list[tuple[float, float]]:
        """E19's change events. `listing.price_history` is the ONLY source: `Fingerprint.
        price_events` is derived from it and carries absolute prices, so no fallback can add
        information — an empty list means "this advert never moved its price", not "unknown"."""
        cached = self._events.get(listing.id)
        if cached is not None:
            return cached
        events = price_change_events(listing.price_history or [])
        self._events[listing.id] = events
        return events

    def phash_gallery(
        self,
        listing_id: int,
        images: Sequence[Image],
        settings: "Settings | None" = None,
    ) -> list[tuple[int, int, int]]:
        """`(image_id, seq, phash)` for the non-catalog, phashed images, in gallery order (E9)."""
        active = settings or self.settings
        key = (listing_id, int(active.catalog_df))
        cached = self._phash.get(key)
        if cached is not None:
            return cached
        limit = int(getattr(active, "phash_sample", PHASH_SAMPLE))
        gallery: list[tuple[int, int, int]] = []
        for index, image in enumerate(images):
            if image.phash is None or image.is_catalog_candidate(active.catalog_df):
                continue
            gallery.append((image.image_id, image.seq if image.seq is not None else index, image.phash))
            if len(gallery) >= limit:
                break
        self._phash[key] = gallery
        return gallery

    def non_catalog_count(
        self,
        listing_id: int,
        images: Sequence[Image],
        settings: "Settings | None" = None,
    ) -> int:
        """Gallery size AFTER E9 subtraction — uncapped, so it is not the sample size."""
        active = settings or self.settings
        key = (listing_id, int(active.catalog_df))
        cached = self._live.get(key)
        if cached is not None:
            return cached
        count = sum(1 for image in images if not image.is_catalog_candidate(active.catalog_df))
        self._live[key] = count
        return count

    def clip_gallery(
        self,
        fp: "Fingerprint",
        images: Sequence[Image],
        settings: "Settings | None" = None,
    ) -> list[tuple["array[float]", float]]:
        """The CLIP sample, INTERIOR first (§4's anchor rule) so the cap can never select a pair
        of cover shots and call the result unit evidence."""
        active = settings or self.settings
        key = (fp.listing_id, int(active.catalog_df))
        cached = self._clip.get(key)
        if cached is not None:
            return cached
        limit = int(getattr(active, "clip_sample", CLIP_SAMPLE))
        interior = set(fp.interior_image_ids or ())
        ordered = sorted(
            images,
            key=lambda image: (
                0 if image.image_id in interior else 1,
                image.seq if image.seq is not None else 1 << 30,
                image.image_id,
            ),
        )
        gallery: list[tuple["array[float]", float]] = []
        for image in ordered:
            if image.is_catalog_candidate(active.catalog_df):
                continue
            vector = image.clip_vector()
            norm = image.clip_norm()
            if vector is None or not norm:
                continue
            gallery.append((vector, norm))
            if len(gallery) >= limit:
                break
        self._clip[key] = gallery
        return gallery


def _block_of(fp: "Fingerprint") -> str:
    return str(fp.block_key) if fp.block_key is not None else "(none)"


def _tfidf_vector(
    counts: Mapping[str, int], df: Mapping[str, int], n_docs: int
) -> dict[str, float]:
    """L2-normalised log-tf x idf vector over one document frequency table."""
    vector: dict[str, float] = {}
    for token, count in counts.items():
        idf = math.log(1.0 + n_docs / max(1, df.get(token, 1)))
        vector[token] = (1.0 + math.log(count)) * idf
    length = math.sqrt(sum(weight * weight for weight in vector.values()))
    if length > 0.0:
        for token in vector:
            vector[token] /= length
    return vector


def _phash_matches(
    gallery_a: Sequence[tuple[int, int, int]],
    gallery_b: Sequence[tuple[int, int, int]],
    tight: int,
    loose: int,
) -> tuple[list[tuple[int, int, int, int]], int]:
    """Greedy ONE-TO-ONE assignment, closest pairs first: `(image_a, seq_a, image_b, seq_b)` tight
    hits plus the loose count. One-to-one is what makes the counts bounded by `min(n_a, n_b)` (so a
    ratio can never exceed 1.0) and identical when the two galleries are swapped — a repeated photo
    on one side used to match every copy of itself on the other."""
    candidates: list[tuple[int, int, int, int, int, int]] = []
    for image_a, seq_a, hash_a in gallery_a:
        for image_b, seq_b, hash_b in gallery_b:
            distance = hamming64(hash_a, hash_b)
            if distance <= loose:
                # The tie-break is symmetric in the two image ids, so swapping the galleries
                # cannot reorder the greedy pass.
                candidates.append((
                    distance, min(image_a, image_b), max(image_a, image_b),
                    image_a, seq_a, image_b,
                ))
    candidates.sort()
    seq_of_b = {image_b: seq_b for image_b, seq_b, _ in gallery_b}
    used_a: set[int] = set()
    used_b: set[int] = set()
    tight_hits: list[tuple[int, int, int, int]] = []
    loose_count = 0
    for distance, _, _, image_a, seq_a, image_b in candidates:
        if image_a in used_a or image_b in used_b:
            continue
        used_a.add(image_a)
        used_b.add(image_b)
        loose_count += 1
        if distance <= tight:
            tight_hits.append((image_a, seq_a, image_b, seq_of_b[image_b]))
    return tight_hits, loose_count


def _seq_monotone_ratio(hits: Sequence[tuple[int, int, int, int]]) -> tuple[float, bool]:
    if len(hits) < 2:
        return ABSENT
    ordered = sorted(hits, key=lambda hit: hit[1])
    rises = sum(
        1
        for index in range(1, len(ordered))
        if ordered[index][3] > ordered[index - 1][3]
    )
    return (rises / (len(ordered) - 1), True)


def _family_ratio(
    hits: Sequence[tuple[int, int, int, int]],
    ids_a: Sequence[int] | None,
    ids_b: Sequence[int] | None,
    live_a: set[int],
    live_b: set[int],
) -> tuple[float, bool]:
    """E10: a family ratio exists only when BOTH sides still have a non-catalog image of it."""
    set_a = set(ids_a or ()) & live_a
    set_b = set(ids_b or ()) & live_b
    if not set_a or not set_b:
        return ABSENT
    matched = sum(1 for image_a, _, image_b, _ in hits if image_a in set_a and image_b in set_b)
    return (matched / min(len(set_a), len(set_b)), True)


def _clip_features(
    gallery_a: Sequence[tuple["array[float]", float]],
    gallery_b: Sequence[tuple["array[float]", float]],
) -> tuple[tuple[float, bool], tuple[float, bool]]:
    if not gallery_a or not gallery_b:
        return ABSENT, ABSENT
    scores: list[float] = []
    for vector_a, norm_a in gallery_a:
        for vector_b, norm_b in gallery_b:
            scores.append(cosine_norm(vector_a, vector_b, norm_a, norm_b))
    scores.sort(reverse=True)
    top = scores[:3]
    return (scores[0], True), (sum(top) / len(top), True)


def _window(listing: Listing) -> tuple[float | None, float | None]:
    start = parse_ts(listing.first_seen_at)
    end = parse_ts(listing.inactive_at) or parse_ts(listing.last_seen_at)
    if start is not None and end is not None and end < start:
        end = start
    return start, end


def pair_features(
    fa: "Fingerprint",
    fb: "Fingerprint",
    la: Listing,
    lb: Listing,
    images_a: Sequence[Image],
    images_b: Sequence[Image],
    ctx: FeatureContext,
    settings: "Settings",
) -> Feats:
    """Every feature of FEATURE_ORDER for one pair, each as `(value, present)` (E12)."""
    feats: Feats = {}

    # --- ATTR -------------------------------------------------------------------------
    if fa.area_m2 is not None and fb.area_m2 is not None:
        difference = rel_diff(float(fa.area_m2), float(fb.area_m2))
        feats["area_rel_diff"] = (difference, True)
        exact = difference <= AREA_EXACT_REL or abs(float(fa.area_m2) - float(fb.area_m2)) <= AREA_EXACT_ABS
        feats["area_exact"] = (1.0 if exact else 0.0, True)
    else:
        feats["area_rel_diff"] = ABSENT
        feats["area_exact"] = ABSENT
    plot_a = plot_area(la)
    plot_b = plot_area(lb)
    if plot_a is not None and plot_b is not None and not thousands_truncation_suspect(plot_a, plot_b):
        difference = rel_diff(plot_a, plot_b)
        feats["plot_area_rel_diff"] = (difference, True)
        feats["plot_area_exact"] = (1.0 if difference <= PLOT_EXACT_REL else 0.0, True)
    else:
        feats["plot_area_rel_diff"] = ABSENT
        feats["plot_area_exact"] = ABSENT
    feats["dispo_equal"] = _eq(fa.disposition, fb.disposition)
    feats["floor_diff"] = (
        (float(abs(fa.floor - fb.floor)), True)
        if fa.floor is not None and fb.floor is not None
        else ABSENT
    )
    feats["total_floors_equal"] = _eq(fa.total_floors, fb.total_floors)

    skip = vocabulary_attr_keys(settings)
    attrs_a = _attr_map(la, skip)
    attrs_b = _attr_map(lb, skip)
    block_a = _block_of(fa)
    block_b = _block_of(fb)
    agreements = 0.0
    contradictions = 0.0
    rare_agreements = 0.0
    compared = 0
    for key, value_a in attrs_a.items():
        value_b = attrs_b.get(key)
        if value_b is None:
            continue
        compared += 1
        weight = CONFLATED_WEIGHT if key in CONFLATED_ATTR_KEYS else 1.0
        if value_a == value_b:
            agreements += weight
            # K3/K4/K5 pair listings across blocks, so rarity is read on BOTH sides and the
            # conservative min wins: an attribute that is common on either side is not evidence.
            rarity = min(
                ctx.attr_rarity(block_a, key, value_a), ctx.attr_rarity(block_b, key, value_b)
            )
            rare_agreements += weight * rarity
        else:
            contradictions += weight
    present = compared > 0
    feats["attr_agreements"] = (agreements, True) if present else ABSENT
    feats["attr_contradictions"] = (contradictions, True) if present else ABSENT
    feats["attr_agreements_rare"] = (rare_agreements, True) if present else ABSENT

    # --- PRICE ------------------------------------------------------------------------
    if fa.price is not None and fb.price is not None and fa.price > 0 and fb.price > 0:
        low, high = sorted((float(fa.price), float(fb.price)))
        feats["price_last_ratio"] = (low / high, True)
    else:
        feats["price_last_ratio"] = ABSENT
    if (
        fa.price is not None
        and fb.price is not None
        and fa.area_m2
        and fb.area_m2
    ):
        feats["ppm2_rel_diff"] = (
            rel_diff(float(fa.price) / float(fa.area_m2), float(fb.price) / float(fb.area_m2)),
            True,
        )
    else:
        feats["ppm2_rel_diff"] = ABSENT
    events_a = ctx.events(la)
    events_b = ctx.events(lb)
    feats["price_path_event_match"] = (
        (price_path_event_match(events_a, events_b), True) if events_a and events_b else ABSENT
    )

    # --- TXT --------------------------------------------------------------------------
    shingles_a = fa.desc_shingles or set()
    shingles_b = fb.desc_shingles or set()
    if shingles_a and shingles_b:
        shared = len(shingles_a & shingles_b)
        union = len(shingles_a) + len(shingles_b) - shared
        feats["jaccard_shingle"] = (shared / union if union else 0.0, True)
        feats["containment_max"] = (
            max(shared / len(shingles_a), shared / len(shingles_b)),
            True,
        )
    else:
        feats["jaccard_shingle"] = ABSENT
        feats["containment_max"] = ABSENT
    same_block = block_a == block_b
    table = ctx.tfidf if same_block else ctx.tfidf_corpus
    vector_a = table.get(la.id) or {}
    vector_b = table.get(lb.id) or {}
    if vector_a and vector_b:
        small, large = (vector_a, vector_b) if len(vector_a) <= len(vector_b) else (vector_b, vector_a)
        feats["tfidf_cos"] = (
            sum(weight * large[token] for token, weight in small.items() if token in large),
            True,
        )
    else:
        feats["tfidf_cos"] = ABSENT
    feats["simhash_hamming"] = (
        (float(hamming64(fa.desc_simhash, fb.desc_simhash)), True)
        if fa.desc_simhash is not None and fb.desc_simhash is not None
        else ABSENT
    )
    len_a = len(fa.desc_norm or "")
    len_b = len(fb.desc_norm or "")
    feats["len_ratio"] = (
        (min(len_a, len_b) / max(len_a, len_b), True) if len_a and len_b else ABSENT
    )
    # E20: `rare` is empty for a block too small for "df <= 2" to mean rare, and the overlap is
    # capped, so a shared broker template can inform the score without dominating it.
    rare_a = ctx.rare.get(la.id) or set()
    rare_b = ctx.rare.get(lb.id) or set()
    feats["rare_token_overlap"] = (
        (min(float(len(rare_a & rare_b)), RARE_TOKEN_CAP), True) if rare_a and rare_b else ABSENT
    )
    numerals_a = fa.numerals or set()
    numerals_b = fb.numerals or set()
    if numerals_a and numerals_b:
        overlap, conflicts = _numeral_agreement(
            numerals_a, numerals_b, frozenset(settings.numeral_conflict_units)
        )
        feats["numeric_fact_overlap"] = (overlap, True)
        feats["numeral_conflict"] = (1.0 if conflicts > 0 else 0.0, True)
    else:
        feats["numeric_fact_overlap"] = ABSENT
        feats["numeral_conflict"] = ABSENT
    feats["unit_number_shared"] = unit_number_shared(numerals_a, numerals_b)

    # --- BRK --------------------------------------------------------------------------
    feats["same_broker_key"] = _eq(fa.broker_key, fb.broker_key)
    feats["same_broker_identity"] = _eq(fa.broker_identity_id, fb.broker_identity_id)
    feats["broker_known_both"] = (
        1.0 if (fa.broker_key is not None and fb.broker_key is not None) else 0.0,
        True,
    )

    # --- LOC --------------------------------------------------------------------------
    feats["same_ruian_adm_kod"] = _eq(fa.ruian_adm_kod, fb.ruian_adm_kod)
    feats["same_street_key"] = _eq(fa.street_key, fb.street_key)
    feats["same_house_number"] = _eq(fa.house_number, fb.house_number)
    feats["same_psc"] = _eq(fa.psc, fb.psc)
    rank_a = fa.granularity_rank
    rank_b = fb.granularity_rank
    if (
        fa.lat is not None
        and fa.lon is not None
        and fb.lat is not None
        and fb.lon is not None
        and rank_a is not None
        and rank_b is not None
        and rank_a >= STREET_GRAIN_RANK
        and rank_b >= STREET_GRAIN_RANK
    ):
        radius_a = uncertainty_radius_m(rank_a, la.location.uncertainty_radius_m)
        radius_b = uncertainty_radius_m(rank_b, lb.location.uncertainty_radius_m)
        metres = haversine_m(float(fa.lat), float(fa.lon), float(fb.lat), float(fb.lon))
        feats["dist_norm"] = (metres / (radius_a + radius_b + DIST_FLOOR_M), True)
    else:
        feats["dist_norm"] = ABSENT
    feats["same_exact_pin"] = _eq(fa.pin_key, fb.pin_key)
    if fa.pin_key and fb.pin_key:
        population = (
            ctx.pin_pop.get(fa.pin_key, 1)
            if fa.pin_key == fb.pin_key
            else max(ctx.pin_pop.get(fa.pin_key, 1), ctx.pin_pop.get(fb.pin_key, 1))
        )
        feats["pin_pop"] = (math.log1p(float(population)), True)
    else:
        feats["pin_pop"] = ABSENT

    # --- IMG (E9 catalog subtraction has already removed the stock photos) --------------
    gallery_a = ctx.phash_gallery(la.id, images_a, settings)
    gallery_b = ctx.phash_gallery(lb.id, images_b, settings)
    if gallery_a and gallery_b:
        hits, loose = _phash_matches(
            gallery_a, gallery_b, settings.phash_tight, settings.phash_loose
        )
        feats["phash_tight_matches"] = (float(len(hits)), True)
        feats["phash_loose_matches"] = (float(loose), True)
        feats["phash_match_ratio"] = (
            len(hits) / min(len(gallery_a), len(gallery_b)),
            True,
        )
        feats["seq_monotone_ratio"] = _seq_monotone_ratio(hits)
        live_a = {entry[0] for entry in gallery_a}
        live_b = {entry[0] for entry in gallery_b}
        feats["interior_match_ratio"] = _family_ratio(
            hits, fa.interior_image_ids, fb.interior_image_ids, live_a, live_b
        )
        feats["exterior_match_ratio"] = _family_ratio(
            hits, fa.exterior_image_ids, fb.exterior_image_ids, live_a, live_b
        )
        feats["plan_match_ratio"] = _family_ratio(
            hits, fa.plan_image_ids, fb.plan_image_ids, live_a, live_b
        )
    else:
        for name in (
            "phash_tight_matches",
            "phash_loose_matches",
            "phash_match_ratio",
            "seq_monotone_ratio",
            "interior_match_ratio",
            "exterior_match_ratio",
            "plan_match_ratio",
        ):
            feats[name] = ABSENT
    clip_max, clip_top3 = _clip_features(
        ctx.clip_gallery(fa, images_a, settings), ctx.clip_gallery(fb, images_b, settings)
    )
    feats["clip_max_cos"] = clip_max
    feats["clip_mean_top3_cos"] = clip_top3
    feats["catalog_ratio_max"] = (
        (max(float(fa.catalog_ratio), float(fb.catalog_ratio)), True)
        if fa.catalog_ratio is not None and fb.catalog_ratio is not None
        else ABSENT
    )
    # E9 subtracts catalogue stock BEFORE any image feature, this one included.
    feats["n_images_min"] = (
        float(min(
            ctx.non_catalog_count(la.id, images_a, settings),
            ctx.non_catalog_count(lb.id, images_b, settings),
        )),
        True,
    )

    # --- TIME -------------------------------------------------------------------------
    start_a, end_a = _window(la)
    start_b, end_b = _window(lb)
    if start_a is not None and start_b is not None:
        first_end = end_a if start_a <= start_b else end_b
        later_start = max(start_a, start_b)
        gap = 0.0 if first_end is None else max(0.0, later_start - first_end)
        feats["gap_days"] = (gap, True)
    else:
        feats["gap_days"] = ABSENT
    if None not in (start_a, end_a, start_b, end_b):
        overlap = min(float(end_a), float(end_b)) - max(float(start_a), float(start_b))
        feats["overlap_days"] = (max(0.0, overlap), True)
    else:
        feats["overlap_days"] = ABSENT
    feats["both_active"] = (1.0 if (la.is_active and lb.is_active) else 0.0, True)
    feats["same_source"] = _eq(fa.source, fb.source)
    return feats


def _rule_holds(feats: Feats, conditions: Sequence[Condition]) -> bool:
    for name, low, high in conditions:
        value, present = feats.get(name, ABSENT)
        if not present or not low <= value <= high:
            return False
    return True


def evidence_families(feats: Feats) -> set[str]:
    """E11: the families carrying at least one PRESENT, unit-grade corroborating signal."""
    found: set[str] = set()
    for family in EVIDENCE_FAMILIES:
        if any(_rule_holds(feats, rule) for rule in EVIDENCE_RULES[family]):
            found.add(family)
    return found
