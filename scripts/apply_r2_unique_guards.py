"""Phase B2 of the R2 refactor: new unique guards + NOT NULL checks on `listing_id`.

Two additions per carrier, both alongside the still-live legacy sreality_id-keyed
constraints (nothing here touches those):

  1. A unique index mirroring the legacy carrier's existing UNIQUE/PRIMARY KEY, but
     keyed on the surrogate column(s) instead. Only the closed set of carriers in
     UNIQUE_GUARDS below get one — declared explicitly rather than derived from
     pg_constraint, because "should this table gain a NEW uniqueness invariant" is a
     design decision (e.g. dedup_pair_audit deliberately has none, ever), not a fact
     mechanically readable off the legacy schema.

     Pair caches key on `(LEAST(a,b), GREATEST(a,b)[, discriminators])`: unlike
     sreality_id, the surrogate has no canonical a<b order, and physically
     re-canonicalizing the pair would desynchronise side-coupled payloads
     (visual_match.py / image_similarity.py store side-ordered image lists) — see
     docs/design/listing-identity-r2-pk-swap-runbook.md §0.5. Postgres's
     `ADD CONSTRAINT ... UNIQUE USING INDEX` refuses expression indexes, so these four
     stay plain unique indexes (never promoted to a named constraint) — that's fine,
     enforcement and ON CONFLICT arbiter inference both work off the index alone.

  2. A validated `CHECK (col IS NOT NULL)` (the mig-313 trick — same integrity as
     SET NOT NULL, no ACCESS EXCLUSIVE table rewrite) on every R2_CARRIERS column
     whose LEGACY sibling is itself NOT NULL. This is derived live per column via
     pg_attribute (mirrors apply_r2_constraints.py's `_legacy_has_fk`), covering the
     full registry automatically — a carrier added to R2_CARRIERS later needs no
     matching edit here.

These are the prerequisite for Phase C's ON CONFLICT arbiter retargets (§4): moving
an arbiter before its replacement guard exists wedges the writer (the #825 failure
class).

Why a script and not a migration: CREATE INDEX CONCURRENTLY cannot run inside a
transaction, and VALIDATE on the images-scale tables (8M+ rows) is far past the
transaction pooler's ~2 minute cap — same reasoning as apply_r2_constraints.py
(Phase B), whose lock-retry and index-state helpers this reuses rather than
duplicating.

    python -m scripts.apply_r2_unique_guards --dry-run
    python -m scripts.apply_r2_unique_guards --table images
    python -m scripts.apply_r2_unique_guards

Requires SUPABASE_DB_URL (+ SUPABASE_DB_SESSION_URL). Safe to re-run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

import psycopg

from scraper import db
from scripts.apply_r2_constraints import (
    _index_present_but_invalid,
    _index_ready,
    _with_lock_retry,
)
from toolkit.listing_identity import R2_CARRIERS

log = logging.getLogger("apply_r2_unique_guards")

# table -> new unique index. `constraint=True` promotes the CONCURRENTLY-built index
# to a named UNIQUE constraint via ADD ... USING INDEX (only legal for plain,
# non-expression indexes); the four pair caches use LEAST/GREATEST and so stay
# index-only. `cols_sql` already includes the wrapping parens.
UNIQUE_GUARDS: list[dict[str, Any]] = [
    {"table": "images", "name": "images_listing_id_sequence_key"[:63],
     "cols_sql": "(listing_id, sequence)", "constraint": True},
    {"table": "listing_videos", "name": "listing_videos_listing_id_sequence_key"[:63],
     "cols_sql": "(listing_id, sequence)", "constraint": True},
    {"table": "listing_condition_scores",
     "name": "listing_condition_scores_listing_id_snapshot_id_key"[:63],
     "cols_sql": "(listing_id, snapshot_id)", "constraint": True},
    {"table": "listing_marker_extractions",
     "name": "listing_marker_extractions_listing_id_snapshot_id_key"[:63],
     "cols_sql": "(listing_id, snapshot_id)", "constraint": True},
    {"table": "listing_summaries",
     "name": "listing_summaries_listing_id_snapshot_id_key"[:63],
     "cols_sql": "(listing_id, snapshot_id)", "constraint": True},
    {"table": "building_unit_extractions",
     "name": "building_unit_extractions_listing_id_snapshot_id_key"[:63],
     "cols_sql": "(listing_id, snapshot_id)", "constraint": True},
    {"table": "listing_description_enrichments",
     "name": "listing_description_enrichments_lid_snap_model_key"[:63],
     "cols_sql": "(listing_id, snapshot_id, model)", "constraint": True},
    {"table": "estimation_cohort_entries",
     "name": "estimation_cohort_entries_run_listing_id_key"[:63],
     "cols_sql": "(estimation_run_id, listing_id)", "constraint": True},
    # Pair caches: order-independent, index-only (expression index; see docstring).
    {"table": "listing_image_comparisons",
     "name": "listing_image_comparisons_listing_id_pair_key"[:63],
     "cols_sql": "(LEAST(listing_id_a, listing_id_b), GREATEST(listing_id_a, listing_id_b))",
     "constraint": False},
    {"table": "listing_visual_matches",
     "name": "listing_visual_matches_listing_id_pair_key"[:63],
     "cols_sql": ("(LEAST(listing_id_a, listing_id_b), GREATEST(listing_id_a, listing_id_b), "
                  "room_type, model)"),
     "constraint": False},
    {"table": "listing_floor_plan_matches",
     "name": "listing_floor_plan_matches_listing_id_pair_key"[:63],
     "cols_sql": "(LEAST(listing_id_a, listing_id_b), GREATEST(listing_id_a, listing_id_b), model)",
     "constraint": False},
    {"table": "listing_site_plan_matches",
     "name": "listing_site_plan_matches_listing_id_pair_key"[:63],
     "cols_sql": "(LEAST(listing_id_a, listing_id_b), GREATEST(listing_id_a, listing_id_b), model)",
     "constraint": False},
]

UNIQUE_GUARD_TABLES = {g["table"] for g in UNIQUE_GUARDS}


def _legacy_column_not_null(conn: "psycopg.Connection", table: str, col: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attnotnull FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE c.relname = %s AND a.attname = %s AND a.attnum > 0 AND NOT a.attisdropped",
            (table, col),
        )
        row = cur.fetchone()
    return bool(row and row[0])


def _unique_constraint_exists(conn: "psycopg.Connection", table: str, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
            "WHERE c.relname = %s AND con.conname = %s AND con.contype = 'u'",
            (table, name),
        )
        return cur.fetchone() is not None


def _check_name(table: str, col: str) -> str:
    return f"{table}_{col}_present_check"[:63]


def _check_state(conn: "psycopg.Connection", table: str, name: str) -> str | None:
    """None = absent, 'not_valid' = present unvalidated, 'valid' = done."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT convalidated FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "WHERE c.relname = %s AND con.conname = %s AND con.contype = 'c'",
            (table, name),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return "valid" if row[0] else "not_valid"


