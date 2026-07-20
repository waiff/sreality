"""The R2 carrier registry — every table that mirrors a legacy listing id.

One definition, two consumers: `scripts.verify_pipeline`'s dual-write parity check
and `scripts.backfill_child_listing_ids`. They must agree on the exact same set —
a carrier present in one and missing from the other is precisely the silent hole
this refactor's audits kept finding (a table nobody backfills, or one nobody
watches). Adding a carrier is one line here.

`legacy` is the column holding the old smart key (`listings.sreality_id`), `new`
the surrogate (`listings.id`) added by migrations 320-325. `cursor` is a
monotonic column used to (a) anchor the dual-write watermark and (b) window the
backfill; `kind='ts'` marks the two carriers with no usable bigint id —
notification_dispatches has a uuid PK and estimation_cohort_entries has none.

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
    {"table": "dedup_pair_audit", "cursor": "id",
     "cols": [("left_sreality_id", "left_listing_id"),
              ("right_sreality_id", "right_listing_id")]},
    {"table": "properties", "cursor": "id", "cols": [("repr_listing_id", "repr_listing_ref_id")]},
    {"table": "property_notes", "cursor": "id",
     "cols": [("origin_listing_id", "origin_listing_ref_id")]},
    {"table": "property_merge_events", "cursor": "id", "cols": [("listing_id", "listing_ref_id")]},
    {"table": "manual_rental_estimates", "cursor": "id", "cols": [("sreality_id", "listing_id")]},
    {"table": "manual_rental_estimates_history", "cursor": "id",
     "cols": [("sreality_id", "listing_id")]},
    {"table": "estimation_runs", "cursor": "id",
     "cols": [("input_sreality_id", "input_listing_id")]},
    {"table": "building_runs", "cursor": "id",
     "cols": [("input_sreality_id", "input_listing_id")]},
    {"table": "notification_dispatches", "cursor": "dispatched_at", "kind": "ts",
     "cols": [("sreality_id", "listing_id")]},
    {"table": "estimation_cohort_entries", "cursor": "created_at", "kind": "ts",
     "cols": [("sreality_id", "listing_id")]},
]

R2_CARRIERS_BY_TABLE: dict[str, dict[str, Any]] = {c["table"]: c for c in R2_CARRIERS}


def is_ts_cursor(carrier: dict[str, Any]) -> bool:
    return carrier.get("kind") == "ts"
