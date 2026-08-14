"""Hermetic tests for the W2a churn readout (02 §2.3.2's storage gate).

This report is the artefact the operator signs a storage decision from, so the four
things it can get wrong are the four groups below:

  * **The denominator.** A change rate is changes per REPEAT fetch, never per fetch — a
    key's first sighting had nothing to be compared against. Dividing by fetches
    understates every rate by the share of the sample seen once, which on a young
    instrument is close to a factor of two, and it understates it silently.
  * **The arithmetic that turns a rate into GB.** Pinned against 02 §2.3.2's own worked
    figure (445,191 pages x ~70 KB "is ~31 GB per full refetch cycle"), so the unit and
    the formula are checked against the design rather than against themselves.
  * **Never projecting from a thin sample.** A surface with one fetch per key has no rate
    at all; one with a handful has noise. Both must print INSUFFICIENT where the number
    would be, because a GB figure carries authority a caveat in prose does not.
  * **Read-only, and the probe cohort kept apart.** The report may not write, and the
    confirmation probe's minutes-apart cadence may not blend into the passive rows.
"""

from __future__ import annotations

import ast
import datetime
import json
import re
from pathlib import Path
from typing import Any

import pytest

from location_data.payload_norm import (
    MEASURED_VOLATILE_PROFILES, NORMALIZER_VERSION, PAGE_KIND_DETAIL,
    probe_normalizer_version,
)
from scripts import location_payload_churn_report as rep
from tests.sql_corpus import first_keyword

_SOURCE_PATH = Path(rep.__file__)
_SOURCE = _SOURCE_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE, _SOURCE_PATH.name)

_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)


def _surface(
    *,
    source: str = "bazos",
    page_kind: str = "detail",
    normalizer_version: str = NORMALIZER_VERSION,
    keys: int = 1_000,
    artefacts: int | None = None,
    keys_repeated: int | None = None,
    fetches: int = 3_000,
    raw_changes: int = 0,
    norm_changes: int = 0,
    mean_raw_bytes: float | None = 70_000.0,
    median_raw_bytes: float | None = 68_000.0,
    mean_norm_bytes: float | None = 40_000.0,
    median_norm_bytes: float | None = 39_000.0,
    mean_interval_s: float | None = 6 * 3_600.0,
    median_interval_s: float | None = 6 * 3_600.0,
    window_end: datetime.datetime = _NOW,
) -> rep.Surface:
    return rep.Surface(rep.SurfaceRow(
        source=source,
        page_kind=page_kind,
        normalizer_version=normalizer_version,
        keys=keys,
        artefacts=keys if artefacts is None else artefacts,
        keys_repeated=keys if keys_repeated is None else keys_repeated,
        fetches=fetches,
        raw_changes=raw_changes,
        norm_changes=norm_changes,
        mean_raw_bytes=mean_raw_bytes,
        median_raw_bytes=median_raw_bytes,
        mean_norm_bytes=mean_norm_bytes,
        median_norm_bytes=median_norm_bytes,
        mean_interval_s=mean_interval_s,
        median_interval_s=median_interval_s,
        window_start=_NOW - datetime.timedelta(days=7),
        window_end=window_end,
    ))


def _sql_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(rep).items()
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


# ------------------------------------------------------------------ 1. read-only


def test_every_executed_statement_is_a_module_level_sql_constant() -> None:
    # Also what keeps the statements discoverable by tests/sql_corpus.py: an f-string or
    # a concatenation would be invisible to the placeholder guard and to the CI PREPARE
    # sweep, which is the only thing that type-checks them against the real schema.
    names = _sql_constants()
    assert sorted(names) == ["_ACTIVE_INVENTORY_SQL", "_CHURN_SURFACE_SQL"]
    for call in _execute_calls():
        first = call.args[0]
        assert isinstance(first, ast.Name), ast.dump(first)
        assert first.id in names, first.id


