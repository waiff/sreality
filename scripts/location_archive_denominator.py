"""Measure the archived-HTML denominator every W2 re-mine gate is a share of.

06 §6.4, W2 step 1 ("before any extraction: measure the denominator"): per portal,
count the rows that HAVE an archived detail body, split by active/inactive, and count
the rows that can never have one. Every W2 gate is then expressed as a share of that
number, never as a share of the whole column — the 100 % archive-coverage reading in
the design corpus is a newest-600-active sample (§6.2.3), which says nothing about
inactive rows or rows that predate a floor, so a gate stated over a whole column
silently counts rows that CANNOT be re-mined. §6.2.5 records this substrate's ceiling
as "latest fetch only, from each portal's floor date; denominator measured in W2 step
1" — this is that measurement.

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

**The floor split is a LISTINGS-side question, not an archive-side one.** §6.4 asks for
the cohort that "has no HTML, ever". That cohort cannot be found by dating rows of
`portal_raw_pages`: `html` is `text not null` (migration 099), so every row in that
table has a body by construction, and the archive is latest-wins — `db.upsert_portal_
raw_page` stamps `fetched_at = now()` on conflict and no detail call site passes
`refresh_after_hours`, so `fetched_at` is the LATEST fetch of a page, not the day it
was first archived. Under a correct floor, "an archived row fetched before the floor"
is unreachable; under a wrong one it is a cohort of re-minable rows mislabelled as
lost. So the archive side reports what it can actually answer (how many bodies exist,
whose listing they belong to, and the observed fetch-day range), and the
recoverability split runs against `listings` (`is_active` + `inactive_at` vs the
floor), where a row that was delisted before its portal ever archived anything is
genuinely un-minable. Rows whose latest fetch predates the declared floor are still
counted — as a WITNESS that the floor constant is wrong, which is the only thing that
count can honestly mean. (The forward payload store, `portal_raw_payloads` (migration
382), carries `first_observed_at`; the W2a denominator over THAT substrate can be
dated on the archive side. This one cannot, because `portal_raw_pages` has no such
column and never had one.)

Shape: the archive statement aggregates to (source, page_kind, fetch DAY, matched?,
is_active) groups and the whole-column statement to (source, is_active, delisting DAY)
groups — a few thousand rows each — and ALL the floor arithmetic happens in Python
against `ARCHIVE_ERAS`. So the per-portal dates exist exactly once in this repo, the
fold is testable without a database, and neither statement carries a per-portal branch.

The join is a LEFT JOIN on purpose: a page whose listing row is missing is still an
archived page, and the count of those is itself a finding (they are un-attributable
substrate). It is reported as its own cohort instead of being silently dropped by an
inner join, which would understate the denominator.

The second statement counts the WHOLE column per portal, which is the other half of
§6.4's instruction: the gate is a share of the archived subset, and "the un-archived
remainder is reported as a number and stays de-accented rather than being counted as a
miss". That remainder is `whole column - archived subset`, so both numbers have to be
measured in the same breath or the gate cannot be stated honestly — and the remainder
is then broken down (active backlog / inactive / delisted before the floor) rather
than reported as one undifferentiated "unrecoverable" number.

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
import zoneinfo
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from location_data import loader_db
from scraper import db

LOG = logging.getLogger("location_archive_denominator")


@dataclass(frozen=True)
class ArchiveEra:
    """One window during which a portal's detail bodies were being archived.

    `end=None` means the era is still open. Eras exist because archiving is not
    monotonic: a portal can be retired and brought back, and "the day the archive
    begins" is then the FIRST era's start — a body written in an early era survives
    the pause, since the archive is never pruned automatically.
    """

    start: datetime.date
    end: datetime.date | None


# 06 §6.2.3: the day each portal's detail-HTML archive begins. "Anything delisted
# before its portal's floor has no HTML, ever" — which is precisely why a gate may not
# be a share of a whole column. THE one place these dates live; the SQL below carries
# no date at all, and `floor_checks` re-derives the observed first archived day from
# the data so a wrong constant shows up in the output instead of quietly reshaping a
# cohort.
#
# The design corpus states one date per portal. Six of them are the day that portal's
# archiving code shipped; the seventh is not. M&M Reality archived detail HTML from
# the day its crawler shipped (commit 1033b29f, end of May 2026) until its cron was
# retired (commit c727667c, mid June), and the date the corpus carries is the day it
# went live AGAIN through the proxy egress (commit 7d291011, end of June). Bodies
# archived in that first era are still on disk, so the corpus date is a re-live date,
# not a floor, and is recorded here as the second era's start.
ARCHIVE_ERAS: dict[str, tuple[ArchiveEra, ...]] = {
    "bazos": (ArchiveEra(datetime.date(2026, 5, 28), None),),
    "idnes": (ArchiveEra(datetime.date(2026, 5, 29), None),),
    "maxima": (ArchiveEra(datetime.date(2026, 5, 30), None),),
    "remax": (ArchiveEra(datetime.date(2026, 6, 1), None),),
    "ceskereality": (ArchiveEra(datetime.date(2026, 6, 26), None),),
    "realitymix": (ArchiveEra(datetime.date(2026, 6, 27), None),),
    "mmreality": (
        ArchiveEra(datetime.date(2026, 5, 30), datetime.date(2026, 6, 11)),
        ArchiveEra(datetime.date(2026, 6, 28), None),
    ),
}

# The floor is the first era's start: the day before which this portal can hold no
# body at all. A pause between eras is not a second floor — a listing that was live
# during an earlier era already has its body, and nothing was fetched (so nothing was
# first seen) during a pause.
ARCHIVE_FLOORS: dict[str, datetime.date] = {
    source: eras[0].start for source, eras in ARCHIVE_ERAS.items()
}

# The two JSON-API portals have zero archived HTML bodies (§6.2.3). Rows can still
# appear for them — W0 item 0n turned FORWARD index archiving on for sreality — so
# they are reported under their own heading with no floor split rather than being
# folded into a denominator whose floor dates do not describe them.
NO_HTML_ARCHIVE: tuple[str, ...] = ("sreality", "bezrealitky")

# Every source the archive can hold rows for is one of these. `CLASS_UNKNOWN` is the
# one that matters: an eighth HTML portal, or a renamed source string, must be LOUD
# rather than silently reported under "no HTML archive" and excluded from every
# denominator below.
CLASS_HTML = "html_archive"
CLASS_JSON_API = "json_api"
CLASS_UNKNOWN = "unknown_source"

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

# Cohorts of the ARCHIVE side. `archived_rows` is the denominator itself; the next
# three partition it by the listing side of the join. The last one partitions nothing
# — see `COHORT_FLOOR_WITNESS`.
COHORT_ALL = "archived_rows"
COHORT_ACTIVE = "active_archived"
COHORT_INACTIVE = "inactive_archived"
COHORT_UNMATCHED = "unmatched_listing"

# NOT a recoverability cohort: every row counted here HAS a body. It counts rows whose
# LATEST fetch predates the declared floor, which under a correct floor is impossible
# (nothing was archived before archiving existed, and a re-fetch moves `fetched_at`
# forward). Non-zero means the declared floor is too late, and the rows it would have
# excluded are re-minable.
COHORT_FLOOR_WITNESS = "latest_fetch_before_declared_floor"

COHORTS: tuple[str, ...] = (
    COHORT_ALL, COHORT_ACTIVE, COHORT_INACTIVE, COHORT_UNMATCHED, COHORT_FLOOR_WITNESS,
)
# The three that do partition `archived_rows`.
PARTITION_COHORTS: tuple[str, ...] = (COHORT_ACTIVE, COHORT_INACTIVE, COHORT_UNMATCHED)

# Cohorts of the WHOLE COLUMN — deliberately named nothing like the archive cohorts,
# because the whole column is what the denominator is NOT. `active`/`inactive`
# partition it; the last two are SUBSETS of `inactive` (a row delisted before the
# floor can hold no body; a row whose flip predates the `inactive_at` stamp of
# migration 175 carries no timestamp to compare at all).
POP_ALL = "whole_column"
POP_ACTIVE = "active"
POP_INACTIVE = "inactive"
POP_DELISTED_PRE_FLOOR = "delisted_before_floor"
POP_DELISTED_UNDATED = "delisted_date_unknown"
POPULATION_COHORTS: tuple[str, ...] = (
    POP_ALL, POP_ACTIVE, POP_INACTIVE, POP_DELISTED_PRE_FLOOR, POP_DELISTED_UNDATED,
)

# 06 §6.4's "per target column": the W2 gates are shares of the archived rows that
# hold (or lack) one of these, not of the archived rows as a whole. Order is the
# projection order of the aggregate below.
COLUMN_DENOMINATORS: tuple[str, ...] = (
    "street_present", "street_ascii_only", "street_resolver_source", "geom_present",
)

# NEVER project `html`. Grouping by the fetch DAY rather than by a floor comparison
# keeps every per-portal date out of the statement: the fold applies the floors in
# Python, so one query serves all portals and no per-portal branch exists here. The
# day grain is also what makes the floors self-validating — min(fetched_day) per
# portal is the observed first archived day.
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
#
# `inactive_at` is the day grain that carries the floor question: it is stamped on
# every flip to is_active=false and cleared on reactivation (migration 175), so a row
# that is inactive today with a flip day before its portal's floor was never
# detail-fetched after the floor and therefore has no body, ever. NULL on rows that
# flipped before migration 175 — reported as its own cohort, never assumed either way.
_POPULATION_SQL = """
    SELECT l.source,
           l.is_active,
           (l.inactive_at AT TIME ZONE %(timezone)s::text)::date AS inactive_day,
           count(*) AS listings,
           count(*) FILTER (WHERE l.street IS NOT NULL) AS street_present,
           count(*) FILTER (WHERE l.street IS NOT NULL
                              AND l.street !~ %(diacritics)s::text) AS street_ascii_only,
           count(*) FILTER (WHERE l.street_source = 'resolver') AS street_resolver_source,
           count(*) FILTER (WHERE l.geom IS NOT NULL) AS geom_present
    FROM listings l
    GROUP BY 1, 2, 3
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
    inactive_day: datetime.date | None
    listings: int
    columns: dict[str, int]


