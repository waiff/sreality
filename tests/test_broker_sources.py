"""The broker attribution registry must reproduce the pre-registry SQL exactly.

Before this registry, `scripts/resolve_brokers.py` carried five hand-copied
families of near-identical SQL (~330 lines, 16 statements) — one per portal. The
registry generates them from config rows instead. That is only safe if every
portal still attributes IDENTICALLY, so the baseline is a verbatim transcript of
what each family did on origin/main (`tests/broker_attribution_snapshot.py`), and
the load-bearing test here compares the WHOLE rendered statement against it. A
property/substring check would not do: a mutation sweep over an earlier draft
(swap a JSON key, drop the `IS DISTINCT FROM` no-op guard, drop a `{sel}` bound)
left every substring assertion green, because a substring test only sees what it
names.

Nine of the sixteen statements come out byte-identical. The other seven differ in
exactly three documented, verified-equivalent ways, each pinned below to the
statements it may explain:

  1. sreality's two contact statements are wrapped in `WITH chunk AS NOT
     MATERIALIZED (...)`. NOT MATERIALIZED forces Postgres to inline a
     single-reference CTE, which is the original direct join.
  2. The four non-sreality identity upserts select the columns their portal lacks
     as explicit NULLs (`NULL::text AS email`, `NULL::numeric AS rating`,
     `NULL::int AS reviews`) and carry them through the INSERT + the latest-wins
     DO UPDATE. `broker_identities.{email,rating,review_count}` are written by
     these four statements and nothing else in the repo, and each is scoped to its
     own `(source, source_broker_id_native)` key — so a row of a portal that never
     had the column cannot hold anything but NULL, and the added CASE can only
     write NULL over NULL. The change is purely additive, which
     `test_null_column_deltas_only_add_columns` proves token by token.
  3. ceskereality's 420-normalisation moved from the INSERT + GROUP BY into the
     chunk CTE. Same value, same grouping key.
"""

from __future__ import annotations

import difflib
import os
import re

import pytest

from tests.broker_attribution_snapshot import PRE_REGISTRY_SQL, REGISTRY_DELTAS
from toolkit.broker_sources import (
    BROKER_FINGERPRINT_KEYS,
    BROKER_SOURCE_NAMES,
    BROKER_SOURCES,
    attribution_statements,
)

SEL = "l.id = ANY(%(ids)s)"

# The statement kinds each source issues, in execution order.
_FAMILIES: dict[str, tuple[str, ...]] = {
    "sreality": ("identity", "email", "phone", "link"),
    "idnes": ("identity", "email", "phone", "link"),
    "ceskereality": ("identity", "phone", "link"),
    "realitymix": ("identity", "link"),
    "remax": ("identity", "email", "link"),
}

# Which of the three documented deviations explains each non-identical statement.
_DELTA_INLINED_CTE = {("sreality", "email"), ("sreality", "phone")}
_DELTA_NULL_COLUMNS = {("idnes", "identity"), ("ceskereality", "identity"),
                       ("realitymix", "identity"), ("remax", "identity")}
_DELTA_NORMALISE_IN_CTE = {("ceskereality", "phone")}

_BY_SOURCE = {c.source: c for c in BROKER_SOURCES}
_KEYS = [(src, kind) for src, kinds in _FAMILIES.items() for kind in kinds]


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def _rendered(source: str) -> list[str]:
    return [_norm(s.format(sel=SEL)) for s in _BY_SOURCE[source].statements()]


def _one(source: str, kind: str) -> str:
    return _rendered(source)[_FAMILIES[source].index(kind)]


def _expected(source: str, kind: str) -> str:
    frozen = REGISTRY_DELTAS.get((source, kind), PRE_REGISTRY_SQL[(source, kind)])
    return _norm(frozen.format(sel=SEL))


def _tokens(sql: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z_0-9]*|'[^']*'|::|->>|->|\|\||>=|\S", sql)