def test_every_statement_the_report_runs_is_a_select() -> None:
    # `WITH` is allowed because the per-key CTE has to compute a per-ROW quantity before
    # the aggregate reads it — but a data-modifying CTE (`WITH … UPDATE …`) is exactly
    # what the write-verb sweep on the same text forbids, so the pair is airtight.
    for name, sql in _sql_constants().items():
        assert first_keyword(sql) in ("SELECT", "WITH"), name
        assert not re.search(r"\b(insert|update|delete|truncate|copy)\b", sql, re.I), name


def test_every_column_the_readout_reads_exists_in_migration_402() -> None:
    """The CI schema job PREPAREs this statement against a replayed schema, which is the
    real gate; this is the same check without a database, so a renamed column fails in
    the fast suite instead of twenty minutes later."""
    ddl = (
        Path(rep.__file__).resolve().parents[1]
        / "migrations" / "402_location_w2a_payload_churn.sql"
    ).read_text(encoding="utf-8")
    body = ddl.split("create table portal_payload_churn (", 1)[1].split(");", 1)[0]
    declared = {
        line.strip().split()[0]
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith(("primary key", "--"))
    }
    read = {
        "source", "source_id_native", "page_kind", "normalizer_version", "fetches",
        "raw_changes", "norm_changes", "last_byte_size", "last_norm_byte_size",
        "first_seen_at", "last_seen_at",
    }
    assert read <= declared, sorted(read - declared)
    for column in read:
        assert re.search(rf"\b{column}\b", rep._CHURN_SURFACE_SQL), column
    # The three the aggregate deliberately does not read: the forensic hashes and the
    # idempotency token, which say nothing once the counters have been summed.
    # (`source_id_native` IS read — only to strip the index archivers' week suffix and
    # count distinct artefacts.) A NEW column lands here and has to be claimed either
    # way rather than ignored.
    assert declared - read == {
        "last_raw_sha256", "last_norm_sha256", "last_observation",
    }


def test_the_reads_are_bounded_by_a_transaction_local_timeout() -> None:
    # db.connect() is autocommit on the transaction-mode pooler: a session-level SET can
    # land on a different backend than the statement it was meant to guard.
    assert "loader_db.bounded(conn, statement_timeout_s)" in _SOURCE
    assert rep.DEFAULT_STATEMENT_TIMEOUT_S > 0


# --------------------------------------------------- 2. the first-sighting correction


def test_the_rate_denominator_subtracts_one_first_sighting_per_key() -> None:
    # 100 keys x 3 fetches = 300 fetches, but only 200 of them could have been a change.
    surface = _surface(keys=100, fetches=300, raw_changes=100, norm_changes=50)
    assert surface.repeat_fetches == 200
    assert surface.raw_change_rate == 0.5
    assert surface.norm_change_rate == 0.25
    # The wrong arithmetic, named so the regression is unmistakable.
    assert surface.raw_change_rate != pytest.approx(100 / 300)


def test_a_key_seen_once_contributes_a_key_but_no_change_opportunity() -> None:
    # 40 keys, of which 10 were fetched twice: 50 fetches, 10 repeat fetches.
    surface = _surface(keys=40, keys_repeated=10, fetches=50, raw_changes=5)
    assert surface.repeat_fetches == 10
    assert surface.raw_change_rate == 0.5


def test_a_corrupt_counter_can_never_produce_a_negative_denominator() -> None:
    surface = _surface(keys=10, fetches=3, raw_changes=0)
    assert surface.repeat_fetches == 0
    assert surface.raw_change_rate is None


# ----------------------------------------------------------------- 3. the arithmetic


def test_gb_per_cycle_matches_the_designs_own_worked_figure() -> None:
    """02 §2.3.2: 445,191 archived pages at ~70 KB mean is "~31 GB per full refetch
    cycle if every page appears changed" — a 100 % change rate. Same unit (decimal GB),
    same formula, to the byte."""
    surface = _surface(
        keys=445_191, keys_repeated=445_191, fetches=890_382,
        raw_changes=445_191, norm_changes=445_191, mean_raw_bytes=70_000.0,
    )
    assert surface.norm_change_rate == 1.0
    assert surface.gb_per_cycle() == pytest.approx(31.16337)