@dataclass
class PortalReport:
    """The archived rows for one (source, page_kind), in every cohort."""

    source: str
    page_kind: str
    source_class: str
    archive_floor: datetime.date | None
    rows: dict[str, int]
    columns: dict[str, dict[str, int]]
    first_archived_day: datetime.date | None = None
    last_archived_day: datetime.date | None = None

    @property
    def html_archive(self) -> bool:
        return self.source_class == CLASS_HTML


@dataclass
class SourcePopulation:
    """The whole column for one portal — what the denominator is NOT a share of."""

    source: str
    rows: dict[str, int]
    columns: dict[str, dict[str, int]]


@dataclass(frozen=True)
class FloorCheck:
    """The declared floor against what the archive actually holds.

    One-sided by construction: the archive keeps only the latest fetch of each page,
    so an observed first day LATER than the floor proves nothing (every early body may
    have been re-fetched since), while an observed day EARLIER than the floor is proof
    the constant is wrong.
    """

    source: str
    declared_floor: datetime.date | None
    observed_first_day: datetime.date | None
    observed_last_day: datetime.date | None
    rows_before_declared_floor: int
    verdict: str


VERDICT_CONTRADICTED = "CONTRADICTED"
VERDICT_NOT_REFUTED = "not refuted"
VERDICT_NO_ROWS = "no archived rows"


