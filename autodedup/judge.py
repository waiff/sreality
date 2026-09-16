"""The LLM judge's core: digests in, one forced-tool verdict out (PROGRAM.md E26-E32).

Nothing here calls a provider, reads the database or touches the network — this module only
BUILDS what crosses the wire and VALIDATES what comes back, so every prompt shape is testable
offline and the paid lane (`autodedup.judge_lane`) stays a thin driver.

Three contracts it holds on behalf of that lane:
  * E28 — no broker name, phone or e-mail is ever rendered. Brokers reach the model only as
    the `same_broker` boolean of the evidence digest, and every description goes through
    `scrub_for_prompt` here even though `autodedup.export` already scrubbed it into the
    artifact: the export pass only catches a name that FOLLOWS its role word, so this module
    adds the name-then-role and contact-lead forms (`Jan Novák, realitní makléř`, `Volejte
    Janu Novákovou`) that are the commonest Czech advert signature. The gold tier ships these
    digests to DashScope, a processor this corpus has never used, so the leak would be
    permanent.
  * E27 — the four-way verdict with a REQUIRED `unit_discriminator` on every non-same answer,
    validated caller-side rather than hoped for.
  * E9/E10 — image selection strips catalogue stock first and labels plan/exterior frames as
    building-level evidence, so a shared facade can never be read as unit identity.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from autodedup import judge_prompts as prompts
from autodedup.dataset import CATALOG_POP_MIN, Image, Listing, cosine_norm
from autodedup.export import NAME_TOKEN, scrub_description
from autodedup.fingerprint import dominant_family, family_scores

JUDGE_VERSION: str = "j1"
TOOL_NAME: str = "record_pair_verdict"
SYSTEM_PROMPT: str = prompts.SYSTEM_PROMPT

VERDICTS: tuple[str, ...] = (
    "same_property",
    "different_property",
    "same_building_different_unit",
    "insufficient_evidence",
)
TIERS: tuple[str, ...] = ("text", "vision", "gold")
STRATEGIES: tuple[str, ...] = ("matched_first", "sequence_first")

DESCRIPTION_MAX_CHARS: int = 1200
MAX_PRICE_POINTS: int = 8
DEFAULT_IMAGES_PER_SIDE: int = 4
GOLD_IMAGES_PER_SIDE: int = 6
GOLD_MAJORITY_CONFIDENCE: float = 0.67
CATALOG_WARN_RATIO: float = 0.80
PHASH_PAIR_MAX: int = 11
CLIP_PAIR_MIN: float = 0.90
RARE_TOKEN_CAP: float = 5.0
PIN_POP_WARN_COUNT: float = 5.0
MISSING_DISCRIMINATOR: str = "no discriminator named for a non-same verdict"

# Families that describe the building rather than the unit: a matched frame from one of these
# is worth nothing about identity, so it must never displace an interior frame (E10).
BUILDING_FAMILIES: frozenset[str] = frozenset({"exterior", "common", "plan"})

# `attrs` slots worth stating per side: the unit-discriminating ones first, then the
# building-level ones the prompt tells the model to weigh second.
DIGEST_ATTRS: tuple[tuple[str, str], ...] = (
    ("area_basis", "area basis"),
    ("usable_area", "usable area"),
    ("estate_area", "plot area"),
    ("garden_area", "garden area"),
    ("has_balcony", "balcony"),
    ("terrace", "terrace"),
    ("cellar", "cellar"),
    ("garage", "garage"),
    ("has_parking", "parking"),
    ("parking_lots", "parking spaces"),
    ("has_lift", "lift"),
    ("furnished", "furnishing"),
    ("ownership", "ownership"),
    ("condition", "condition"),
    ("energy_rating", "energy rating"),
    ("building_type", "building type"),
    ("published_at", "published by the portal"),
)

# Rendered as a day, not as a raw timestamp.
DAY_ATTRS: frozenset[str] = frozenset({"published_at"})

# Rendered WITH the unit: a bare `usable area: 136.0` beside `area: 136 m²` reads as two
# measurements in two units, which is the one ambiguity the area evidence cannot afford.
AREA_ATTRS: frozenset[str] = frozenset({"usable_area", "estate_area", "garden_area"})

TOOL_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Record the identity verdict for one pair of Czech property adverts. Call exactly once."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {
                "type": "string",
                "enum": list(VERDICTS),
                "description": (
                    "same_property = one and the same physical unit; different_property = two "
                    "different units; same_building_different_unit = the same building or "
                    "project but different units in it; insufficient_evidence = the evidence "
                    "does not decide."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Your confidence in the verdict, 0 to 1.",
            },
            "deal_or_category_conflict": {
                "type": "boolean",
                "description": (
                    "True when the pair mixes prodej with pronájem, or byt with komerční / dům "
                    "/ pozemek. Such a pair is always different_property."
                ),
            },
            "unit_discriminator": {
                "type": "string",
                "description": (
                    "The single fact that decides the pair, in a few words (e.g. 'floor 2 vs "
                    "floor 5'). REQUIRED for every verdict except same_property."
                ),
            },
            "key_evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short English phrases, quoting numbers, that drove the verdict.",
            },
            "contradicting_evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short English phrases pointing the other way.",
            },
            "developer_project_suspected": {
                "type": "boolean",
                "description": (
                    "True when the pair looks like one developer project's shared marketing: "
                    "identical template text, shared photos, sequential adverts."
                ),
            },
        },
        "required": [
            "verdict",
            "confidence",
            "deal_or_category_conflict",
            "key_evidence",
            "contradicting_evidence",
            "developer_project_suspected",
        ],
    },
}


class JudgeParseError(ValueError):
    """The model's tool call did not satisfy the E27 contract."""


