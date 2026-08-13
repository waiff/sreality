"""Measure the archived-HTML denominator every W2 re-mine gate is a share of.

06 §6.4, W2 step 1 ("before any extraction: measure the denominator"): per portal,
count the rows that HAVE an archived detail body, split by active/inactive and by
pre/post that portal's archive floor date. Every W2 gate is then expressed as a share
of that number, never as a share of the whole column — the 100 % archive-coverage
reading in the design corpus is a newest-600-active sample (§6.2.3), which says
nothing about inactive rows or rows that predate a floor, so a gate stated over a
whole column silently counts rows that CANNOT be re-mined. §6.2.5 records this
substrate's ceiling as "latest fetch only, from each portal's floor date; denominator
measured in W2 step 1" — this is that measurement.

Two rules the module exists to obey:

  * **It never reads `portal_raw_pages.html`.** The archive is ~14 GB over ~445k rows,
    effectively all of it TOASTed out of line. Projecting the key/metadata columns
    touches only the main heap; naming `html` anywhere in the projection would
    detoast and decompress the whole archive to produce a handful of integers. The two
    statements below are this module's entire database surface, and
    tests/location_data/test_archive_denominator.py pins that.
  * **It only ever reads.** Every statement it runs is a SELECT — a measurement leaves
    no ledger row (the numbers belong in the W2 gate write-up, not in a table nothing
    else reads). The test file fails the build if any write verb appears anywhere in
    this module, prose included, so the guard needs no judgement call about which
    occurrence was "only a comment".

Shape: the archive statement aggregates to (source, page_kind, fetch DAY, matched?,
is_active) groups — a few thousand rows — and ALL the floor arithmetic happens in
Python against `ARCHIVE_FLOORS`. So the per-portal dates exist exactly once in this
repo, the fold is testable without a database, and the statement carries no per-portal
branch.

The join is a LEFT JOIN on purpose: a page whose listing row is missing is still an
archived page, and the count of those is itself a finding (they are un-attributable
substrate). It is reported as its own cohort instead of being silently dropped by an
inner join, which would understate the denominator.

The second statement counts the WHOLE column per portal, which is the other half of
§6.4's instruction: the gate is a share of the archived subset, and "the un-archived
remainder is reported as a number and stays de-accented rather than being counted as a
miss". That remainder is `whole column - archived subset`, so both numbers have to be
measured in the same breath or the gate cannot be stated honestly.

Usage:
  python -m scripts.location_archive_denominator
  python -m scripts.location_archive_denominator --json > denominator.json
Required: SUPABASE_DB_URL. To sanity-check a plan first, EXPLAIN the statement by hand
in psql: this module never sends an EXPLAIN, so each statement has exactly one text and
the one that ran is the one that was reviewed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from location_data import loader_db
from scraper import db

LOG = logging.getLogger("location_archive_denominator")

# 06 §6.2.3: the day each portal's detail-HTML archive begins. "Anything delisted
# before its portal's floor has no HTML, ever" — which is precisely why a gate may not
# be a share of a whole column. THE one place these dates live; `floor_for()` is the
# only reader and the SQL below carries no date at all.
ARCHIVE_FLOORS: dict[str, datetime.date] = {
    "bazos": datetime.date(2026, 5, 28),
    "idnes": datetime.date(2026, 5, 29),
    "maxima": datetime.date(2026, 5, 30),
    "remax": datetime.date(2026, 6, 1),
    "ceskereality": datetime.date(2026, 6, 26),
    "realitymix": datetime.date(2026, 6, 27),
    "mmreality": datetime.date(2026, 6, 28),
}

# The two JSON-API portals have zero archived HTML bodies (§6.2.3). Rows can still
# appear for them — W0 item 0n turned FORWARD index archiving on for sreality — so
# they are reported under their own heading with no floor split rather than being
# folded into a denominator whose floor dates do not describe them.
NO_HTML_ARCHIVE: tuple[str, ...] = ("sreality", "bezrealitky")

DETAIL = "detail"

# Floor dates are Czech calendar days; comparing a timestamptz against them in
# whatever timezone the pooled backend happens to run under would move rows fetched
# near midnight across the floor.
FLOOR_TIMEZONE = "Europe/Prague"

# Czech diacritics, for the ceskereality cohort of §6.2.3: its street column is stored
# ASCII-folded at source (2.1 % carry a diacritic vs 72.8-84.3 % elsewhere), so the
# gazetteer-unjoinable subset is "street present, no diacritic in it".
CZ_DIACRITICS = "[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]"

STATEMENT_TIMEOUT_ENV = "LOCATION_DENOMINATOR_TIMEOUT_S"
DEFAULT_STATEMENT_TIMEOUT_S = 120

# Cohorts every count is split into. `archived_rows` is the denominator itself; the
# next three partition it by the listing side of the join; the last two partition it
# by the floor and are populated only where a floor applies.
COHORT_ALL = "archived_rows"
COHORT_ACTIVE = "active_archived"
COHORT_INACTIVE = "inactive_archived"
COHORT_UNMATCHED = "unmatched_listing"
COHORT_PRE = "pre_floor"
COHORT_POST = "post_floor"
COHORTS: tuple[str, ...] = (
    COHORT_ALL, COHORT_ACTIVE, COHORT_INACTIVE, COHORT_UNMATCHED, COHORT_PRE, COHORT_POST,
)

# The whole column has no join and no floor, so only three of the cohorts can exist.
POPULATION_COHORTS: tuple[str, ...] = (COHORT_ALL, COHORT_ACTIVE, COHORT_INACTIVE)

# 06 §6.4's "per target column": the W2 gates are shares of the archived rows that
# hold (or lack) one of these, not of the archived rows as a whole. Order is the
# projection order of the aggregate below.
COLUMN_DENOMINATORS: tuple[str, ...] = (
    "street_present", "street_ascii_only", "street_resolver_source", "geom_present",
)

# NEVER project `html`. Grouping by the fetch DAY rather than by a floor comparison
# keeps every per-portal date out of the statement: the fold applies the floors in
# Python, so one query serves all portals and no per-portal branch exists here.
_GROUPS_SQL = """
    SELECT p.source,
           p.page_kind,
           (p.fetched_at AT TIME ZONE %(timezone)s::text)::date AS fetched_day,
           (l.id IS NOT NULL) AS listing_matched,
           l.is_active,
           count(*) AS pages,
           count(*) FILTER (WHERE l.street IS NOT NULL) AS street_present,
           count(*) FILTER (WHERE l.street IS NOT NULL
                              AND l.street !~ %(diacritics)s::text) AS street_ascii_only,
           count(*) FILTER (WHERE l.street_source = 'resolver') AS street_resolver_source,
           count(*) FILTER (WHERE l.geom IS NOT NULL) AS geom_present
    FROM portal_raw_pages p
    LEFT JOIN listings l
      ON l.source = p.source
     AND l.source_id_native = p.source_id_native
    GROUP BY 1, 2, 3, 4, 5
