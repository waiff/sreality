"""Tier-2 cross-source dedup sweep (multi-portal dedup PR3).

Finds property pairs the cheap insert-time Tier-1 matcher missed -- the same
real-world property listed on two portals whose coords/area/price differ enough
to slip the 20m / +-1m2 / +-2% Tier-1 gate. Generates candidate pairs SQL-side
(cross-source only, within 150m), scores each through a confidence ladder
(disposition equivalence -> address similarity -> [pHash, PR5] -> vision), then:

  * AUTO-MERGE the high-confidence few (tight geo+price+area + an independent
    corroborator) via toolkit.property_identity.merge_properties -- reversible,
    audited, one-click-undoable (the operator's auto-merge decision);
  * QUEUE the rest as property_identity_candidates 'proposed' for the /dedup
    review UI;
  * REJECT pairs that fail the gate.

CROSS-SOURCE ONLY is the safety invariant: two listings from the SAME portal at
one address are legitimately distinct units, never merged (mirrors the Tier-1
same-source exclusion). The self-join is driven from the (few) non-sreality
properties so it stays bounded as bazos/idnes grow.

Runnable as `python -m scripts.dedup_sweep`. Required env var: SUPABASE_DB_URL.
Vision is OFF by default (--max-vision-calls 0); enable it (and, from PR5, the
pHash rung) to let auto-merge fire without a near-exact address match.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, replace
from typing import Any, Callable

from toolkit.addresses import address_similarity
from toolkit.comparables import _DISPOSITION_LOOSE
from toolkit.property_identity import merge_properties

LOG = logging.getLogger("dedup_sweep")

# Candidate-generation gate (wider than Tier-1, to catch its misses).
GEN_RADIUS_M = 150.0
GEN_PRICE_DRIFT_MAX = 0.08
GEN_AREA_DIFF_MAX_M2 = 10.0
GEN_AREA_DIFF_MAX_PCT = 0.10

# Auto-merge gate: strictly tighter than generation + an independent corroborator.
AUTO_RADIUS_M = 30.0
AUTO_PRICE_DRIFT_MAX = 0.02
AUTO_AREA_DIFF_MAX_M2 = 2.0
AUTO_ADDR_SIM_MIN = 0.9
AUTO_PHASH_HAMMING_MAX = 6
AUTO_VISION_SIM_MIN = 0.85

VisionFn = Callable[[int, int], float | None]
PhashFn = Callable[[int, int], int | None]


@dataclass(frozen=True)
class PairSignals:
    distance_m: float
    price_a: int | None
    price_b: int | None
    area_a: float | None
    area_b: float | None
    disposition_a: str | None
    disposition_b: str | None
    address_similarity: float
    phash_hamming: int | None = None
    vision_similarity: float | None = None


@dataclass(frozen=True)
class PairDecision:
    action: str               # 'auto_merge' | 'queue' | 'reject'
    corroborator: str | None  # 'address' | 'phash' | 'vision' | None
    confidence: float


def _disposition_compatible(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return a == b or b in _DISPOSITION_LOOSE.get(a, ())


def classify_pair(s: PairSignals) -> PairDecision:
    """Pure classifier: the auto-merge policy in one place.

    Reject unless disposition-compatible with full price/area data inside the
    generation gate. Auto-merge only when the match is tight (<=30m, <=2% price,
    <=2m2 area) AND an independent corroborator agrees (near-exact address, or
    a close pHash [PR5], or a high vision score). Everything else queues for
    review -- the conservative half of the operator's auto-merge decision.
    """
    if not _disposition_compatible(s.disposition_a, s.disposition_b):
        return PairDecision("reject", None, 0.0)
    if None in (s.price_a, s.price_b, s.area_a, s.area_b):
        return PairDecision("reject", None, 0.0)

    price_drift = abs(s.price_a - s.price_b) / max(s.price_a, s.price_b)
    area_diff = abs(s.area_a - s.area_b)
    if s.distance_m > GEN_RADIUS_M or price_drift > GEN_PRICE_DRIFT_MAX:
        return PairDecision("reject", None, 0.0)

    corroborator: str | None = None
    if s.address_similarity >= AUTO_ADDR_SIM_MIN:
        corroborator = "address"
    if s.phash_hamming is not None and s.phash_hamming <= AUTO_PHASH_HAMMING_MAX:
        corroborator = "phash"
    if s.vision_similarity is not None and s.vision_similarity >= AUTO_VISION_SIM_MIN:
        corroborator = "vision"

    tight = (
        s.distance_m <= AUTO_RADIUS_M
        and price_drift <= AUTO_PRICE_DRIFT_MAX
        and area_diff <= AUTO_AREA_DIFF_MAX_M2
    )
    if tight and corroborator is not None:
        return PairDecision("auto_merge", corroborator, 0.95)
    return PairDecision("queue", corroborator, 0.6)


# Cross-source pairs within radius, seeded from non-sreality properties so the
# spatial self-join is bounded. Same-source pairs (legitimately distinct units)
# are excluded by the disjoint-sources test. Already-decided pairs are skipped.
_CANDIDATE_SQL = """
    WITH src AS (
      SELECT property_id AS pid, array_agg(DISTINCT source) AS sources
      FROM listings WHERE property_id IS NOT NULL GROUP BY property_id
    )
    SELECT
      a.id, b.id, ST_Distance(a.geom, b.geom) AS distance_m,
      a.current_price_czk, b.current_price_czk, a.area_m2, b.area_m2,
      a.disposition, b.disposition,
      a.locality, a.district, b.locality, b.district,
      a.first_seen_at, b.first_seen_at, a.repr_listing_id, b.repr_listing_id
    FROM src sa
    JOIN properties a ON a.id = sa.pid
    JOIN properties b ON b.id <> a.id AND ST_DWithin(a.geom, b.geom, %(radius)s)
    JOIN src sb ON sb.pid = b.id
    WHERE NOT (sa.sources <@ ARRAY['sreality'])
      AND a.status = 'active' AND b.status = 'active'
      AND a.geom IS NOT NULL AND b.geom IS NOT NULL
      AND a.current_price_czk IS NOT NULL AND b.current_price_czk IS NOT NULL
      AND a.area_m2 IS NOT NULL AND b.area_m2 IS NOT NULL
      AND NOT (sa.sources && sb.sources)
      AND abs(a.current_price_czk - b.current_price_czk)::numeric
            / GREATEST(a.current_price_czk, b.current_price_czk) <= %(price_drift)s
      AND abs(a.area_m2 - b.area_m2)
            <= GREATEST(%(area_abs)s, %(area_pct)s * GREATEST(a.area_m2, b.area_m2))
      AND NOT EXISTS (
        SELECT 1 FROM property_identity_candidates c
        WHERE c.left_property_id = LEAST(a.id, b.id)
          AND c.right_property_id = GREATEST(a.id, b.id)
      )
    ORDER BY a.id, b.id
    LIMIT %(limit)s