# --- listing digest ------------------------------------------------------------------------


@dataclass(slots=True)
class ListingDigest:
    listing_id: int
    portal: str | None = None
    deal: str | None = None
    category: str | None = None
    subtype: str | None = None
    disposition: str | None = None
    area_m2: float | None = None
    floor: int | None = None
    total_floors: int | None = None
    price: float | None = None
    price_unit: str | None = None
    price_history: list[tuple[str, float | None]] = field(default_factory=list)
    attributes: dict[str, str | None] = field(default_factory=dict)
    first_seen: str | None = None
    last_seen: str | None = None
    active: bool = True
    description: str | None = None
    description_truncated: bool = False
    absent: list[str] = field(default_factory=list)


_NAME_WORD = r"[A-ZÁ-Ž][a-zá-ž]+"
# export.scrub_description only catches ROLE-then-name. These two catch the other direction and
# the imperative lead-ins, which is how a Czech advert actually signs off.
_NAME_THEN_ROLE_RE = re.compile(
    rf"\b{_NAME_WORD}\s+{_NAME_WORD}"
    r"(?=\s*[,\-–—]?\s*(?:[Rr]ealitní\s+)?(?:[Mm]akléř|[Mm]aklér|[Ss]pecialist)(?:ka|a)?\b)"
)
_CONTACT_LEAD_RE = re.compile(
    r"(?:Kontaktn[ií]\s+osoba|Kontaktujte|Kontakt|[Vv]olejte|[Zz]avolejte|[Dd]omlouvá"
    r"|[Ii]nformace\s+(?:u|podá))"
    rf"\s*[:\-–]?\s*({_NAME_WORD}\s+{_NAME_WORD})"
)


def _scrub_names(text: str) -> str:
    cleaned = _NAME_THEN_ROLE_RE.sub(NAME_TOKEN, text)
    return _CONTACT_LEAD_RE.sub(
        lambda match: match.group(0).replace(match.group(1), NAME_TOKEN), cleaned
    )


def scrub_for_prompt(text: str | None) -> str | None:
    """E28's second layer: `autodedup.export`'s scrub, then the name forms it does not cover."""
    cleaned = scrub_description(text)
    return _scrub_names(cleaned) if cleaned else cleaned


