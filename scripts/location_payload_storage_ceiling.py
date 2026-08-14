"""Re-derive the payload archive's storage ceiling from live data (W2a, 02 §2.3.2 P4).

`location_data.payload_budget` freezes the arithmetic that chose
`payloads.DEFAULT_VERSION_CAP`, and CI asserts the default still fits the subsystem
budget. This script is the other half of that pair: it re-measures the same quantities
against production and prints the drift, so the frozen table can be re-blessed on
evidence rather than trusted indefinitely. Run it before signing a cap change, after a
portal is onboarded, and after any redesign that moves a portal's page weight.

It prints TWO ceilings, because the archive spends two currencies: Postgres metadata
ROWS against `payload_budget.ARCHIVE_ALLOWANCE_GB` — what is LEFT of the subsystem's
20 GB envelope once the RÚIAN mirror and the claim spine are counted, not the whole of
it — and R2 BYTES against a price list. Only the first is a budget, and the `fits`
column reports it; the second is a bill of cents. Both are quoted over the `ever`
cohort, since rule 3 delists but never deletes and a pinned first/latest body outlives
the listing's activity.

Four measurements, and the middle two are the ones nobody had done:

  * **corpus** — active listings and listings-ever per source. The cap multiplies THIS,
    which is why the ceiling is tens of GB for a number that looks like "20".
  * **bytes per metadata row** — `pg_total_relation_size` over the live archive once it
    holds enough rows to mean anything, against the figure `POSTGRES_ROW_LAYOUT` froze
    from a 200k-row replay. This is what the gate multiplies now that bodies are in R2.
  * **bytes per stored body** — sampled real bodies out of `portal_raw_pages`, gzipped
    through `payloads.encode_body`, the writer's own encoder. Not `octet_length` (that
    is the body before compression, ~4x too big) and not `pg_column_size` (that is
    Postgres's own pglz/lz4, consistently ~1.5x looser than the gzip the archive
    actually stores). Sources that stage no page — sreality, bezrealitky — have no body
    to sample, so their size comes from `portal_payload_churn.last_byte_size` scaled by
    the fleet's measured compression ratio, and the row is marked `churn`.
  * **churn** — the per-surface normalised change rate, for context only. It sets how
    FAST the ceiling is reached, never how high it is; `scripts/location_payload_churn_report.py`
    is the instrument that owns this number and this script only echoes it.

Read-only: every statement is a SELECT.

Usage:
  python -m scripts.location_payload_storage_ceiling
  python -m scripts.location_payload_storage_ceiling --sample 50 --json
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

import psycopg

from location_data import loader_db, payload_budget, payloads
from location_data.payload_norm import NORMALIZER_VERSION
from scraper import db

LOG = logging.getLogger("location_payload_storage_ceiling")

READ_TIMEOUT_ENV = "LOCATION_PAYLOAD_CEILING_TIMEOUT_S"
DEFAULT_READ_TIMEOUT_S = 300

# Bodies gzipped per (source, page_kind). 25 is enough to place a portal's mean page
# weight to within a few percent — the fixture-vs-live cross-check behind
# payload_budget.PORTAL_STORAGE agreed to 0.3 % over the whole 447k-page corpus — and
# small enough that the detoast cost stays trivial.
DEFAULT_SAMPLE = 25

# Drift past this and the frozen table is stale rather than merely rounded.
DRIFT_WARN_PCT = 15.0

# Below this the archive is too empty for pg_total_relation_size to say anything about a
# row: the answer is mostly page headers and half-filled index leaves.
_ROW_SAMPLE_MIN = 10_000

_CORPUS_SQL = """
SELECT source,
       count(*) FILTER (WHERE is_active) AS active,
       count(*)                          AS ever
  FROM listings
 GROUP BY source
"""

# The legacy staging archive, which is also W2a-4's migration source: its row count per
# surface is the closest thing to "how many groups will the archive hold".
_STAGED_SQL = """
SELECT source, page_kind, count(*) AS pages,
       round(avg(octet_length(html)))   AS avg_raw_bytes,
       round(avg(pg_column_size(html))) AS avg_toast_bytes
  FROM portal_raw_pages
 GROUP BY source, page_kind
