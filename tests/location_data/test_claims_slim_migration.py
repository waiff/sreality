"""W1-b: the claim spine is 19 columns, and NOTHING still names what migration 497 drops.

Two gates, both offline.

1. THE MIGRATION SAYS WHAT IT DOES. Migration 497 is the destructive half of the wave —
   seven tables (one of them 263 M rows / 50 GB), three views, 26 columns, one header flag
   and one CHECK value. Parsed here against migration 382's own CREATE TABLE, so "kept" is
   derived rather than transcribed: a column silently left behind fails, and so does a
   column dropped that the resolver reads.

2. NOTHING IN THE RUNTIME NAMES A DROPPED OBJECT. The CI PREPARE sweep (which runs the
   whole migration chain against a real Postgres, `.github/workflows/migrations.yml`) is
   the authoritative version of this check, but it needs a database and a 15-minute lane.
   This one runs in the normal pytest job, over the SAME corpus (`tests/sql_corpus`), and
   fails a PR in seconds.

   It matters in BOTH directions, because the code ships before the migration is applied:
   a statement that still reads a dropped column breaks after the apply, and a statement
   that reads a column only the NEW schema has breaks before it.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests import sql_corpus

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
_CREATE = (_MIGRATIONS / "382_location_w1_claims.sql").read_text(encoding="utf-8")
_SLIM = (_MIGRATIONS / "497_location_w1b_claims_slim.sql").read_text(encoding="utf-8")

# The 19 the resolver, the scorecard and the inspector actually read. Transcribed because
# this list IS the contract — everything else in this file is derived from the migrations.
KEPT_COLUMNS = frozenset({
    "id", "listing_id", "source", "claim_type", "surface", "extraction_method",
    "first_observed_at", "contract_entry_id", "value_text", "value_num", "value_geom",
    "value_jsonb", "subject_scoped", "declared_precision_label", "declared_radius_m",
    "blur_evidence", "claim_confidence", "licence_class", "claim_fingerprint",
})

DROPPED_TABLES = (
    "location_claim_observations", "location_claim_links", "location_claim_retractions",
    "location_claim_absences", "location_claim_type_meta", "location_enrichment_state",
    "portal_payload_churn",
)
DROPPED_VIEWS = (
    "location_claims_live", "location_claims_unretracted", "location_claims_shadow",
)


def _declared_columns() -> set[str]:
    """Every column `create table location_claims (...)` declares in migration 382.

    Split on TOP-LEVEL commas: the table's CHECK constraints span several lines and carry
    commas of their own, and a line-oriented parse reads their continuations as columns.
    """
    body = _CREATE.split("create table location_claims (")[1].split("\n);")[0]
    body = re.sub(r"--[^\n]*", "", body)
    items, depth, current = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    items.append("".join(current))
    out: set[str] = set()
    for item in items:
        words = item.split()
        if not words or words[0] in ("unique", "check", "primary", "foreign", "constraint"):
            continue
        out.add(words[0])
    return out


def _dropped_columns() -> set[str]:
    """Only the claim spine's — `portal_contracts.shadow` rides the same verb."""
    block = _SLIM.split("alter table location_claims\n  drop column")[1].split(";")[0]
    return set(re.findall(r"([a-z0-9_]+),?$", block, re.M)) | {
        block.split()[-1].rstrip(",")}


# --------------------------------------------------------------- 1. the migration


def test_the_kept_and_dropped_columns_partition_the_table():
    declared = _declared_columns()
    dropped = _dropped_columns()
    assert declared, "DDL parse failed — migration 382 declared no columns"
    assert dropped & declared == dropped, (
        f"497 drops column(s) 382 never declared: {sorted(dropped - declared)}")
    assert declared - dropped == set(KEPT_COLUMNS), (
        "the claim spine is not the 19 kept columns; left behind: "
        f"{sorted((declared - dropped) - KEPT_COLUMNS)}, over-dropped: "
        f"{sorted(KEPT_COLUMNS - (declared - dropped))}")
    assert len(dropped) == 26 and len(KEPT_COLUMNS) == 19


def test_the_migration_drops_every_side_table_and_view():
    for view in DROPPED_VIEWS:
        assert f"drop view if exists {view};" in _SLIM, view
    for table in DROPPED_TABLES:
        assert f"drop table if exists {table};" in _SLIM, table
    # Views before the columns they `select *` from, or the DROP COLUMN needs CASCADE —
    # which would take anything else hanging off them with it, silently.
    assert _SLIM.index("drop view if exists") < _SLIM.index("drop column if exists")
    # The lane's run ledger and cursor is NOT a side table.
    assert "drop table if exists location_claim_batches" not in _SLIM