def classify_source(source: str) -> str:
    """Which of the three source classes this portal is in (§6.2.3)."""
    if source in ARCHIVE_ERAS:
        return CLASS_HTML
    if source in NO_HTML_ARCHIVE:
        return CLASS_JSON_API
    return CLASS_UNKNOWN


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
    if floor is not None and group.fetched_day is not None and group.fetched_day < floor:
        # A page fetched ON the floor date is not a witness: the floor is the first
        # day the archive holds anything.
        cohorts.append(COHORT_FLOOR_WITNESS)
    return tuple(cohorts)


def population_cohorts_of(
    group: PopulationGroup, floor: datetime.date | None,
) -> tuple[str, ...]:
    """Every whole-column cohort this group's count belongs to."""
    if group.is_active:
        return (POP_ALL, POP_ACTIVE)
    cohorts = [POP_ALL, POP_INACTIVE]
    if group.inactive_day is None:
        cohorts.append(POP_DELISTED_UNDATED)
    elif floor is not None and group.inactive_day < floor:
        cohorts.append(POP_DELISTED_PRE_FLOOR)
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
    source, is_active, inactive_day, listings, *columns = row
    return PopulationGroup(
        source=str(source),
        is_active=bool(is_active),
        inactive_day=inactive_day,
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
                source_class=classify_source(group.source),
                archive_floor=floor_for(group.source, group.page_kind),
                rows=dict.fromkeys(COHORTS, 0),
                columns={name: dict.fromkeys(COHORTS, 0) for name in COLUMN_DENOMINATORS},
            )
            reports[key] = report
        for cohort in cohorts_of(group, report.archive_floor):
            report.rows[cohort] += group.pages
            for name, value in group.columns.items():
                report.columns[name][cohort] += value
        if group.fetched_day is not None:
            report.first_archived_day = min(
                group.fetched_day, report.first_archived_day or group.fetched_day
            )
            report.last_archived_day = max(
                group.fetched_day, report.last_archived_day or group.fetched_day
            )
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
        for cohort in population_cohorts_of(group, floor_for(group.source, DETAIL)):
            entry.rows[cohort] += group.listings
            for name, value in group.columns.items():
                entry.columns[name][cohort] += value
    return population