"""

# The same column predicates over the WHOLE column, so the archived subset can be
# reported as a share of it and the un-archived remainder as a number (§6.4). The
# per-column projection order matches the statement above, so `_columns_from_row`
# reads both and the two can never drift into measuring different things.
_POPULATION_SQL = """
    SELECT l.source,
           l.is_active,
           count(*) AS listings,
           count(*) FILTER (WHERE l.street IS NOT NULL) AS street_present,
           count(*) FILTER (WHERE l.street IS NOT NULL
                              AND l.street !~ %(diacritics)s::text) AS street_ascii_only,
           count(*) FILTER (WHERE l.street_source = 'resolver') AS street_resolver_source,
           count(*) FILTER (WHERE l.geom IS NOT NULL) AS geom_present
    FROM listings l
    GROUP BY 1, 2
"""


@dataclass(frozen=True)
class Group:
    """One aggregate group as the statement returns it."""

    source: str
    page_kind: str
    fetched_day: datetime.date | None
    listing_matched: bool
    is_active: bool | None
    pages: int
    columns: dict[str, int]


@dataclass(frozen=True)
class PopulationGroup:
    """One whole-column group as the second statement returns it."""

    source: str
    is_active: bool
    listings: int
    columns: dict[str, int]


@dataclass
class PortalReport:
    """The denominator for one (source, page_kind), in every cohort."""

    source: str
    page_kind: str
    archive_floor: datetime.date | None
    html_archive: bool
    rows: dict[str, int]
    columns: dict[str, dict[str, int]]


@dataclass
class SourcePopulation:
    """The whole column for one portal — what the denominator is NOT a share of."""

    source: str
    rows: dict[str, int]
    columns: dict[str, dict[str, int]]


@dataclass
class Measurement:
    """Both halves: the archived denominator and the column it is drawn from."""

    reports: list[PortalReport]
    population: dict[str, SourcePopulation]


def floor_for(source: str, page_kind: str) -> datetime.date | None:
    """The archive floor that applies to these rows, or None when none does.

    Detail pages only: index archiving is a different, already-switched-off archive
    (last rows 2026-06-05, §6.2.3) whose start these dates do not describe, and the
    two JSON-API portals have no HTML archive to have a floor for.
    """
    if page_kind != DETAIL:
        return None
    return ARCHIVE_FLOORS.get(source)


def cohorts_of(group: Group, floor: datetime.date | None) -> tuple[str, ...]:
    """Every cohort this group's count belongs to."""
    cohorts = [COHORT_ALL]
    if not group.listing_matched:
        cohorts.append(COHORT_UNMATCHED)
    elif group.is_active:
        cohorts.append(COHORT_ACTIVE)
    else:
        cohorts.append(COHORT_INACTIVE)
    if floor is not None and group.fetched_day is not None:
        # A page fetched ON the floor date is post-floor: the floor is the first day
        # the archive holds anything.
        cohorts.append(COHORT_PRE if group.fetched_day < floor else COHORT_POST)
    return tuple(cohorts)


