"""The lab's CI fixture cohort: the engine e2e cohort (synthetic adverts, PROGRAM.md 2's six shapes)
plus synthetic pairs that reach the branches it does not, so `lab verify` in CI reads every branch of
`decide_pair` the lab restates. No real advert is in here.

Retrieval drops a walled pair before it is ever decided (`BlockIndex._guarded`), so the one path
never writes one; `with_wall_pairs` adds the five walls to the artefact as the ENGINE decides them
(`decide_pair`), which is what the lab's veto rung must reproduce. `EXERCISE` holds the settings
rows that push the fixture's pairs through the branches w31 leaves dark."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Callable

from autodedup.decide import decide_pair
from autodedup.incremental import PairRow
from tests.autodedup.test_engine_e2e import (
    BLOCK_A,
    _gallery,
    _listing,
    _location,
    _spread_hashes,
    build_records,
)

NUMERAL_A, NUMERAL_B = 1601, 1602
UNIT_A, UNIT_B = 1701, 1702
GATE_A, GATE_B = 1801, 1802
PROMOTE_A, PROMOTE_B = 1901, 1902
COLIVE_A, COLIVE_B = 1951, 1952
WALL_BASE = 2201
WALLS = {"category_type": 2202, "category_main": 2203, "area": 2204, "disposition": 2205,
         "floor": 2206}
WALL_PAIRS: tuple[tuple[int, int], ...] = tuple((WALL_BASE, other) for other in WALLS.values())

TEXT_FLOOR = ("Nabizime k prodeji svetly byt 2+kk v cihlovem dome na ulici Nabrezni v Turnove. "
              "Byt se nachazi ve {floor}. patre domu s vytahem, ma sklep a zasklenou lodzii do "
              "vnitrobloku. Dum prosel rekonstrukci strechy, v dosahu je skola i zastavka.")
TEXT_UNIT = ("Prodej bytove jednotky c. {unit} v novem bytovem dome na ulici Luzicka v Turnove. "
             "Jednotka ma dispozici 3+kk, balkon, sklepni kotec a parkovaci stani v garazi. "
             "Energeticky usporny dum s rekuperaci, dokonceni v minulem roce.")
TEXT_PROMOTE = ("Prodej prostorneho bytu 3+1 s lodzii v cihlovem dome na ulici Palackeho v Turnove. "
                "Byt je ve tretim patre, okna do zahrady, puvodni parkety a zdena koupelna s oknem. "
                "K bytu patri sklep a podil na spolecne zahrade, topeni ustredni z kotelny domu. "
                "Klidna ulice kousek od namesti, obchody i nadrazi v dochazkove vzdalenosti.")
TEXT_PROMOTE_VARIANT = TEXT_PROMOTE[:TEXT_PROMOTE.index(" Klidna")] + " Prohlidky po domluve."
TEXT_COLIVE = ("Nabizime k prodeji mezonetovy byt 4+kk v rezidenci Pod Kozakovem. Obytna kuchyne "
               "s vyhledem do udoli, dve koupelny, satna a velka terasa na jih. Soucasti je kryte "
               "stani a sklep, dum ma vytah a spolecnou kolarnu. Vhodne pro rodinu s detmi.")


def records() -> list[dict[str, Any]]:
    rows = build_records()
    # A body-stated floor conflict on an otherwise shared advert: the numerals auto-reject.
    numeral = {"street_key": "turnov|nabrezni", "house_number": "77", "house_number_cp": "77",
               "granularity": "address", "granularity_rank": 80, "is_address_grain": True}
    for listing_id, floor, source in ((NUMERAL_A, 3, "sreality"), (NUMERAL_B, 5, "idnes")):
        rows.append(_listing(listing_id, BLOCK_A, source=source, disposition="2+kk",
                             area_m2=54.0, floor=None, price=3_950_000,
                             description=TEXT_FLOOR.format(floor=floor),
                             location=_location(**numeral)))
        rows.extend(_gallery(listing_id, 510_000 + 10 * listing_id,
                             _spread_hashes(listing_id, 3)))
    # Two units of one house, each body naming its own unit number: E61's designator veto.
    unit = {"street_key": "turnov|luzicka", "house_number": "905", "house_number_cp": "905",
            "granularity": "address", "granularity_rank": 80, "is_address_grain": True}
    shared = _spread_hashes(UNIT_A, 4)
    for listing_id, number in ((UNIT_A, 12), (UNIT_B, 14)):
        rows.append(_listing(listing_id, BLOCK_A, disposition="3+kk", area_m2=81.0, floor=2,
                             price=7_800_000, description=TEXT_UNIT.format(unit=number),
                             location=_location(**unit)))
        rows.extend(_gallery(listing_id, 520_000 + 10 * listing_id, shared))
    # One shoot in gallery order (K-C) on one portal, on two flats a floor apart: the D43 gate.
    shoot = _spread_hashes(GATE_A, 5)
    for listing_id, floor, source in ((GATE_A, 2, "sreality"), (GATE_B, 3, "sreality")):
        rows.append(_listing(listing_id, BLOCK_A, source=source, disposition="2+1",
                             area_m2=66.0, floor=floor, total_floors=5, price=4_300_000,
                             description=None,
                             location=_location(street_key="turnov|jiraskova")))
        rows.extend(_gallery(listing_id, 530_000 + 10 * listing_id, shoot))
    # One flat on two portals that states and agrees on everything and shares three photos:
    # a band pair (no certificate) the D43 promotion merges.
    promote = {"street_key": "turnov|palackeho", "house_number": "15", "house_number_cp": "15",
               "psc": "51101"}
    history = [["2025-04-01T08:00:00+00:00", 6_200_000], ["2025-08-01T08:00:00+00:00", 5_950_000]]
    photos = _spread_hashes(PROMOTE_A, 3)
    for listing_id, source, text, broker in (
            (PROMOTE_A, "sreality", TEXT_PROMOTE, "5" * 64),
            (PROMOTE_B, "idnes", TEXT_PROMOTE_VARIANT, None)):
        rows.append(_listing(listing_id, BLOCK_A, source=source, description=text,
                             disposition="3+1", area_m2=88.0, floor=3, total_floors=4,
                             price=5_950_000, broker_key=broker,
                             broker_identity_id=55 if broker else None,
                             attrs={"has_lift": False, "cellar": True, "energy_rating": "D"},
                             location=_location(**promote), price_history=history))
        rows.extend(_gallery(listing_id, 540_000 + 10 * listing_id, photos))
    # One broker, one portal, two identical adverts live together for a year: K-C, which the
    # E47 co-live guard blocks when a settings row switches it on.
    shoot = _spread_hashes(COLIVE_A, 5)
    for listing_id in (COLIVE_A, COLIVE_B):
        rows.append(_listing(listing_id, BLOCK_A, description=TEXT_COLIVE, disposition="4+kk",
                             area_m2=112.0, floor=4, total_floors=5, price=9_900_000,
                             broker_key="6" * 64, broker_identity_id=66,
                             first_seen_at="2025-06-01T08:00:00+00:00",
                             location=_location(street_key="turnov|nadrazni")))
        rows.extend(_gallery(listing_id, 550_000 + 10 * listing_id, shoot))
    # One advert per wall against a base flat (`with_wall_pairs` decides these five pairs).
    base = {"disposition": "3+kk", "area_m2": 80.0, "floor": 3, "total_floors": 6,
            "price": 6_100_000, "location": _location(street_key="turnov|zelezna")}
    variants = {WALL_BASE: {}, WALLS["category_type"]: {"category_type": "pronajem",
                                                         "price": 21_000},
                WALLS["category_main"]: {"category_main": "komercni", "disposition": None},
                WALLS["area"]: {"area_m2": 96.0}, WALLS["disposition"]: {"disposition": "2+1"},
                WALLS["floor"]: {"floor": 6}}
    for listing_id, over in variants.items():
        rows.append(_listing(listing_id, BLOCK_A, **{**base, **over}))
        rows.extend(_gallery(listing_id, 560_000 + 10 * listing_id,
                             _spread_hashes(listing_id, 2)))
    return rows


def write(path: Path) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in records():
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def with_wall_pairs(write_evidence: Callable[..., Any]) -> Callable[..., Any]:
    """`evidence.write` with the five walled pairs added, each decided by `decide_pair` itself
    (retrieval never hands the one path such a pair, so only this puts one in an artefact)."""
    def wrapped(out_dir: Path, rows: Any, dataset: Any, fps: Any, census: Any, settings: Any,
                model: Any, request: Any) -> Any:
        rows = dict(rows)
        for lo, hi in WALL_PAIRS:
            d = decide_pair(fps[lo], fps[hi], dataset.listings[lo], dataset.listings[hi], {}, (),
                            model, settings, census)
            rows[(lo, hi)] = PairRow(lo, hi, ["wall"], True, True, d.zone, d.score,
                                     sorted(d.families), d.certificate, d.veto, d.reason,
                                     dict(d.evidence), {}, "", "", feats={})
        return write_evidence(out_dir, rows, dataset, fps, census, settings, model, request)
    return wrapped


# The settings rows that push the fixture through the branches w31 leaves dark. Each is w31 plus
# these fields; every pair of every row is read rung by rung against the engine.
_E63_OPEN = {"context_rule_min_score": 0.0, "context_rule_area_max": 1.0,
             "context_rule_price_ratio_min": 0.0, "context_rule_containment_min": 0.0}
EXERCISE: dict[str, dict[str, Any]] = {
    # E63 on any stated body; sales of flats held by D65.
    "context_policy": {**_E63_OPEN, "merge_policy": {"prodej|byt": "propose"}},
    # Propose-only strata (K-B, same-portal models) that E63 then re-reads, a lowered model cut,
    # and E11 at five families (certificates and cuts held by the evidence gate).
    "strata": {**_E63_OPEN, "min_evidence_families": 5,
               "t_hi_by_stratum": {"K-A|cross": None, "K-A|same": None, "K-B|same": None,
                                   "K-C|same": 0.0, "model|cross": 0.9, "model|same": None}},
    # The developer guards (E46, E47) and E45's unit evidence over a model cut every pair clears:
    # the blocks, the score-cut merges and the evidence gate at two families.
    "guards": {"developer_signature_guard": True, "developer_colive_guard": True,
               "unit_evidence_required": True, "min_evidence_families": 2,
               "t_hi_by_stratum": {"K-A|cross": None, "K-A|same": None, "K-C|same": 0.0,
                                   "model|cross": 0.0, "model|same": 0.0}},
    # Everything above the floor is band (store floor and t_lo at 0), E63 open with the census
    # block limb: pure `model` band pairs, E63 merges and its fungible-catalogue refusals.
    "census": {**_E63_OPEN, "store_floor": 0.0, "t_lo": 0.0, "context_rule_block_min": 3},
}
# The rows `with_wall_pairs` adds the walls to. Not `census`: at a zero store floor a walled row
# would be stored by the lab but was never in the run's store (retrieval refused it).
WALLED: frozenset[str] = frozenset({"w31", "context_policy", "strata", "guards"})

# Every branch of `decide_pair` the lab restates, as (zone, reason pattern) that some engine stage
# (`verify._engine_stages`) of some pair of some row must reach: `test_lab_verify` fails on a dark
# branch.
BRANCHES: tuple[tuple[str, str], ...] = (
    ("veto", r"^guard:category_type$"), ("veto", r"^guard:category_main$"),
    ("veto", r"^guard:area$"), ("veto", r"^guard:disposition$"), ("veto", r"^guard:floor$"),
    ("veto", r"^guard:unit_designator_conflict$"), ("reject", r"^auto_reject:numeral_conflict$"),
    ("merge", r"^certificate:K-B$"), ("merge", r"^certificate:K-C$"),
    ("band", r"^certificate:K-B:stratum_propose_only$"),
    ("band", r"^certificate:K-C:developer_colive$"), ("band", r"^certificate:K-C:evidence_gate$"),
    ("band", r"^developer_signature$"), ("band", r"^unit_evidence_gate$"),
    ("band", r"^evidence_gate$"), ("merge", r"^model$"), ("band", r"^model$"),
    ("reject", r"^model$"), ("merge", r"^context_rule:text$"),
    ("band", r"^context_rule:fungible:catalogue_block$"), ("band", r":d43_gate:"),
    ("band", r":d43_demonstrate:"), ("merge", r"^d43_promote:"), ("band", r":policy_hold:"),
)