def test_a_known_change_rate_yields_a_known_gb_per_cycle_and_month() -> None:
    # 100,000 keys, 25 % of the repeat fetches changed, 80 KB mean body:
    # 100,000 x 0.25 x 80,000 = 2,000,000,000 bytes = 2.0 GB per cycle.
    surface = _surface(
        source="bazos", keys=100_000, keys_repeated=100_000, fetches=300_000,
        norm_changes=50_000, raw_changes=200_000, mean_raw_bytes=80_000.0,
    )
    assert surface.norm_change_rate == 0.25
    assert surface.gb_per_cycle() == pytest.approx(2.0)
    # bazos is a 6 h portal: 4 cycles/day x 30 days.
    assert surface.cycles_per_day == 4.0
    assert surface.gb_per_month() == pytest.approx(240.0)
    # The raw-hash arm is the same pass with no normaliser: 100 % of repeat fetches.
    assert surface.raw_change_rate == 1.0
    assert surface.gb_per_month_raw() == pytest.approx(960.0)


def test_the_projection_uses_the_raw_body_size_not_the_normalised_one() -> None:
    """The archive stores the BODY; the normalised projection exists only to be hashed,
    so sizing the storage by it would understate the bill by the strip ratio."""
    surface = _surface(
        keys=1_000, fetches=2_000, norm_changes=1_000,
        mean_raw_bytes=100_000.0, mean_norm_bytes=10_000.0,
    )
    assert surface.gb_per_cycle() == pytest.approx(0.1)


def test_srealitys_declared_cadence_is_its_index_walk_and_is_not_the_detail_projection() -> None:
    """The bug this pins: CYCLES_PER_DAY['sreality'] is the HOURLY INDEX WALK, but a
    detail body is refetched only when the index enqueues it (rule 19). Projecting a
    detail surface at 24/day when the instrument watched each listing refetched every
    three days overstates the signed total by ~72x."""
    assert rep.CYCLES_PER_DAY["sreality"] == 24.0
    surface = _surface(
        source="sreality", keys=100_000, fetches=200_000, norm_changes=100_000,
        mean_raw_bytes=50_000.0, mean_interval_s=3 * 86_400.0,   # refetched every 3 days
    )
    assert surface.gb_per_cycle() == pytest.approx(5.0)
    assert surface.observed_cycles_per_day == pytest.approx(1 / 3)
    assert surface.projection_cycles_per_day == pytest.approx(1 / 3)
    assert surface.cadence_basis == "observed"
    assert surface.gb_per_month() == pytest.approx(5.0 * (1 / 3) * 30)
    assert surface.gb_per_month_declared() == pytest.approx(5.0 * 24 * 30)
    assert surface.gb_per_month() != pytest.approx(surface.gb_per_month_declared())
    # ... and the 72x gap is named on the row, not left for the reader to spot.
    assert "CADENCE 72x" in surface.cadence_note
    assert "CADENCE 72x" in "\n".join(rep._render_projection([surface]))


def test_the_declared_cadence_is_the_fallback_when_nothing_was_refetched_yet() -> None:
    surface = _surface(
        source="bazos", keys=1_000, fetches=3_000, norm_changes=1_000,
        mean_raw_bytes=100_000.0, mean_interval_s=None,
    )
    assert surface.observed_cycles_per_day is None
    assert surface.projection_cycles_per_day == 4.0
    assert surface.cadence_basis == "declared"
    assert surface.gb_per_month() == pytest.approx(surface.gb_per_month_declared())
    assert surface.cadence_note == rep.CADENCE_DECLARED_ONLY


def test_a_cadence_that_agrees_with_the_declared_one_is_not_marked() -> None:
    # bazos declares 4 cycles/day and the instrument saw a 6 h interval: same number.
    surface = _surface(source="bazos", mean_interval_s=6 * 3_600.0)
    assert surface.observed_cycles_per_day == pytest.approx(4.0)
    assert surface.cadence_note == ""