def _columns_from_row(values: Sequence[Any]) -> dict[str, int]:
    return {
        name: int(value)
        for name, value in zip(COLUMN_DENOMINATORS, values, strict=True)
    }


def group_from_row(row: Sequence[Any]) -> Group:
    source, page_kind, fetched_day, matched, is_active, pages, *columns = row
    return Group(
        source=str(source),
        page_kind=str(page_kind),
        fetched_day=fetched_day,
        listing_matched=bool(matched),
        is_active=None if is_active is None else bool(is_active),
        pages=int(pages),
        columns=_columns_from_row(columns),
    )


def population_from_row(row: Sequence[Any]) -> PopulationGroup:
    source, is_active, listings, *columns = row
    return PopulationGroup(
        source=str(source),
        is_active=bool(is_active),
        listings=int(listings),
        columns=_columns_from_row(columns),
    )


def fold(groups: Iterable[Group]) -> list[PortalReport]:
    """Day-grain groups -> one report per (source, page_kind), floors applied here."""
    reports: dict[tuple[str, str], PortalReport] = {}
    for group in groups:
        key = (group.source, group.page_kind)
        report = reports.get(key)
        if report is None:
            report = PortalReport(
                source=group.source,
                page_kind=group.page_kind,
                archive_floor=floor_for(group.source, group.page_kind),
                html_archive=group.source in ARCHIVE_FLOORS,
                rows=dict.fromkeys(COHORTS, 0),
                columns={name: dict.fromkeys(COHORTS, 0) for name in COLUMN_DENOMINATORS},
            )
            reports[key] = report
        for cohort in cohorts_of(group, report.archive_floor):
            report.rows[cohort] += group.pages
            for name, value in group.columns.items():
                report.columns[name][cohort] += value
    return sorted(reports.values(), key=lambda r: (r.page_kind, r.source))


def fold_population(groups: Iterable[PopulationGroup]) -> dict[str, SourcePopulation]:
    population: dict[str, SourcePopulation] = {}
    for group in groups:
        entry = population.get(group.source)
        if entry is None:
            entry = SourcePopulation(
                source=group.source,
                rows=dict.fromkeys(POPULATION_COHORTS, 0),
                columns={
                    name: dict.fromkeys(POPULATION_COHORTS, 0)
                    for name in COLUMN_DENOMINATORS
                },
            )
            population[group.source] = entry
        for cohort in (COHORT_ALL, COHORT_ACTIVE if group.is_active else COHORT_INACTIVE):
            entry.rows[cohort] += group.listings
            for name, value in group.columns.items():
                entry.columns[name][cohort] += value
    return population


def totals(reports: Sequence[PortalReport]) -> dict[str, int]:
    return {
        cohort: sum(report.rows[cohort] for report in reports) for cohort in COHORTS
    }