"""

_PAIR_FIELDS = (
    "a_id", "b_id", "distance_m", "a_price", "b_price", "a_area", "b_area",
    "a_disp", "b_disp", "a_locality", "a_district", "b_locality", "b_district",
    "a_first_seen", "b_first_seen", "a_repr", "b_repr",
)


def generate_candidate_pairs(
    conn: Any, *, radius: float, price_drift: float,
    area_abs: float, area_pct: float, limit: int,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_CANDIDATE_SQL, {
            "radius": radius, "price_drift": price_drift,
            "area_abs": area_abs, "area_pct": area_pct, "limit": limit,
        })
        return [dict(zip(_PAIR_FIELDS, row)) for row in cur.fetchall()]


def _enqueue_candidate(
    conn: Any, lo: int, hi: int, confidence: float, markers: dict[str, Any],
) -> None:
    from psycopg.types.json import Jsonb
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO property_identity_candidates
                (left_property_id, right_property_id, tier, confidence, markers_matched)
            VALUES (%s, %s, 'tier2', %s, %s)
            ON CONFLICT (left_property_id, right_property_id) DO NOTHING
            """,
            (lo, hi, confidence, Jsonb(markers)),
        )


def run_sweep(
    conn: Any, *,
    radius: float = GEN_RADIUS_M,
    price_drift: float = GEN_PRICE_DRIFT_MAX,
    area_abs: float = GEN_AREA_DIFF_MAX_M2,
    area_pct: float = GEN_AREA_DIFF_MAX_PCT,
    limit: int = 2000,
    max_auto_merges: int = 200,
    compare_fn: VisionFn | None = None,
    max_vision_calls: int = 0,
    phash_fn: "PhashFn | None" = None,
) -> dict[str, int]:
    pairs = generate_candidate_pairs(
        conn, radius=radius, price_drift=price_drift,
        area_abs=area_abs, area_pct=area_pct, limit=limit,
    )
    stats = {
        "pairs": len(pairs), "auto_merged": 0, "queued": 0,
        "rejected": 0, "vision_calls": 0,
    }
    seen: set[tuple[int, int]] = set()
    vision_budget = max_vision_calls

    for p in pairs:
        lo, hi = sorted((int(p["a_id"]), int(p["b_id"])))
        if (lo, hi) in seen:
            continue
        seen.add((lo, hi))

        signals = PairSignals(
            distance_m=float(p["distance_m"]),
            price_a=p["a_price"], price_b=p["b_price"],
            area_a=float(p["a_area"]) if p["a_area"] is not None else None,
            area_b=float(p["b_area"]) if p["b_area"] is not None else None,
            disposition_a=p["a_disp"], disposition_b=p["b_disp"],
            address_similarity=address_similarity(
                f"{p['a_locality'] or ''} {p['a_district'] or ''}",
                f"{p['b_locality'] or ''} {p['b_district'] or ''}",
            ),
        )

        # pHash rung (cheap, SQL): min image Hamming between the two
        # representatives. A close match is a strong auto-merge corroborator,
        # so it can settle a pair without paying for the vision call.
        if phash_fn is not None:
            ph = phash_fn(int(p["a_repr"]), int(p["b_repr"]))
            if ph is not None:
                signals = replace(signals, phash_hamming=ph)

        decision = classify_pair(signals)

        # Vision escalation: only for the tight-but-uncorroborated few, bounded.
        if (
            decision.action == "queue"
            and decision.corroborator is None
            and compare_fn is not None
            and vision_budget > 0
            and signals.distance_m <= AUTO_RADIUS_M
        ):
            vision_budget -= 1
            stats["vision_calls"] += 1
            vscore = compare_fn(int(p["a_repr"]), int(p["b_repr"]))
            if vscore is not None:
                signals = replace(signals, vision_similarity=vscore)
                decision = classify_pair(signals)

        markers = {
            "tier": "tier2",
            "distance_m": round(signals.distance_m, 1),
            "address_similarity": round(signals.address_similarity, 3),
            "phash_hamming": signals.phash_hamming,
            "vision_similarity": signals.vision_similarity,
            "corroborator": decision.corroborator,
        }

        if decision.action == "auto_merge" and stats["auto_merged"] < max_auto_merges:
            survivor, retired = (
                (int(p["a_id"]), int(p["b_id"]))
                if p["a_first_seen"] <= p["b_first_seen"]
                else (int(p["b_id"]), int(p["a_id"]))
            )
            merge_properties(
                conn, survivor_id=survivor, retired_id=retired,
                reason=f"tier2_{decision.corroborator}", source="auto",
                confidence=decision.confidence, markers=markers,
            )
            stats["auto_merged"] += 1
            LOG.info("AUTO-MERGE %s<-%s via=%s", survivor, retired, decision.corroborator)
        elif decision.action == "reject":
            stats["rejected"] += 1
        else:
            _enqueue_candidate(conn, lo, hi, decision.confidence, markers)
            stats["queued"] += 1

    return stats