def test_the_cadence_constant_covers_every_portal_the_instrument_measures() -> None:
    # The nine volatile profiles are the fleet; a portal with no cadence constant gets a
    # loud marker rather than a wrong number, but it should not be missing in the first
    # place.
    assert set(rep.CYCLES_PER_DAY) == set(MEASURED_VOLATILE_PROFILES)


def test_an_unknown_source_is_projected_from_what_was_observed_and_marked() -> None:
    surface = _surface(source="newportal", keys=1_000, fetches=2_000, norm_changes=500)
    assert surface.cycles_per_day is None
    assert surface.gb_per_cycle() is not None, "a per-cycle number needs no cadence"
    assert surface.gb_per_month() == pytest.approx(
        (surface.gb_per_cycle() or 0.0) * 4.0 * rep.DAYS_PER_MONTH
    )
    assert surface.cadence_basis == "observed"
    rendered = "\n".join(rep._render_projection([surface]))
    assert rep.UNKNOWN_CADENCE in rendered


def test_a_surface_with_neither_cadence_is_not_projected_at_a_guess() -> None:
    surface = _surface(
        source="newportal", keys=1_000, fetches=2_000, norm_changes=500,
        mean_interval_s=None,
    )
    assert surface.projection_cycles_per_day is None
    assert surface.cadence_basis is None
    assert surface.gb_per_cycle() is not None
    assert surface.gb_per_month() is None
    lines = "\n".join(rep._render_projection([surface]))
    assert rep.NO_CADENCE in lines
    assert "over 0 projectable surface(s)" in lines


def test_the_observed_cadence_is_reported_beside_the_declared_one() -> None:
    surface = _surface(mean_interval_s=6 * 3_600.0)
    assert surface.observed_cycles_per_day == pytest.approx(4.0)
    # ... and a portal walked twice as often as declared shows it.
    faster = _surface(mean_interval_s=3 * 3_600.0)
    assert faster.observed_cycles_per_day == pytest.approx(8.0)
    assert faster.gb_per_month_observed() == pytest.approx(
        (faster.gb_per_cycle() or 0.0) * 8.0 * rep.DAYS_PER_MONTH
    )
    assert faster.gb_per_month() == pytest.approx(faster.gb_per_month_observed())


# --------------------------------------------- 3b. week-stamped index artefacts


def test_an_index_surfaces_per_cycle_base_is_positions_not_position_weeks() -> None:
    """The three index archivers week-stamp their churn key (…/{offset}/{week}), so the
    row count grows one multiple per ISO week the instrument stays on while a walk still
    touches each position once. Projecting from rows would inflate the index surface —
    the exact surface P2 (index archiving) is gated on — linearly with the window."""
    surface = _surface(
        source="sreality", page_kind="index", keys=200, artefacts=100,
        keys_repeated=200, fetches=5_000, norm_changes=2_500,
        mean_raw_bytes=500_000.0, mean_interval_s=3_600.0,
    )
    rate = surface.norm_change_rate or 0.0
    assert surface.artefacts == 100
    assert rate == pytest.approx(2_500 / 4_800)
    assert surface.gb_per_cycle() == pytest.approx(100 * rate * 500_000 / 1e9)
    # The pre-fix number, stated so the regression is unmistakable.
    assert surface.gb_per_cycle() != pytest.approx(200 * rate * 500_000 / 1e9)


def test_a_detail_surfaces_artefacts_are_just_its_keys() -> None:
    surface = _surface(keys=1_000, artefacts=1_000)
    assert surface.artefacts == surface.row.keys
    # ... and an older readout (or a fake) with no artefact count degrades to keys
    # rather than projecting zero bytes.
    legacy = _surface(keys=1_000, artefacts=0)
    assert legacy.artefacts == 1_000


def test_the_inventory_scaled_projection_uses_the_portals_live_row_count() -> None:
    surface = _surface(
        keys=1_000, fetches=2_000, norm_changes=1_000, mean_raw_bytes=100_000.0,
    )
    # 1,000 measured keys -> 0.1 GB/cycle; 50,000 active listings -> 5.0 GB/cycle.
    assert surface.gb_per_cycle(50_000) == pytest.approx(5.0)
    lines = "\n".join(rep._render_inventory_scaled([surface], {"bazos": 50_000}))
    assert "50,000" in lines
    assert "5.00" in lines