def test_the_migration_creates_nothing():
    """Rule 25: this wave is subtraction. The one `add constraint` re-states an invariant
    that already existed, minus a column it named — it is not a new object."""
    for verb in ("create table", "create view", "create materialized view", "create index",
                 "create function", "create type", "create or replace"):
        assert verb not in _SLIM.lower(), verb
    added = re.findall(r"add constraint ([a-z0-9_]+)", _SLIM)
    assert added == ["loc_claim_value_present", "dirty_locations_reason_check"], added


def test_the_shadow_flag_and_its_queue_reason_go_together():
    """The flag was only half of it: `dirty_locations.reason` carried a value nothing
    could produce once `set_shadow` was gone, and a CHECK is the one place a dead
    vocabulary entry is invisible."""
    assert "alter table portal_contracts drop column if exists shadow;" in _SLIM
    check = _SLIM.split("add constraint dirty_locations_reason_check")[1]
    assert "'contract_shadow'" not in check
    for kept in ("claim_insert", "resolution_written", "registry_version", "policy_version",
                 "collision_recompute", "property_grouping", "operator_edit", "full_sweep"):
        assert f"'{kept}'" in check, kept


def test_the_fingerprint_function_is_untouched():
    """Migration 386's 23-argument function still takes every value the readers compute —
    nine of which are no longer stored. Narrowing it would re-dialect 5 M fingerprints on
    disk, and a re-dialected fingerprint does not conflict, it inserts."""
    assert "location_claim_fingerprint" not in _SLIM.replace(
        "-- ", "").split("begin;")[1], (
        "the migration must not touch location_claim_fingerprint")


# --------------------------------------------------------------- 2. the runtime corpus


def _corpus():
    return sql_corpus.discover(include_inline=True)


def _names_location_claims(sql: str) -> bool:
    return re.search(r"\blocation_claims\b(?!_)", sql) is not None


def test_no_runtime_sql_names_a_dropped_relation():
    dead = DROPPED_TABLES + DROPPED_VIEWS
    offenders: list[str] = []
    for item in _corpus():
        for name in dead:
            if re.search(rf"\b{name}\b", item.sql):
                offenders.append(f"{item.origin} -> {name}")
    assert not offenders, (
        "runtime SQL still names a relation migration 497 drops:\n  "
        + "\n  ".join(sorted(set(offenders))))


def test_no_runtime_sql_names_a_dropped_claim_column():
    """Scoped to the statements that touch `location_claims`, because the dropped names are
    ordinary elsewhere (`snapshot_id` on listing_snapshots, `model` on llm_calls…).

    The two claim WRITES are checked through their INSERT column list only: their input CTE
    still parses the FULL row dict and still feeds the whole fingerprint tuple, which is
    exactly what keeps every fingerprint already on disk valid.
    """
    dropped = _dropped_columns()
    offenders: list[str] = []
    for item in _corpus():
        if not _names_location_claims(item.sql):
            continue
        inserts = re.findall(r"INSERT INTO location_claims \(([^)]*)\)", item.sql)
        if inserts:
            for columns in inserts:
                for column in (c.strip() for c in columns.replace("\n", " ").split(",")):
                    if column and column not in KEPT_COLUMNS:
                        offenders.append(f"{item.origin} INSERT -> {column}")
            continue
        for column in dropped:
            # `%(extractor_id)s` is a psycopg PARAMETER name, not a column reference.
            if re.search(rf"(?<!%\(){re.escape(column)}\b(?!\)s)", item.sql):
                offenders.append(f"{item.origin} -> {column}")
    assert not offenders, (
        "runtime SQL still reads/writes a column migration 497 drops:\n  "
        + "\n  ".join(sorted(set(offenders))))


def test_both_claim_writes_insert_exactly_the_kept_columns():
    """`id` and `claim_fingerprint` are the two ends of the kept set that behave
    differently: one is the serial the INSERT never names, the other is computed in SQL."""
    from location_data import claims_intake, operator_corrections

    for label, sql in (("intake", claims_intake._CLAIM_WRITE_SQL),
                       ("operator", operator_corrections._OPERATOR_CLAIM_SQL)):
        columns = re.search(r"INSERT INTO location_claims \(([^)]*)\)", sql)
        assert columns, label
        written = {c.strip() for c in columns.group(1).replace("\n", " ").split(",")}
        assert written == KEPT_COLUMNS - {"id"}, (label, sorted(written))