def _clean(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None if value is None else ("yes" if value else "no")
    text = str(value).strip()
    return text or None


def _day(value: str | None) -> str | None:
    text = _clean(value)
    return text[:10] if text else None


def _amount(value: float | None) -> str:
    if value is None:
        return prompts.ABSENT_TOKEN
    return f"{value:,.0f}".replace(",", " ")


def _area(value: float | None) -> str:
    return prompts.ABSENT_TOKEN if value is None else f"{value:g} m²"


def _attr_text(key: str, raw: Any) -> str | None:
    if raw is None:
        return None
    if key in DAY_ATTRS:
        return _day(raw)
    if key in AREA_ATTRS:
        try:
            return _area(float(raw))
        except (TypeError, ValueError):
            return _clean(raw)
    return _clean(raw)


def listing_digest(listing: Listing) -> ListingDigest:
    """One side of the pair, PII-free: no broker field of any kind reaches this record."""
    attrs = listing.attrs or {}
    attributes = {label: _attr_text(key, attrs.get(key)) for key, label in DIGEST_ATTRS}
    scrubbed = scrub_for_prompt(listing.description)
    truncated = bool(scrubbed) and len(scrubbed or "") > DESCRIPTION_MAX_CHARS
    description = (scrubbed or "")[:DESCRIPTION_MAX_CHARS] or None

    digest = ListingDigest(
        listing_id=listing.id,
        portal=_clean(listing.source),
        deal=_clean(listing.category_type),
        category=_clean(listing.category_main),
        subtype=_clean(listing.subtype),
        disposition=_clean(listing.disposition),
        area_m2=listing.area_m2,
        floor=listing.floor,
        total_floors=listing.total_floors,
        price=listing.price,
        price_unit=_clean(attrs.get("price_unit")),
        price_history=list(listing.price_history or [])[-MAX_PRICE_POINTS:],
        attributes=attributes,
        first_seen=_day(listing.first_seen_at),
        last_seen=_day(listing.inactive_at or listing.last_seen_at),
        active=bool(listing.is_active),
        description=description,
        description_truncated=truncated,
    )

    absent: list[str] = []
    for name, value in (
        ("disposition", digest.disposition),
        ("area", digest.area_m2),
        ("floor", digest.floor),
        ("total floors", digest.total_floors),
        ("price", digest.price),
        ("description", digest.description),
        ("first seen", digest.first_seen),
        ("last seen", digest.last_seen),
    ):
        if value is None:
            absent.append(name)
    absent.extend(label for label, value in attributes.items() if value is None)
    digest.absent = absent
    return digest


def render_digest(d: ListingDigest) -> str:
    """The per-side text block. Every unknown field is NAMED as absent (E12 / prompt rule)."""
    lines: list[str] = [
        f"listing id: {d.listing_id}",
        f"portal: {d.portal or prompts.ABSENT_TOKEN}",
        f"deal type: {d.deal or prompts.ABSENT_TOKEN}",
        f"category: {d.category or prompts.ABSENT_TOKEN}"
        + (f" / {d.subtype}" if d.subtype else ""),
        f"disposition: {d.disposition or prompts.ABSENT_TOKEN}",
        f"area: {_area(d.area_m2)}",
        f"floor: {prompts.ABSENT_TOKEN if d.floor is None else d.floor}"
        f" of {prompts.ABSENT_TOKEN if d.total_floors is None else d.total_floors}",
        f"price: {_amount(d.price)}"
        + (f" CZK {d.price_unit}" if d.price_unit else (" CZK" if d.price is not None else "")),
    ]
    if d.price_history:
        points = "; ".join(
            f"{_day(stamp) or '?'}: {_amount(value)}" for stamp, value in d.price_history
        )
        lines.append(f"price path (oldest first, at most {MAX_PRICE_POINTS} points): {points}")
    else:
        lines.append(f"price path: {prompts.ABSENT_TOKEN}")
    for label, value in d.attributes.items():
        lines.append(f"{label}: {value or prompts.ABSENT_TOKEN}")
    lines.append(
        prompts.OBSERVED_WINDOW.format(
            first=d.first_seen or prompts.ABSENT_TOKEN,
            last=d.last_seen or prompts.ABSENT_TOKEN,
            active="yes" if d.active else "no",
        )
    )
    lines.append(
        prompts.ABSENT_SUMMARY.format(names=", ".join(d.absent))
        if d.absent
        else prompts.NO_ABSENT_SUMMARY
    )
    suffix = prompts.TRUNCATED_SUFFIX if d.description_truncated else ""
    lines.append(prompts.DESCRIPTION_HEADER.format(truncated=suffix))
    lines.append(d.description or prompts.ABSENT_TOKEN)
    return "\n".join(lines)


# --- evidence digest -----------------------------------------------------------------------


def _value(feats: Mapping[str, tuple[float, bool]], name: str) -> float | None:
    entry = feats.get(name)
    if entry is None:
        return None
    value, present = entry
    return float(value) if present else None


def _flag(feats: Mapping[str, tuple[float, bool]], name: str) -> str:
    value = _value(feats, name)
    if value is None:
        return "unknown on at least one side"
    return "yes" if value >= 0.5 else "no"


def _num(value: float | None, suffix: str = "", digits: int = 2) -> str:
    if value is None:
        return prompts.NOT_MEASURED
    return f"{value:.{digits}f}{suffix}"


def _short(value: float | None, digits: int = 2) -> str:
    """The absent marker for a value nested inside a parenthetical, where the full sentence
    would repeat itself three times on one line."""
    if value is None:
        return prompts.NOT_MEASURED_SHORT
    return f"{value:.{digits}f}"


def _pct(value: float | None) -> str:
    return prompts.NOT_MEASURED if value is None else f"{value * 100.0:.2f}%"


def evidence_digest(
    feats: Mapping[str, tuple[float, bool]],
    probes: Iterable[str] = (),
    families: Iterable[str] = (),
    block: str | None = None,
    *,
    attr_conflicts: Sequence[str] = (),
    distance_m: float | None = None,
) -> str:
    """The engine's own numbers, stated as facts so the model reasons over them (spec 3a).

    `attr_conflicts` and `distance_m` are optional because the feature vector carries counts,
    not names: the lane passes the contradicting FIELD NAMES and the raw metres when it has
    them, and the digest degrades to the counts when it does not."""
    family_list = sorted(set(families))
    area_pct = _value(feats, "area_rel_diff")
    distance = _value(feats, "dist_norm")
    catalog = _value(feats, "catalog_ratio_max")

    floor_diff = _value(feats, "floor_diff")

    lines: list[str] = [
        f"block: {block or 'unknown'}",
        f"retrieval probes that produced this pair: {', '.join(sorted(set(probes))) or 'none'}",
        "",
        "ATTRIBUTES",
        f"- area difference: {_pct(area_pct)}"
        + (f"  ({prompts.AREA_BASIS_NOTE})" if area_pct is not None else ""),
        f"- areas identical to within 1%: {_flag(feats, 'area_exact')}",
        f"- dispositions agree: {_flag(feats, 'dispo_equal')}",
        f"- floor difference: {_num(floor_diff, digits=0)}"
        + (f"  ({prompts.FLOOR_CONVENTION_NOTE})" if floor_diff is not None else ""),
        f"- total floors agree: {_flag(feats, 'total_floors_equal')}",
        f"- attribute agreements: {_num(_value(feats, 'attr_agreements'), digits=1)}"
        f", contradictions: {_num(_value(feats, 'attr_contradictions'), digits=1)}"
        + (f" (contradicting fields: {', '.join(attr_conflicts)})" if attr_conflicts else ""),
        "",
        "PRICE",
        f"- smaller price / larger price: {_num(_value(feats, 'price_last_ratio'))}"
        + (prompts.PRICE_RATIO_NOTE if _value(feats, "price_last_ratio") is not None else ""),
        f"- price per m2 difference: {_pct(_value(feats, 'ppm2_rel_diff'))}",
        f"- share of price-CHANGE events matched within 3 days and 1%: "
        f"{_num(_value(feats, 'price_path_event_match'))}",
        "",
        "TEXT",
        f"- text containment (larger side covers smaller): "
        f"{_num(_value(feats, 'containment_max'))}",
        f"- tf-idf cosine: {_num(_value(feats, 'tfidf_cos'))}",
        f"- rare-token overlap (tokens rare in this block, i.e. unit-specific wording): "
        f"{_rare_tokens(_value(feats, 'rare_token_overlap'))}",
        f"- numeric facts shared: {_num(_value(feats, 'numeric_fact_overlap'), digits=1)}"
        f", numeral conflict: {_flag(feats, 'numeral_conflict')}",
        "",
        "PHOTOGRAPHS (catalogue/stock photos already subtracted)",
    ]
    tight = _value(feats, "phash_tight_matches")
    clip_max = _value(feats, "clip_max_cos")
    if tight is None and clip_max is None:
        # features.py drops all seven match features together when either non-catalogue gallery
        # is empty, so this is ONE fact, not eight repetitions of the absent marker — and on the
        # all-catalogue developer pairs the catalogue lines below are the whole story.
        lines.append(prompts.PHOTOS_NOT_COMPARABLE)
        warning = prompts.ALL_CATALOG_WARNING
    else:
        lines.extend([
            f"- exact photo matches: {_num(tight, digits=0)}"
            f" (near matches {_short(_value(feats, 'phash_loose_matches'), 0)},"
            f" share of the smaller gallery {_short(_value(feats, 'phash_match_ratio'))})",
            f"- matched photos in preserved gallery order: "
            f"{_num(_value(feats, 'seq_monotone_ratio'))}",
            f"- share of the smaller side's INTERIOR photos that matched (unit-level "
            f"evidence): {_num(_value(feats, 'interior_match_ratio'))}",
            f"- share of the smaller side's EXTERIOR photos that matched (building-level "
            f"evidence only): {_num(_value(feats, 'exterior_match_ratio'))}",
            f"- share of the smaller side's PLAN photos that matched (building-level evidence "
            f"only): {_num(_value(feats, 'plan_match_ratio'))}",
            f"- best CLIP similarity: {_num(clip_max)}"
            f" (mean of top 3 {_short(_value(feats, 'clip_mean_top3_cos'))})",
        ])
        warning = prompts.CATALOG_WARNING
    lines.append(
        f"- catalogue share of the MORE catalogue-heavy of the two galleries: {_num(catalog)}"
    )
    lines.append(
        f"- non-catalogue photos on the smaller side: "
        f"{_num(_value(feats, 'n_images_min'), digits=0)}"
    )
    if catalog is not None and catalog >= CATALOG_WARN_RATIO:
        lines.append(f"- {warning}")

    lines.extend([
        "",
        "LOCATION",
        f"- same RUIAN address point: {_flag(feats, 'same_ruian_adm_kod')}",
        f"- same street: {_flag(feats, 'same_street_key')}"
        f", same house number: {_flag(feats, 'same_house_number')}"
        f", same postcode: {_flag(feats, 'same_psc')}",
    ])
    if distance is None:
        lines.append(f"- {prompts.DISTANCE_NOT_COMPARABLE}")
    else:
        metres = f" ({distance_m:.0f} m apart)" if distance_m is not None else ""
        lines.append(
            f"- pin distance normalised by both pins' uncertainty radii: {_num(distance)}"
            f"{metres}, same exact pin: {_flag(feats, 'same_exact_pin')}"
        )
    pin_pop = _value(feats, "pin_pop")
    if pin_pop is not None:
        population = math.expm1(pin_pop)
        lines.append(
            f"- listings sharing this exact address pin: about {population:.0f}"
            + (f" {prompts.PIN_POP_WARNING}" if population >= PIN_POP_WARN_COUNT else "")
        )

    lines.extend([
        "",
        "BROKER AND PORTAL",
        f"- same broker: {_flag(feats, 'same_broker_key')}"
        f" (same resolved broker identity: {_flag(feats, 'same_broker_identity')})",
        f"- same portal: {_flag(feats, 'same_source')}",
        "",
        "TIME",
        f"- gap between the two advert windows: "
        f"{_num(_value(feats, 'gap_days'), ' days', digits=0)}",
        f"- days the two adverts were live AT THE SAME TIME: "
        f"{_num(_value(feats, 'overlap_days'), ' days', digits=0)}",
        f"- both adverts currently active: {_flag(feats, 'both_active')}",
    ])
    if _value(feats, "gap_days") is not None or _value(feats, "overlap_days") is not None:
        lines.append(prompts.CRAWL_WINDOW_NOTE)

    lines.extend([
        "",
        f"independent evidence families corroborating ({len(family_list)} of 5): "
        f"{', '.join(family_list) or 'none'}",
        prompts.FAMILY_COUNT_NOTE,
    ])
    return "\n".join(lines)


def _rare_tokens(value: float | None) -> str:
    if value is None:
        return prompts.NOT_MEASURED
    return f"{value:.0f}" + (prompts.RARE_TOKEN_CAP_NOTE if value >= RARE_TOKEN_CAP else "")


# --- image selection -----------------------------------------------------------------------


def _order_key(image: Image) -> tuple[int, int]:
    return (image.seq if image.seq is not None else 10**6, image.image_id)


def _non_catalog(images: Sequence[Image], catalog_pop_min: int) -> list[Image]:
    return sorted(
        (img for img in images if not img.is_catalog_candidate(catalog_pop_min)), key=_order_key
    )


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _pair_rank(a: Image, b: Image) -> int:
    """A matched facade or site plan is the developer signature, not unit identity, so it may
    only take the first slot when no interior pair matched at all (E10 / spec 3c)."""
    unit_level = (
        dominant_family(a) not in BUILDING_FAMILIES
        and dominant_family(b) not in BUILDING_FAMILIES
    )
    return 0 if unit_level else 1


def _best_phash_pair(
    left: Sequence[Image], right: Sequence[Image]
) -> tuple[Image, Image] | None:
    best: tuple[int, int, tuple[int, int], tuple[int, int], Image, Image] | None = None
    for a in left:
        if a.phash is None:
            continue
        for b in right:
            if b.phash is None:
                continue
            distance = _hamming(a.phash, b.phash)
            if distance > PHASH_PAIR_MAX:
                continue
            candidate = (_pair_rank(a, b), distance, _order_key(a), _order_key(b), a, b)
            if best is None or candidate[:4] < best[:4]:
                best = candidate
    return (best[4], best[5]) if best else None


def _best_clip_pair(
    left: Sequence[Image], right: Sequence[Image]
) -> tuple[Image, Image] | None:
    best: tuple[int, float, tuple[int, int], tuple[int, int], Image, Image] | None = None
    for a in left:
        vector_a, norm_a = a.clip_vector(), a.clip_norm()
        if vector_a is None or not norm_a:
            continue
        for b in right:
            vector_b, norm_b = b.clip_vector(), b.clip_norm()
            if vector_b is None or not norm_b:
                continue
            similarity = cosine_norm(vector_a, vector_b, norm_a, norm_b)
            if similarity < CLIP_PAIR_MIN:
                continue
            candidate = (
                _pair_rank(a, b), -similarity, _order_key(a), _order_key(b), a, b
            )
            if best is None or candidate[:4] < best[:4]:
                best = candidate
    return (best[4], best[5]) if best else None


def _interior_score(image: Image) -> float:
    return family_scores(image).get("interior", -1.0)


def _fill_order(images: Sequence[Image], chosen: set[int]) -> list[Image]:
    """Interior frames first; plan/exterior/common only once no interior frame is left."""
    remaining = [img for img in images if img.image_id not in chosen]
    interior = [img for img in remaining if dominant_family(img) == "interior"]
    other = [img for img in remaining if dominant_family(img) in ("other", "unknown")]
    building = [img for img in remaining if dominant_family(img) in BUILDING_FAMILIES]
    return interior + other + building


def select_images(
    listing_a: Listing,
    images_a: Sequence[Image],
    listing_b: Listing,
    images_b: Sequence[Image],
    feats: Mapping[str, tuple[float, bool]] | None = None,
    n_per_side: int = DEFAULT_IMAGES_PER_SIDE,
    strategy: str = "matched_first",
    *,
    catalog_pop_min: int = CATALOG_POP_MIN,
) -> tuple[list[Image], list[Image]]:
    """Deterministic, evidence-driven selection (spec 3c). `feats` is part of the contract and
    deliberately unused: the two selections must be reproducible from the galleries alone, so a
    re-judge at a different feature version picks the same frames."""
    del feats, listing_a, listing_b
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; expected one of {STRATEGIES}")
    if n_per_side < 1:
        raise ValueError(f"n_per_side must be at least 1: {n_per_side}")

    left = _non_catalog(images_a, catalog_pop_min)
    right = _non_catalog(images_b, catalog_pop_min)
    if strategy == "sequence_first":
        # Gallery order INSIDE each family band: a genuinely different view from matched_first,
        # without spending gold-tier slots on the marketing stock the prompt calls worthless.
        return (
            _fill_order(left, set())[:n_per_side],
            _fill_order(right, set())[:n_per_side],
        )

    picked_a: list[Image] = []
    picked_b: list[Image] = []
    chosen_a: set[int] = set()
    chosen_b: set[int] = set()

    def take(a: Image | None, b: Image | None) -> None:
        if a is not None and a.image_id not in chosen_a and len(picked_a) < n_per_side:
            picked_a.append(a)
            chosen_a.add(a.image_id)
        if b is not None and b.image_id not in chosen_b and len(picked_b) < n_per_side:
            picked_b.append(b)
            chosen_b.add(b.image_id)

    phash_pair = _best_phash_pair(left, right)
    if phash_pair:
        take(*phash_pair)
    clip_pair = _best_clip_pair(
        [img for img in left if img.image_id not in chosen_a],
        [img for img in right if img.image_id not in chosen_b],
    )
    if clip_pair:
        take(*clip_pair)

    unmatched_a = sorted(
        (img for img in left if img.image_id not in chosen_a),
        key=lambda img: (-_interior_score(img), _order_key(img)),
    )
    unmatched_b = sorted(
        (img for img in right if img.image_id not in chosen_b),
        key=lambda img: (-_interior_score(img), _order_key(img)),
    )
    take(
        unmatched_a[0] if unmatched_a and _interior_score(unmatched_a[0]) >= 0.0 else None,
        unmatched_b[0] if unmatched_b and _interior_score(unmatched_b[0]) >= 0.0 else None,
    )

    for side, picked, chosen in ((left, picked_a, chosen_a), (right, picked_b, chosen_b)):
        cover = next((img for img in side if img.seq == 0), None)
        if cover is not None and cover.image_id not in chosen and len(picked) < n_per_side:
            picked.append(cover)
            chosen.add(cover.image_id)
        for image in _fill_order(side, chosen):
            if len(picked) >= n_per_side:
                break
            picked.append(image)
            chosen.add(image.image_id)

    return picked_a[:n_per_side], picked_b[:n_per_side]


_FAMILY_LABELS: dict[str, str] = {
    "interior": prompts.INTERIOR_LABEL,
    "exterior": prompts.EXTERIOR_LABEL,
    "common": prompts.COMMON_LABEL,
    "plan": prompts.PLAN_LABEL,
}


def image_caption(image: Image, side: str, index: int) -> str:
    """`A-1 interior photo (kitchen), gallery position 2` — plan/exterior frames say what they
    are worth, because an unlabelled facade is exactly how a building becomes a 'unit match'."""
    family = dominant_family(image)
    label = _FAMILY_LABELS.get(family, prompts.UNCLASSIFIED_LABEL)
    tag = max(image.tags, key=lambda entry: (entry[1] or 0.0))[0] if image.tags else None
    position = image.seq if image.seq is not None else "?"
    return (
        f"{side}-{index} {label}"
        + (f" ({tag})" if tag else "")
        + f", gallery position {position}"
    )


def image_captions(images: Sequence[Image], side: str) -> list[str]:
    return [image_caption(image, side, i + 1) for i, image in enumerate(images)]


# --- messages ------------------------------------------------------------------------------


def _blocks_with_captions(
    entries: Sequence[Any], side: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        if isinstance(entry, tuple):
            caption, block = entry
            # The lane may reorder the blocks (the shuffled gold vote), so the index is stamped
            # here rather than taken on trust from a caption built before the shuffle.
            text = f"{side}-{index} " + re.sub(rf"^{side}-\d+\s*", "", caption)
        else:
            block, text = entry, f"{side}-{index}"
        out.append({"type": "text", "text": text})
        out.append(dict(block))
    return out


def build_messages(
    pair: Mapping[str, Any],
    digests: tuple[ListingDigest | str, ListingDigest | str],
    evidence: str,
    image_blocks_a: Sequence[Any] = (),
    image_blocks_b: Sequence[Any] = (),
    tier: str = "text",
) -> list[dict[str, Any]]:
    """One user message in the legacy dict shape `LLMClient.call` accepts; the system prompt
    travels separately as `system=SYSTEM_PROMPT`.

    A text-tier call with images raises rather than silently dropping them: paid image work
    that never reaches the model is the failure shape E31 exists to prevent. A vision-tier call
    with NO images says so in words: the all-catalogue developer pairs are exactly the class the
    judge has to separate, and an unexplained absence reads as "there were no photos"."""
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of {TIERS}")
    if tier == "text" and (image_blocks_a or image_blocks_b):
        raise ValueError("tier 'text' takes no images")

    left, right = digests
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": prompts.PAIR_HEADER.format(lo=pair.get("lo"), hi=pair.get("hi")),
        },
        {
            "type": "text",
            "text": prompts.SIDE_HEADER.format(side="A")
            + "\n"
            + (left if isinstance(left, str) else render_digest(left)),
        },
    ]
    content.extend(_blocks_with_captions(image_blocks_a, "A"))
    if tier != "text" and not image_blocks_a:
        content.append({"type": "text", "text": prompts.NO_IMAGES_NOTE.format(side="A")})
    content.append(
        {
            "type": "text",
            "text": prompts.SIDE_HEADER.format(side="B")
            + "\n"
            + (right if isinstance(right, str) else render_digest(right)),
        }
    )
    content.extend(_blocks_with_captions(image_blocks_b, "B"))
    if tier != "text" and not image_blocks_b:
        content.append({"type": "text", "text": prompts.NO_IMAGES_NOTE.format(side="B")})
    content.append({"type": "text", "text": prompts.EVIDENCE_HEADER + "\n" + evidence})
    coda = prompts.TIER_CODA.get(tier)
    if coda:
        content.append({"type": "text", "text": coda})
    content.append({"type": "text", "text": prompts.TASK_INSTRUCTION})
    return [{"role": "user", "content": content}]


