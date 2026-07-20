"""Operator-facing dedup review: list candidate pairs, merge, dismiss, unmerge.

Thin DB layer over `property_identity_candidates` + `property_merge_events`. The
actual merge/unmerge transaction mechanics live in `toolkit.property_identity`
(shared with the Tier-2 sweep); this module is the API's read + orchestration
surface. Mounted under `/dedup/*` (see `api.routes.dedup`).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg

from api.location_filter import DistrictChip, district_where
from toolkit.dedup_audit import build_audit_breakdown
from toolkit.dedup_engine import (
    PHASH_IDENTICAL_MAX,
    RENDER_SCORE_EXCLUDE_MIN,
    phash_excluded_tags_for,
    phash_render_exclude_for,
    render_exclusion_clause,
)
from toolkit.property_identity import MergeError, merge_properties, unmerge_group
from toolkit.room_taxonomy import FLOOR_PLAN_ROOM_TYPE, SITE_PLAN_ROOM_TYPE

LOG = logging.getLogger(__name__)

_EXPECTED_OUTCOMES = ("should_merge", "should_dismiss", "unsure")


def _canon_pair(a: int, b: int) -> tuple[int, int]:
    """Canonical (low, high) property pair — the dedup_decision_feedback key order."""
    a, b = int(a), int(b)
    return (a, b) if a < b else (b, a)


def _feedback_obj(
    is_incorrect: Any, expected_outcome: Any, note: Any, updated_at: Any,
) -> dict[str, Any] | None:
    """Build the per-row `feedback` payload from a LEFT-joined feedback row, or None
    when the pair carries no flag (is_incorrect is NULL on a no-match)."""
    if is_incorrect is None:
        return None
    return {
        "is_incorrect": bool(is_incorrect),
        "expected_outcome": expected_outcome,
        "note": note,
        "updated_at": updated_at,
    }


def set_decision_feedback(
    conn: psycopg.Connection,
    *,
    left_property_id: int,
    right_property_id: int,
    is_incorrect: bool = True,
    expected_outcome: str | None = None,
    note: str | None = None,
    category_main: str | None = None,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Upsert the operator's "this decision was wrong" flag for ONE canonical PROPERTY
    pair (the Decision-history AND Needs-review flag — same store; property-grain so it
    never orphans on a repr-listing recompute). Idempotent: a repeat call edits the
    existing row's direction/note."""
    lo, hi = _canon_pair(left_property_id, right_property_id)
    if lo == hi:
        raise ValueError("a decision pair needs two distinct properties")
    if expected_outcome is not None and expected_outcome not in _EXPECTED_OUTCOMES:
        raise ValueError(f"expected_outcome must be one of {_EXPECTED_OUTCOMES}")
    clean_note = (note or "").strip() or None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dedup_decision_feedback (left_property_id, right_property_id, "
            "  is_incorrect, expected_outcome, note, category_main, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (left_property_id, right_property_id) DO UPDATE SET "
            "  is_incorrect = excluded.is_incorrect, "
            "  expected_outcome = excluded.expected_outcome, "
            "  note = excluded.note, "
            "  category_main = coalesce(excluded.category_main, "
            "                           dedup_decision_feedback.category_main) "
            "RETURNING is_incorrect, expected_outcome, note, updated_at",
            (lo, hi, bool(is_incorrect), expected_outcome, clean_note,
             category_main, created_by),
        )
        r = cur.fetchone()
    return {
        "data": {
            "left_property_id": lo, "right_property_id": hi,
            **(_feedback_obj(r[0], r[1], r[2], r[3]) or {}),
        }
    }


def delete_decision_feedback(
    conn: psycopg.Connection, *, left_property_id: int, right_property_id: int,
) -> dict[str, Any]:
    """Un-flag a pair (delete its feedback row). No-op if it wasn't flagged."""
    lo, hi = _canon_pair(left_property_id, right_property_id)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM dedup_decision_feedback "
            "WHERE left_property_id = %s AND right_property_id = %s",
            (lo, hi),
        )
        deleted = cur.rowcount
    return {"data": {"deleted": bool(deleted)}}


def set_image_annotation(
    conn: psycopg.Connection,
    *,
    image_id: int,
    tag_flagged: bool = False,
    render_flagged: bool = False,
    note: str | None = None,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Upsert the operator's correction on ONE image's CLIP call (the /clip-audit
    "this tag/render score is wrong" flag + note) — same upsert-on-conflict shape as
    `set_decision_feedback`, at image grain instead of property-pair grain (migration 308)."""
    clean_note = (note or "").strip() or None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO image_tag_annotations (image_id, tag_flagged, render_flagged, "
            "  note, created_by) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (image_id) DO UPDATE SET "
            "  tag_flagged = excluded.tag_flagged, "
            "  render_flagged = excluded.render_flagged, "
            "  note = excluded.note, "
            "  updated_at = now() "
            "RETURNING tag_flagged, render_flagged, note, updated_at",
            (image_id, bool(tag_flagged), bool(render_flagged), clean_note, created_by),
        )
        r = cur.fetchone()
    return {
        "data": {
            "image_id": image_id, "tag_flagged": bool(r[0]), "render_flagged": bool(r[1]),
            "note": r[2], "updated_at": r[3],
        }
    }


def delete_image_annotation(conn: psycopg.Connection, *, image_id: int) -> dict[str, Any]:
    """Clear an image's annotation row entirely. No-op if it had none."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM image_tag_annotations WHERE image_id = %s", (image_id,),
        )
        deleted = cur.rowcount
    return {"data": {"deleted": bool(deleted)}}


def _canon_image_pair(a: int, b: int) -> tuple[int, int]:
    """Canonical (low, high) image-id pair — the phash_pair_notes key order."""
    a, b = int(a), int(b)
    return (a, b) if a < b else (b, a)


def set_phash_note(
    conn: psycopg.Connection,
    *,
    image_id_a: int,
    image_id_b: int,
    note: str | None,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Upsert the operator's note on ONE image pair from the /phash-audit page — image-
    PAIR grain (migration 308), the same mutable upsert shape as `set_decision_feedback`."""
    lo, hi = _canon_image_pair(image_id_a, image_id_b)
    if lo == hi:
        raise ValueError("a phash note needs two distinct images")
    clean_note = (note or "").strip() or None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO phash_pair_notes (image_id_a, image_id_b, note, created_by) "
            "VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (image_id_a, image_id_b) DO UPDATE SET "
            "  note = excluded.note, updated_at = now() "
            "RETURNING note, updated_at",
            (lo, hi, clean_note, created_by),
        )
        r = cur.fetchone()
    return {"data": {"image_id_a": lo, "image_id_b": hi, "note": r[0], "updated_at": r[1]}}


def delete_phash_note(
    conn: psycopg.Connection, *, image_id_a: int, image_id_b: int,
) -> dict[str, Any]:
    """Clear a phash-pair note. No-op if it had none."""
    lo, hi = _canon_image_pair(image_id_a, image_id_b)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM phash_pair_notes WHERE image_id_a = %s AND image_id_b = %s",
            (lo, hi),
        )
        deleted = cur.rowcount
    return {"data": {"deleted": bool(deleted)}}


def set_training_example(
    conn: psycopg.Connection,
    *,
    image_id: int,
    label: str,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Upsert ONE image's linear-probe training-set label (the /phash-audit "Train"
    CTA) — one label per image (migration 309), overwritten on a repeat Train click
    with a different label. `label` is free text (open-vocabulary), not constrained to
    the CLIP taxonomy — but IS normalized here (trim + collapse internal whitespace),
    the write boundary, so every reader (this table's distinct-label list feeds the
    training combobox's suggestions) sees already-clean values instead of re-deriving
    normalization at each read site. Data-collection only: nothing reads this table yet."""
    clean = " ".join((label or "").split())
    if not clean:
        raise ValueError("a training example needs a non-empty label")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO image_training_examples (image_id, label, created_by) "
            "VALUES (%s,%s,%s) "
            "ON CONFLICT (image_id) DO UPDATE SET "
            "  label = excluded.label, updated_at = now() "
            "RETURNING label, updated_at",
            (image_id, clean, created_by),
        )
        r = cur.fetchone()
    return {"data": {"image_id": image_id, "label": r[0], "updated_at": r[1]}}


def delete_training_example(conn: psycopg.Connection, *, image_id: int) -> dict[str, Any]:
    """Remove an image from the training set. No-op if it wasn't in it."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM image_training_examples WHERE image_id = %s", (image_id,),
        )
        deleted = cur.rowcount
    return {"data": {"deleted": bool(deleted)}}


def set_border_case(
    conn: psycopg.Connection, *, image_id: int, created_by: str = "operator",
) -> dict[str, Any]:
    """Flag ONE image as a border case (migration 310) — even a human isn't confident
    about its room/plan classification. A separate concern from image_training_examples:
    no label required, and independent of whether the image has one. Idempotent — a
    repeat flag is a no-op, not a second row (image_id is unique)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO image_border_cases (image_id, created_by) VALUES (%s,%s) "
            "ON CONFLICT (image_id) DO NOTHING "
            "RETURNING created_at",
            (image_id, created_by),
        )
        r = cur.fetchone()
        if r is None:
            cur.execute(
                "SELECT created_at FROM image_border_cases WHERE image_id = %s", (image_id,),
            )
            r = cur.fetchone()
    return {"data": {"image_id": image_id, "created_at": r[0]}}