"""

# The newest N bodies of one surface. Newest rather than random on purpose: the ceiling
# is a forward projection, so the right sample is the pages the portal serves NOW, and a
# random draw over a table this wide costs a full scan to answer the same question.
#
# `convert_to(..., 'UTF8')` because `portal_raw_pages.html` is TEXT — the bytes as
# fetched are gone (a windows-1250 page was decoded on the way in), and UTF-8 is the
# closest recoverable form. It is also the right one for a projection: the payload
# archive stores what the fetcher hands it, which for every portal here is UTF-8.
_SAMPLE_BODIES_SQL = """
SELECT convert_to(html, 'UTF8') AS body
  FROM portal_raw_pages
 WHERE source = %(source)s
   AND page_kind = %(page_kind)s
 ORDER BY id DESC
 LIMIT %(limit)s
"""

# What a metadata ROW costs, once the archive holds any. `pg_total_relation_size` is heap
# + every index + TOAST, which is exactly the ledger `payload_budget.POSTGRES_ROW_LAYOUT`
# freezes; below `_ROW_SAMPLE_MIN` rows the answer is dominated by empty pages and the
# frozen figure is the better one.
_ROW_BYTES_SQL = """
SELECT count(*) AS rows,
       pg_total_relation_size('portal_raw_payloads') AS total_bytes,
       count(*) FILTER (WHERE body_r2_key IS NOT NULL) AS spilled
  FROM portal_raw_payloads
"""

# Sizes and rates for the surfaces that stage no page at all, and the churn context for
# the ones that do. Newest cohort only — a normalizer_version bump starts a clean one
# (migration 402) and summing across cohorts double-counts.
_CHURN_SQL = """
SELECT source, page_kind::text AS page_kind, count(*) AS keys,
       sum(fetches) AS fetches,
       sum(greatest(fetches - 1, 0)) AS repeats,
       sum(norm_changes) AS norm_changes,
       round(avg(last_byte_size)) AS avg_raw_bytes
  FROM portal_payload_churn
 WHERE normalizer_version = %(normalizer_version)s
 GROUP BY source, page_kind
"""


@dataclass(frozen=True, slots=True)
class SurfaceMeasurement:
    """One (source, page_kind) surface, as production reports it today."""

    source: str
    page_kind: str
    pages: int
    stored_bytes_per_body: int
    method: str
    repeats: int
    norm_changes: int

    @property
    def change_rate(self) -> float | None:
        return None if self.repeats == 0 else self.norm_changes / self.repeats


def measure_compression(
    conn: psycopg.Connection, *, source: str, page_kind: str, sample: int,
    statement_timeout: int,
) -> tuple[int, int] | None:
    """(mean raw bytes, mean stored bytes) over a sample of real bodies, or None.

    The stored figure goes through `payloads.encode_body`, so it is the number of bytes
    Postgres would actually hold — including the decision not to gzip a body under the
    threshold, which no analytical proxy models.
    """
    with loader_db.bounded(conn, statement_timeout) as cur:
        cur.execute(_SAMPLE_BODIES_SQL, {
            "source": source, "page_kind": page_kind, "limit": sample})
        bodies = [bytes(row[0]) for row in cur.fetchall()]
    if not bodies:
        return None
    raw = sum(len(b) for b in bodies)
    stored = sum(len(payloads.encode_body(b)[0]) for b in bodies)
    return round(raw / len(bodies)), round(stored / len(bodies))


def fleet_compression_ratio(measured: dict[tuple[str, str], tuple[int, int]]) -> float:
    """Median raw:stored ratio over the sampled surfaces — the fallback for unstaged ones.

    MEDIAN, not mean: this is a handful of per-portal ratios, and one portal whose pages
    are mostly repeated boilerplate compresses an order of magnitude better than the rest
    and would drag a mean far enough to misprice every source that has no sample of its
    own. Unweighted for the same reason it is applied at all — the sources it serves
    (sreality, bezrealitky) send JSON of a different shape entirely, so any finer
    weighting would be false precision.
    """
    ratios = sorted(raw / stored for raw, stored in measured.values() if stored)
    if not ratios:
        return 1.0
    mid = len(ratios) // 2
    return ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2


def measure(
    conn: psycopg.Connection, *, sample: int, statement_timeout: int,
) -> tuple[list[SurfaceMeasurement], dict[str, tuple[int, int]]]:
    """Every surface's stored-body weight, plus the corpus each cap multiplies."""
    with loader_db.bounded(conn, statement_timeout) as cur:
        cur.execute(_CORPUS_SQL)
        corpus = {str(r[0]): (int(r[1]), int(r[2])) for r in cur.fetchall()}
        cur.execute(_STAGED_SQL)
        staged = {(str(r[0]), str(r[1])): (int(r[2]), int(r[3]), int(r[4]))
                  for r in cur.fetchall()}
        cur.execute(_CHURN_SQL, {"normalizer_version": NORMALIZER_VERSION})
        churn = {(str(r[0]), str(r[1])): (int(r[2]), int(r[3]), int(r[4]), int(r[5]),
                                          int(r[6] or 0)) for r in cur.fetchall()}

    sampled: dict[tuple[str, str], tuple[int, int]] = {}
    for key in sorted(staged):
        got = measure_compression(
            conn, source=key[0], page_kind=key[1], sample=sample,
            statement_timeout=statement_timeout)
        if got:
            sampled[key] = got

    ratio = fleet_compression_ratio(sampled)
    surfaces: list[SurfaceMeasurement] = []
    for key in sorted(set(staged) | set(churn)):
        source, page_kind = key
        keys, _fetches, repeats, norm_changes, churn_raw = churn.get(key, (0, 0, 0, 0, 0))
        if key in sampled:
            _raw, stored = sampled[key]
            method = f"gzip of {sample} live bodies"
            pages = staged[key][0]
        elif churn_raw:
            stored = round(churn_raw / ratio)
            method = f"churn mean / fleet ratio {ratio:.2f}x"
            pages = keys
        else:
            continue
        surfaces.append(SurfaceMeasurement(
            source=source, page_kind=page_kind, pages=pages,
            stored_bytes_per_body=stored, method=method,
            repeats=repeats, norm_changes=norm_changes))
    return surfaces, corpus