@dataclass
class Measurement:
    """Both halves: the archived denominator and the column it is drawn from."""

    reports: list[PortalReport]
    population: dict[str, SourcePopulation]


def totals(reports: Sequence[PortalReport]) -> dict[str, int]:
    return {
        cohort: sum(report.rows[cohort] for report in reports) for cohort in COHORTS
    }


def floor_checks(reports: Sequence[PortalReport]) -> list[FloorCheck]:
    """The declared floors against the observed first archived day, per portal."""
    checks: list[FloorCheck] = []
    for report in reports:
        if report.page_kind != DETAIL or not report.html_archive:
            continue
        witness = report.rows[COHORT_FLOOR_WITNESS]
        if report.rows[COHORT_ALL] == 0:
            verdict = VERDICT_NO_ROWS
        elif witness:
            verdict = VERDICT_CONTRADICTED
        else:
            verdict = VERDICT_NOT_REFUTED
        checks.append(
            FloorCheck(
                source=report.source,
                declared_floor=report.archive_floor,
                observed_first_day=report.first_archived_day,
                observed_last_day=report.last_archived_day,
                rows_before_declared_floor=witness,
                verdict=verdict,
            )
        )
    return checks


def archived_listings(report: PortalReport) -> int:
    """Archived pages that resolve to a listing — one per listing, since the archive is
    UNIQUE on (source, source_id_native, page_kind)."""
    return report.rows[COHORT_ALL] - report.rows[COHORT_UNMATCHED]


def coverage_rows(
    report: PortalReport, population: SourcePopulation,
) -> list[tuple[str, int, int, int]]:
    """(column, whole column, archived subset, un-archived remainder) per target column.

    The remainder is the number §6.4 requires to be REPORTED rather than counted as a
    gate miss. It is NOT all permanently lost: `recoverability_rows` breaks it into the
    part that is (delisted before the floor) and the parts that are not (still active,
    so a body can still arrive from the detail drain; or delisted at an unknown date).
    """
    rows = [("listings", population.rows[POP_ALL], archived_listings(report),
             population.rows[POP_ALL] - archived_listings(report))]
    for name in COLUMN_DENOMINATORS:
        whole = population.columns[name][POP_ALL]
        archived = report.columns[name][COHORT_ALL]
        rows.append((name, whole, archived, whole - archived))
    return rows