def _sections(reports: Sequence[PortalReport]) -> list[tuple[str, list[PortalReport]]]:
    detail = [r for r in reports if r.page_kind == DETAIL]
    return [
        (
            "ARCHIVED DETAIL PAGES — the W2 denominator (06 §6.4 step 1, §6.2.3)",
            [r for r in detail if r.html_archive],
        ),
        (
            "DETAIL PAGES ON PORTALS WITH NO HTML ARCHIVE (JSON-API; §6.2.3 expects none)",
            [r for r in detail if not r.html_archive],
        ),
        (
            "INDEX PAGES — no floor applies (index archiving stopped 2026-06-05, §6.2.3)",
            [r for r in reports if r.page_kind != DETAIL],
        ),
    ]


def _num(value: int) -> str:
    return f"{value:,}"


def _floor_label(report: PortalReport) -> str:
    return report.archive_floor.isoformat() if report.archive_floor else "—"


def render(measurement: Measurement) -> list[str]:
    reports = measurement.reports
    lines: list[str] = []
    for title, section in _sections(reports):
        if not section:
            continue
        lines.append("")
        lines.append(title)
        lines.append(
            f"{'source':<14}{'floor':<12}{'archived':>12}{'active':>12}{'inactive':>12}"
            f"{'unmatched':>12}{'pre_floor':>12}{'post_floor':>12}"
        )
        for report in section:
            lines.append(
                f"{report.source:<14}{_floor_label(report):<12}"
                f"{_num(report.rows[COHORT_ALL]):>12}"
                f"{_num(report.rows[COHORT_ACTIVE]):>12}"
                f"{_num(report.rows[COHORT_INACTIVE]):>12}"
                f"{_num(report.rows[COHORT_UNMATCHED]):>12}"
                f"{_num(report.rows[COHORT_PRE]):>12}"
                f"{_num(report.rows[COHORT_POST]):>12}"
            )
        section_totals = totals(section)
        lines.append(
            f"{'TOTAL':<14}{'':<12}"
            + "".join(_num(section_totals[cohort]).rjust(12) for cohort in COHORTS)
        )
    lines.extend(_render_columns(reports))
    lines.extend(_render_coverage(reports, measurement.population))
    return lines