def measure_row_bytes(
    conn: psycopg.Connection, *, statement_timeout: int,
) -> tuple[int, int, int] | None:
    """(rows, spilled rows, bytes per row) from the live archive, or None while it is
    too small to answer.

    This is the figure `payload_budget.POSTGRES_ROW_LAYOUT` freezes from a 200k-row
    replay, and it is the one the gate multiplies — so once production holds real rows
    it should be re-blessed from here rather than trusted indefinitely.
    """
    with loader_db.bounded(conn, statement_timeout) as cur:
        cur.execute(_ROW_BYTES_SQL)
        rows, total_bytes, spilled = cur.fetchone()
    if int(rows) < _ROW_SAMPLE_MIN:
        return None
    return int(rows), int(spilled), round(int(total_bytes) / int(rows))


def ceiling_rows(
    surfaces: list[SurfaceMeasurement], corpus: dict[str, tuple[int, int]],
    caps: tuple[int, ...], *, row_bytes: int,
) -> list[dict[str, Any]]:
    """Ceiling vs cap over the DETAIL surface, in BOTH footprints.

    Two columns, not one, because the archive spends two currencies: Postgres ROWS
    against `payload_budget.ARCHIVE_ALLOWANCE_GB` (what is left of the subsystem's
    envelope, not the whole of it), and R2 BYTES against a price list. Only the first is
    a budget; the second is a bill, and `fits` reports the first.

    The `ever` cohort is what the gate reads and what is marked, because rule 3 delists
    but never deletes and a pinned first/latest body outlives the listing's activity.

    Index surfaces are excluded and reported separately: they are gated behind their own
    flag, their groups are index POSITIONS rather than listings, and their keys are
    week-stamped, so multiplying them by a listing count would be nonsense.
    """
    detail = {s.source: s for s in surfaces if s.page_kind == "detail"}
    threshold = payload_budget.INLINE_THRESHOLD_BYTES

    def _sum(idx: int, only_inline: bool) -> int:
        return sum(detail[src].stored_bytes_per_body * counts[idx]
                   for src, counts in corpus.items()
                   if src in detail
                   and (detail[src].stored_bytes_per_body <= threshold) == only_inline)

    groups_ever = sum(c[1] for src, c in corpus.items() if src in detail)
    out: list[dict[str, Any]] = []
    for cap in caps:
        bodies = payload_budget.bodies_per_group(cap)
        inline = bodies * _sum(1, True)
        postgres_gb = (bodies * groups_ever * row_bytes + inline) / payload_budget.BYTES_PER_GB
        r2_gb = bodies * _sum(1, False) / payload_budget.BYTES_PER_GB
        out.append({
            "cap": cap,
            "bodies_per_group": bodies,
            "rows": bodies * groups_ever,
            "active_bodies_gb": round(bodies * (_sum(0, True) + _sum(0, False))
                                      / payload_budget.BYTES_PER_GB, 1),
            "postgres_gb": round(postgres_gb, 2),
            "r2_gb": round(r2_gb, 1),
            "r2_usd_month": round(r2_gb * payload_budget.R2_USD_PER_GB_MONTH, 2),
            "fits_allowance": postgres_gb <= payload_budget.ARCHIVE_ALLOWANCE_GB,
        })
    return out


