"""Blank the cells the DELETED description-enrichment lane wrote and never measured.

Field-capture W7, approved destructive step (iii) (operator OK 2026-09-21). Dry run is the
default; `--apply` writes, and REFUSES to run without `--backup-table NAME`: every value it
is about to blank is first copied into that table as (listing_id, column_name, value), page
by page ahead of each clear — a proportionate backup for ~21k cells on a table too hot and
too large for a data-only pg_dump. Dispatch through `clear_unmeasured_fills.yml`.

WHAT AND WHY. Two of the old lane's outputs failed measurement afterwards:

  * `floor`, at ~73 % correct, converted by the MODEL and across two conventions — the
    lane's rubric said ground = 0 while half the corpus it learned from says otherwise.
  * `has_lift` / `has_balcony` / `has_parking` written FALSE from silence: the model
    answered "not mentioned" and the lane stored a negative. Sibling-panel precision on
    predicted-false `has_lift` was 92.9 %, and a false negative is the expensive error for
    a filter predicate. A `true` is left alone — it was 96.4 % and a stated fact.

WHAT IT WILL NOT TOUCH. The candidate set is the join of `listing_description_enrichments.
filled` — the lane's own ledger of what it wrote — to `listings`, restricted to rows whose
column STILL holds exactly the value the lane put there. bazos has no structured producer
for any of these cells (`scraper/attribute_contract.py`), so nothing here can be a
portal-stated value; anything the regex or a later writer changed is already excluded by
the compare-and-set. The clear obeys the W3 seam's rules: one statement per batch, the
`dirty_properties` enqueue in the same CTE (rule 20), no `listing_snapshots`, no
`last_seen_at`.

MEASURED BEFORE (2026-09-22), all bazos, and this is the honest shape of it: floor 6,340
(6,333 on INACTIVE rows), has_balcony-false 7,103 (7,098 inactive), has_lift-false 6,655
(6,651 inactive), has_parking-false 1,361 (all inactive) — 21,459 values over ~15k rows,
of which SIXTEEN are on an active listing. An inactive bazos row never refetches, so what
this mostly does is stop delisted history asserting a number nobody measured; the new lane
refills the sixteen and everything that arrives after it — but only once the bake-off has
opened those fields' gates, because a closed gate is outside the lane's scope entirely.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from typing import Any

from scraper import db

LOG = logging.getLogger("clear_unmeasured_enrichment_fills")

# The columns whose measurement failed, and on which value. `false` means only the
# silence-inferred negatives go; `any` means every value the lane wrote.
TARGETS: dict[str, str] = {
    "floor": "any",
    "has_lift": "false",
    "has_balcony": "false",
    "has_parking": "false",
}

# The ledger join, once. `filled` is the lane's own record of the columns it wrote, and
# `to_jsonb(l.<col>) = f.value` is the compare-and-set: a value the regex or a later detail
# fetch has since changed is not this lane's to blank.
_CANDIDATE_SQL_TEMPLATE = """
SELECT DISTINCT l.id
  FROM listing_description_enrichments e
 CROSS JOIN LATERAL jsonb_each(e.filled) AS f(key, value)
  JOIN listings l ON l.id = e.listing_id
 WHERE e.filled <> '{{}}'::jsonb
   AND f.key = %(column)s
   AND l.source = 'bazos'
   AND l.{column} IS NOT NULL
   AND to_jsonb(l.{column}) = f.value
   {only_false}
   AND l.id > %(after)s
 ORDER BY l.id
 LIMIT %(page)s