# --- verdicts ------------------------------------------------------------------------------


@dataclass(slots=True)
class Verdict:
    verdict: str
    confidence: float
    deal_or_category_conflict: bool = False
    unit_discriminator: str | None = None
    key_evidence: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)
    developer_project_suspected: bool = False
    downgraded_from: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "deal_or_category_conflict": self.deal_or_category_conflict,
            "unit_discriminator": self.unit_discriminator,
            "key_evidence": list(self.key_evidence),
            "contradicting_evidence": list(self.contradicting_evidence),
            "developer_project_suspected": self.developer_project_suspected,
            "downgraded_from": self.downgraded_from,
        }


def _string_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise JudgeParseError(f"{name} must be a list of strings, got {type(value).__name__}")
    return [str(entry).strip() for entry in value if str(entry).strip()]


def parse_verdict(tool_call_args: Mapping[str, Any] | str) -> Verdict:
    """Validate one tool call against E27, applying the caller-side downgrades the spec names:
    a same_property with no evidence is insufficient_evidence, a flagged deal or category
    conflict can never be same_property (E2/E3 forbid the merge outright), and a non-same
    verdict with no `unit_discriminator` becomes insufficient_evidence — never an exception,
    because discarding a call that has already been billed is the E31 failure shape."""
    if isinstance(tool_call_args, str):
        try:
            payload: Any = json.loads(tool_call_args)
        except json.JSONDecodeError as exc:
            raise JudgeParseError(f"tool call is not JSON: {exc}") from exc
    else:
        payload = tool_call_args
    if not isinstance(payload, Mapping):
        raise JudgeParseError(f"tool call must be an object, got {type(payload).__name__}")

    verdict = payload.get("verdict")
    if verdict not in VERDICTS:
        raise JudgeParseError(f"verdict must be one of {VERDICTS}, got {verdict!r}")

    raw_confidence = payload.get("confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        raise JudgeParseError(f"confidence must be a number, got {raw_confidence!r}")
    confidence = float(raw_confidence)
    if not 0.0 <= confidence <= 1.0:
        raise JudgeParseError(f"confidence must be within [0, 1], got {confidence}")

    discriminator = payload.get("unit_discriminator")
    discriminator = str(discriminator).strip() if discriminator is not None else ""

    key_evidence = _string_list(payload.get("key_evidence"), "key_evidence")
    contradicting = _string_list(payload.get("contradicting_evidence"), "contradicting_evidence")
    conflict = bool(payload.get("deal_or_category_conflict", False))

    downgraded_from: str | None = None
    if verdict == "same_property" and conflict:
        downgraded_from, verdict = verdict, "different_property"
        discriminator = discriminator or "deal or category conflict"
    elif verdict == "same_property" and not key_evidence:
        downgraded_from, verdict = verdict, "insufficient_evidence"
        discriminator = discriminator or "no evidence given for same_property"
    elif verdict != "same_property" and not discriminator:
        discriminator = MISSING_DISCRIMINATOR
        if verdict != "insufficient_evidence":
            downgraded_from, verdict = verdict, "insufficient_evidence"

    return Verdict(
        verdict=verdict,
        confidence=confidence,
        deal_or_category_conflict=conflict,
        unit_discriminator=discriminator or None,
        key_evidence=key_evidence,
        contradicting_evidence=contradicting,
        developer_project_suspected=bool(payload.get("developer_project_suspected", False)),
        downgraded_from=downgraded_from,
    )


@dataclass(slots=True)
class GoldVerdict:
    verdict: str
    confidence: float
    unanimous: bool
    flagged: bool
    votes: list[str] = field(default_factory=list)
    unit_discriminator: str | None = None
    developer_project_suspected: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "unanimous": self.unanimous,
            "flagged": self.flagged,
            "votes": list(self.votes),
            "unit_discriminator": self.unit_discriminator,
            "developer_project_suspected": self.developer_project_suspected,
        }