def recoverability_rows(
    report: PortalReport, population: SourcePopulation,
) -> list[tuple[str, int]]:
    """The floor split §6.4 asks for, on the side of the join that can answer it.

    `delisted_before_floor` is counted over the whole column, so under a correct floor
    it is a subset of `no_body_inactive`; if it ever exceeds it, either the floor
    constant or the `inactive_at` stamp is wrong and the two numbers say so.
    """
    with_body = archived_listings(report)
    whole = population.rows[POP_ALL]
    return [
        ("whole_column", whole),
        ("has_archived_body", with_body),
        ("no_archived_body", whole - with_body),
        ("no_body_active", population.rows[POP_ACTIVE] - report.rows[COHORT_ACTIVE]),
        ("no_body_inactive", population.rows[POP_INACTIVE] - report.rows[COHORT_INACTIVE]),
        ("delisted_before_floor", population.rows[POP_DELISTED_PRE_FLOOR]),
        ("delisted_date_unknown", population.rows[POP_DELISTED_UNDATED]),
    ]


def _sections(
    reports: Sequence[PortalReport],
) -> list[tuple[str, tuple[str, ...], list[PortalReport]]]:
    detail = [r for r in reports if r.page_kind == DETAIL]
    return [
        (
            "ARCHIVED DETAIL PAGES — the W2 denominator (06 §6.4 step 1, §6.2.3)",
            ("  every row here HAS a body; which LISTINGS can never have one is the",
             "  RECOVERABILITY table at the bottom"),
            [r for r in detail if r.source_class == CLASS_HTML],
        ),
        (
            "DETAIL PAGES ON PORTALS WITH NO HTML ARCHIVE (JSON-API; §6.2.3 expects none)",
            (),
            [r for r in detail if r.source_class == CLASS_JSON_API],
        ),
        (
            "!! UNKNOWN SOURCE — in neither ARCHIVE_ERAS nor NO_HTML_ARCHIVE",
            ("  these rows are in NO denominator below and in no coverage or",
             "  recoverability row: classify the source before stating any gate"),
            [r for r in detail if r.source_class == CLASS_UNKNOWN],
        ),
        (
            "INDEX PAGES — no floor applies (index archiving stopped 2026-06-05, §6.2.3)",
            (),
            [r for r in reports if r.page_kind != DETAIL],
        ),
    ]


def _num(value: int) -> str:
    return f"{value:,}"


def _day(value: datetime.date | None) -> str:
    return value.isoformat() if value else "—"


def render(measurement: Measurement) -> list[str]:
    reports = measurement.reports
    lines: list[str] = []
    for title, notes, section in _sections(reports):
        if not section:
            continue
        lines.append("")
        lines.append(title)
        lines.extend(notes)
        lines.append(
            f"{'source':<14}{'floor':<12}{'first_day':<12}{'last_day':<12}"
            f"{'archived':>12}{'active':>12}{'inactive':>12}{'unmatched':>12}"
        )
        for report in section:
            lines.append(
                f"{report.source:<14}{_day(report.archive_floor):<12}"
                f"{_day(report.first_archived_day):<12}"
                f"{_day(report.last_archived_day):<12}"
                f"{_num(report.rows[COHORT_ALL]):>12}"
                f"{_num(report.rows[COHORT_ACTIVE]):>12}"
                f"{_num(report.rows[COHORT_INACTIVE]):>12}"
                f"{_num(report.rows[COHORT_UNMATCHED]):>12}"
            )
        section_totals = totals(section)
        lines.append(
            f"{'TOTAL':<14}{'':<36}"
            + "".join(
                _num(section_totals[cohort]).rjust(12)
                for cohort in (COHORT_ALL, *PARTITION_COHORTS)
            )
        )
    lines.extend(_render_floor_checks(reports))
    lines.extend(_render_columns(reports))
    lines.extend(_render_coverage(reports, measurement.population))
    lines.extend(_render_recoverability(reports, measurement.population))
    return lines


