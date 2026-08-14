"""Re-derive the payload archive's storage ceiling from live data (W2a, 02 §2.3.2 P4).

`location_data.payload_budget` freezes the arithmetic that chose
`payloads.DEFAULT_VERSION_CAP`, and CI asserts the default still fits the subsystem
budget. This script is the other half of that pair: it re-measures the same quantities
against production and prints the drift, so the frozen table can be re-blessed on
evidence rather than trusted indefinitely. Run it before signing a cap change, after a
portal is onboarded, and after any redesign that moves a portal's page weight.

Three measurements, and the middle one is the one nobody had done:

  * **corpus** — active listings and listings-ever per source. The cap multiplies THIS,
    which is why the ceiling is tens of GB for a number that looks like "20".
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


def ceiling_rows(
    surfaces: list[SurfaceMeasurement], corpus: dict[str, tuple[int, int]],
    caps: tuple[int, ...],
) -> list[dict[str, Any]]:
    """Ceiling vs cap over the DETAIL surface — the one `payload_dual_write` archives.

    Index surfaces are excluded and reported separately: they are gated behind their own
    flag, their groups are index POSITIONS rather than listings, and their keys are
    week-stamped, so multiplying them by a listing count would be nonsense.
    """
    detail = {s.source: s for s in surfaces if s.page_kind == "detail"}
    one_body_active = sum(
        detail[src].stored_bytes_per_body * counts[0]
        for src, counts in corpus.items() if src in detail)
    one_body_ever = sum(
        detail[src].stored_bytes_per_body * counts[1]
        for src, counts in corpus.items() if src in detail)
    return [
        {
            "cap": cap,
            "bodies_per_group": payload_budget.bodies_per_group(cap),
            "active_gb": round(payload_budget.bodies_per_group(cap) * one_body_active
                               / payload_budget.BYTES_PER_GB, 1),
            "ever_gb": round(payload_budget.bodies_per_group(cap) * one_body_ever
                             / payload_budget.BYTES_PER_GB, 1),
        }
        for cap in caps
    ]


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
    caps: tuple[int, ...],
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
        f"  {'cap':>4s} {'bodies':>7s} {'active GB':>10s} {'ever GB':>9s}",
    ]
    for row in ceiling_rows(surfaces, corpus, caps):
        marker = "  <- default" if row["cap"] == payloads.DEFAULT_VERSION_CAP else ""
        out.append(f"  {row['cap']:4d} {row['bodies_per_group']:7d} "
                   f"{row['active_gb']:10.1f} {row['ever_gb']:9.1f}{marker}")

    out += ["", f"  subsystem budget: {payload_budget.SUBSYSTEM_BUDGET_GB:.0f} GB total "
                f"(06 sizing envelope, shared with the RUIAN mirror and the claim spine)",
            "", "  drift against the frozen table "
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
            "ceiling": ceiling_rows(surfaces, corpus, caps),
            "frozen_drift": frozen_drift(surfaces),
        }, indent=2, sort_keys=True))
    else:
        print("\n".join(render(surfaces, corpus, caps)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