def frozen_drift(surfaces: list[SurfaceMeasurement]) -> list[dict[str, Any]]:
    """Live bytes-per-body against the table `payload_budget` froze, per source."""
    live = {s.source: s.stored_bytes_per_body for s in surfaces if s.page_kind == "detail"}
    rows: list[dict[str, Any]] = []
    for portal in payload_budget.PORTAL_STORAGE:
        measured = live.get(portal.source)
        if measured is None:
            continue
        drift = 100.0 * (measured - portal.stored_bytes_per_body) / portal.stored_bytes_per_body
        rows.append({
            "source": portal.source,
            "frozen_bytes": portal.stored_bytes_per_body,
            "live_bytes": measured,
            "drift_pct": round(drift, 1),
            "stale": abs(drift) > DRIFT_WARN_PCT,
        })
    return rows


def render(
    surfaces: list[SurfaceMeasurement], corpus: dict[str, tuple[int, int]],
    caps: tuple[int, ...], *, row_bytes: int, row_measured: tuple[int, int, int] | None,
) -> list[str]:
    out = [
        f"PAYLOAD ARCHIVE STORAGE CEILING  (decimal GB; normalizer {NORMALIZER_VERSION})",
        "",
        "  The cap sets the CEILING; churn only sets how fast it is reached. Worst case",
        "  is cap+1 bodies per listing — the first version is pinned OUTSIDE the cap.",
        "",
        f"  {'surface':26s} {'pages':>8s} {'B/body':>9s} {'churn':>7s}  method",
    ]
    for s in surfaces:
        rate = "-" if s.change_rate is None else f"{100 * s.change_rate:.1f}%"
        out.append(f"  {s.source + '/' + s.page_kind:26s} {s.pages:8,d} "
                   f"{s.stored_bytes_per_body:9,d} {rate:>7s}  {s.method}")

    detail_sources = {s.source for s in surfaces if s.page_kind == "detail"}
    active = sum(c[0] for src, c in corpus.items() if src in detail_sources)
    ever = sum(c[1] for src, c in corpus.items() if src in detail_sources)
    out += [
        "",
        f"  corpus the cap multiplies: {active:,} active listings, {ever:,} ever",
        "",
        f"  ever-cohort ceiling at {row_bytes:,} B/metadata-row "
        f"({'live' if row_measured else 'frozen'}):",
        "",
        f"  {'cap':>4s} {'bodies':>7s} {'rows':>12s} {'PG GB':>8s} {'R2 GB':>8s} "
        f"{'$/month':>8s}  fits",
    ]
    for row in ceiling_rows(surfaces, corpus, caps, row_bytes=row_bytes):
        marker = "  <- default" if row["cap"] == payloads.DEFAULT_VERSION_CAP else ""
        fits = "yes" if row["fits_allowance"] else "NO"
        out.append(f"  {row['cap']:4d} {row['bodies_per_group']:7d} {row['rows']:12,d} "
                   f"{row['postgres_gb']:8.2f} {row['r2_gb']:8.1f} "
                   f"{row['r2_usd_month']:8.2f}  {fits:4s}{marker}")

    out += ["", f"  subsystem budget: {payload_budget.SUBSYSTEM_BUDGET_GB:.0f} GB total, "
                f"~{payload_budget.SUBSYSTEM_SPENT_GB:.0f} GB already spent (RUIAN mirror "
                f"+ claim spine) =",
            f"  archive allowance: {payload_budget.ARCHIVE_ALLOWANCE_GB:.1f} GB of "
            f"POSTGRES. R2 bytes are a bill, not a budget "
            f"(${payload_budget.R2_USD_PER_GB_MONTH:.3f}/GB/month).",
            f"  deepest cap that still fits: "
            f"{payload_budget.largest_affordable_cap('ever')}",]
    if row_measured:
        rows, spilled, per_row = row_measured
        frozen = payload_budget.postgres_row_bytes()
        drift = 100.0 * (per_row - frozen) / frozen
        out += ["", f"  live metadata row: {per_row:,} B over {rows:,} rows "
                    f"({spilled:,} spilled) vs {frozen:,} B frozen ({drift:+.1f}%)"]
        if abs(drift) > DRIFT_WARN_PCT:
            out.append("    -> re-bless POSTGRES_ROW_LAYOUT: the row footprint drifted")
    out += ["", "  drift against the frozen table "
                f"(location_data/payload_budget.py, measured {payload_budget.MEASURED_AT}):"]
    drifts = frozen_drift(surfaces)
    for row in drifts:
        flag = "  STALE" if row["stale"] else ""
        out.append(f"    {row['source']:14s} frozen {row['frozen_bytes']:8,d} "
                   f"live {row['live_bytes']:8,d}  {row['drift_pct']:+6.1f}%{flag}")
    if any(row["stale"] for row in drifts):
        out.append(f"    -> re-bless PORTAL_STORAGE: some sources drifted past "
                   f"{DRIFT_WARN_PCT:.0f}%")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-derive the payload archive's storage ceiling from live data.")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                        help=f"bodies gzipped per surface (default {DEFAULT_SAMPLE})")
    parser.add_argument("--caps", default="1,2,3,5,10,20",
                        help="comma-separated caps to tabulate")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--statement-timeout", type=int,
        default=loader_db.env_timeout_s(READ_TIMEOUT_ENV, DEFAULT_READ_TIMEOUT_S))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    caps = tuple(int(c) for c in args.caps.split(",") if c.strip())
    with db.connect() as conn:
        surfaces, corpus = measure(
            conn, sample=max(1, args.sample), statement_timeout=args.statement_timeout)
        # Live if the archive holds enough rows to answer, frozen otherwise — the
        # archive is empty until the dual-write flags are turned on, and an average
        # taken over a handful of rows is mostly empty page headers.
        row_measured = measure_row_bytes(conn, statement_timeout=args.statement_timeout)
    row_bytes = row_measured[2] if row_measured else payload_budget.postgres_row_bytes()

    if args.json:
        print(json.dumps({
            "normalizer_version": NORMALIZER_VERSION,
            "default_version_cap": payloads.DEFAULT_VERSION_CAP,
            "default_min_append_interval_days": payloads.DEFAULT_MIN_APPEND_INTERVAL_DAYS,
            "surfaces": [
                {"source": s.source, "page_kind": s.page_kind, "pages": s.pages,
                 "stored_bytes_per_body": s.stored_bytes_per_body, "method": s.method,
                 "repeats": s.repeats, "norm_changes": s.norm_changes,
                 "change_rate": s.change_rate}
                for s in surfaces],
            "corpus": {src: {"active": c[0], "ever": c[1]} for src, c in corpus.items()},
            "ceiling": ceiling_rows(surfaces, corpus, caps, row_bytes=row_bytes),
            "postgres_row_bytes": row_bytes,
            "postgres_row_bytes_source": "live" if row_measured else "frozen",
            "archive_allowance_gb": payload_budget.ARCHIVE_ALLOWANCE_GB,
            "largest_affordable_cap": payload_budget.largest_affordable_cap("ever"),
            "frozen_drift": frozen_drift(surfaces),
        }, indent=2, sort_keys=True))
    else:
        print("\n".join(render(surfaces, corpus, caps, row_bytes=row_bytes,
                               row_measured=row_measured)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