def test_an_index_surface_is_absent_from_the_inventory_scaled_table_not_zero() -> None:
    index_surface = _surface(page_kind="index", keys=500, fetches=5_000, norm_changes=4_000)
    assert rep._render_inventory_scaled([index_surface], {"bazos": 50_000}) == []


# ------------------------------------------------------------- 4. insufficient data


def test_a_surface_where_no_key_was_fetched_twice_projects_nothing() -> None:
    surface = _surface(keys=200, keys_repeated=0, fetches=200)
    assert surface.raw_change_rate is None
    assert surface.norm_change_rate is None
    assert surface.insufficient == rep.INSUFFICIENT_NO_REPEAT
    assert surface.gb_per_cycle() is None
    assert surface.gb_per_month() is None
    assert surface.gb_per_cycle_raw() is None


def test_a_handful_of_repeat_fetches_shows_a_rate_but_no_projection() -> None:
    surface = _surface(keys=20, keys_repeated=20, fetches=25, norm_changes=1)
    assert surface.repeat_fetches == 5 < rep.MIN_REPEAT_FETCHES
    assert surface.norm_change_rate == pytest.approx(0.2), "the rate is still honest"
    assert surface.insufficient == rep.INSUFFICIENT_FEW_REPEATS
    assert surface.gb_per_cycle() is None


def test_many_repeats_of_too_few_listings_is_also_insufficient() -> None:
    """200 refetches of two listings measure those two listings, not the portal."""
    surface = _surface(keys=2, keys_repeated=2, fetches=202, norm_changes=100)
    assert surface.repeat_fetches == 200 >= rep.MIN_REPEAT_FETCHES
    assert surface.insufficient == rep.INSUFFICIENT_FEW_KEYS
    assert surface.gb_per_cycle() is None


def test_a_surface_with_no_recorded_body_size_cannot_be_projected() -> None:
    surface = _surface(fetches=3_000, norm_changes=1_000, mean_raw_bytes=None)
    assert surface.norm_change_rate is not None
    assert surface.insufficient == rep.INSUFFICIENT_NO_SIZE
    assert surface.gb_per_cycle() is None


def test_more_changes_than_repeat_fetches_is_reported_as_an_anomaly_not_a_rate() -> None:
    """Each repeat fetch can move the hash at most once, so >100 % is arithmetically
    impossible — it means the counters and the fetch total disagree, and a GB number
    computed from them would be authoritative nonsense."""
    surface = _surface(keys=1_000, fetches=3_000, raw_changes=2_400, norm_changes=300)
    assert (surface.raw_change_rate or 0) > 1.0
    assert surface.insufficient == rep.ANOMALY_RATE_ABOVE_ONE
    assert surface.gb_per_cycle() is None


def test_the_marker_is_printed_where_the_projection_would_have_been() -> None:
    thin = _surface(source="maxima", keys=5, keys_repeated=5, fetches=6, norm_changes=1)
    lines = rep._render_projection([thin])
    body = next(line for line in lines if line.startswith("maxima"))
    assert rep.INSUFFICIENT_FEW_REPEATS in body
    # and there is no number standing in for the suppressed projection
    assert "—" in body


def test_an_insufficient_surface_is_excluded_from_the_total_and_named() -> None:
    good = _surface(source="bazos", keys=1_000, fetches=3_000, norm_changes=1_000,
                    mean_raw_bytes=100_000.0)
    thin = _surface(source="maxima", keys=5, keys_repeated=5, fetches=6, norm_changes=1)
    lines = "\n".join(rep._render_totals([good, thin]))
    assert "over 1 projectable surface(s)" in lines
    assert "maxima/detail" in lines


# ------------------------------------------------------- 5. cohorts and the probe


