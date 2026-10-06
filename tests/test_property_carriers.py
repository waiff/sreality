"""The carrier list, `toolkit.property_carriers` (rules 15, 18, 22), offline: the FK census over
`migrations/*.sql` (every column that references `properties` is carried or named in
`NOT_CARRIED`), the order the list must keep, the protocol every carrier meets, and a strict
cursor that runs each REAL carrier and fails on any statement it did not declare or any param it
did not supply. What each carrier does to rows is executed in tests/test_property_carriers_live.py
(with the live column census); the writers' use of the seam is tests/test_property_merge_set.py
and tests/test_detach_listing.py."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tests import sql_corpus
from toolkit import property_carriers as carriers
from toolkit.property_carriers import (
    CARRIER_SQL,
    NOT_CARRIED,
    PROPERTY_CARRIERS,
    Carrier,
    DetachStep,
    Hop,
    MergeStep,
    carried_columns,
)

_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
G, G2 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
_HOW_TO_FIX = (
    "carry it (a SET/APPEND table keyed on property_id = one `CurationTable(...)` line before "
    "`Pipeline()`; any other shape = one class meeting `Carrier`), or name it in `NOT_CARRIED` "
    "with the reason a merge leaves it — toolkit/property_carriers.py")


def _n(sql: str) -> str:
    return " ".join(sql.split())


# --- the census ---------------------------------------------------------------------------

_REF = r"references\s+(?:public\.)?properties\b(?:\s*\(\s*id\s*\))?"
_REFERS = re.compile(_REF, re.I)
_CREATE = re.compile(
    r"create\s+(?:unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?([\w.]+)\s*\((.*)\)\s*$",
    re.S | re.I)
_ALTER = re.compile(r"alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?([\w.]+)\s+(.*)$",
                    re.S | re.I)
# A table constraint, in CREATE TABLE or after ALTER ... ADD: one column only (a composite key
# is not read, so it fails the census below rather than passing it).
_TABLE_FK = re.compile(r"(?:constraint\s+\w+\s+)?foreign\s+key\s*\(\s*(\w+)\s*\)\s*" + _REF,
                       re.I)
_COLUMN_FK = re.compile(
    r"(?!(?:constraint|foreign|primary|unique|check|exclude|like)\b)(\w+)\s+.*?" + _REF,
    re.I | re.S)
_ADD = re.compile(r"add\s+(?:column\s+)?(?:if\s+not\s+exists\s+)?(.*)$", re.I | re.S)
_DROP_COLUMN = re.compile(r"drop\s+(?:column\s+)?(?:if\s+exists\s+)?(?!constraint\b)(\w+)",
                          re.I)
_DROP_TABLE = re.compile(
    r"drop\s+table\s+(?:if\s+exists\s+)?([\w.,\s]+?)(?:\s+cascade|\s+restrict)?\s*$", re.I | re.S)


def _table(name: str) -> str:
    return name.lower().removeprefix("public.")


def _elements(body: str) -> list[str]:
    """A CREATE TABLE body or an ALTER action list, split at its top-level commas (a type,
    default or key list holds its own)."""
    out, depth, quoted, start = [], 0, False, 0
    for i, ch in enumerate(body):
        if ch == "'":
            quoted = not quoted
        elif quoted:
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(body[start:i].strip())
            start = i + 1
    out.append(body[start:].strip())
    return out


def _fk_column(element: str) -> str | None:
    """The column a column definition or a table constraint points at `properties`, if any."""
    if m := _TABLE_FK.match(element) or _COLUMN_FK.match(element):
        return m.group(1).lower()
    return None


def foreign_keys_to_properties(root: Path = _MIGRATIONS) -> dict[tuple[str, str], str]:
    """(table, column) -> the migration that added it, for every column still referencing
    `properties` after replaying `migrations/*.sql` in order (510 skipped, as the replay does:
    migrations.yml). A reference it cannot attribute to one column raises, naming the
    migration, so a shape the parser does not model fails the census instead of passing it."""
    fks: dict[tuple[str, str], str] = {}
    unread: list[str] = []
    for path in sorted(root.glob("*.sql")):
        if path.name.startswith("510_"):
            continue
        for statement in re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8")).split(";"):
            s = statement.strip()
            if m := _CREATE.match(s):
                table, elements = _table(m.group(1)), _elements(m.group(2))
            elif m := _ALTER.match(s):
                table, elements = _table(m.group(1)), []
                for action in _elements(m.group(2)):
                    if d := _DROP_COLUMN.match(action):
                        fks.pop((table, d.group(1).lower()), None)
                    elif a := _ADD.match(action):
                        elements.append(a.group(1))
            elif m := _DROP_TABLE.match(s):
                for dropped in (_table(t.strip()) for t in m.group(1).split(",")):
                    for key in [k for k in fks if k[0] == dropped]:
                        del fks[key]
                continue
            else:
                elements, table = [], ""
            found = [c for c in map(_fk_column, elements) if c]
            for col in found:
                fks[(table, col)] = path.name
            if len(_REFERS.findall(s)) > len(found):
                unread.append(f"{path.name}: {' '.join(s.split())[:120]}")
    if unread:
        raise ValueError("the census cannot read these references to properties; teach "
                         "foreign_keys_to_properties the shape:\n  " + "\n  ".join(unread))
    return fks


def test_every_foreign_key_to_properties_is_carried_or_named():
    fks = foreign_keys_to_properties()
    assert ("listings", "property_id") in fks and ("property_tags", "property_id") in fks, (
        "the migration walk found nothing it must: the census parser is broken")
    unclassified = sorted(set(fks) - carried_columns() - NOT_CARRIED.keys())
    assert not unclassified, (
        "a column references properties(id) but no carrier keeps it true across a merge and "
        "NOT_CARRIED does not say why a merge leaves it:\n"
        + "\n".join(f"  {t}.{c} ({fks[(t, c)]})" for t, c in unclassified)
        + f"\nFix: {_HOW_TO_FIX}.")


def test_the_census_sees_a_new_reference_and_a_dropped_table(tmp_path):
    (tmp_path / "001_a.sql").write_text(
        "create table watch_list (\n  id bigint,\n  property_id bigint not null\n"
        "    references properties(id) on delete cascade -- why\n);\n"
        "create table gone (x bigint references public.properties (id));\n"
        "alter table listings add column if not exists twin_id bigint references properties(id);")
    (tmp_path / "002_b.sql").write_text("drop table if exists gone, other cascade;")
    assert foreign_keys_to_properties(tmp_path) == {("watch_list", "property_id"): "001_a.sql",
                                            ("listings", "twin_id"): "001_a.sql"}



@pytest.mark.parametrize("sql, column", [
    pytest.param("create table w (id bigint, property_id bigint references properties);",
                 "property_id", id="references-without-(id)"),
    pytest.param("alter table w add property_id bigint references public.properties(id);",
                 "property_id", id="alter-add-without-COLUMN"),
    pytest.param("alter table w add constraint w_fk foreign key (property_id) "
                 "references properties(id) on delete cascade;",
                 "property_id", id="alter-add-constraint-foreign-key"),
    pytest.param("alter table w add foreign key (twin_id) references properties;",
                 "twin_id", id="alter-add-unnamed-foreign-key"),
    pytest.param("create table w (id bigint, twin_id bigint,\n"
                 "  constraint w_fk foreign key (twin_id) references properties (id));",
                 "twin_id", id="create-table-constraint-one-line"),
    pytest.param("create table w (\n  id bigint,\n  twin_id bigint not null,\n"
                 "  constraint w_fk\n    foreign key (twin_id)\n    references properties(id)\n);",
                 "twin_id", id="create-table-constraint-multi-line"),
    pytest.param("create table w (id bigint, price numeric(12, 2), twin_id bigint,\n"
                 "  foreign key (twin_id) references properties);",
                 "twin_id", id="create-table-unnamed-foreign-key"),
])
def test_the_census_reads_every_foreign_key_shape(tmp_path, sql, column):
    (tmp_path / "001_a.sql").write_text(sql)
    assert foreign_keys_to_properties(tmp_path) == {("w", column): "001_a.sql"}


@pytest.mark.parametrize("sql", [
    pytest.param("alter table w add constraint w_fk foreign key (account_id, property_id) "
                 "references properties (account_id, id);", id="composite-key"),
    pytest.param("do $$ begin alter table w add column property_id bigint "
                 "references properties(id); end $$;", id="inside-a-do-block"),
])
def test_a_reference_the_census_cannot_read_fails_it(tmp_path, sql):
    (tmp_path / "001_a.sql").write_text(sql)
    with pytest.raises(ValueError, match="001_a.sql"):
        foreign_keys_to_properties(tmp_path)

# --- the list -----------------------------------------------------------------------------


def _names() -> list[str]:
    return [c.name for c in PROPERTY_CARRIERS]


def test_dismissals_run_after_the_pipeline():
    """The dismissal lift reads the live card the pipeline carry just put on the survivor."""
    assert _names().index("pipeline") < _names().index("dismissals")


def test_every_carrier_meets_the_protocol():
    assert len(set(_names())) == len(_names()), "two carriers share a name"
    for c in PROPERTY_CARRIERS:
        assert isinstance(c, Carrier), c
        assert c.columns and c.sql, f"{c.name} declares no columns or no SQL"
    assert not carried_columns() & NOT_CARRIED.keys(), "a column both carried and not carried"
    for key, why in NOT_CARRIED.items():
        assert why.strip(), f"NOT_CARRIED[{key}] needs its reason"


def test_the_status_log_stays_with_its_own_property():
    """Migration 559: a survivor holding two properties' activity logs charts false gaps."""
    assert ("property_status_events", "property_id") in NOT_CARRIED
    assert not any(t == "property_status_events" for t, _c in carried_columns())


