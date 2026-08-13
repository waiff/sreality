"""Hermetic tests for the W2 archive denominator (06 §6.4 step 1, §6.2.3, §6.2.5).

Four things this measurement has to get right, and they are the four groups below:

  * **It must never touch `portal_raw_pages.html`.** 14 GB of TOASTed bodies read to
    produce a handful of integers would be a self-inflicted outage, and the mistake is
    one word wide. Pinned statically, so it cannot regress into a query nobody runs
    locally.
  * **The floor dates live in exactly one named constant, and the data can refute
    them.** A per-portal date copied into a query, a comment or a second dict is how
    the denominator quietly stops matching the design — and the whole point of the
    denominator is that gates are shares of it.
  * **The arithmetic.** Every gate in W2 is `hits / denominator`, so the split
    (active/inactive/unmatched on the archive side, the floor split on the listings
    side) is load-bearing. It runs here over a fixture table, with no database.
  * **The two populations can never be confused for each other.** The archive cohorts
    and the whole-column cohorts share no key name, because the one thing this module
    exists to prevent is a gate stated over the whole column.
"""

from __future__ import annotations

import ast
import datetime
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from scripts import location_archive_denominator as den
from tests.sql_corpus import first_keyword

_SOURCE_PATH = Path(den.__file__)
_SOURCE = _SOURCE_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE, _SOURCE_PATH.name)


def _norm(sql: str) -> str:
    return " ".join(sql.split()).lower()


def _sql_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(den).items()
        if name.endswith(("_SQL", "_QUERY")) and isinstance(value, str)
    }


def _execute_calls() -> list[ast.Call]:
    return [
        node
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("execute", "executemany")
    ]


# --------------------------------------------------------------- never read the html

def test_no_sql_constant_mentions_the_html_column() -> None:
    constants = _sql_constants()
    assert constants, "the SQL constant scan found nothing — the module or the scan moved"
    for name, sql in constants.items():
        assert not re.search(r"\bhtml\b", sql, re.I), name


def test_the_projection_reads_only_key_and_metadata_columns_of_the_archive() -> None:
    # Everything the statement asks of portal_raw_pages, so a future edit that adds a
    # wide column has to come through this list.
    referenced = set(re.findall(r"\bp\.([a-z_]+)", den._GROUPS_SQL))
    assert referenced == {"source", "page_kind", "fetched_at", "source_id_native"}


def test_every_executed_statement_is_a_module_level_sql_constant() -> None:
    # Also what keeps the statement discoverable by tests/sql_corpus.py: an f-string or
    # a concatenation would be invisible to the placeholder guard and to the CI PREPARE
    # sweep, which is the only thing that type-checks this against the real schema.
    names = _sql_constants()
    for call in _execute_calls():
        first = call.args[0]
        assert isinstance(first, ast.Name), ast.dump(first)
        assert first.id in names, first.id


def test_every_statement_the_module_runs_is_a_select() -> None:
    assert sorted(_sql_constants()) == ["_GROUPS_SQL", "_POPULATION_SQL"]
    for name, sql in _sql_constants().items():
        assert first_keyword(sql) == "SELECT", name


def test_the_module_contains_no_write_verb_anywhere() -> None:
    # Prose included: a measurement script that can never write is worth more than a
    # guard with a carve-out for "that one was only a comment".
    offenders = re.findall(r"\b(insert|update|delete|truncate)\b", _SOURCE, re.I)
    assert not offenders, offenders


def test_the_statement_is_bounded_by_a_transaction_local_timeout() -> None:
    # connect() is autocommit on the transaction-mode pooler: a session-level SET can
    # land on a different backend than the statement it was meant to guard.
    assert "set_config('statement_timeout', %(statement_timeout)s, true)" in _norm(
        den.loader_db._TIMEOUT_GUARD_SQL
    )
    assert den.DEFAULT_STATEMENT_TIMEOUT_S > 0


def test_the_join_keeps_pages_whose_listing_row_is_missing() -> None:
    sql = _norm(den._GROUPS_SQL)
    assert "left join listings" in sql, "an inner join would understate the denominator"
    assert "on l.source = p.source and l.source_id_native = p.source_id_native" in sql