def test_the_probe_cohort_is_recognised_and_rendered_apart() -> None:
    passive = _surface()
    probe = _surface(normalizer_version=probe_normalizer_version())
    assert not passive.is_probe_cohort
    assert probe.is_probe_cohort
    titles = [title for title, _notes, section in rep._sections([passive, probe]) if section]
    assert len(titles) == 2
    passive_section = rep._sections([passive, probe])[0][2]
    probe_section = rep._sections([passive, probe])[1][2]
    assert passive_section == [passive]
    assert probe_section == [probe]


def test_the_probe_cohort_never_enters_the_signed_projection() -> None:
    """Three fetches ten minutes apart would otherwise pull the fleet total towards a
    cadence no portal runs at."""
    probe = _surface(
        normalizer_version=probe_normalizer_version(),
        keys=200, keys_repeated=200, fetches=600, norm_changes=600,
        mean_interval_s=300.0,
    )
    assert rep._render_projection([probe]) == []
    assert rep._render_inventory_scaled([probe], {"bazos": 50_000}) == []


def test_the_probe_run_never_widens_the_reported_measurement_window() -> None:
    """A probe dispatched today would otherwise stretch (or shift) the window the passive
    rates — and so the signed projection — were measured over."""
    passive = _surface(window_end=_NOW - datetime.timedelta(days=30))
    probe = _surface(normalizer_version=probe_normalizer_version(), window_end=_NOW)
    lines = rep._render_window([passive, probe])
    assert len(lines) == 3, lines            # blank line + one window per section
    assert "PASSIVE MEASUREMENT" in lines[1]
    assert "CONFIRMATION PROBE" in lines[2]
    assert rep._stamp(_NOW) not in lines[1]
    assert rep._stamp(_NOW) in lines[2]


def test_the_distribution_table_keeps_the_probe_rows_in_their_own_section() -> None:
    passive = _surface()
    probe = _surface(normalizer_version=probe_normalizer_version(), mean_interval_s=300.0)
    lines = rep._render_medians([passive, probe])
    headings = [line for line in lines if line.startswith("DISTRIBUTION")]
    assert len(headings) == 2
    assert "PASSIVE MEASUREMENT" in headings[0]
    assert "CONFIRMATION PROBE" in headings[1]


def test_a_normaliser_rollouts_two_cohorts_are_not_both_added_to_the_total() -> None:
    """`normalizer_version` exists to split a rolling profile change into clean
    before/after cohorts, so two rows on one (source, page_kind) is the DESIGNED steady
    state mid-rollout — and summing both would silently double the signed number while
    each row stayed individually correct."""
    old = _surface(
        normalizer_version="payload_norm@1", keys=1_000, fetches=3_000,
        norm_changes=1_000, mean_raw_bytes=100_000.0,
        window_end=_NOW - datetime.timedelta(days=2),
    )
    new = _surface(
        normalizer_version="payload_norm@2", keys=1_000, fetches=3_000,
        norm_changes=1_000, mean_raw_bytes=100_000.0, window_end=_NOW,
    )
    newest, superseded = rep.newest_cohorts([old, new])
    assert newest == [new]
    assert superseded == [old]

    one_cohort = float(new.gb_per_month() or 0.0)
    lines = "\n".join(rep._render_totals([old, new]))
    assert "over 1 projectable surface(s)" in lines
    assert f"{one_cohort:,.2f}" in lines
    assert f"{2 * one_cohort:,.2f}" not in lines
    assert "superseded cohort" in lines
    assert "payload_norm@1" in lines


def test_the_inventory_scaled_total_also_counts_one_cohort_per_surface() -> None:
    old = _surface(normalizer_version="payload_norm@1", keys=1_000, fetches=2_000,
                   norm_changes=1_000, mean_raw_bytes=100_000.0,
                   window_end=_NOW - datetime.timedelta(days=2))
    new = _surface(normalizer_version="payload_norm@2", keys=1_000, fetches=2_000,
                   norm_changes=1_000, mean_raw_bytes=100_000.0, window_end=_NOW)
    lines = rep._render_inventory_scaled([old, new], {"bazos": 50_000})
    total_line = next(line for line in lines if line.startswith("TOTAL"))
    assert "over 1 surface(s)" in total_line
    assert "1 superseded cohort(s) excluded" in total_line
    assert f"{float(new.gb_per_month(50_000) or 0.0):,.2f}" in total_line