@pytest.mark.parametrize(("source", "kind"), _KEYS)
def test_rendered_sql_matches_the_frozen_snapshot(source: str, kind: str) -> None:
    """Whole-statement equality against the frozen transcript — the guard that
    actually catches a template mutation. For the nine byte-identical statements
    the expectation IS origin/main's independently written SQL."""
    assert _one(source, kind) == _expected(source, kind)


def test_the_generated_statements_are_exactly_the_pre_registry_families() -> None:
    """Same statements, same order, no extras — a dropped contact upsert loses a
    whole portal's bridging silently, and a statement issued for a source that
    never had one would write contacts nobody verified."""
    for source, kinds in _FAMILIES.items():
        assert len(_rendered(source)) == len(kinds), source
    assert len(attribution_statements()) == len(PRE_REGISTRY_SQL) == 16


def test_the_set_of_statements_that_deviate_is_frozen() -> None:
    """Nine statements are byte-identical to the pre-registry SQL and must stay
    that way; the other seven are each claimed by exactly one documented
    deviation. A statement silently leaving or joining the identical set is the
    refactor breaking its own equivalence argument."""
    deviating = {k for k in _KEYS if _norm(PRE_REGISTRY_SQL[k].format(sel=SEL)) != _one(*k)}
    assert len(_KEYS) - len(deviating) == 9
    assert deviating == set(REGISTRY_DELTAS)
    assert deviating == _DELTA_INLINED_CTE | _DELTA_NULL_COLUMNS | _DELTA_NORMALISE_IN_CTE
    assert len(_DELTA_INLINED_CTE) + len(_DELTA_NULL_COLUMNS) \
        + len(_DELTA_NORMALISE_IN_CTE) == len(deviating)


@pytest.mark.parametrize(("source", "kind"), sorted(_DELTA_NULL_COLUMNS))
def test_null_column_deltas_only_add_columns(source: str, kind: str) -> None:
    """Deviation 2. Purely additive: the pre-registry token stream survives
    unchanged and every inserted run introduces one of the NULL-valued columns.
    Nothing is removed or rewritten, so nothing that used to be written stopped
    being written."""
    before = _tokens(_norm(PRE_REGISTRY_SQL[(source, kind)].format(sel=SEL)))
    after = _tokens(_one(source, kind))
    added: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=before, b=after).get_opcodes():
        assert op in ("equal", "insert"), f"{op} at {before[i1:i2]} -> {after[j1:j2]}"
        if op == "insert":
            added.append(" ".join(after[j1:j2]))
    assert added
    for run in added:
        assert any(col in run for col in ("email", "rating", "reviews", "review_count")), run


@pytest.mark.parametrize("kind", ["email", "phone"])
def test_sreality_contacts_keep_the_inlined_plan(kind: str) -> None:
    """Deviation 1. The shared template always builds a chunk CTE; sreality's
    contacts predate the materialisation fix, so they render NOT MATERIALIZED —
    which is Postgres' instruction to inline a single-reference CTE, i.e. the
    original direct join. Every other source materialises to bound the listings
    scan by {sel} before the identity join."""
    pre, now = _norm(PRE_REGISTRY_SQL[("sreality", kind)]), _one("sreality", kind)
    assert "WITH chunk" not in pre and "FROM listings l JOIN broker_identities" in pre
    assert now.startswith("WITH chunk AS NOT MATERIALIZED (")
    assert "FROM chunk c JOIN broker_identities bi ON bi.source = 'sreality'" in now
    for other in ("idnes", "ceskereality", "remax"):
        for sql in _rendered(other):
            assert "NOT MATERIALIZED" not in sql