def test_the_diacritic_class_is_a_plain_posix_bracket_expression() -> None:
    # It is spliced into `!~` as a POSIX regex, where `]`, `^`, `-` and `\` carry
    # bracket-expression meaning. A future edit can be a valid Python set and still be
    # a different — or invalid — POSIX class.
    assert den.CZ_DIACRITICS.startswith("[") and den.CZ_DIACRITICS.endswith("]")
    inner = den.CZ_DIACRITICS[1:-1]
    assert not set(inner) & set("]^-\\"), inner
    compiled = re.compile(den.CZ_DIACRITICS)
    assert compiled.search("Šumavská") and not compiled.search("Sumavska")


# ------------------------------------------------------------------- the floor dates

def test_archive_eras_are_the_seven_html_portals_with_the_design_dates() -> None:
    assert den.ARCHIVE_FLOORS == {
        "bazos": datetime.date(2026, 5, 28),
        "idnes": datetime.date(2026, 5, 29),
        "maxima": datetime.date(2026, 5, 30),
        "remax": datetime.date(2026, 6, 1),
        "ceskereality": datetime.date(2026, 6, 26),
        "realitymix": datetime.date(2026, 6, 27),
        # The corpus date (end of June) is the day M&M Reality went live AGAIN after
        # its cron was retired; the bodies it archived in its first era are still on
        # disk, so the floor — "no HTML before this, ever" — is the earlier one.
        "mmreality": datetime.date(2026, 5, 30),
    }
    assert set(den.NO_HTML_ARCHIVE) == {"sreality", "bezrealitky"}
    assert not set(den.NO_HTML_ARCHIVE) & set(den.ARCHIVE_ERAS)


def test_the_floor_is_the_first_era_and_the_eras_are_ordered_and_disjoint() -> None:
    assert len(den.ARCHIVE_ERAS["mmreality"]) == 2, "the retired-then-relived portal"
    for source, eras in den.ARCHIVE_ERAS.items():
        assert eras, source
        assert den.ARCHIVE_FLOORS[source] == eras[0].start
        assert eras[-1].end is None, f"{source}: the current era is open"
        for earlier, later in zip(eras, eras[1:]):
            assert earlier.end is not None and earlier.end < later.start, source


def _archive_eras_assignment() -> ast.AST:
    for node in _TREE.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "ARCHIVE_ERAS":
            return node
    raise AssertionError("ARCHIVE_ERAS is not a module-level annotated assignment")


def test_no_portal_name_or_floor_date_is_written_a_second_time() -> None:
    assignment = _archive_eras_assignment()
    inside = {id(node) for node in ast.walk(assignment)}
    portals = set(den.ARCHIVE_ERAS)

    for node in ast.walk(_TREE):
        if id(node) in inside:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in portals, f"{node.value!r} at line {node.lineno}"
        if isinstance(node, ast.Call) and _is_date_call(node):
            raise AssertionError(f"a second date literal at line {node.lineno}")

    # Prose included: the commentary reconciling the two archiving eras names the
    # commits, not the dates, so this stays the only place a floor date is written.
    for floor in den.ARCHIVE_FLOORS.values():
        assert floor.isoformat() not in _SOURCE, floor


def _is_date_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "date" and (
        isinstance(func.value, ast.Name) and func.value.id == "datetime"
    )


def test_a_floor_applies_only_to_detail_pages_of_the_html_portals() -> None:
    assert den.floor_for("bazos", "detail") == datetime.date(2026, 5, 28)
    # The index archive is a different, already-stopped one; these dates do not
    # describe when it started (§6.2.3).
    assert den.floor_for("bazos", "index") is None
    assert den.floor_for("sreality", "detail") is None
    assert den.floor_for("sreality", "index") is None