def test_no_carrier_deletes_history():
    """The one sanctioned delete is a SET table's collision collapse (and the pipeline's
    current-state card, snapshotted to its ledger first); never a ledger or log row."""
    history = ("property_dismissals", "property_pipeline_events",
               "property_merge_events", "property_status_events", "properties")
    for statement in CARRIER_SQL:
        deleted = re.findall(r"\bDELETE\s+FROM\s+(\w+)", statement, re.I)
        assert not set(deleted) & set(history), f"a carrier deletes history: {_n(statement)[:120]}"


# --- every real carrier, through a cursor that refuses what it did not declare -------------

_NAMED = re.compile(r"%\((\w+)\)s")


class _StrictCur:
    def __init__(self, carrier: Carrier) -> None:
        self.carrier = carrier
        self.declared = {_n(s) for s in carrier.sql}
        self.ran: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        s = _n(sql)
        assert s in self.declared, f"{self.carrier.name} ran SQL it does not declare: {s[:140]}"
        named = set(_NAMED.findall(s))
        if named:
            assert isinstance(params, dict) and named <= params.keys(), (
                f"{self.carrier.name}: {sorted(named - set(params or {}))} not supplied")
        else:
            assert s.count("%s") == len(params or ()), f"{self.carrier.name}: param count"
        self.ran.append((s, params))