def _build_vision_fn(conn: Any) -> VisionFn:
    """Lazily wire compare_listing_images (PR2's cached vision tool) as the
    sweep's vision corroborator. Imported only when --max-vision-calls > 0 so
    the common path stays free of the api / LLM dependency."""
    from api.dependencies import get_providers
    from api.llm_client import LLMClient
    from toolkit.image_similarity import compare_listing_images

    llm = LLMClient(conn, providers=get_providers())

    def _score(a_repr: int, b_repr: int) -> float | None:
        try:
            res = compare_listing_images(
                conn, llm, sreality_id_a=a_repr, sreality_id_b=b_repr,
            )
            return float(res["data"]["comparison"]["overall_similarity"])
        except Exception as exc:  # noqa: BLE001 - one bad pair must not kill the sweep
            LOG.warning("vision compare failed %s/%s: %s", a_repr, b_repr, exc)
            return None

    return _score


def _build_phash_fn(conn: Any) -> PhashFn:
    """The pHash corroborator: the min image Hamming between two listings'
    perceptual hashes, computed in SQL via bit_count(a # b). Cheap, so it runs
    for every candidate pair. None when neither side has a hashed image yet."""
    def _hamming(a_repr: int, b_repr: int) -> int | None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT min(bit_count((ia.phash # ib.phash)::bit(64))) "
                "FROM images ia JOIN images ib ON true "
                "WHERE ia.sreality_id = %s AND ib.sreality_id = %s "
                "AND ia.phash IS NOT NULL AND ib.phash IS NOT NULL",
                (a_repr, b_repr),
            )
            row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    return _hamming


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=2000,
                        help="Max candidate pairs generated per run.")
    parser.add_argument("--max-auto-merges", type=int, default=200,
                        help="Cap auto-merges per run (overflow is queued).")
    parser.add_argument("--max-vision-calls", type=int, default=0,
                        help="Cap vision corroborations per run (0 = vision off).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report candidate-pair count and exit without writing.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    import psycopg

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        if args.dry_run:
            pairs = generate_candidate_pairs(
                conn, radius=GEN_RADIUS_M, price_drift=GEN_PRICE_DRIFT_MAX,
                area_abs=GEN_AREA_DIFF_MAX_M2, area_pct=GEN_AREA_DIFF_MAX_PCT,
                limit=args.limit,
            )
            LOG.info("SWEEP dry-run candidate_pairs=%d; exit", len(pairs))
            return 0

        compare_fn = _build_vision_fn(conn) if args.max_vision_calls > 0 else None
        stats = run_sweep(
            conn, limit=args.limit, max_auto_merges=args.max_auto_merges,
            compare_fn=compare_fn, max_vision_calls=args.max_vision_calls,
            phash_fn=_build_phash_fn(conn),
        )

    LOG.info(
        "SWEEP done pairs=%d auto_merged=%d queued=%d rejected=%d vision_calls=%d",
        stats["pairs"], stats["auto_merged"], stats["queued"],
        stats["rejected"], stats["vision_calls"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