def test_every_source_is_classified_and_an_unknown_one_is_not_silently_json_api() -> None:
    assert den.classify_source("bazos") == den.CLASS_HTML
    assert den.classify_source("sreality") == den.CLASS_JSON_API
    # An eighth HTML portal, or a renamed source string, must not land in the
    # "JSON-API, expects none" bucket and vanish from every denominator.
    assert den.classify_source("newportal") == den.CLASS_UNKNOWN


def test_the_archive_and_whole_column_cohorts_share_no_key_name() -> None:
    # The whole column is exactly what a W2 gate may NOT be a share of, so no reader
    # of the JSON can pull a whole-column count under a name that reads "archived".
    assert not set(den.COHORTS) & set(den.POPULATION_COHORTS)
    assert not [name for name in den.POPULATION_COHORTS if "archived" in name]


# ---------------------------------------------------------------------- the fixture

_PRAGUE_NOON = datetime.time(12, 0)


def _page(
    source: str,
    day: tuple[int, int, int],
    *,
    page_kind: str = "detail",
    matched: bool = True,
    is_active: bool | None = True,
    street: str | None = None,
    street_source: str | None = None,
    geom: bool = False,
) -> dict[str, Any]:
    """One portal_raw_pages row joined to its listing, as the fixture table holds it.

    Timestamps sit at midday so the SQL's `AT TIME ZONE` conversion and the fixture's
    naive day can never disagree — the floor boundary that IS under test here is the
    Python one (`< floor` vs `>= floor`), not Postgres' timezone arithmetic.
    """
    return {
        "source": source,
        "page_kind": page_kind,
        "fetched_at": datetime.datetime.combine(datetime.date(*day), _PRAGUE_NOON),
        "matched": matched,
        "is_active": is_active if matched else None,
        "street": street if matched else None,
        "street_source": street_source if matched else None,
        "geom": geom if matched else False,
    }


_DIACRITICS = set(den.CZ_DIACRITICS.strip("[]"))

# bazos floor 2026-05-28 · ceskereality floor 2026-06-26. The page fetched the day
# BEFORE each floor is the floor witness: it has a body, so the declared floor is too
# late — exactly the reading the pre/post-floor split used to invert.
_FIXTURE_PAGES: list[dict[str, Any]] = [
    _page("bazos", (2026, 5, 27), street="Náměstí Míru", geom=True),
    _page("bazos", (2026, 5, 28), street="Namesti Miru"),
    _page("bazos", (2026, 6, 10), is_active=False, geom=True),
    _page("bazos", (2026, 6, 10), matched=False),
    _page("bazos", (2026, 6, 1), page_kind="index"),
    _page("ceskereality", (2026, 6, 25), street="Sumavska", street_source="resolver"),
    _page("ceskereality", (2026, 6, 27), street="Šumavská", geom=True),
    _page("sreality", (2026, 7, 1), page_kind="index"),
    _page("sreality", (2026, 7, 1)),
]


def _listing(
    source: str,
    *,
    is_active: bool = True,
    inactive_day: tuple[int, int, int] | None = None,
    street: str | None = None,
    street_source: str | None = None,
    geom: bool = False,
) -> dict[str, Any]:
    return {
        "source": source, "is_active": is_active,
        "inactive_at": (
            datetime.datetime.combine(datetime.date(*inactive_day), _PRAGUE_NOON)
            if inactive_day else None
        ),
        "street": street, "street_source": street_source, "geom": geom,
    }


# The whole column: a superset of the archived subset above. The first rows of each
# portal are the ones the fixture pages match; the rest have no archived body and are
# the "un-archived remainder" §6.4 wants reported rather than counted as a gate miss —
# split here into the three reasons a row can be in it.
_FIXTURE_LISTINGS: list[dict[str, Any]] = [
    _listing("bazos", street="Náměstí Míru", geom=True),
    _listing("bazos", street="Namesti Miru"),
    _listing("bazos", is_active=False, inactive_day=(2026, 6, 10), geom=True),
    _listing("bazos", street="Dlouhá", geom=True),
    # Delisted before bazos ever archived anything: no body, ever.
    _listing("bazos", is_active=False, inactive_day=(2026, 5, 20),
             street="Krátká", street_source="resolver"),
    _listing("bazos"),
    # Flipped before migration 175 stamped inactive_at — claimed neither way.
    _listing("bazos", is_active=False),
    _listing("ceskereality", street="Sumavska", street_source="resolver"),
    _listing("ceskereality", street="Šumavská", geom=True),
    _listing("ceskereality", is_active=False, inactive_day=(2026, 7, 1),
             street="Brnenska"),
    _listing("sreality"),
    _listing("sreality"),
]