"""

_CLEAR_SQL_TEMPLATE = """
WITH updated AS (
    UPDATE listings AS l
       SET {column} = NULL
     WHERE l.id = ANY(%(ids)s::bigint[])
       AND l.{column} IS NOT NULL
    RETURNING l.property_id
)
INSERT INTO dirty_properties (property_id)
SELECT DISTINCT property_id FROM updated WHERE property_id IS NOT NULL
ON CONFLICT (property_id) DO UPDATE SET marked_at = now()
"""

_STATEMENT_TIMEOUT_SQL = "SET statement_timeout = '120s'"

_BACKUP_TABLE_SQL_TEMPLATE = """
CREATE TABLE IF NOT EXISTS {table} (
    listing_id  bigint      NOT NULL,
    column_name text        NOT NULL,
    value       jsonb       NOT NULL,
    backed_up   timestamptz NOT NULL DEFAULT now()
)
"""
# RLS on with no policies and no browser grant: the service role reads it, nothing else.
_BACKUP_GUARD_SQL_TEMPLATE = (
    "ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
    "REVOKE ALL ON {table} FROM anon, authenticated, public",
)
_BACKUP_SQL_TEMPLATE = """
INSERT INTO {table} (listing_id, column_name, value)
SELECT l.id, %(column)s, to_jsonb(l.{column})
  FROM listings l
 WHERE l.id = ANY(%(ids)s::bigint[]) AND l.{column} IS NOT NULL
"""
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def candidate_sql(column: str, *, only_false: bool) -> str:
    return _CANDIDATE_SQL_TEMPLATE.format(
        column=column,
        only_false="AND f.value = 'false'::jsonb" if only_false else "",
    )


def clear_sql(column: str) -> str:
    return _CLEAR_SQL_TEMPLATE.format(column=column)


def backup_sql(column: str, table: str) -> str:
    return _BACKUP_SQL_TEMPLATE.format(column=column, table=table)


def create_backup_table(conn: Any, table: str) -> None:
    if not _IDENT.match(table):
        raise ValueError(f"backup table name must be a plain identifier: {table!r}")
    with conn.cursor() as cur:
        cur.execute(_BACKUP_TABLE_SQL_TEMPLATE.format(table=table))
        for statement in _BACKUP_GUARD_SQL_TEMPLATE:
            cur.execute(statement.format(table=table))


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--columns", default=",".join(TARGETS),
                    help=f"comma-separated subset of {sorted(TARGETS)}")
    ap.add_argument("--apply", action="store_true",
                    help="write. Without it the run only counts.")
    ap.add_argument("--backup-table", default=None,
                    help="required with --apply: every cell is copied here before it is blanked")
    ap.add_argument("--page", type=int, default=2000)
    ap.add_argument("--verbose", action="store_true")
    return ap


def run(conn: Any, columns: list[str], *, apply: bool, page: int,
        backup_table: str | None = None) -> dict[str, int]:
    if apply and not backup_table:
        raise ValueError("--apply needs --backup-table: nothing is blanked without a copy")
    counts: dict[str, int] = {}
    for column in columns:
        select = candidate_sql(column, only_false=TARGETS[column] == "false")
        clear = clear_sql(column)
        after, total = 0, 0
        # Walked by id, in both modes. A write would also shrink the candidate set (a
        # cleared cell stops matching), but a keyset cursor is what makes the loop
        # terminate on its own rather than on the write having landed.
        while True:
            with conn.cursor() as cur:
                cur.execute(select, {"column": column, "after": after, "page": page})
                ids = [row[0] for row in cur.fetchall()]
            if not ids:
                break
            total += len(ids)
            if apply:
                with conn.cursor() as cur:
                    cur.execute(backup_sql(column, backup_table), {"column": column, "ids": ids})
                    cur.execute(clear, {"ids": ids})
            after = ids[-1]
            if len(ids) < page:
                break
        counts[column] = total
        LOG.info("CLEAR %s: %d value(s)%s", column, total, "" if apply else " (dry run)")
    return counts


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    columns = [c.strip() for c in args.columns.split(",") if c.strip()]
    unknown = [c for c in columns if c not in TARGETS]
    if unknown:
        print(f"ERROR: {unknown} is not a measured-failure column; "
              f"choose from {sorted(TARGETS)}.", file=sys.stderr)
        return 2
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    if args.apply and not args.backup_table:
        print("ERROR: --apply needs --backup-table NAME.", file=sys.stderr)
        return 2

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_STATEMENT_TIMEOUT_SQL)
        if args.apply:
            create_backup_table(conn, args.backup_table)
            LOG.warning("APPLY: blanking cells; each is copied to %s first", args.backup_table)
        counts = run(conn, columns, apply=args.apply, page=args.page,
                     backup_table=args.backup_table)

    LOG.info("DONE %s total=%d%s", counts, sum(counts.values()),
             "" if args.apply else " (dry run — nothing was written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