def test_the_probe_cohort_never_supersedes_the_passive_one_it_confirms() -> None:
    passive = _surface(window_end=_NOW - datetime.timedelta(days=2))
    probe = _surface(normalizer_version=probe_normalizer_version(), window_end=_NOW)
    newest, superseded = rep.newest_cohorts([passive, probe])
    assert sorted(s.row.normalizer_version for s in newest) == sorted(
        [NORMALIZER_VERSION, probe_normalizer_version()]
    )
    assert superseded == []


def test_a_surface_is_keyed_by_source_page_kind_and_normalizer_version() -> None:
    grouped = re.search(r"GROUP BY 1, 2, 3", rep._CHURN_SURFACE_SQL)
    assert grouped, "the aggregate must not collapse the three cohort columns"
    projected = rep._CHURN_SURFACE_SQL.split("SELECT source,", 1)[1]
    for column in ("page_kind", "normalizer_version"):
        assert column in projected.split("count(*)", 1)[0]


# --------------------------------------------------------------------- 6. plumbing


def _projected_items() -> list[tuple[str, str]]:
    """The OUTER SELECT's (expression, alias) pairs, in projection order.

    The outer list is everything between the last `SELECT` before `FROM per_key` and that
    `FROM` — the CTE's own SELECT and its aliases are on the other side of the split.
    """
    outer = rep._CHURN_SURFACE_SQL.rsplit("FROM per_key", 1)[0].rsplit("SELECT", 1)[1]
    items: list[tuple[str, str]] = []
    depth = 0
    current = ""
    for char in outer:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            items.append(_split_alias(current))
            current = ""
            continue
        current += char
    items.append(_split_alias(current))
    return items


def _split_alias(item: str) -> tuple[str, str]:
    expression, _, alias = item.rpartition(" AS ")
    return " ".join(expression.split()), alias.strip()


def test_the_row_reader_matches_the_statements_projection_order() -> None:
    """The reader is POSITIONAL, so the SELECT's column order is a contract with it.

    Asserting on the aliases alone would not be enough: swapping two same-typed
    expressions under unchanged aliases (sum(raw_changes) <-> sum(norm_changes)) inverts
    the raw-vs-normalised gap — the entire output of this instrument, and the thing
    volatile_paths is decided from — while leaving valid SQL that PREPAREs and a passing
    positional test. So the EXPRESSION is pinned to its alias too.
    """
    expected = [
        ("source", "source"),
        ("page_kind", "page_kind"),
        ("normalizer_version", "normalizer_version"),
        ("count(*)", "keys"),
        ("count(DISTINCT artefact_key)", "artefacts"),
        ("count(*) FILTER (WHERE fetches > 1)", "keys_repeated"),
        ("coalesce(sum(fetches), 0)", "fetches"),
        ("coalesce(sum(raw_changes), 0)", "raw_changes"),
        ("coalesce(sum(norm_changes), 0)", "norm_changes"),
        ("avg(last_byte_size::double precision)", "mean_raw_bytes"),
        ("percentile_cont(0.5) WITHIN GROUP ( ORDER BY last_byte_size::double precision)",
         "median_raw_bytes"),
        ("avg(last_norm_byte_size::double precision)", "mean_norm_bytes"),
        ("percentile_cont(0.5) WITHIN GROUP ( ORDER BY last_norm_byte_size::double"
         " precision)", "median_norm_bytes"),
        ("avg(refetch_interval_s)", "mean_interval_s"),
        ("percentile_cont(0.5) WITHIN GROUP ( ORDER BY refetch_interval_s)",
         "median_interval_s"),
        ("min(first_seen_at)", "window_start"),
        ("max(last_seen_at)", "window_end"),
    ]
    assert _projected_items() == expected
    # ... and the reader unpacks exactly those aliases, in exactly that order.
    assert [alias for _expr, alias in expected] == list(
        rep.SurfaceRow.__dataclass_fields__
    )