def apply_unique_guard(conn: "psycopg.Connection", guard: dict[str, Any], dry_run: bool) -> None:
    table, name, cols_sql = guard["table"], guard["name"], guard["cols_sql"]

    if dry_run:
        log.info(
            "%-34s %-46s index=%s constraint=%s",
            table, name,
            "ok" if _index_ready(conn, name) else "todo",
            ("n/a (expression index)" if not guard["constraint"]
             else ("ok" if _unique_constraint_exists(conn, table, name) else "todo")),
        )
        return

    if _index_present_but_invalid(conn, name):
        log.warning("%s: dropping INVALID index left by a killed build", name)
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        conn.commit()

    if not _index_ready(conn, name):
        log.info("%s: building unique index CONCURRENTLY", name)
        # CONCURRENTLY cannot run inside a transaction block.
        conn.commit()
        old_autocommit = conn.autocommit
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '60min'")
                cur.execute(f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                            f"ON {table} {cols_sql}")
        finally:
            conn.autocommit = old_autocommit
        log.info("%s: built", name)

    if not guard["constraint"]:
        return

    if not _unique_constraint_exists(conn, table, name):
        log.info("%s: promoting to UNIQUE constraint", name)
        _with_lock_retry(
            conn,
            f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE USING INDEX {name}",
            name,
        )


def apply_not_null_check(conn: "psycopg.Connection", table: str, col: str, dry_run: bool) -> None:
    name = _check_name(table, col)

    if dry_run:
        log.info("%-34s %-46s check=%s", table, col, _check_state(conn, table, name) or "todo")
        return

    state = _check_state(conn, table, name)
    if state is None:
        log.info("%s: adding CHECK NOT VALID", name)
        _with_lock_retry(
            conn,
            f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({col} IS NOT NULL) NOT VALID",
            name,
        )
        state = "not_valid"

    if state == "not_valid":
        log.info("%s: validating", name)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '60min'")
            cur.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
        conn.commit()
        log.info("%s: validated", name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default="", help="One carrier table only (default: all).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    if args.table and args.table not in UNIQUE_GUARD_TABLES and args.table not in {
        c["table"] for c in R2_CARRIERS
    }:
        print(f"ERROR: {args.table} is not an R2 carrier.", file=sys.stderr)
        return 2

    with db.connect_session() as conn:
        for guard in UNIQUE_GUARDS:
            if args.table and guard["table"] != args.table:
                continue
            apply_unique_guard(conn, guard, args.dry_run)

        for carrier in R2_CARRIERS:
            if args.table and carrier["table"] != args.table:
                continue
            for legacy, new in carrier["cols"]:
                if _legacy_column_not_null(conn, carrier["table"], legacy):
                    apply_not_null_check(conn, carrier["table"], new, args.dry_run)

    log.info("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