def _share(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "—"


def archived_listings(report: PortalReport) -> int:
    """Archived pages that resolve to a listing — one per listing, since the archive is
    UNIQUE on (source, source_id_native, page_kind)."""
    return report.rows[COHORT_ALL] - report.rows[COHORT_UNMATCHED]


def _render_columns(reports: Sequence[PortalReport]) -> list[str]:
    """Per-target-column denominators — what a W2 gate is actually a share of."""
    section = [r for r in reports if r.page_kind == DETAIL and r.html_archive]
    if not section:
        return []
    lines = [
        "",
        "COLUMN DENOMINATORS within the archived detail rows (06 §6.4 'per target column')",
        "  share is of the archived pages that resolve to a listing — a page with no",
        "  listing row can hold no column value, so it must not dilute the share",
        f"{'source':<14}{'column':<24}{'archived':>12}{'share':>8}{'active':>12}"
        f"{'inactive':>12}{'pre_floor':>12}{'post_floor':>12}",
    ]
    for report in section:
        archived = archived_listings(report)
        for name in COLUMN_DENOMINATORS:
            counts = report.columns[name]
            lines.append(
                f"{report.source:<14}{name:<24}{_num(counts[COHORT_ALL]):>12}"
                f"{_share(counts[COHORT_ALL], archived):>8}"
                f"{_num(counts[COHORT_ACTIVE]):>12}{_num(counts[COHORT_INACTIVE]):>12}"
                f"{_num(counts[COHORT_PRE]):>12}{_num(counts[COHORT_POST]):>12}"
            )
    return lines


def coverage_rows(
    report: PortalReport, population: SourcePopulation,
) -> list[tuple[str, int, int, int]]:
    """(column, whole column, archived subset, un-archived remainder) per target column.

    The remainder is the number §6.4 requires to be REPORTED rather than counted as a
    gate miss: those rows have no body to re-mine, and never will.
    """
    rows = [("listings", population.rows[COHORT_ALL], archived_listings(report),
             population.rows[COHORT_ALL] - archived_listings(report))]
    for name in COLUMN_DENOMINATORS:
        whole = population.columns[name][COHORT_ALL]
        archived = report.columns[name][COHORT_ALL]
        rows.append((name, whole, archived, whole - archived))
    return rows


def _render_coverage(
    reports: Sequence[PortalReport], population: dict[str, SourcePopulation],
) -> list[str]:
    section = [r for r in reports if r.page_kind == DETAIL and r.html_archive]
    if not section or not population:
        return []
    lines = [
        "",
        "ARCHIVE COVERAGE vs the whole column (§6.4: the remainder is a number, not a miss)",
        f"{'source':<14}{'column':<24}{'whole_column':>14}{'archived':>12}"
        f"{'remainder':>12}{'coverage':>10}",
    ]
    for report in section:
        entry = population.get(report.source)
        if entry is None:
            continue
        for name, whole, archived, remainder in coverage_rows(report, entry):
            lines.append(
                f"{report.source:<14}{name:<24}{_num(whole):>14}{_num(archived):>12}"
                f"{_num(remainder):>12}{_share(archived, whole):>10}"
            )
    return lines


def to_json(measurement: Measurement, *, timezone_name: str) -> dict[str, Any]:
    reports = measurement.reports
    detail_with_archive = [r for r in reports if r.page_kind == DETAIL and r.html_archive]
    return {
        "measured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "floor_timezone": timezone_name,
        "archive_floors": {
            source: floor.isoformat() for source, floor in sorted(ARCHIVE_FLOORS.items())
        },
        "no_html_archive": list(NO_HTML_ARCHIVE),
        "cohorts": list(COHORTS),
        "column_denominators": list(COLUMN_DENOMINATORS),
        "portals": [
            {
                "source": report.source,
                "page_kind": report.page_kind,
                "archive_floor": (
                    report.archive_floor.isoformat() if report.archive_floor else None
                ),
                "html_archive": report.html_archive,
                "rows": dict(report.rows),
                "columns": {name: dict(counts) for name, counts in report.columns.items()},
            }
            for report in reports
        ],
        "totals_archived_detail": totals(detail_with_archive),
        "column_population": {
            source: {
                "rows": dict(entry.rows),
                "columns": {name: dict(counts) for name, counts in entry.columns.items()},
            }
            for source, entry in sorted(measurement.population.items())
        },
        "coverage": {
            report.source: [
                {"column": name, "whole_column": whole, "archived": archived,
                 "remainder": remainder}
                for name, whole, archived, remainder in coverage_rows(
                    report, measurement.population[report.source]
                )
            ]
            for report in detail_with_archive
            if report.source in measurement.population
        },
    }


def measure(
    conn: psycopg.Connection, *, timezone_name: str, statement_timeout_s: int,
) -> Measurement:
    """Run both statements, each inside its own bounded transaction, and fold them.

    `loader_db.bounded` rather than a session SET: connect() is autocommit against the
    transaction-mode pooler, where a session-level timeout can land on a different
    backend than the statement it was meant to guard.
    """
    with loader_db.bounded(conn, statement_timeout_s) as cur:
        cur.execute(_GROUPS_SQL, {"timezone": timezone_name, "diacritics": CZ_DIACRITICS})
        archive_rows = cur.fetchall()
    LOG.info("DENOMINATOR archive groups=%d", len(archive_rows))

    with loader_db.bounded(conn, statement_timeout_s) as cur:
        cur.execute(_POPULATION_SQL, {"diacritics": CZ_DIACRITICS})
        population_rows = cur.fetchall()
    LOG.info("DENOMINATOR column-population groups=%d", len(population_rows))

    return Measurement(
        reports=fold(group_from_row(row) for row in archive_rows),
        population=fold_population(population_from_row(row) for row in population_rows),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="Emit the measurement as JSON on stdout instead of a table.")
    parser.add_argument("--timezone", default=FLOOR_TIMEZONE,
                        help="Timezone the floor dates are calendar days in.")
    parser.add_argument(
        "--statement-timeout", type=int,
        default=loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S),
        help=f"Per-statement timeout in seconds (${STATEMENT_TIMEOUT_ENV}).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    with db.connect() as conn:
        measurement = measure(
            conn,
            timezone_name=args.timezone,
            statement_timeout_s=args.statement_timeout,
        )

    if args.json:
        print(json.dumps(to_json(measurement, timezone_name=args.timezone), indent=2))
    else:
        for line in render(measurement):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