def delete_border_case(conn: psycopg.Connection, *, image_id: int) -> dict[str, Any]:
    """Unflag an image as a border case. No-op if it wasn't flagged."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM image_border_cases WHERE image_id = %s", (image_id,),
        )
        deleted = cur.rowcount
    return {"data": {"deleted": bool(deleted)}}


def _ledger_side_sql(pid_expr: str, grp_expr: str) -> str:
    """Scalar SQL for the representative listing id of property `pid_expr` on one
    side of merge group `grp_expr`, resolved from the `property_merge_events`
    LEDGER — the source of truth for what merged with what.

    A merge re-points the retired property's listings onto the survivor and
    recomputes ONLY the survivor's `repr_listing_id`; if the just-absorbed listing
    wins that recompute, both properties' `repr_listing_id` end up the SAME id, so
    reading `repr_listing_id` for the audit label collapses both sides to one id
    (the reported bug). The ledger doesn't drift: survivor side -> a listing on it
    that did NOT move in this group; retired side -> the (min) listing that moved
    from it. Returns NULL for the survivor side only when the survivor was itself
    later merged away (its listings moved on) — an honest "—", never a false pair.
    """
    return f"""(
      SELECT CASE WHEN {pid_expr} = _ev.survivor_property_id THEN (
               SELECT min(_l.sreality_id) FROM listings _l
                WHERE _l.property_id = {pid_expr}
                  AND NOT (_l.sreality_id = ANY(_ev.moved)))
             ELSE (
               SELECT min(_pme.listing_id) FROM property_merge_events _pme
                WHERE _pme.merge_group_id = {grp_expr}
                  AND _pme.retired_property_id = {pid_expr}) END
      FROM (SELECT survivor_property_id, array_agg(listing_id) AS moved
            FROM property_merge_events
            WHERE merge_group_id = {grp_expr}
            GROUP BY survivor_property_id LIMIT 1) _ev
    )"""


def _record_operator_decision(
    conn: psycopg.Connection,
    *,
    left_property_id: int,
    right_property_id: int,
    outcome: str,
    markers: dict[str, Any] | None = None,
    merge_group_id: Any = None,
) -> None:
    """Write ONE operator decision (merged | dismissed) into the unified
    `dedup_pair_audit` log with `source='operator'` — so Decision history shows the
    operator's own actions alongside the engine's, with the SAME factor detail (from
    the candidate's `markers_matched`) and, for a merge, the undo handle. Best-effort:
    a logging failure must never fail the merge/dismiss the operator asked for (the
    merge itself is already committed + recorded in property_merge_events)."""
    markers = markers or {}
    stage = markers.get("stage") or "operator"
    detail = {k: v for k, v in markers.items() if v is not None}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, repr_listing_id, category_main FROM properties "
                "WHERE id IN (%s, %s)",
                (left_property_id, right_property_id),
            )
            info = {int(r[0]): (r[1], r[2]) for r in cur.fetchall()}
            ls, lc = info.get(int(left_property_id), (None, None))
            rs, rc = info.get(int(right_property_id), (None, None))
            if merge_group_id is not None:
                # A MERGE just re-pointed listings + recomputed the survivor's
                # repr_listing_id, so the repr read above can be the SAME id on
                # both sides. Resolve each side's listing from the merge ledger
                # instead (source of truth) — the merge already committed its
                # property_merge_events rows in the same connection.
                cur.execute(
                    f"SELECT {_ledger_side_sql('%(lp)s', '%(g)s::uuid')}, "
                    f"       {_ledger_side_sql('%(rp)s', '%(g)s::uuid')}",
                    {"lp": int(left_property_id), "rp": int(right_property_id),
                     "g": str(merge_group_id)},
                )
                led = cur.fetchone()
                if led is not None and led[0] is not None and led[0] != led[1]:
                    ls, rs = led[0], led[1]
            # left/right_listing_id are the surrogate mirrors of the two
            # *_sreality_id columns (R2 dual-write), resolved inline.
            cur.execute(
                "INSERT INTO dedup_pair_audit (run_at, left_sreality_id, "
                "left_listing_id, right_sreality_id, right_listing_id, "
                "left_property_id, right_property_id, "
                "category_main, stage, outcome, source, merge_group_id, detail) "
                "VALUES (now(),%s,(SELECT id FROM listings WHERE sreality_id = %s),"
                "%s,(SELECT id FROM listings WHERE sreality_id = %s),"
                "%s,%s,%s,%s,%s,'operator',%s,%s::jsonb)",
                (ls, ls, rs, rs, left_property_id, right_property_id, lc or rc, stage,
                 outcome, str(merge_group_id) if merge_group_id is not None else None,
                 json.dumps(detail)),
            )
    except Exception:  # noqa: BLE001 — audit logging must never break a real write
        LOG.warning("operator decision audit failed (non-fatal)", exc_info=True)

# A property side rendered on a review card. Built in SQL so geom -> lat/lng and
# the display fields come straight off the canonical row. `{p}` is the properties
# alias; `{rl}` is its representative listing (joined in list_candidates) — the
# source of the disambiguating free-text (`description`) + portal `source_url`
# that only live on the listing row and are the strongest tell for a town-pin
# non-match (two houses in different villages sharing one town coordinate).
_PROP_SIDE_SQL = """
  jsonb_build_object(
    'property_id',         {p}.id,
    'status',              {p}.status,
    'sreality_id',         {p}.repr_listing_id,
    'price_czk',           {p}.current_price_czk,
    'area_m2',             {p}.area_m2,
    'estate_area',         {p}.estate_area,
    'disposition',         {p}.disposition,
    'district',            {p}.district,
    'street',              {p}.street,
    'category_main',       {p}.category_main,
    'category_type',       {p}.category_type,
    'building_type',       {p}.building_type,
    'condition',           {p}.condition,
    'source_count',        {p}.source_count,
    'distinct_site_count', {p}.distinct_site_count,
    'first_seen_at',       {p}.first_seen_at,
    'lat',                 ST_Y({p}.geom::geometry),
    'lng',                 ST_X({p}.geom::geometry),
    'source',              {rl}.source,
    'source_url',          {rl}.source_url,
    'description',         left({rl}.description, 240)
  )
"""


# Sentinel reason for candidates from an older engine version that never wrote a
# `reason` into markers_matched — the operator filters these as one bucket.
LEGACY_REASON = "(legacy)"
# Sentinel verdict for "no verdict recorded" (most buckets), so a bucket keyed on
# (reason, NULL verdict) drills in exactly rather than mixing verdicts.
NULL_VERDICT = "(none)"


def _either_side_eq(
    column: str, value: Any, aliases: tuple[str, str], param_key: str,
) -> tuple[str, dict[str, Any]]:
    """A pair matches if EITHER side's `column` equals `value` — the same
    "which half of the pair matched" latitude `district_where` gives a
    location chip, for a plain scalar equality instead. A pair CAN
    legitimately span two categories (the sanctioned dům↔komerční cross-type
    merge, rule #15), so a single-column equality (`a.category_main = ...`)
    silently drops half of those pairs from both of the type tabs they
    actually belong in."""
    l, r = aliases
    return (
        f"({l}.{column} = %({param_key})s OR {r}.{column} = %({param_key})s)",
        {param_key: value},
    )


# The `property_identity_candidates` <-> `properties` join every list_candidates
# query needs, factored out so its COUNT and its page SELECT can never disagree
# about which tables the shared WHERE clause can reference — an inconsistency
# here is exactly the bug that broke the location filter (a COUNT missing the
# `l`/`r` join while its WHERE referenced `l.obec_id`/`r.obec_id`). Both sides
# always exist (NOT NULL FKs), so the INNER JOIN never drops a candidate row.
_CANDIDATES_FROM = (
    "FROM property_identity_candidates c "
    "JOIN properties l ON l.id = c.left_property_id "
    "JOIN properties r ON r.id = c.right_property_id"
)


def _candidate_filters(
    status: str | None,
    tier: str | None,
    reason: str | None,
    verdict: str | None,
    districts: list[DistrictChip] | None = None,
    category_main: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Shared WHERE for list_candidates + its COUNT (so the page total is real)."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if status is not None:
        clauses.append("c.status = %(status)s")
        params["status"] = status
    if tier is not None:
        clauses.append("c.tier = %(tier)s")
        params["tier"] = tier
    if reason == LEGACY_REASON:
        clauses.append("c.markers_matched->>'reason' IS NULL")
    elif reason is not None:
        clauses.append("c.markers_matched->>'reason' = %(reason)s")
        params["reason"] = reason
    if verdict == NULL_VERDICT:
        clauses.append("c.markers_matched->>'verdict' IS NULL")
    elif verdict is not None:
        clauses.append("c.markers_matched->>'verdict' = %(verdict)s")
        params["verdict"] = verdict
    if category_main is not None:
        # Same either-side latitude as `districts` below — a Type tab picks a
        # property type, not a claim that both sides of the pair already agree.
        cat_where, cat_params = _either_side_eq(
            "category_main", category_main, ("l", "r"), "category_main",
        )
        clauses.append(cat_where)
        params.update(cat_params)
    if districts:
        # A pair matches if EITHER property in it touches the picked place —
        # the operator is prioritising review work by location, not asserting
        # both sides already agree on one (that's exactly what review decides).
        d_where, d_params = district_where(districts, aliases=["l", "r"])
        clauses.extend(d_where)
        params.update(d_params)
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where_sql, params


def list_candidates(
    conn: psycopg.Connection,
    *,
    status: str | None = "proposed",
    tier: str | None = None,
    reason: str | None = None,
    verdict: str | None = None,
    districts: list[DistrictChip] | None = None,
    category_main: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    where_sql, params = _candidate_filters(
        status, tier, reason, verdict, districts, category_main,
    )

    with conn.cursor() as cur:
        # Real total for THIS filter (the page is capped at `limit`), so the UI
        # can show the full backlog size + paginate — not just the page count.
        # Shares `_CANDIDATES_FROM` with the page SELECT below (see its comment).
        cur.execute(f"SELECT count(*) {_CANDIDATES_FROM} {where_sql}", params)
        total = int(cur.fetchone()[0])

        cur.execute(
            f"""
            SELECT
              c.id, c.tier, c.status, c.confidence, c.markers_matched,
              c.auto_merged, c.merge_group_id::text, c.created_at, c.reviewed_at,
              {_PROP_SIDE_SQL.format(p="l", rl="ll")} AS left_property,
              {_PROP_SIDE_SQL.format(p="r", rl="rl")} AS right_property,
              f.is_incorrect, f.expected_outcome, f.note, f.updated_at
            {_CANDIDATES_FROM}
            LEFT JOIN listings ll ON ll.sreality_id = l.repr_listing_id
            LEFT JOIN listings rl ON rl.sreality_id = r.repr_listing_id
            LEFT JOIN dedup_decision_feedback f
              ON f.left_property_id = least(c.left_property_id, c.right_property_id)
             AND f.right_property_id = greatest(c.left_property_id, c.right_property_id)
            {where_sql}
            ORDER BY c.created_at DESC, c.id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {**params, "limit": limit, "offset": offset},
        )
        rows = cur.fetchall()

    data = [
        {
            "id": r[0],
            "tier": r[1],
            "status": r[2],
            "confidence": float(r[3]) if r[3] is not None else None,
            "markers_matched": r[4],
            "auto_merged": r[5],
            "merge_group_id": r[6],
            "created_at": r[7],
            "reviewed_at": r[8],
            "left_property": r[9],
            "right_property": r[10],
            "feedback": _feedback_obj(r[11], r[12], r[13], r[14]),
            "audit_breakdown": build_audit_breakdown(r[4]),
        }
        for r in rows
    ]
    return {"data": data, "total": total, "returned": len(data)}


def summary(conn: psycopg.Connection, *, status: str = "proposed") -> dict[str, Any]:
    """Cumulative review backlog + its composition by reason (why each pair queued).

    The /dedup dashboard reads this so the operator sees the WHOLE pending queue
    and what it's made of — not just the page of cards on screen. `reason` /
    `verdict` are the filter keys list_candidates accepts to drill into a bucket
    (legacy rows — no recorded reason — bucket under LEGACY_REASON).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              coalesce(markers_matched->>'reason', %(legacy)s) AS reason,
              markers_matched->>'verdict' AS verdict,
              count(*) AS n
            FROM property_identity_candidates
            WHERE status = %(status)s
            GROUP BY 1, 2
            ORDER BY n DESC
            """,
            {"status": status, "legacy": LEGACY_REASON},
        )
        buckets = [
            {"reason": r[0], "verdict": r[1], "count": int(r[2])}
            for r in cur.fetchall()
        ]
        # Per-tier facet for the review rail: each family's pending count + the
        # bulk-approvable STRONG subset (geo matches with concordant area+price/house#).
        cur.execute(
            """
            SELECT c.tier, count(*) AS n,
                   count(*) FILTER (
                     WHERE c.markers_matched->>'reason' IN ('geo_exact', 'geo_strong')
                   ) AS strong
            FROM property_identity_candidates c
            WHERE c.status = %(status)s
            GROUP BY c.tier
            ORDER BY n DESC
            """,
            {"status": status},
        )
        tiers = [
            {"tier": r[0], "count": int(r[1]), "strong": int(r[2])}
            for r in cur.fetchall()
        ]
    return {
        "data": {
            "status": status,
            "total": sum(b["count"] for b in buckets),
            "buckets": buckets,
            "tiers": tiers,
        }
    }