def aggregate_gold(votes: Sequence[Verdict]) -> GoldVerdict:
    """Unanimous -> the label at confidence 1.0; a strict majority -> the label at 0.67,
    FLAGGED; no majority -> insufficient_evidence. The split case is a finding about the task
    (spec §9 iii), never a coin toss."""
    if not votes:
        raise JudgeParseError("aggregate_gold needs at least one vote")
    labels = [vote.verdict for vote in votes]
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    top = max(sorted(counts), key=lambda label: counts[label])
    suspected = any(vote.developer_project_suspected for vote in votes)

    if counts[top] == len(labels):
        winners = [vote for vote in votes if vote.verdict == top]
        return GoldVerdict(
            verdict=top,
            confidence=1.0,
            unanimous=True,
            flagged=False,
            votes=labels,
            unit_discriminator=_pick_discriminator(winners),
            developer_project_suspected=suspected,
        )
    if counts[top] * 2 > len(labels):
        winners = [vote for vote in votes if vote.verdict == top]
        return GoldVerdict(
            verdict=top,
            confidence=GOLD_MAJORITY_CONFIDENCE,
            unanimous=False,
            flagged=True,
            votes=labels,
            unit_discriminator=_pick_discriminator(winners),
            developer_project_suspected=suspected,
        )
    return GoldVerdict(
        verdict="insufficient_evidence",
        confidence=0.0,
        unanimous=False,
        flagged=True,
        votes=labels,
        unit_discriminator=None,
        developer_project_suspected=suspected,
    )


def _pick_discriminator(votes: Sequence[Verdict]) -> str | None:
    for vote in sorted(votes, key=lambda v: -v.confidence):
        if vote.unit_discriminator:
            return vote.unit_discriminator
    return None
