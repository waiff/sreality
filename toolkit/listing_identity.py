"""The R2 carrier registry — every table that mirrors a legacy listing id.

One definition, four readers since the dual-write parity check retired (Health sprint
wave 3, Q6): `scripts.backfill_child_listing_ids` fills each carrier's surrogate column, and
`scripts.apply_r2_constraints`, `scripts.apply_r2_unique_guards` and
`scripts.apply_r2_phase_d_prep` walk each carrier's column pairs for their DDL. A carrier is
added in one line here; the backfill's skip predicates are pinned in
tests/scripts/test_backfill_child_listing_ids.py.

`legacy` is the column holding the old smart key (`listings.sreality_id`), `new`
the surrogate (`listings.id`) added by migrations 320-325. `cursor` is a
monotonic column that windows the backfill; `kind='ts'` marks the carrier with no
usable bigint id, estimation_cohort_entries (it has no PK).
notification_dispatches left the registry when migration 656 dropped its legacy
`sreality_id` (every row already held its `listing_id`).

Pair carriers list two column pairs. They are stamped and checked POSITIONALLY:
`listing_id_a` is the surrogate of `sreality_id_a`. Under the surrogate the
legacy `a < b` canonical order does not hold, and re-sorting would desynchronise
the side-coupled payloads — so nothing here ever reorders a pair.

`dirty_broker_listings` is deliberately absent: its lifecycle is a queue that is
claimed and deleted, never read as history, so it re-keys by a writer swap at
cutover rather than a backfill.
"""

from __future__ import annotations

from typing import Any

R2_CARRIERS: list[dict[str, Any]] = [
    {"table": "images", "cursor": "id", "cols": [("sreality_id", "listing_id")]},
    {"table": "listing_snapshots", "cursor": "id", "cols": [("sreality_id", "listing_id")]},
    {"table": "listing_videos", "cursor": "id", "cols": [("sreality_id", "listing_id")]},
    {"table": "listing_condition_scores", "cursor": "id", "cols": [("sreality_id", "listing_id")]},
    {"table": "listing_marker_extractions", "cursor": "id", "cols": [("sreality_id", "listing_id")]},
    {"table": "listing_summaries", "cursor": "id", "cols": [("sreality_id", "listing_id")]},
    {"table": "building_unit_extractions", "cursor": "id", "cols": [("sreality_id", "listing_id")]},
    {"table": "listing_description_enrichments", "cursor": "id",
     "cols": [("sreality_id", "listing_id")]},
    {"table": "listing_image_comparisons", "cursor": "id",
     "cols": [("sreality_id_a", "listing_id_a"), ("sreality_id_b", "listing_id_b")]},
    {"table": "listing_visual_matches", "cursor": "id",
     "cols": [("sreality_id_a", "listing_id_a"), ("sreality_id_b", "listing_id_b")]},
    {"table": "listing_floor_plan_matches", "cursor": "id",
     "cols": [("sreality_id_a", "listing_id_a"), ("sreality_id_b", "listing_id_b")]},
    {"table": "listing_site_plan_matches", "cursor": "id",
     "cols": [("sreality_id_a", "listing_id_a"), ("sreality_id_b", "listing_id_b")]},
    # An optional `skip` SQL predicate excludes rows the backfill must not touch
    # (rows a NOT VALID CHECK would reject on UPDATE). It is applied to COUNTING as
    # well as updating: skip one but not the other and "remaining" never reaches
    # zero, which would make the self-chaining workflow re-dispatch forever.
    # A property the recompute reset to no ads (`source_count` 0, which only that reset writes)
    # names no advert on purpose: its two NULL ids are no writer's gap.
    {"table": "properties", "cursor": "id", "cols": [("repr_listing_id", "repr_listing_ref_id")],
     "skip": "t.source_count = 0"},
    {"table": "property_notes", "cursor": "id",
     "cols": [("origin_listing_id", "origin_listing_ref_id")]},
    {"table": "property_merge_events", "cursor": "id", "cols": [("listing_id", "listing_ref_id")]},
    {"table": "estimation_runs", "cursor": "id",
     "cols": [("input_sreality_id", "input_listing_id")]},
    {"table": "building_runs", "cursor": "id",
     "cols": [("input_sreality_id", "input_listing_id")]},
    {"table": "estimation_cohort_entries", "cursor": "created_at", "kind": "ts",
     "cols": [("sreality_id", "listing_id")]},
]

R2_CARRIERS_BY_TABLE: dict[str, dict[str, Any]] = {c["table"]: c for c in R2_CARRIERS}


def is_ts_cursor(carrier: dict[str, Any]) -> bool:
    return carrier.get("kind") == "ts"