def test_the_row_reader_places_every_value_in_its_declared_field() -> None:
    row = (
        "bazos", "detail", NORMALIZER_VERSION, 10, 9, 8, 30, 12, 3,
        70_000.0, 68_000.0, 40_000.0, 39_000.0, 21_600.0, 21_600.0, _NOW, _NOW,
    )
    assert len(row) == len(rep.SurfaceRow.__dataclass_fields__)
    surface = rep.surface_from_row(row)
    assert surface.row.source == "bazos"
    assert surface.row.keys == 10
    assert surface.row.artefacts == 9
    assert surface.row.keys_repeated == 8
    assert surface.row.fetches == 30
    assert surface.row.raw_changes == 12
    assert surface.row.norm_changes == 3
    assert surface.row.mean_raw_bytes == 70_000.0
    assert surface.row.median_interval_s == 21_600.0


def test_the_assumption_block_states_every_constant_a_projection_rests_on() -> None:
    block = "\n".join(rep._assumptions())
    assert f"{rep.BYTES_PER_GB:,} bytes" in block
    assert "sreality=24/day" in block
    assert "bazos=4/day" in block
    assert f"month = {rep.DAYS_PER_MONTH:g} days" in block
    assert "UPPER BOUND" in block
    assert "fetches - keys" in block


def test_the_assumption_block_names_the_cadence_the_projection_actually_uses() -> None:
    """The declared constants are the INDEX-WALK schedule, and reading a GB/month figure
    without knowing which cadence produced it is how a 72x error gets signed."""
    block = "\n".join(rep._assumptions())
    assert "OBSERVED refetch cadence" in block
    assert "INDEX-WALK schedule" in block
    assert "rule 19" in block
    assert "ARTEFACTS" in block and "/{week}" in block
    assert "newest" in block


def test_the_json_assumptions_name_the_declared_cadence_for_what_it_is() -> None:
    payload = rep.to_json(rep.Measurement(surfaces=[], active_inventory={}))
    assumptions = payload["assumptions"]
    assert "declared_cycles_per_day_is_the_index_walk" in assumptions
    assert "cycles_per_day" not in assumptions, "the bare name read as the projection's"
    assert assumptions["projection_cadence"].startswith("observed")
    assert assumptions["index_keys_are_week_stamped"] is True


def test_the_json_payload_is_serialisable_and_carries_the_corrected_denominator() -> None:
    measurement = rep.Measurement(
        surfaces=[_surface(keys=1_000, fetches=3_000, norm_changes=500,
                           raw_changes=2_000)],
        active_inventory={"bazos": 50_000},
    )
    payload = rep.to_json(measurement)
    round_tripped: dict[str, Any] = json.loads(json.dumps(payload, default=str))
    surface = round_tripped["surfaces"][0]
    assert surface["repeat_fetches"] == 2_000
    assert surface["norm_change_rate"] == pytest.approx(0.25)
    assert surface["active_listings"] == 50_000
    assert surface["gb_per_month_active_inventory"] is not None
    assert round_tripped["assumptions"]["bytes_per_gb"] == rep.BYTES_PER_GB


def test_the_whole_report_renders_without_a_database() -> None:
    measurement = rep.Measurement(
        surfaces=[
            _surface(source="bazos"),
            _surface(source="sreality", page_kind="index", keys=300, fetches=7_000,
                     norm_changes=6_000, raw_changes=6_900),
            _surface(normalizer_version=probe_normalizer_version(), keys=200,
                     keys_repeated=200, fetches=600, raw_changes=590, norm_changes=2),
        ],
        active_inventory={"bazos": 50_000, "sreality": 102_000},
    )
    lines = rep.render(measurement)
    text = "\n".join(lines)
    assert "PASSIVE MEASUREMENT" in text
    assert "CONFIRMATION PROBE" in text
    assert "MEASUREMENT WINDOW" in text
    assert probe_normalizer_version() in text


def test_an_empty_instrument_renders_a_window_notice_rather_than_crashing() -> None:
    lines = rep.render(rep.Measurement(surfaces=[], active_inventory={}))
    assert any("recorded nothing yet" in line for line in lines)