# The decision-FACTOR filter (the photo/score signal a decision turned on): each maps to
# a row predicate and, where the factor is numeric, the detail key to threshold. Lets the
# operator review the borderline tail of one signal — e.g. pHash merges with the fewest
# matching photos, or cosine routings just below a cutoff — to validate the engine's bars.
# Keys come from this fixed dict (never the request), so interpolating them is injection-safe.
_FACTOR_FILTER: dict[str, tuple[str, str | None]] = {
    "phash":      ("a.stage = 'phash'",   "phash_pairs"),
    "cosine":     ("a.detail ? 'cosine'", "cosine"),
    "visual":     ("a.stage = 'visual'",  None),
    "address":    ("a.stage = 'address'", None),
    # A real terminal reason (stamped under stage='phash' — the floor-plan gate runs as a
    # post-check on a phash match), unlike site_plan_different_unit which never reaches
    # dedup_pair_audit at all (that gate's action is always "queue" — see Needs-review's
    # bucket picker / bucketLabel instead).
    "floor_plan": ("a.detail->>'reason' = 'floor_plan_different_layout'", None),
}


def list_pair_audit(
    conn: psycopg.Connection,
    *,
    outcome: str | None = None,
    category_main: str | None = None,
    source: str | None = None,
    stage: str | None = None,
    factor: str | None = None,
    factor_min: float | None = None,
    factor_max: float | None = None,
    verdict: str | None = None,
    room_type: str | None = None,
    property_id: int | None = None,
    property_id_in: list[int] | None = None,
    flagged: bool | None = None,
    districts: list[DistrictChip] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """The unified Decision history feed: every TERMINAL dedup decision (merged |
    dismissed), engine AND operator, newest first. Filterable by property type
    (`category_main`), `outcome`, `source`, `stage`, and by the decision FACTOR: `factor`
    ∈ {phash, cosine, visual, address, floor_plan} with a numeric `factor_min`/`factor_max`
    on its signal (phash_pairs / cosine), or a `verdict` for visual rows — so the operator
    can audit the borderline decisions of one signal. `room_type` filters to decisions
    whose factor detail names that compared room/plan tag (`detail->>'room_type'`, the
    same value `DedupFactors` already renders — e.g. 'kitchen', 'floor_plan', 'site_plan').
    `property_id` scopes to the decisions that touch one property's child listings (the
    listing-detail "merge decisions" link); `property_id_in` is the batched form (one call
    for many on-screen properties, e.g. the CLIP audit page's property cards) — keyed on
    the stable `sreality_id` since `property_id` re-points on every merge.
    `merge_group_id` is the inline-undo handle; `undone` is DERIVED by joining the merge
    ledger (the single source of truth). `category_main` and `districts` both match a
    decision if EITHER side's (merged-away-surviving, rule #18) `properties` row
    satisfies it — `dedup_pair_audit.category_main` is the engine's single stamped
    classification for the whole pair (falls back to whichever side was non-NULL), so
    a sanctioned dům↔komerční cross-type merge can be stamped with only ONE of the two
    types; filtering the pair's OWN two `properties` rows instead of that stamped column
    is what lets it surface under both type tabs. The property row's location columns
    aren't cleared on merge, so even a since-retired property still filters correctly."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if outcome is not None:
        clauses.append("a.outcome = %(outcome)s")
        params["outcome"] = outcome
    if category_main is not None:
        cat_where, cat_params = _either_side_eq(
            "category_main", category_main, ("pl", "pr"), "category_main",
        )
        clauses.append(cat_where)
        params.update(cat_params)
    if source is not None:
        clauses.append("a.source = %(source)s")
        params["source"] = source
    if stage is not None:
        clauses.append("a.stage = %(stage)s")
        params["stage"] = stage
    if factor in _FACTOR_FILTER:
        clause, key = _FACTOR_FILTER[factor]
        clauses.append(clause)
        if key is not None and factor_min is not None:
            clauses.append(f"(a.detail->>'{key}')::numeric >= %(factor_min)s")
            params["factor_min"] = factor_min
        if key is not None and factor_max is not None:
            clauses.append(f"(a.detail->>'{key}')::numeric <= %(factor_max)s")
            params["factor_max"] = factor_max
    if verdict is not None:
        clauses.append("a.detail->>'verdict' = %(verdict)s")
        params["verdict"] = verdict
    if room_type is not None:
        clauses.append("a.detail->>'room_type' = %(room_type)s")
        params["room_type"] = room_type
    if property_id is not None:
        clauses.append(
            "(a.left_sreality_id IN "
            "  (SELECT sreality_id FROM listings WHERE property_id = %(audit_pid)s)"
            " OR a.right_sreality_id IN "
            "  (SELECT sreality_id FROM listings WHERE property_id = %(audit_pid)s))"
        )
        params["audit_pid"] = property_id
    if property_id_in:
        clauses.append(
            "(a.left_sreality_id IN "
            "  (SELECT sreality_id FROM listings WHERE property_id = ANY(%(audit_pids)s))"
            " OR a.right_sreality_id IN "
            "  (SELECT sreality_id FROM listings WHERE property_id = ANY(%(audit_pids)s)))"
        )
        params["audit_pids"] = list(property_id_in)
    if flagged:
        clauses.append("f.is_incorrect IS TRUE")
    if districts:
        # A decision matches if EITHER property it touched is in the picked
        # place. `dedup_pair_audit` has no location columns of its own — join
        # the (nullable) property ids to `properties` for its retained
        # (merge-survivng, rule #18) location columns.
        d_where, d_params = district_where(districts, aliases=["pl", "pr"])
        clauses.extend(d_where)
        params.update(d_params)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    # The pair-keyed feedback flag (Decision history + Needs-review share one store):
    # join on the canonical (low, high) PROPERTY pair the audit row SNAPSHOTTED at
    # decision time (immutable — unlike the drifting repr listing), in EITHER stored
    # order, guarded against a NULL property id.
    feedback_join = (
        "LEFT JOIN dedup_decision_feedback f "
        "  ON a.left_property_id IS NOT NULL AND a.right_property_id IS NOT NULL "
        " AND f.left_property_id = least(a.left_property_id, a.right_property_id) "
        " AND f.right_property_id = greatest(a.left_property_id, a.right_property_id) "
    )
    # properties joins for the per-side filters (location, category_main) only —
    # LEFT (not INNER) so a NULL property_id (pre-migration-091 rows) still
    # surfaces when neither filter is applied; `category_main`/`districts`
    # above simply can't match a NULL join, which correctly drops those rows
    # once either filter is set. Shared by BOTH filters (not two separate
    # joins) so the COUNT and the page SELECT below can't disagree about which
    # tables the WHERE clause references.
    side_join = (
        "LEFT JOIN properties pl ON pl.id = a.left_property_id "
        "LEFT JOIN properties pr ON pr.id = a.right_property_id "
    ) if (districts or category_main is not None) else ""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM dedup_pair_audit a {feedback_join} "
            f"{side_join}{where}",
            params,
        )
        total = int(cur.fetchone()[0])
        cur.execute(
            f"""
            SELECT a.id, a.run_at,
                   CASE WHEN a.left_sreality_id = a.right_sreality_id
                             AND a.merge_group_id IS NOT NULL
                        THEN {_ledger_side_sql("a.left_property_id", "a.merge_group_id::uuid")}
                        ELSE a.left_sreality_id END AS left_sreality_id,
                   CASE WHEN a.left_sreality_id = a.right_sreality_id
                             AND a.merge_group_id IS NOT NULL
                        THEN {_ledger_side_sql("a.right_property_id", "a.merge_group_id::uuid")}
                        ELSE a.right_sreality_id END AS right_sreality_id,
                   a.left_property_id, a.right_property_id, a.category_main,
                   a.stage, a.outcome, a.source, a.merge_group_id, a.detail,
                   m.fully_undone,
                   f.is_incorrect, f.expected_outcome, f.note, f.updated_at
            FROM dedup_pair_audit a
            {feedback_join}
            {side_join}
            LEFT JOIN LATERAL (
              SELECT bool_and(e.undone_at IS NOT NULL) AS fully_undone
              FROM property_merge_events e
              WHERE a.merge_group_id IS NOT NULL
                AND e.merge_group_id = a.merge_group_id::uuid
            ) m ON true
            {where}
            ORDER BY a.run_at DESC, a.id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {**params, "limit": limit, "offset": offset},
        )
        rows = cur.fetchall()
    return {
        "data": [
            {
                "audit_id": r[0], "run_at": r[1],
                "left_sreality_id": r[2], "right_sreality_id": r[3],
                "left_property_id": r[4], "right_property_id": r[5],
                "category_main": r[6], "stage": r[7], "outcome": r[8],
                "source": r[9], "merge_group_id": r[10], "detail": r[11],
                "undone": bool(r[12]),
                "feedback": _feedback_obj(r[13], r[14], r[15], r[16]),
                "audit_breakdown": build_audit_breakdown(r[11]),
            }
            for r in rows
        ],
        "total": total,
        "returned": len(rows),
    }