def _walk(carrier: Carrier, *, undo: tuple[Hop, ...] = (Hop(1, G, 10, 20),),
          left: int = 10) -> _StrictCur:
    cur = _StrictCur(carrier)
    carrier.on_merge(cur, MergeStep(10, 20, G, "operator"))
    carrier.on_detach(cur, DetachStep(20, left, undo, "operator"))
    return cur


@pytest.mark.parametrize("carrier", PROPERTY_CARRIERS, ids=lambda c: c.name)
def test_every_carrier_runs_only_its_declared_sql_with_its_params_supplied(carrier):
    cur = _walk(carrier)
    assert {s for s, _p in cur.ran} == {_n(s) for s in carrier.sql}, (
        f"{carrier.name} declares SQL it never runs")


def test_the_dispatch_collapse_hands_its_sends_to_the_kept_twin_first():
    """A send on a collapsed row would be nulled (ON DELETE SET NULL), which channel_sends_check
    refuses: the resend runs before the DELETE and pairs each retired row with exactly the twin
    the DELETE collapses it onto (the same keys, NULL-safe), so the two cannot drift apart."""
    dispatches = next(c for c in PROPERTY_CARRIERS if c.name == "notification_dispatches")
    assert isinstance(dispatches, carriers.Dispatches)
    lock, resend, collapse, move = (_n(s) for s in dispatches.sql)
    assert [s for s, _p in _walk(dispatches).ran] == [
        _n(carriers.Dispatches.LOCK_SQL), _n(carriers.Dispatches.RESEND_SQL), collapse, move]
    assert collapse.startswith("DELETE FROM notification_dispatches")
    assert move.startswith("UPDATE notification_dispatches SET property_id")
    twin = re.compile(r"s\.(\w+) IS NOT DISTINCT FROM r\.\1\b")
    assert twin.findall(resend) == twin.findall(collapse) == list(dispatches.keys)
    assert "s.property_id = %(survivor)s" in resend and "r.property_id = %(retired)s" in resend
    assert {"subscription_id", "collection_id"} <= set(dispatches.keys), (
        "the pairing is account-partitioned only through account-owned keys")