def _share(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "—"


def _render_floor_checks(reports: Sequence[PortalReport]) -> list[str]:
    """The floor constants against the data — the only thing that can refute them."""
    checks = floor_checks(reports)
    if not checks:
        return []
    lines = [
        "",
        "ARCHIVE FLOORS vs OBSERVED (the constant is self-validating, one-sided)",
        "  the archive keeps only the LATEST fetch of a page, so an observed first day",
        "  later than the floor proves nothing; an EARLIER one proves the floor wrong,",
        "  and the rows it would have written off as un-minable are not",
        f"{'source':<14}{'declared':<12}{'observed_first':<16}{'observed_last':<16}"
        f"{'rows_before':>12}  verdict",
    ]
    for check in checks:
        lines.append(
            f"{check.source:<14}{_day(check.declared_floor):<12}"
            f"{_day(check.observed_first_day):<16}{_day(check.observed_last_day):<16}"
            f"{_num(check.rows_before_declared_floor):>12}  {check.verdict}"
        )
    return lines


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
        f"{'inactive':>12}",
    ]
    for report in section:
        archived = archived_listings(report)
        for name in COLUMN_DENOMINATORS:
            counts = report.columns[name]
            lines.append(
                f"{report.source:<14}{name:<24}{_num(counts[COHORT_ALL]):>12}"
                f"{_share(counts[COHORT_ALL], archived):>8}"
                f"{_num(counts[COHORT_ACTIVE]):>12}{_num(counts[COHORT_INACTIVE]):>12}"
            )
    return lines


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


def _render_recoverability(
    reports: Sequence[PortalReport], population: dict[str, SourcePopulation],
) -> list[str]:
    """The floor split, on the listings side — which rows can never be re-mined."""
    section = [r for r in reports if r.page_kind == DETAIL and r.html_archive]
    if not section or not population:
        return []
    lines = [
        "",
        "RECOVERABILITY OF THE UN-ARCHIVED REMAINDER (the §6.4 floor split, listings side)",
        "  every row of portal_raw_pages HAS a body (migration 099: html text not null),",
        "  so 'no HTML, ever' is a property of a LISTING, not of an archive row:",
        "  delisted_before_floor is permanently un-minable; no_body_active is a drain",
        "  backlog that can still archive itself; delisted_date_unknown predates the",
        "  inactive_at stamp (migration 175) and is claimed neither way",
        f"{'source':<14}{'cohort':<26}{'listings':>12}{'share':>10}",
    ]
    for report in section:
        entry = population.get(report.source)
        if entry is None:
            continue
        whole = entry.rows[POP_ALL]
        for name, count in recoverability_rows(report, entry):
            lines.append(
                f"{report.source:<14}{name:<26}{_num(count):>12}{_share(count, whole):>10}"
            )
    return lines