def _column_counts(row: dict[str, Any]) -> list[int]:
    """The four `count(*) FILTER (...)` expressions, evaluated in Python."""
    street = row["street"]
    return [
        int(street is not None),
        int(street is not None and not (set(street) & _DIACRITICS)),
        int(row["street_source"] == "resolver"),
        int(bool(row["geom"])),
    ]


def _aggregate(pages: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """What the archive statement's GROUP BY returns over the fixture table."""
    groups: dict[tuple[Any, ...], list[int]] = {}
    for page in pages:
        key = (
            page["source"], page["page_kind"], page["fetched_at"].date(),
            page["matched"], page["is_active"],
        )
        counts = groups.setdefault(key, [0, 0, 0, 0, 0])
        counts[0] += 1
        for index, value in enumerate(_column_counts(page), start=1):
            counts[index] += value
    return [(*key, *counts) for key, counts in sorted(groups.items(), key=repr)]


def _aggregate_population(listings: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """What the whole-column statement's GROUP BY returns over the fixture table."""
    groups: dict[tuple[Any, ...], list[int]] = {}
    for row in listings:
        stamp = row["inactive_at"]
        key = (row["source"], row["is_active"], stamp.date() if stamp else None)
        counts = groups.setdefault(key, [0, 0, 0, 0, 0])
        counts[0] += 1
        for index, value in enumerate(_column_counts(row), start=1):
            counts[index] += value
    return [(*key, *counts) for key, counts in sorted(groups.items(), key=repr)]


class _FakeCursor:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        text = _norm(sql)
        self.state["executed"].append((text, params))
        if "from portal_raw_pages" in text:
            self._aggregate_preconditions(text, params)
            self._result = _aggregate(self.state["pages"])
        elif "from listings" in text:
            self._aggregate_preconditions(text, params)
            self._result = _aggregate_population(self.state["listings"])
        else:
            self._result = [(None,)]

    def _aggregate_preconditions(self, text: str, params: dict[str, Any] | None) -> None:
        assert self.state["transactions"], "the aggregate ran outside a transaction"
        # Both day grains are timezone-converted, so both statements must be given the
        # zone; a statement that silently used the backend's zone would move rows
        # fetched near midnight across the floor.
        assert params and params["timezone"], text
        assert params["diacritics"] == den.CZ_DIACRITICS

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result

    def fetchone(self) -> tuple[Any, ...]:
        return self._result[0]


class _FakeConn:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.state)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.state["transactions"] += 1
        yield


@pytest.fixture
def state() -> dict[str, Any]:
    return {
        "executed": [], "transactions": 0,
        "pages": list(_FIXTURE_PAGES), "listings": list(_FIXTURE_LISTINGS),
    }


@pytest.fixture
def measured(state: dict[str, Any]) -> den.Measurement:
    return den.measure(_FakeConn(state), timezone_name="Europe/Prague", statement_timeout_s=5)


def _report(
    measurement: den.Measurement, source: str, page_kind: str = "detail",
) -> den.PortalReport:
    return next(
        r for r in measurement.reports if r.source == source and r.page_kind == page_kind
    )


# ---------------------------------------------------------------------- arithmetic

def test_the_denominator_splits_by_listing_state(measured: den.Measurement) -> None:
    bazos = _report(measured, "bazos")
    assert bazos.archive_floor == datetime.date(2026, 5, 28)
    assert bazos.rows == {
        "archived_rows": 4,
        "active_archived": 2,
        "inactive_archived": 1,
        "unmatched_listing": 1,
        # A body whose LATEST fetch predates the declared floor: proof the constant is
        # too late, NOT a row that cannot be re-mined.
        "latest_fetch_before_declared_floor": 1,
    }


def test_the_listing_state_cohorts_partition_the_denominator(
    measured: den.Measurement,
) -> None:
    for report in measured.reports:
        rows = report.rows
        assert (
            sum(rows[cohort] for cohort in den.PARTITION_COHORTS) == rows["archived_rows"]
        ), report.source
        # The witness partitions nothing — it is a subset of the same rows.
        assert rows["latest_fetch_before_declared_floor"] <= rows["archived_rows"]


def test_column_denominators_are_split_into_the_same_cohorts(
    measured: den.Measurement,
) -> None:
    bazos = _report(measured, "bazos")
    assert bazos.columns["street_present"] == {
        "archived_rows": 2, "active_archived": 2, "inactive_archived": 0,
        "unmatched_listing": 0, "latest_fetch_before_declared_floor": 1,
    }
    # The ceskereality gate is a share of the de-accented rows that still have a page.
    assert bazos.columns["street_ascii_only"]["archived_rows"] == 1
    assert bazos.columns["geom_present"] == {
        "archived_rows": 2, "active_archived": 1, "inactive_archived": 1,
        "unmatched_listing": 0, "latest_fetch_before_declared_floor": 1,
    }


def test_an_accented_street_is_not_counted_as_de_accented(
    measured: den.Measurement,
) -> None:
    cr = _report(measured, "ceskereality")
    assert cr.columns["street_present"]["archived_rows"] == 2
    assert cr.columns["street_ascii_only"]["archived_rows"] == 1
    assert cr.columns["street_resolver_source"]["archived_rows"] == 1


def test_index_pages_are_reported_separately_and_never_floor_split(
    measured: den.Measurement,
) -> None:
    index = _report(measured, "bazos", "index")
    assert index.archive_floor is None
    assert index.html_archive is True
    assert index.rows["archived_rows"] == 1
    assert index.rows["latest_fetch_before_declared_floor"] == 0


def test_the_json_api_portals_are_labelled_distinctly(
    measured: den.Measurement,
) -> None:
    for page_kind in ("detail", "index"):
        report = _report(measured, "sreality", page_kind)
        assert report.source_class == den.CLASS_JSON_API
        assert report.html_archive is False
        assert report.archive_floor is None
        assert report.rows["archived_rows"] == 1


def test_an_unmatched_page_contributes_to_no_column_denominator(
    measured: den.Measurement,
) -> None:
    bazos = _report(measured, "bazos")
    assert bazos.rows["unmatched_listing"] == 1
    for counts in bazos.columns.values():
        assert counts["unmatched_listing"] == 0


# --------------------------------------------------------------- the floor, validated

def test_the_observed_archive_days_are_reported_next_to_the_declared_floor(
    measured: den.Measurement,
) -> None:
    bazos = _report(measured, "bazos")
    assert bazos.first_archived_day == datetime.date(2026, 5, 27)
    assert bazos.last_archived_day == datetime.date(2026, 6, 10)


def test_a_body_older_than_the_declared_floor_contradicts_it(
    measured: den.Measurement,
) -> None:
    checks = {c.source: c for c in den.floor_checks(measured.reports)}
    assert set(checks) == {"bazos", "ceskereality"}, "detail pages of HTML portals only"
    bazos = checks["bazos"]
    assert bazos.declared_floor == datetime.date(2026, 5, 28)
    assert bazos.observed_first_day == datetime.date(2026, 5, 27)
    assert bazos.rows_before_declared_floor == 1
    assert bazos.verdict == den.VERDICT_CONTRADICTED


def test_a_floor_no_body_predates_is_reported_as_unrefuted_not_as_confirmed() -> None:
    # The archive keeps only the latest fetch, so nothing in it can CONFIRM a floor.
    report = den.PortalReport(
        source="bazos", page_kind="detail", source_class=den.CLASS_HTML,
        archive_floor=den.ARCHIVE_FLOORS["bazos"],
        rows=dict.fromkeys(den.COHORTS, 0) | {"archived_rows": 5},
        columns={name: dict.fromkeys(den.COHORTS, 0) for name in den.COLUMN_DENOMINATORS},
        first_archived_day=datetime.date(2026, 7, 1),
        last_archived_day=datetime.date(2026, 7, 2),
    )
    check = den.floor_checks([report])[0]
    assert check.verdict == den.VERDICT_NOT_REFUTED
    assert check.rows_before_declared_floor == 0


@pytest.mark.parametrize(
    ("day", "witnessed"),
    [((2026, 5, 26), True), ((2026, 5, 27), True),
     ((2026, 5, 28), False), ((2026, 5, 29), False)],
)
def test_the_floor_boundary_is_inclusive_of_the_floor_day(
    day: tuple[int, int, int], witnessed: bool,
) -> None:
    group = den.Group(
        source="bazos", page_kind="detail", fetched_day=datetime.date(*day),
        listing_matched=True, is_active=True, pages=1,
        columns=dict.fromkeys(den.COLUMN_DENOMINATORS, 0),
    )
    cohorts = den.cohorts_of(group, den.ARCHIVE_FLOORS["bazos"])
    assert (den.COHORT_FLOOR_WITNESS in cohorts) is witnessed


# ------------------------------------------- the whole column and what it cannot be

def test_the_whole_column_is_measured_alongside_the_archived_subset(
    measured: den.Measurement,
) -> None:
    bazos = measured.population["bazos"]
    assert bazos.rows == {
        "whole_column": 7, "active": 4, "inactive": 3,
        "delisted_before_floor": 1, "delisted_date_unknown": 1,
    }
    assert bazos.columns["street_present"]["whole_column"] == 4
    assert bazos.columns["street_resolver_source"]["whole_column"] == 1


@pytest.mark.parametrize(
    ("day", "cohort"),
    [((2026, 5, 27), "delisted_before_floor"),
     ((2026, 5, 28), "inactive"),
     (None, "delisted_date_unknown")],
)
def test_a_row_delisted_before_the_floor_can_hold_no_body(
    day: tuple[int, int, int] | None, cohort: str,
) -> None:
    group = den.PopulationGroup(
        source="bazos", is_active=False,
        inactive_day=datetime.date(*day) if day else None,
        listings=1, columns=dict.fromkeys(den.COLUMN_DENOMINATORS, 0),
    )
    cohorts = den.population_cohorts_of(group, den.ARCHIVE_FLOORS["bazos"])
    assert cohort in cohorts
    # A row delisted ON the floor day could still have been fetched that day.
    assert ("delisted_before_floor" in cohorts) == (cohort == "delisted_before_floor")


def test_an_active_row_is_never_in_a_delisted_cohort() -> None:
    group = den.PopulationGroup(
        source="bazos", is_active=True, inactive_day=None, listings=1,
        columns=dict.fromkeys(den.COLUMN_DENOMINATORS, 0),
    )
    assert den.population_cohorts_of(group, den.ARCHIVE_FLOORS["bazos"]) == (
        "whole_column", "active",
    )


def test_the_un_archived_remainder_is_reported_per_target_column(
    measured: den.Measurement,
) -> None:
    # §6.4: a row with no archived body can never be re-mined, so it is reported as a
    # number rather than counted as a gate miss. Here 3 of 7 bazos listings have a
    # body, and 2 of the 4 rows holding a street have none.
    rows = dict(
        (name, (whole, archived, remainder))
        for name, whole, archived, remainder in den.coverage_rows(
            _report(measured, "bazos"), measured.population["bazos"]
        )
    )
    assert rows["listings"] == (7, 3, 4)
    assert rows["street_present"] == (4, 2, 2)
    assert rows["street_resolver_source"] == (1, 0, 1)
    for whole, archived, remainder in rows.values():
        assert archived + remainder == whole


def test_the_remainder_is_broken_down_into_why_a_row_has_no_body(
    measured: den.Measurement,
) -> None:
    rows = dict(den.recoverability_rows(
        _report(measured, "bazos"), measured.population["bazos"]
    ))
    assert rows == {
        "whole_column": 7,
        "has_archived_body": 3,
        "no_archived_body": 4,
        # Still active: the detail drain can still archive them, so they are NOT
        # permanently unrecoverable.
        "no_body_active": 2,
        "no_body_inactive": 2,
        # The only permanently un-minable cohort — and the one §6.4 asks for.
        "delisted_before_floor": 1,
        "delisted_date_unknown": 1,
    }
    assert rows["no_body_active"] + rows["no_body_inactive"] == rows["no_archived_body"]
    assert rows["delisted_before_floor"] <= rows["no_body_inactive"]


def test_the_archived_subset_never_counts_a_page_without_a_listing(
    measured: den.Measurement,
) -> None:
    # `archived_rows` counts pages; the coverage arithmetic has to count LISTINGS, or
    # the bazos page whose listing row is missing would inflate coverage past the
    # column it is supposedly a subset of.
    bazos = _report(measured, "bazos")
    assert bazos.rows["archived_rows"] == 4
    assert den.archived_listings(bazos) == 3


def test_totals_sum_the_reports_cohort_by_cohort(measured: den.Measurement) -> None:
    detail = [r for r in measured.reports if r.page_kind == "detail" and r.html_archive]
    assert den.totals(detail)["archived_rows"] == 6
    assert den.totals(detail)["latest_fetch_before_declared_floor"] == 2


# ------------------------------------------------------------------ the run itself

def _run(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any], argv: list[str]) -> int:
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://fake")
    monkeypatch.setattr(den.db, "connect", lambda *a, **k: _FakeConn(state))
    return den.main(argv)