def _listing_room_images(
    cur: Any, sreality_id: int, room_type: str | None, n: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Images for the deciding room of one listing (CLIP image_clip_tags OR the
    LLM image_room_classifications cache — both backend-only, hence this server
    hop), falling back to the listing's first images when the room is unset /
    untagged. Prefers CLIP so the photos reflect what the engine actually paired
    on (the engine runs dedup_prefer_clip_tags). Returns (images, fallback)."""
    if room_type:
        cur.execute(
            "SELECT i.id, i.sreality_url, i.storage_path FROM images i "
            "WHERE i.sreality_id = %(sid)s AND ("
            "  EXISTS (SELECT 1 FROM image_clip_tags t "
            "          WHERE t.image_id = i.id AND t.logical_tag = %(rt)s) "
            "  OR EXISTS (SELECT 1 FROM image_room_classifications c "
            "             WHERE c.image_id = i.id AND c.room_type = %(rt)s)) "
            "ORDER BY i.sequence NULLS LAST, i.id LIMIT %(n)s",
            {"sid": sreality_id, "rt": room_type, "n": n},
        )
        rows = cur.fetchall()
        if rows:
            return ([{"image_id": r[0], "sreality_url": r[1], "storage_path": r[2]}
                     for r in rows], False)
    cur.execute(
        "SELECT id, sreality_url, storage_path FROM images WHERE sreality_id = %s "
        "ORDER BY sequence NULLS LAST, id LIMIT %s",
        (sreality_id, n),
    )
    return ([{"image_id": r[0], "sreality_url": r[1], "storage_path": r[2]}
             for r in cur.fetchall()],
            bool(room_type))


# Verified live (EXPLAIN ANALYZE against prod): this DB's shared_buffers is small
# relative to images/image_clip_tags, so any sufficiently-scattered read pattern over
# them is cold-I/O-bound — a single request crossing images for ~800 dedup_pair_audit
# rows already costs ~5s; 3800 in one shot measured 26s+. No query rewrite fixed this
# (tried: pushing the tag filter earlier via CTEs/indexes — all measured SLOWER, since
# "kitchen" alone is ~774k rows, not selective enough to help, and the per-image tag
# lookup has to hit the same cold pages either way). So /phash-audit paginates the
# SCOPE itself in bounded chunks, not the joined result: each request processes up to
# a few CHUNK-sized windows of dedup_pair_audit (newest-first), stopping as soon as it
# has `limit` matching image pairs or hits the ceiling — bounding worst-case latency
# per request while letting the operator reach the full ceiling by scrolling.
_PHASH_AUDIT_CHUNK = 800
_PHASH_AUDIT_SCAN_CEILING = 3800  # the operator's ask: the prior 800 + 3000 more


def _phash_audit_chunk(
    cur: Any, *, scope_where: str, scope_params: dict[str, Any],
    hamming_min: int, hamming_max: int, room_types: list[str] | None,
    training_only: bool, training_label: str | None, chunk_offset: int, chunk_size: int,
) -> list[tuple[Any, ...]]:
    """One bounded window of the scan: the `chunk_size` dedup_pair_audit rows starting
    at `chunk_offset` (newest-first), cross-joined against images, Hamming-filtered.
    OFFSET on dedup_pair_audit itself is cheap at this scale (verified: ~90ms at
    offset=3800 — a much smaller table than images) — the cost lives entirely in the
    per-chunk image cross-join below, which is what stays bounded."""
    join_params: dict[str, Any] = {
        **scope_params, "hmin": hamming_min, "hmax": hamming_max,
        "off": chunk_offset, "chunk": chunk_size,
    }
    room_clause = ""
    if room_types:
        room_clause = (
            " AND ta.logical_tag = ANY(%(room_types)s)"
            " AND tb.logical_tag = ANY(%(room_types)s)"
            " AND ta.logical_tag = tb.logical_tag"
        )
        join_params["room_types"] = list(room_types)
    # Exact per-image check (not just "this pair's LISTING has a trained image
    # somewhere") — image_training_examples.image_id is unique-indexed, so each EXISTS
    # is a cheap point lookup, not a scan. phash_audit()'s scope-level sreality_id
    # pre-filter (below) is what keeps chunks from coming back mostly-empty; this is
    # what keeps the result correct at the individual-photo grain. `training_label`
    # narrows further to a SPECIFIC label (e.g. auditing one class at a time), not just
    # "has any training label at all".
    training_clause = ""
    if training_only or training_label:
        label_match = " AND te.label = %(training_label)s" if training_label else ""
        training_clause = (
            f" AND (EXISTS (SELECT 1 FROM image_training_examples te WHERE te.image_id = ia.id{label_match})"
            f"   OR EXISTS (SELECT 1 FROM image_training_examples te WHERE te.image_id = ib.id{label_match}))"
        )
        if training_label:
            join_params["training_label"] = training_label
    cur.execute(
        f"""
        WITH scoped AS (
            SELECT a.id, a.left_sreality_id, a.right_sreality_id,
                   a.left_property_id, a.right_property_id,
                   a.outcome, a.category_main, a.run_at, a.stage, a.detail
            FROM dedup_pair_audit a
            {scope_where}
            ORDER BY a.run_at DESC
            OFFSET %(off)s LIMIT %(chunk)s
        )
        SELECT s.id, s.left_sreality_id, s.right_sreality_id,
               s.left_property_id, s.right_property_id, s.outcome,
               s.category_main, s.run_at, s.stage, s.detail,
               ia.id, ia.sreality_url, ia.storage_path,
               ta.logical_tag, ta.fine_tag, ta.confidence, ta.render_score,
               ib.id, ib.sreality_url, ib.storage_path,
               tb.logical_tag, tb.fine_tag, tb.confidence, tb.render_score,
               bit_count((ia.phash # ib.phash)::bit(64)) AS hamming
        FROM scoped s
        JOIN images ia ON ia.sreality_id = s.left_sreality_id AND ia.phash IS NOT NULL
        JOIN images ib ON ib.sreality_id = s.right_sreality_id AND ib.phash IS NOT NULL
        LEFT JOIN LATERAL (
            SELECT t.logical_tag, t.fine_tag, t.confidence, t.render_score
            FROM image_clip_tags t
            WHERE t.image_id = ia.id ORDER BY t.tagged_at DESC LIMIT 1
        ) ta ON true
        LEFT JOIN LATERAL (
            SELECT t.logical_tag, t.fine_tag, t.confidence, t.render_score
            FROM image_clip_tags t
            WHERE t.image_id = ib.id ORDER BY t.tagged_at DESC LIMIT 1
        ) tb ON true
        WHERE bit_count((ia.phash # ib.phash)::bit(64)) BETWEEN %(hmin)s AND %(hmax)s
        {room_clause}
        {training_clause}
        ORDER BY hamming ASC, s.run_at DESC
        LIMIT 500
        """,
        join_params,
    )
    return cur.fetchall()


def phash_audit(
    conn: psycopg.Connection,
    *,
    hamming_min: int,
    hamming_max: int,
    category_main: str | None = None,
    outcome: str | None = None,
    room_types: list[str] | None = None,
    training_only: bool = False,
    training_label: str | None = None,
    training_exclude: bool = False,
    scan_offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """/phash-audit: matching-photo image PAIRs, across the pairs the engine already
    looked at (dedup_pair_audit), whose live-recomputed Hamming distance falls in
    [hamming_min, hamming_max] — a direct range browse, not a merged-vs-dismissed
    comparison (the operator's ask: "show me image pairs that were phash scored and were
    within this range"). Hamming is a cheap bitwise `bit_count(a XOR b)` over the already-
    stored `images.phash` bigints — no vision recompute, no re-scoring; this mirrors the
    ENGINE's own phash pass (`_phash_identical_pairs`), which is deliberately room-blind
    (a near-identical Hamming distance detects a reused/re-scaled PHOTO FILE, not "the
    same room" — CLIP's room tag is a separate, independent classification of each image,
    shown for context only, never a comparison constraint on the engine side). `room_types`
    requires BOTH images to carry the SAME tag, and that tag to be one of the given set —
    the operator's ask is "show me kitchen-vs-kitchen (or bathroom-vs-bathroom, ...)
    pairs", not "either side happens to be one of these". `training_only` narrows to
    pairs where at least one of the two specific images shown already has a
    linear-probe training-set label (image_training_examples) — the exact photo, not
    just some other photo on the same listing. `training_label`, if given, narrows
    further to that SPECIFIC label (implies training_only regardless of its literal
    value) — auditing one class's coverage at a time. `training_exclude` is the
    inverse — pairs where NEITHER shown image is in the training set yet, for finding
    fresh material to review rather than revisiting what's already been picked. Takes
    priority over training_only/training_label if somehow both are set (the frontend
    never does — it's a single 3-way choice).

    Pagination is over the SCOPE (dedup_pair_audit), not the joined result — see
    `_PHASH_AUDIT_CHUNK`'s comment for why. `scan_offset` is the opaque cursor (how many
    scoped decisions have already been scanned); pass back `next_scan_offset` for the
    next call. ONE call processes exactly ONE chunk (bounded, predictable latency —
    matches the CHUNK size regardless of how sparse the filter is); the caller (the
    /phash-audit page) loops calls forward until it has a full page or `next_scan_offset`
    comes back null, since a single chunk can legitimately return fewer than `limit`
    rows while more remains to scan (an infinite-scroll page-size check can't tell
    "short because sparse" from "short because done" apart on its own)."""
    scope_clauses: list[str] = []
    scope_params: dict[str, Any] = {}
    if category_main is not None:
        scope_clauses.append("a.category_main = %(category_main)s")
        scope_params["category_main"] = category_main
    if outcome is not None:
        scope_clauses.append("a.outcome = %(outcome)s")
        scope_params["outcome"] = outcome
    with conn.cursor() as cur:
        if training_exclude:
            # Unlike the inclusion case below, the SCOPE check alone is already exact
            # here — trained_sreality_ids is "every sreality_id that owns at least one
            # trained image", so excluding those listings guarantees neither remaining
            # image can possibly be a trained one. No post-join re-check needed (see
            # _phash_audit_chunk: training_only/training_label stay False here, so it
            # never adds one). Also no "narrows to a tiny set" efficiency win the
            # inclusion branch gets — the training set is tiny, so excluding it barely
            # shrinks a huge population — but a small NOT-IN-style array check is cheap
            # regardless, and an empty training set means nothing to exclude at all.
            cur.execute(
                "SELECT DISTINCT i.sreality_id FROM images i "
                "JOIN image_training_examples te ON te.image_id = i.id "
                "WHERE i.sreality_id IS NOT NULL",
            )
            trained_sreality_ids = [r[0] for r in cur.fetchall()]
            if trained_sreality_ids:
                scope_clauses.append(
                    "NOT (a.left_sreality_id = ANY(%(trained_sreality_ids)s)"
                    " OR a.right_sreality_id = ANY(%(trained_sreality_ids)s))",
                )
                scope_params["trained_sreality_ids"] = trained_sreality_ids
        elif training_only or training_label:
            # The training set is tiny (an operator hand-picks it) next to
            # dedup_pair_audit's population — without this, almost every chunk would
            # come back empty post-join, forcing the frontend to scan the whole
            # ceiling for a handful of rows. Narrowing the SCOPE to only the listings
            # that own a (matching) trained image keeps a chunk's odds of matching
            # close to 1, same as any other scope filter (category_main/outcome)
            # already does.
            lookup_sql = (
                "SELECT DISTINCT i.sreality_id FROM images i "
                "JOIN image_training_examples te ON te.image_id = i.id "
                "WHERE i.sreality_id IS NOT NULL"
            )
            lookup_params: dict[str, Any] = {}
            if training_label:
                lookup_sql += " AND te.label = %(training_label)s"
                lookup_params["training_label"] = training_label
            cur.execute(lookup_sql, lookup_params)
            trained_sreality_ids = [r[0] for r in cur.fetchall()]
            if not trained_sreality_ids:
                return {
                    "data": [], "returned": 0, "scanned_pairs": 0,
                    "scan_cap": _PHASH_AUDIT_SCAN_CEILING, "scanned_so_far": 0,
                    "next_scan_offset": None,
                }
            scope_clauses.append(
                "(a.left_sreality_id = ANY(%(trained_sreality_ids)s)"
                " OR a.right_sreality_id = ANY(%(trained_sreality_ids)s))",
            )
            scope_params["trained_sreality_ids"] = trained_sreality_ids

        scope_where = ("WHERE " + " AND ".join(scope_clauses)) if scope_clauses else ""
        cur.execute(
            f"SELECT count(*) FROM dedup_pair_audit a {scope_where}", scope_params,
        )
        scanned_pairs = int(cur.fetchone()[0])

        ceiling = min(_PHASH_AUDIT_SCAN_CEILING, scanned_pairs)
        offset = scan_offset
        collected: list[tuple[Any, ...]] = []
        if offset < ceiling:
            window = min(_PHASH_AUDIT_CHUNK, ceiling - offset)
            collected = _phash_audit_chunk(
                cur, scope_where=scope_where, scope_params=scope_params,
                hamming_min=hamming_min, hamming_max=hamming_max,
                room_types=room_types,
                # training_exclude's scope-level NOT check is already exact (see
                # above) — never let the inclusion post-join EXISTS clause layer on
                # top of it, even if a caller sent both.
                training_only=training_only and not training_exclude,
                training_label=None if training_exclude else training_label,
                chunk_offset=offset, chunk_size=window,
            )
            offset += window
    rows = collected[:limit]
    next_scan_offset = offset if offset < ceiling else None
    return {
        "data": [
            {
                "audit_id": r[0], "left_sreality_id": r[1], "right_sreality_id": r[2],
                "left_property_id": r[3], "right_property_id": r[4],
                "outcome": r[5], "category_main": r[6], "run_at": r[7],
                "stage": r[8],
                # What ACTUALLY decided this pair (may well be a different signal than
                # the phash row it's shown on — the point of this page is exactly to
                # surface that gap, e.g. "phash found nothing, forensic vision dismissed
                # it" — so the operator never has to guess from the Hamming number alone).
                "audit_breakdown": build_audit_breakdown(r[9]),
                "left_image": {
                    "image_id": r[10], "sreality_url": r[11], "storage_path": r[12],
                    "room_type": r[13], "fine_tag": r[14], "confidence": r[15],
                    "render_score": r[16],
                },
                "right_image": {
                    "image_id": r[17], "sreality_url": r[18], "storage_path": r[19],
                    "room_type": r[20], "fine_tag": r[21], "confidence": r[22],
                    "render_score": r[23],
                },
                "hamming": int(r[24]),
            }
            for r in rows
        ],
        "returned": len(rows),
        "scanned_pairs": scanned_pairs,
        "scan_cap": _PHASH_AUDIT_SCAN_CEILING,
        "scanned_so_far": offset,
        "next_scan_offset": next_scan_offset,
    }


def _phash_pair_evidence(
    cur: Any, a_id: int, b_id: int, category_main: str | None, limit: int,
) -> list[dict[str, Any]]:
    """The actual near-identical image PAIRS (Hamming <= bar) the pHash signal turned on,
    recomputed from stored phashes with the SAME category exclusions the engine applied —
    so 'the specific pictures' are exactly the ones that drove the decision, recoverable
    for ANY historical row (nothing extra had to be stored at decision time)."""
    excluded = phash_excluded_tags_for(category_main)
    rmin = phash_render_exclude_for(category_main, RENDER_SCORE_EXCLUDE_MIN)
    params: dict[str, Any] = {"a": a_id, "b": b_id, "max": PHASH_IDENTICAL_MAX, "lim": limit}
    sql = (
        "SELECT ia.id, ia.sreality_url, ia.storage_path, "
        "       ib.id, ib.sreality_url, ib.storage_path, "
        "       bit_count((ia.phash # ib.phash)::bit(64)) AS hamming "
        "FROM images ia JOIN images ib ON true "
        "WHERE ia.sreality_id = %(a)s AND ib.sreality_id = %(b)s "
        "  AND ia.phash IS NOT NULL AND ib.phash IS NOT NULL "
        "  AND bit_count((ia.phash # ib.phash)::bit(64)) <= %(max)s"
    )
    sql += render_exclusion_clause(params, "ia", excluded, rmin)
    sql += render_exclusion_clause(params, "ib", excluded, rmin)
    sql += " ORDER BY hamming ASC, ia.id, ib.id LIMIT %(lim)s"
    cur.execute(sql, params)
    return [
        {
            "hamming": int(r[6]),
            "left": {"image_id": r[0], "sreality_url": r[1], "storage_path": r[2]},
            "right": {"image_id": r[3], "sreality_url": r[4], "storage_path": r[5]},
        }
        for r in cur.fetchall()
    ]


def decision_evidence(
    conn: psycopg.Connection,
    *,
    left_sreality_id: int,
    right_sreality_id: int,
    stage: str | None = None,
    reason: str | None = None,
    room_type: str | None = None,
    category_main: str | None = None,
    per_side: int = 4,
) -> dict[str, Any]:
    """The SPECIFIC pictures behind a decision, resolved at READ time from stored data
    (no decision-time bloat, works on every historical row). The engine's own gate picks
    the evidence: a pHash decision → the exact near-identical PAIRS; a floor/site-plan
    override → both sides' PLANS; a forensic verdict → the deciding ROOM; else first
    photos. Faithful to what the engine compared. Read-only."""
    per_side = max(1, min(per_side, 8))
    reason = reason or ""
    # The strip room: a plan override shows the plans; a forensic verdict its room.
    if "floor_plan" in reason:
        strip_room: str | None = FLOOR_PLAN_ROOM_TYPE
    elif reason == "site_plan_different_unit":
        strip_room = SITE_PLAN_ROOM_TYPE
    else:
        strip_room = room_type
    want_pairs = stage == "phash" or reason == "image_phash"
    with conn.cursor() as cur:
        pairs = (_phash_pair_evidence(
            cur, left_sreality_id, right_sreality_id, category_main, per_side * 2)
            if want_pairs else None)
        left, lfb = _listing_room_images(cur, left_sreality_id, strip_room, per_side)
        right, rfb = _listing_room_images(cur, right_sreality_id, strip_room, per_side)
    return {
        "data": {
            "pairs": pairs or None,
            "room_type": strip_room,
            "left": {"sreality_id": left_sreality_id, "images": left, "fallback": lfb},
            "right": {"sreality_id": right_sreality_id, "images": right, "fallback": rfb},
        }
    }


def pipeline_overview(conn: psycopg.Connection) -> dict[str, Any]:
    """The top-of-page dedup funnel: one cheap number per stage plus its last-24h
    movement, so the operator can see work flowing top→bottom (photos tagged →
    listings eligible → candidate pairs → decisions). Polled every 60s, so it must
    not scan the multi-million-row CLIP tables: cumulative CLIP totals are O(1)
    pg_class.reltuples planner estimates (exact magnitude doesn't matter on a
    dashboard), the 24h tag delta is a bounded index range scan
    (image_clip_tags_tagged_at_idx, migration 231), and the rest are small-table
    aggregates + the single latest run row."""
    with conn.cursor() as cur:
        # Cumulative CLIP totals: planner estimates, not exact counts (those would
        # seq-scan tables on a 5M-row growth path on every poll).
        cur.execute(
            "SELECT relname, greatest(reltuples, 0)::bigint FROM pg_class "
            "WHERE relname IN ('image_clip_tags', 'image_clip_embeddings')"
        )
        est = {row[0]: int(row[1]) for row in cur.fetchall()}
        tags_total = est.get("image_clip_tags", 0)
        emb_total = est.get("image_clip_embeddings", 0)
        # The moving number: exact, but bounded by the tagged_at index.
        cur.execute(
            "SELECT count(*) FROM image_clip_tags WHERE tagged_at > now() - interval '24 hours'"
        )
        tags_24h = int(cur.fetchone()[0])
        cur.execute(
            "SELECT count(*) FILTER (WHERE status='proposed'), "
            "count(*) FILTER (WHERE status='proposed' AND created_at > now() - interval '24 hours') "
            "FROM property_identity_candidates"
        )
        cand_open, cand_24h = cur.fetchone()
        cur.execute(
            "SELECT count(*) FILTER (WHERE outcome='merged'), "
            "count(*) FILTER (WHERE outcome='dismissed'), "
            "count(*) FILTER (WHERE run_at > now() - interval '24 hours') "
            "FROM dedup_pair_audit"
        )
        merged_total, dismissed_total, dec_24h = cur.fetchone()
        # Gauges and activity decoupled (migration 265): scoped runs write NULL market
        # gauges (the ~9s full-table aggregate only runs on full scans), so the gauges
        # come from the latest FULL-scan row (run_kind='full'; run_kind IS NULL covers
        # pre-262 rows, which always carried market-wide values). Activity stays the
        # latest run of ANY lane, labeled by run_kind.
        cur.execute(
            "SELECT eligible, flagged_location, flagged_disposition "
            "FROM dedup_engine_runs "
            "WHERE eligible IS NOT NULL AND (run_kind = 'full' OR run_kind IS NULL) "
            "ORDER BY id DESC LIMIT 1"
        )
        g = cur.fetchone()
        cur.execute(
            "SELECT started_at, run_kind, "
            "auto_address, auto_phash, auto_visual, auto_dismissed, queued, "
            "clip_classified, routed_haiku, routed_sonnet, vision_calls "
            # id (insert order), NOT started_at: since migration 262 started_at is the
            # REAL run start, so a long full scan would sort below dirty runs that
            # started after it and this headline would never show a completed scan.
            "FROM dedup_engine_runs ORDER BY id DESC LIMIT 1"
        )
        r = cur.fetchone()
    eligible = flagged_loc = flagged_disp = 0
    if g is not None:
        eligible, flagged_loc, flagged_disp = int(g[0]), int(g[1]), int(g[2])
    last_run: dict[str, Any] | None = None
    if r is not None:
        last_run = {
            "started_at": r[0],
            "run_kind": r[1],
            "auto_merged": int(r[2]) + int(r[3]) + int(r[4]),
            "auto_dismissed": int(r[5]),
            "queued": int(r[6]),
            "clip_classified": int(r[7]),
            "routed_haiku": int(r[8]),
            "routed_sonnet": int(r[9]),
            "vision_calls": int(r[10]),
        }
    return {
        "data": {
            "tagging": {
                "total": int(tags_total), "delta_24h": int(tags_24h),
                "embeddings": emb_total,
            },
            "eligible": {
                "total": eligible, "flagged_location": flagged_loc,
                "flagged_disposition": flagged_disp,
            },
            "candidates": {"total": int(cand_open), "delta_24h": int(cand_24h)},
            "decisions": {
                "total": int(merged_total) + int(dismissed_total),
                "delta_24h": int(dec_24h),
                "merged": int(merged_total), "dismissed": int(dismissed_total),
            },
            "last_run": last_run,
        }
    }


def pipeline_timeline(
    conn: psycopg.Connection, *, bucket: str = "day", points: int | None = None,
) -> dict[str, Any]:
    """Throughput for the dedup funnel, zero-filled, per `bucket` ('day' over ~2 weeks,
    or 'hour' over ~2 days) so the operator can see how it evolves at either grain
    (mirrors the Health reconciliation Hour/Day toggle). Images tagged
    (image_clip_tags.tagged_at, indexed), candidates created, and decisions (merged /
    dismissed). One query, small/indexed aggregates — no image-table scan. `bucket` is
    validated to a fixed set, so interpolating it (and its make_interval keyword) is safe."""
    bucket = "hour" if bucket == "hour" else "day"
    kw = "hours" if bucket == "hour" else "days"
    default, cap = (48, 168) if bucket == "hour" else (14, 90)
    n = max(1, min(points or default, cap))
    sql = f"""
        WITH d AS (
          SELECT generate_series(
                   date_trunc('{bucket}', now()) - make_interval({kw} => %(n)s - 1),
                   date_trunc('{bucket}', now()), interval '1 {bucket}') AS b
        ),
        tg AS (
          SELECT date_trunc('{bucket}', tagged_at) AS b, count(*) AS n FROM image_clip_tags
          WHERE tagged_at >= now() - make_interval({kw} => %(n)s) GROUP BY 1
        ),
        de AS (
          SELECT date_trunc('{bucket}', run_at) AS b,
                 count(*) FILTER (WHERE outcome='merged')    AS merged,
                 count(*) FILTER (WHERE outcome='dismissed') AS dismissed
          FROM dedup_pair_audit
          WHERE run_at >= now() - make_interval({kw} => %(n)s) GROUP BY 1
        ),
        ca AS (
          SELECT date_trunc('{bucket}', created_at) AS b, count(*) AS n
          FROM property_identity_candidates
          WHERE created_at >= now() - make_interval({kw} => %(n)s) GROUP BY 1
        )
        SELECT d.b, coalesce(tg.n,0), coalesce(de.merged,0),
               coalesce(de.dismissed,0), coalesce(ca.n,0)
        FROM d
        LEFT JOIN tg ON tg.b = d.b
        LEFT JOIN de ON de.b = d.b
        LEFT JOIN ca ON ca.b = d.b
        ORDER BY d.b
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"n": n})
        rows = cur.fetchall()
    return {
        "grain": bucket,
        "data": [
            {"bucket": r[0].isoformat(), "tagged": int(r[1]), "merged": int(r[2]),
             "dismissed": int(r[3]), "candidates": int(r[4])}
            for r in rows
        ],
    }


def clip_coverage(
    conn: psycopg.Connection, *, priority_region_id: int = 27,
) -> dict[str, Any]:
    """CLIP backfill progress — totals + the priority tiers the operator tracks to
    time the flip. LISTING-grain (a listing counts as covered once ANY of its images
    is tagged): far cheaper than image-grain (one scan of the small listings table +
    two hash semi-joins) and clearer to read ("X of Y listings"). Model-agnostic
    (one CLIP model is in use) so the API needs no taxonomy file."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM image_clip_tags")
        total_tags = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM image_clip_embeddings")
        total_emb = int(cur.fetchone()[0])

        cur.execute(
            """
            WITH tagged AS (
              SELECT DISTINCT i.sreality_id
              FROM image_clip_tags t JOIN images i ON i.id = t.image_id
            ),
            cand AS (
              SELECT left_property_id AS pid FROM property_identity_candidates
              WHERE status = 'proposed'
              UNION
              SELECT right_property_id FROM property_identity_candidates
              WHERE status = 'proposed'
            )
            SELECT
              count(*) FILTER (WHERE inc.cand)                  AS cand_total,
              count(*) FILTER (WHERE inc.cand AND inc.tagged)   AS cand_tagged,
              count(*) FILTER (WHERE inc.sc_dk)                 AS scdk_total,
              count(*) FILTER (WHERE inc.sc_dk AND inc.tagged)  AS scdk_tagged,
              count(*) FILTER (WHERE inc.sc_byt)                AS scbyt_total,
              count(*) FILTER (WHERE inc.sc_byt AND inc.tagged) AS scbyt_tagged
            FROM (
              SELECT
                (l.property_id IN (SELECT pid FROM cand)) AS cand,
                (l.region_id = %(r)s
                 AND l.category_main IN ('dum', 'komercni')) AS sc_dk,
                (l.region_id = %(r)s AND l.category_main = 'byt') AS sc_byt,
                (l.sreality_id IN (SELECT sreality_id FROM tagged)) AS tagged
              FROM listings l WHERE l.is_active
            ) inc
            """,
            {"r": priority_region_id},
        )
        r = cur.fetchone()

    return {
        "data": {
            "total_tags": total_tags,
            "total_embeddings": total_emb,
            "priority_region_id": priority_region_id,
            "grain": "listings",
            "tiers": [
                {"key": "candidates", "label": "Dedup candidates",
                 "tagged": int(r[1]), "total": int(r[0])},
                {"key": "sc_dum_komercni",
                 "label": "Středočeský — domy & komerční",
                 "tagged": int(r[3]), "total": int(r[2])},
                {"key": "sc_byt", "label": "Středočeský — byty",
                 "tagged": int(r[5]), "total": int(r[4])},
            ],
        }
    }


def archive_reset_candidates(
    conn: psycopg.Connection, *, batch: str | None = None,
) -> dict[str, Any]:
    """Snapshot the PROPOSED candidate queue to the archive, then clear it so the
    engine regenerates fresh ("disregard candidates, keep a backup, redo all").
    Merges/dismissals are untouched (they live in property_merge_events + the
    property rows). EXPLICIT column lists on both sides: the archive's physical
    order permanently diverged from the source's when migration 272 widened the
    candidates table and 277 appended the mirrors AFTER archived_at/archive_batch
    (Postgres can only append) — the old positional `SELECT c.*` 42601'd from
    2026-07-05 until 277. Widening the candidates table again means extending BOTH
    the migration-277 mirror and this list; the schema-aware SQL gate enforces it."""
    from datetime import datetime, timezone

    label = batch or datetime.now(timezone.utc).strftime("reset-%Y%m%d-%H%M%S")
    cols = ("id, left_property_id, right_property_id, confidence, markers_matched, "
            "tier, status, created_at, reviewed_at, reviewed_action, auto_merged, "
            "merge_group_id, engine_decision, last_engine_decision_at")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO property_identity_candidates_archive "
            f"({cols}, archived_at, archive_batch) "
            f"SELECT {cols}, now(), %s FROM property_identity_candidates c "
            f"WHERE c.status = 'proposed'",
            (label,),
        )
        archived = cur.rowcount
        cur.execute(
            "DELETE FROM property_identity_candidates WHERE status = 'proposed'"
        )
        deleted = cur.rowcount
    return {"archived": archived, "deleted": deleted, "batch": label}


def merge_candidate(
    conn: psycopg.Connection, candidate_id: int,
) -> dict[str, Any] | None:
    """Merge a proposed candidate (survivor = the older property). None = 404."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT left_property_id, right_property_id, status, tier, "
            "confidence, markers_matched "
            "FROM property_identity_candidates WHERE id = %s",
            (candidate_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    left, right, status, _tier, confidence, markers = row
    if status != "proposed":
        raise MergeError(
            f"candidate {candidate_id} is not proposed (status={status})"
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM properties WHERE id IN (%s, %s) "
            "ORDER BY first_seen_at ASC, id ASC LIMIT 1",
            (left, right),
        )
        survivor = int(cur.fetchone()[0])
    retired = int(right) if survivor == int(left) else int(left)

    result = merge_properties(
        conn,
        survivor_id=survivor,
        retired_id=retired,
        reason="manual",
        source="operator",
        confidence=float(confidence) if confidence is not None else None,
        markers=markers,
    )
    _record_operator_decision(
        conn, left_property_id=int(left), right_property_id=int(right),
        outcome="merged", markers=markers,
        merge_group_id=result["data"]["merge_group_id"],
    )
    return result


def bulk_merge_candidates(
    conn: psycopg.Connection, candidate_ids: list[int],
) -> dict[str, Any]:
    """Approve many proposed candidates as INDEPENDENT pairs — each its own reversible
    merge group, so any one can be undone alone. Per-pair tolerant: a candidate whose
    property an earlier pair in this batch already merged (a shared endpoint) is skipped,
    not fatal, and stays proposed for the next batch (the engine self-heals it). This is
    the operator's scoped bulk-approve; the caller decides WHICH ids (e.g. the loaded
    STRONG geo pairs of one category) — this function never selects, only executes."""
    merged = 0
    skipped = 0
    group_ids: list[str] = []
    for cid in candidate_ids:
        try:
            result = merge_candidate(conn, cid)
        except MergeError:
            skipped += 1
            continue
        if result is None:
            skipped += 1
            continue
        merged += 1
        group_ids.append(result["data"]["merge_group_id"])
    return {"data": {"merged": merged, "skipped": skipped, "merge_group_ids": group_ids}}


def merge_cluster(
    conn: psycopg.Connection, candidate_ids: list[int],
) -> dict[str, Any] | None:
    """Merge a CLUSTER of proposed candidates into one property in one group.

    A cluster is several pairwise candidates that connect the same real-world
    property (A-B, B-C, A-C ...). We collect every distinct property across the
    given candidate ids, pick the oldest as the single survivor, and merge every
    other into it under ONE merge_group_id — so the whole cluster reverses with
    one Undo. Merging always targets the single oldest survivor (never a chain),
    so no intermediate property is retired before it's used. None = 404 (no rows).
    """
    if not candidate_ids:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, left_property_id, right_property_id, status "
            "FROM property_identity_candidates WHERE id = ANY(%s)",
            (candidate_ids,),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    for cid, _l, _r, status in rows:
        if status != "proposed":
            raise MergeError(f"candidate {cid} is not proposed (status={status})")

    prop_ids: set[int] = set()
    for _cid, left, right, _status in rows:
        prop_ids.add(int(left))
        prop_ids.add(int(right))
    if len(prop_ids) < 2:
        raise MergeError("cluster has fewer than two distinct properties")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM properties WHERE id = ANY(%s) AND status = 'active' "
            "ORDER BY first_seen_at ASC, id ASC LIMIT 1",
            (list(prop_ids),),
        )
        srow = cur.fetchone()
    if srow is None:
        raise MergeError("no active property in the cluster")
    survivor = int(srow[0])
    retired_ids = sorted(p for p in prop_ids if p != survivor)

    # One outer transaction so the whole cluster merge is ATOMIC: each
    # merge_properties opens its own `with conn.transaction()` which nests as a
    # savepoint here, so if a later pair is refused (e.g. the category guard)
    # the entire cluster rolls back rather than leaving a partial merge.
    group: str | None = None
    moved = 0
    with conn.transaction():
        for retired in retired_ids:
            result = merge_properties(
                conn,
                survivor_id=survivor,
                retired_id=retired,
                reason="manual_cluster",
                source="operator",
                merge_group_id=group,
            )
            group = result["data"]["merge_group_id"]
            moved += int(result["data"]["listings_moved"])

        # merge_properties only marks the (survivor, retired) candidate row per
        # call; cluster-internal pairs (retired_i, retired_j) are now stale —
        # mark them all.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE property_identity_candidates "
                "SET status = 'merged', reviewed_at = now(), "
                "    reviewed_action = 'operator', merge_group_id = %s "
                "WHERE id = ANY(%s) AND status = 'proposed'",
                (group, candidate_ids),
            )

    for retired in retired_ids:
        _record_operator_decision(
            conn, left_property_id=survivor, right_property_id=retired,
            outcome="merged",
            markers={"reason": "manual_cluster", "stage": "operator"},
            merge_group_id=group,
        )
    return {
        "merge_group_id": group,
        "survivor_id": survivor,
        "retired_ids": retired_ids,
        "listings_moved": moved,
        "candidates_resolved": len(rows),
    }


def merge_property_set(
    conn: psycopg.Connection, property_ids: list[int],
) -> dict[str, Any] | None:
    """Merge an explicit SET of properties into one (the operator-checked subset).

    Unlike merge_cluster (which works off candidate edges), this takes the
    property ids the operator ticked — so "merge exactly these, regardless of
    which pairwise edges exist between them" works directly. The oldest is the
    survivor; every other merges into it under one reversible group.

    Candidate hygiene afterward: edges fully inside the set are marked 'merged';
    edges with ONE endpoint in the set (a still-proposed match to an UNCHECKED
    property) are re-pointed onto the survivor so the remaining proposal stays
    valid and points at an active property — never a merged-away ghost. None =
    nothing to do (fewer than two active properties).
    """
    ids = sorted({int(p) for p in property_ids})
    if len(ids) < 2:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM properties WHERE id = ANY(%s) AND status = 'active' "
            "ORDER BY first_seen_at ASC, id ASC",
            (ids,),
        )
        active = [int(r[0]) for r in cur.fetchall()]
    if len(active) < 2:
        raise MergeError("fewer than two active properties in the selection")
    survivor, retired_ids = active[0], active[1:]

    # One outer transaction so the subset merge is ATOMIC: each merge_properties
    # nests as a savepoint, so a later refusal (e.g. the category guard) rolls
    # the whole set back instead of committing a partial merge.
    group: str | None = None
    moved = 0
    with conn.transaction():
        for retired in retired_ids:
            result = merge_properties(
                conn, survivor_id=survivor, retired_id=retired,
                reason="manual_subset", source="operator", merge_group_id=group,
            )
            group = result["data"]["merge_group_id"]
            moved += int(result["data"]["listings_moved"])

        _repoint_proposed_candidates(conn, survivor, retired_ids)

    for retired in retired_ids:
        _record_operator_decision(
            conn, left_property_id=survivor, right_property_id=retired,
            outcome="merged",
            markers={"reason": "manual_subset", "stage": "operator"},
            merge_group_id=group,
        )
    return {
        "merge_group_id": group,
        "survivor_id": survivor,
        "retired_ids": retired_ids,
        "listings_moved": moved,
    }


def _repoint_proposed_candidates(
    conn: psycopg.Connection, survivor: int, retired_ids: list[int],
) -> None:
    """Keep the still-proposed candidate queue consistent after a merge.

    Any 'proposed' candidate that referenced a now-retired property is re-pointed
    onto the survivor (preserving the ordered-pair invariant left<right, deduping
    via ON CONFLICT). A pair that collapses to (survivor, survivor) — both ends
    were merged — is just dropped. Done in Python for clarity over a clever SQL.
    """
    retired = set(retired_ids)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT id, left_property_id, right_property_id "
            "FROM property_identity_candidates "
            "WHERE status = 'proposed' "
            "AND (left_property_id = ANY(%s) OR right_property_id = ANY(%s))",
            (retired_ids, retired_ids),
        )
        rows = cur.fetchall()
        for cid, left, right in rows:
            a = survivor if int(left) in retired else int(left)
            b = survivor if int(right) in retired else int(right)
            if a == b:
                # both endpoints merged into the survivor — fully resolved.
                cur.execute(
                    "UPDATE property_identity_candidates "
                    "SET status = 'merged', reviewed_at = now(), reviewed_action = 'operator' "
                    "WHERE id = %s",
                    (cid,),
                )
                continue
            lo, hi = sorted((a, b))
            # Re-point this edge onto the survivor; if that pair already exists
            # drop this now-redundant row, else move it.
            cur.execute(
                "DELETE FROM property_identity_candidates WHERE id = %s "
                "AND EXISTS (SELECT 1 FROM property_identity_candidates x "
                "  WHERE x.left_property_id = %s AND x.right_property_id = %s AND x.id <> %s)",
                (cid, lo, hi, cid),
            )
            cur.execute(
                "UPDATE property_identity_candidates "
                "SET left_property_id = %s, right_property_id = %s "
                "WHERE id = %s",
                (lo, hi, cid),
            )


def dismiss_cluster(
    conn: psycopg.Connection, candidate_ids: list[int],
) -> dict[str, Any] | None:
    """Dismiss every proposed candidate in a cluster. None = nothing dismissed."""
    if not candidate_ids:
        return None
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE property_identity_candidates "
            "SET status = 'dismissed', reviewed_at = now(), "
            "    reviewed_action = 'operator' "
            "WHERE id = ANY(%s) AND status = 'proposed' "
            "RETURNING id, left_property_id, right_property_id, markers_matched",
            (candidate_ids,),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    for _id, left, right, markers in rows:
        _record_operator_decision(
            conn, left_property_id=int(left), right_property_id=int(right),
            outcome="dismissed", markers=markers,
        )
    return {"dismissed": [int(r[0]) for r in rows], "status": "dismissed"}


def dismiss_candidate(
    conn: psycopg.Connection, candidate_id: int,
) -> dict[str, Any] | None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE property_identity_candidates "
            "SET status = 'dismissed', reviewed_at = now(), "
            "    reviewed_action = 'operator' "
            "WHERE id = %s AND status = 'proposed' "
            "RETURNING left_property_id, right_property_id, markers_matched",
            (candidate_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    left, right, markers = row
    _record_operator_decision(
        conn, left_property_id=int(left), right_property_id=int(right),
        outcome="dismissed", markers=markers,
    )
    return {"id": candidate_id, "status": "dismissed"}


def list_merges(
    conn: psycopg.Connection, *, limit: int = 50, offset: int = 0,
) -> dict[str, Any]:
    sql = """
        SELECT
          merge_group_id::text,
          min(created_at)                       AS merged_at,
          max(survivor_property_id)             AS survivor_property_id,
          count(distinct retired_property_id)   AS retired_count,
          count(*)                              AS listings_moved,
          max(source)                           AS source,
          max(reason)                           AS reason,
          bool_and(undone_at IS NOT NULL)       AS fully_undone
        FROM property_merge_events
        GROUP BY merge_group_id
        ORDER BY min(created_at) DESC
        LIMIT %(limit)s OFFSET %(offset)s
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"limit": limit, "offset": offset})
        rows = cur.fetchall()
    data = [
        {
            "merge_group_id": r[0],
            "merged_at": r[1],
            "survivor_property_id": r[2],
            "retired_count": r[3],
            "listings_moved": r[4],
            "source": r[5],
            "reason": r[6],
            "fully_undone": r[7],
        }
        for r in rows
    ]
    return {"data": data, "total": len(data)}


def unmerge(
    conn: psycopg.Connection, merge_group_id: str, *, undone_by: str,
) -> dict[str, Any]:
    """Reverse a merge group. Raises MergeError if it has no active events."""
    return unmerge_group(conn, merge_group_id=merge_group_id, undone_by=undone_by)
