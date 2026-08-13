"""Location W1v: draw a frozen random labelled sample for one portal (06 6.4.0).

Every portal contract ships with a frozen random labelled sample: n >= 100
active listings, drawn RANDOMLY (never pathology-stratified) and BEFORE the
extraction sweep, then hand-labelled by the operator against the portal page.
The contract's acceptance gate is precision on that sample (street >= 95 %,
obec/okres >= 98 %, precision-class >= 95 %), and the same rows score the OLD
system, whose serving values are snapshotted here at draw time - the refetch
that follows a draw may itself rewrite listings.street, so scoring "the old
system as it stood" needs the values as they stood.

One current sample per source (unique partial index); a re-draw requires an
explicit --replace, which retires the previous sample (is_current = false)
without deleting it - a frozen label set is reused across contract versions,
so replacing one is an explicit operator decision, never a side effect.

Usage:
  python -m scripts.location_draw_labelled_sample --source bezrealitky            # dry-run count
  python -m scripts.location_draw_labelled_sample --source bezrealitky --write
  python -m scripts.location_draw_labelled_sample --source bezrealitky --write --replace
"""

from __future__ import annotations

import argparse
import sys

from scraper import db

DEFAULT_N = 120
DEFAULT_SEED = 0.20260813


def draw(source: str, n: int, seed: float, write: bool, replace: bool) -> int:
    conn = db.connect()
    try:
        with conn.transaction():
            cur = conn.execute(
                "select count(*) from listings where source = %s and is_active",
                (source,),
            )
            active = cur.fetchone()[0]
            if active < n:
                print(f"REFUSED: only {active} active {source} listings (< n={n})")
                return 2

            cur = conn.execute(
                "select id, drawn_at, n from location_labelled_samples"
                " where source = %s and is_current",
                (source,),
            )
            current = cur.fetchone()
            if current is not None and not replace:
                print(
                    f"REFUSED: sample {current[0]} (n={current[2]}, drawn {current[1]:%Y-%m-%d})"
                    f" is current for {source}; the label set is frozen - pass --replace"
                    " only if you mean to retire it"
                )
                return 2

            if not write:
                print(
                    f"DRY-RUN: would draw n={n} of {active} active {source} rows"
                    f" (seed={seed})"
                    + (f", retiring current sample {current[0]}" if current else "")
                )
                return 0

            if current is not None:
                conn.execute(
                    "update location_labelled_samples set is_current = false where id = %s",
                    (current[0],),
                )

            method = (
                f"setseed({seed}); uniform random over active rows"
                f" (order by random() limit {n}), one statement, drawn pre-sweep"
            )
            cur = conn.execute(
                "insert into location_labelled_samples (source, method, n)"
                " values (%s, %s, %s) returning id",
                (source, method, n),
            )
            sample_id = cur.fetchone()[0]

            conn.execute("select setseed(%s)", (seed,))
            cur = conn.execute(
                """
                insert into location_labelled_sample_members
                  (sample_id, listing_id, source_id_native, position,
                   legacy_street, legacy_street_source, legacy_house_number,
                   legacy_obec, legacy_okres, legacy_zip)
                select %s, id, source_id_native,
                       row_number() over () as position,
                       street, street_source, house_number, obec, okres, zip
                from (
                  select * from listings
                  where source = %s and is_active
                  order by random()
                  limit %s
                ) drawn
                """,
                (sample_id, source, n),
            )
            print(f"DRAWN: sample {sample_id} for {source}: {cur.rowcount} members (seed={seed})")
            return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True)
    ap.add_argument("--n", type=int, default=DEFAULT_N)
    ap.add_argument("--seed", type=float, default=DEFAULT_SEED)
    ap.add_argument("--write", action="store_true", help="actually draw (default dry-run)")
    ap.add_argument("--replace", action="store_true", help="retire the current sample first")
    args = ap.parse_args()
    if args.n < 100:
        print("REFUSED: 06 6.4.0 requires n >= 100")
        return 2
    return draw(args.source, args.n, args.seed, args.write, args.replace)


if __name__ == "__main__":
    sys.exit(main())