def test_the_json_output_carries_the_floors_and_every_cohort(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any], capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(monkeypatch, state, ["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["archive_floors"]["bazos"] == "2026-05-28"
    assert payload["archive_eras"]["mmreality"] == [
        {"start": "2026-05-30", "end": "2026-06-11"},
        {"start": "2026-06-28", "end": None},
    ]
    assert payload["floor_timezone"] == "Europe/Prague"
    assert payload["totals_archived_detail"]["archived_rows"] == 6
    assert payload["unknown_sources"] == []

    bazos = next(
        p for p in payload["portals"] if p["source"] == "bazos" and p["page_kind"] == "detail"
    )
    assert bazos["archive_floor"] == "2026-05-28"
    assert bazos["html_archive"] is True
    assert bazos["first_archived_day"] == "2026-05-27"
    assert bazos["rows"]["archived_rows"] == 4
    assert set(bazos["columns"]) == set(den.COLUMN_DENOMINATORS)

    check = next(c for c in payload["floor_checks"] if c["source"] == "bazos")
    assert check["verdict"] == den.VERDICT_CONTRADICTED
    assert check["rows_before_declared_floor"] == 1

    sreality = next(
        p for p in payload["portals"] if p["source"] == "sreality" and p["page_kind"] == "detail"
    )
    assert sreality["archive_floor"] is None and sreality["html_archive"] is False


def test_the_json_never_labels_the_whole_column_as_an_archived_count(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any], capsys: pytest.CaptureFixture[str],
) -> None:
    # The payload is what gets pasted into the W2 gate write-up. A reader pulling a
    # whole-column count out of a key named "archived_rows" is the exact confusion
    # this module exists to prevent.
    assert _run(monkeypatch, state, ["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    population = payload["whole_column_population"]["bazos"]
    assert "column_population" not in payload
    assert set(population["rows"]) == set(den.POPULATION_COHORTS)
    assert population["rows"]["whole_column"] == 7
    for counts in population["columns"].values():
        assert not set(counts) & set(den.COHORTS)
    assert payload["recoverability"]["bazos"]["delisted_before_floor"] == 1


def test_a_contradicted_floor_is_warned_about_on_stderr_in_json_mode(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any],
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture,
) -> None:
    # --json prints no table, and a wrong floor reshapes the cohort every W2 gate is
    # stated against — so it has to be audible without reading the payload.
    with caplog.at_level("WARNING", logger="location_archive_denominator"):
        assert _run(monkeypatch, state, ["--json"]) == 0
    capsys.readouterr()
    warned = [r.getMessage() for r in caplog.records if "floor for" in r.getMessage()]
    assert len(warned) == 2, warned
    assert "bazos" in warned[0] and "too late" in warned[0]


def test_the_printed_table_reports_each_section_and_the_column_denominators(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any], capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(monkeypatch, state, []) == 0
    out = capsys.readouterr().out
    assert "ARCHIVED DETAIL PAGES" in out
    assert "INDEX PAGES" in out
    assert "NO HTML ARCHIVE" in out
    assert "COLUMN DENOMINATORS" in out
    assert "ARCHIVE COVERAGE" in out
    assert "ARCHIVE FLOORS vs OBSERVED" in out
    assert "RECOVERABILITY" in out
    assert "street_ascii_only" in out
    assert "UNKNOWN SOURCE" not in out, "no fixture source is unclassified"
    header = next(line for line in out.splitlines() if line.startswith("source"))
    assert "first_day" in header and "last_day" in header
    coverage = next(
        line.split() for line in out.splitlines()
        if line.split()[:2] == ["bazos", "listings"]
    )
    assert coverage == ["bazos", "listings", "7", "3", "4", "42.9%"]
    recoverable = next(
        line.split() for line in out.splitlines()
        if line.split()[:2] == ["bazos", "delisted_before_floor"]
    )
    assert recoverable == ["bazos", "delisted_before_floor", "1", "14.3%"]


def test_an_unclassified_source_is_reported_loudly_and_in_no_denominator(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any], capsys: pytest.CaptureFixture[str],
) -> None:
    state["pages"].append(_page("newportal", (2026, 7, 1), street="Dlouhá"))
    assert _run(monkeypatch, state, []) == 0
    out = capsys.readouterr().out
    assert "UNKNOWN SOURCE" in out and "newportal" in out
    # ...and it stays out of every denominator the gates are stated against.
    denominator = out.split("ARCHIVE FLOORS vs OBSERVED")[0]
    assert "newportal" not in denominator.split("UNKNOWN SOURCE")[0]


def test_every_statement_is_bounded_and_none_asks_for_html(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any],
) -> None:
    assert _run(monkeypatch, state, ["--json", "--statement-timeout", "37"]) == 0
    guards = [p for text, p in state["executed"] if "set_config" in text]
    aggregates = [text for text, _p in state["executed"] if "set_config" not in text]
    assert len(guards) == len(aggregates) == state["transactions"] == 2
    assert {g["statement_timeout"] for g in guards} == {"37s"}
    assert not [text for text in aggregates if re.search(r"\bhtml\b", text)]


def test_the_timeout_default_is_env_overridable(monkeypatch: pytest.MonkeyPatch,
                                                state: dict[str, Any]) -> None:
    monkeypatch.setenv(den.STATEMENT_TIMEOUT_ENV, "45")
    assert _run(monkeypatch, state, ["--json"]) == 0
    guards = [p for text, p in state["executed"] if "set_config" in text]
    assert guards[0]["statement_timeout"] == "45s"


def test_a_missing_dsn_is_refused_before_any_connection(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any],
) -> None:
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setattr(den.db, "connect", lambda *a, **k: _FakeConn(state))
    assert den.main([]) == 2
    assert state["executed"] == []


def test_an_unknown_timezone_is_refused_before_any_connection(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any],
) -> None:
    # Otherwise it reaches `AT TIME ZONE` and aborts the run with a driver traceback,
    # after the statement budget has been spent.
    assert den.known_timezone(den.FLOOR_TIMEZONE)
    assert not den.known_timezone("Europe/Praha")
    assert _run(monkeypatch, state, ["--timezone", "Europe/Praha"]) == 2
    assert state["executed"] == []


def test_help_is_available(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        den.main(["--help"])
    assert exc.value.code == 0
    assert "denominator" in capsys.readouterr().out.lower()