def to_json(measurement: Measurement, *, timezone_name: str) -> dict[str, Any]:
    reports = measurement.reports
    detail_with_archive = [r for r in reports if r.page_kind == DETAIL and r.html_archive]
    return {
        "measured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "floor_timezone": timezone_name,
        "archive_eras": {
            source: [
                {"start": era.start.isoformat(),
                 "end": era.end.isoformat() if era.end else None}
                for era in eras
            ]
            for source, eras in sorted(ARCHIVE_ERAS.items())
        },
        "archive_floors": {
            source: floor.isoformat() for source, floor in sorted(ARCHIVE_FLOORS.items())
        },
        "floor_checks": [
            {
                "source": check.source,
                "declared_floor": _day_or_none(check.declared_floor),
                "observed_first_day": _day_or_none(check.observed_first_day),
                "observed_last_day": _day_or_none(check.observed_last_day),
                "rows_before_declared_floor": check.rows_before_declared_floor,
                "verdict": check.verdict,
            }
            for check in floor_checks(reports)
        ],
        "no_html_archive": list(NO_HTML_ARCHIVE),
        "unknown_sources": sorted(
            {r.source for r in reports if r.source_class == CLASS_UNKNOWN}
        ),
        "cohorts": list(COHORTS),
        "population_cohorts": list(POPULATION_COHORTS),
        "column_denominators": list(COLUMN_DENOMINATORS),
        "portals": [
            {
                "source": report.source,
                "page_kind": report.page_kind,
                "source_class": report.source_class,
                "archive_floor": _day_or_none(report.archive_floor),
                "html_archive": report.html_archive,
                "first_archived_day": _day_or_none(report.first_archived_day),
                "last_archived_day": _day_or_none(report.last_archived_day),
                "rows": dict(report.rows),
                "columns": {name: dict(counts) for name, counts in report.columns.items()},
            }
            for report in reports
        ],
        "totals_archived_detail": totals(detail_with_archive),
        "whole_column_population": {
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
        "recoverability": {
            report.source: dict(
                recoverability_rows(report, measurement.population[report.source])
            )
            for report in detail_with_archive
            if report.source in measurement.population
        },
    }


def _day_or_none(value: datetime.date | None) -> str | None:
    return value.isoformat() if value else None


def measure(
    conn: psycopg.Connection, *, timezone_name: str, statement_timeout_s: int,
) -> Measurement:
    """Run both statements, each inside its own bounded transaction, and fold them.

    `loader_db.bounded` rather than a session SET: connect() is autocommit against the
    transaction-mode pooler, where a session-level timeout can land on a different
    backend than the statement it was meant to guard.
    """
    params = {"timezone": timezone_name, "diacritics": CZ_DIACRITICS}
    with loader_db.bounded(conn, statement_timeout_s) as cur:
        cur.execute(_GROUPS_SQL, params)
        archive_rows = cur.fetchall()
    LOG.info("DENOMINATOR archive groups=%d", len(archive_rows))

    with loader_db.bounded(conn, statement_timeout_s) as cur:
        cur.execute(_POPULATION_SQL, params)
        population_rows = cur.fetchall()
    LOG.info("DENOMINATOR column-population groups=%d", len(population_rows))

    return Measurement(
        reports=fold(group_from_row(row) for row in archive_rows),
        population=fold_population(population_from_row(row) for row in population_rows),
    )


def known_timezone(name: str) -> bool:
    """Whether `name` is a zone both this process and Postgres will accept.

    Checked BEFORE the connection is opened: an unknown zone would otherwise reach
    `AT TIME ZONE` and abort the run with a raw driver traceback, after the statement
    budget had already been spent. Python and Postgres both read the IANA database, so
    a zone zoneinfo knows is one `AT TIME ZONE` accepts.
    """
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False
    return True


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

    if not known_timezone(args.timezone):
        print(f"ERROR: --timezone {args.timezone!r} is not an IANA zone.", file=sys.stderr)
        return 2

    with db.connect() as conn:
        measurement = measure(
            conn,
            timezone_name=args.timezone,
            statement_timeout_s=args.statement_timeout,
        )

    # On stderr, so it is seen in --json mode too: a contradicted floor reshapes the
    # cohort every W2 gate is stated against, and the payload alone is easy to skim.
    for check in floor_checks(measurement.reports):
        if check.verdict == VERDICT_CONTRADICTED:
            LOG.warning(
                "DENOMINATOR floor for %s is too late: declared=%s observed_first=%s"
                " rows_before=%d",
                check.source, check.declared_floor, check.observed_first_day,
                check.rows_before_declared_floor,
            )

    if args.json:
        print(json.dumps(to_json(measurement, timezone_name=args.timezone), indent=2))
    else:
        for line in render(measurement):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