def test_the_retired_dispatches_are_locked_in_their_own_statement_before_the_resend():
    """An outbox claim's foreign key check holds FOR KEY SHARE on the dispatch; FOR UPDATE waits
    it out and holds off the next one. A separate statement, not a CTE of the resend, so the
    resend's fresh READ COMMITTED snapshot sees a send that claim committed."""
    lock = _n(carriers.Dispatches.LOCK_SQL)
    assert lock.startswith("SELECT ") and lock.endswith(" FOR UPDATE")
    assert "FROM notification_dispatches WHERE property_id = %(retired)s" in lock
    assert "FOR UPDATE" not in _n(carriers.Dispatches.RESEND_SQL)


def test_curation_dispatches_and_dismissals_give_nothing_back_on_a_detach():
    """Rule 18 best-effort: these rows stay on the property the advert left."""
    for carrier in PROPERTY_CARRIERS:
        if carrier.name == "pipeline":
            continue
        cur = _StrictCur(carrier)
        carrier.on_detach(cur, DetachStep(20, 10, (Hop(1, G, 10, 20),), "operator"))
        assert cur.ran == [], carrier.name


def test_the_pipeline_restore_drops_the_absorbed_card_only_where_today_does():
    """With one hop (or any detach whose property left is that merge's survivor) the card the
    survivor absorbed comes off it; off a later survivor on a chain, the restore runs alone."""
    pipeline = next(c for c in PROPERTY_CARRIERS if c.name == "pipeline")
    one_hop = _walk(pipeline)
    detach = one_hop.ran[4:]
    assert [p for _s, p in detach] == [{"g": G, "r": 20, "s": 10}] * 2
    chained = _StrictCur(pipeline)
    pipeline.on_detach(chained, DetachStep(20, 5, (Hop(1, G, 10, 20), Hop(2, G2, 5, 10)),
                                           "operator"))
    assert [(s.split()[0], p) for s, p in chained.ran] == [("INSERT", {"g": G, "r": 20, "s": None})]


def test_every_carrier_statement_is_in_the_prepare_corpus():
    """The PREPARE sweep (migrations.yml) reaches the f-string statements through CARRIER_SQL."""
    corpus = {_n(i.sql) for i in sql_corpus.discover(resolve_imports=True)}
    missing = [_n(s)[:100] for s in CARRIER_SQL if _n(s) not in corpus]
    assert not missing, missing