def test_ceskereality_normalises_the_phone_inside_the_chunk() -> None:
    """Deviation 3, proved by construction: relocating the 420 CASE from the
    INSERT's select list + GROUP BY into the chunk CTE turns the pre-registry
    statement into the rendered one exactly. Same value, and the GROUP BY still
    groups on the normalised number rather than the raw digits."""
    digits = "regexp_replace(l.raw_json->'broker'->>'phone', '[^0-9]', '', 'g')"
    at_insert = "CASE WHEN length(c.digits) = 9 THEN '420' || c.digits ELSE c.digits END"
    in_chunk = at_insert.replace("c.digits", digits)
    pre = _norm(PRE_REGISTRY_SQL[("ceskereality", "phone")].format(sel=SEL))
    assert pre.count(at_insert) == 2  # once in the select list, once in the GROUP BY
    moved = pre.replace(f"{digits} AS digits", f"{in_chunk} AS phone")
    assert moved.replace(at_insert, "c.phone") == _one("ceskereality", "phone")


@pytest.mark.parametrize("source", sorted(_FAMILIES))
def test_every_statement_is_pinned_to_its_own_source(source: str) -> None:
    """_attribute runs EVERY source's SQL over the same id chunk, so a statement
    that lost its source literal would attribute another portal's listings to this
    portal's identities."""
    for sql in _rendered(source):
        assert f"l.source = '{source}'" in sql
        for other in BROKER_SOURCE_NAMES:
            if other != source:
                assert f"'{other}'" not in sql


def test_every_statement_carries_exactly_one_sel_slot() -> None:
    """An unbounded attribution statement would scan the whole corpus per chunk."""
    for sql in attribution_statements():
        assert sql.count("{sel}") == 1
        rendered = sql.format(sel=SEL)
        assert "%(ids)s" in rendered
        # No unfilled slot survived into the executed text.
        assert not re.search(r"(?<!')\{[A-Za-z_]\w*\}", rendered)


def test_registry_order_drives_the_full_sweep_source_scan() -> None:
    assert BROKER_SOURCE_NAMES == (
        "sreality", "idnes", "ceskereality", "realitymix", "remax")


def test_fingerprint_keys_reproduce_the_pre_registry_allowlist() -> None:
    """The dirty-queue allowlist that makes a broker-only page change re-enqueue.
    Losing a key silently stops re-attribution for that portal; deriving it can
    only reorder the tuple, never change the set."""
    assert set(BROKER_FINGERPRINT_KEYS) == {
        "account_oid", "broker_id", "name", "email", "phone",
        "agency_name", "agency_slug", "agency_id"}
    registered = {k for c in BROKER_SOURCES if c.block == "broker"
                  for k in c.fingerprint_keys()}
    assert set(BROKER_FINGERPRINT_KEYS) == registered


def test_scraper_db_derives_both_registries_from_this_module() -> None:
    """The half-landed-onboarding guard: before this, a new portal had to be added
    to three hand-maintained lists in two files."""
    from scraper import db

    assert db.BROKER_ATTRIBUTED_SOURCES == frozenset(BROKER_SOURCE_NAMES)
    assert db._BROKER_FINGERPRINT_KEYS == BROKER_FINGERPRINT_KEYS


# --- schema check (CI's replayed-schema job only) ---------------------------

_DB_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not _DB_URL, reason="TEST_DATABASE_URL not set")
def test_every_attribution_statement_plans_against_the_real_schema() -> None:
    """PREPARE parses, name-resolves and type-checks without touching a row — the
    one thing a fake cursor structurally cannot answer about generated SQL. These
    statements are built at import time, so tests/sql_corpus.py cannot discover
    them the way it discovers a module-level `*_SQL` constant."""
    import psycopg

    conn = psycopg.connect(_DB_URL, autocommit=True)
    try:
        for i, sql in enumerate(attribution_statements()):
            concrete = sql.format(sel="l.id = ANY(ARRAY[1]::bigint[])")
            with conn.cursor() as cur:
                cur.execute(f"PREPARE _bs_{i} AS {concrete}")
                cur.execute(f"DEALLOCATE _bs_{i}")
    finally:
        conn.close()
