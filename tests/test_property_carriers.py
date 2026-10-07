"""The carrier list, `toolkit.property_carriers` (rules 15, 18, 22), offline: the FK census over
`migrations/*.sql` (every column that references `properties` is carried or named in
`NOT_CARRIED`), the order the list must keep, the protocol every carrier meets, and a strict
cursor that runs each REAL carrier, fails on any statement it did not declare or any param it
did not supply, and answers canned RETURNING rows, so what each carrier hands the carry record is
pinned here. What each carrier does to rows is executed in tests/test_property_carriers_live.py
(with the live column census and the count invariant); the merge's use of the seam is
tests/test_property_merge_set.py."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tests import sql_corpus
from tests._property_ledger import ledger_carriers  # noqa: F401 — the fixture
from toolkit import pipeline_identity
from toolkit import property_carriers as carriers
from toolkit.property_carriers import (
    CARRIER_SQL,
    NOT_CARRIED,
    PROPERTY_CARRIERS,
    Carried,
    Carrier,
    MergeStep,
    carried_columns,
)

_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
G = "11111111-1111-1111-1111-111111111111"
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
    """The sanctioned deletes are a SET table's collision collapse and the losing pipeline card,
    each folded into the carry record with its snapshot; never a ledger or log row."""
    history = ("property_dismissals", "property_pipeline_events", "property_merge_carries",
               "property_merge_events", "property_status_events", "properties")
    for statement in CARRIER_SQL:
        deleted = re.findall(r"\bDELETE\s+FROM\s+(\w+)", statement, re.I)
        assert not set(deleted) & set(history), f"a carrier deletes history: {_n(statement)[:120]}"


# --- every real carrier, through a cursor that refuses what it did not declare -------------

_NAMED = re.compile(r"%\((\w+)\)s")


class _StrictCur:
    def __init__(self, carrier: Carrier, canned: dict[str, list[tuple]] | None = None) -> None:
        self.carrier = carrier
        self.declared = {_n(s) for s in carrier.sql}
        self.canned = {_n(sql): rows for sql, rows in (canned or {}).items()}
        self.ran: list[tuple[str, Any]] = []
        self.rows: list[tuple] = []
        self.carried: list[Carried] = []

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
        self.rows = list(self.canned.get(s, []))

    def fetchall(self) -> list[tuple]:
        return self.rows


def _walk(carrier: Carrier, canned: dict[str, list[tuple]] | None = None) -> _StrictCur:
    """One merge step, S=10 survives R=20, each statement answering its canned rows."""
    cur = _StrictCur(carrier, canned)
    cur.carried = carrier.on_merge(cur, MergeStep(10, 20, G, "operator"))
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


# --- what each carrier hands the carry record (migration 589) ----------------------------

A, B, T1, T2, T3, T4 = "acc-a", "acc-b", "t1", "t2", "t3", "t4"


def _carrier(name: str) -> Any:
    return next(c for c in PROPERTY_CARRIERS if c.name == name)


@pytest.mark.parametrize("name", ["collection_properties", "property_tags", "property_notes"])
def test_a_curation_table_folds_what_it_collapses_and_moves_the_rest(name):
    """Each from the retired property, keyed on `keys[0]` (else `id`) and dated by `at`; a row
    with no account moves as it is."""
    table = _carrier(name)
    *collapse, move = table.sql
    key = (table.keys or ("id",))[0]
    assert _n(move).endswith(f"RETURNING {key}, {table.at}, account_id")
    snap = {key: 5}
    cur = _walk(table, {**{s: [(5, T1, A, snap)] for s in collapse}, move: [(6, T2, None)]})
    assert cur.carried == [Carried(name, 5, T1, A, 20, "folded", snap)] * len(collapse) + [
        Carried(name, 6, T2, None, 20, "moved", None)]


def test_alert_events_get_no_carry_rows_and_five_tables_get_them():
    """MS14's default: the dispatch carrier runs its four statements and hands back nothing. The
    carry record's five tables (no CHECK pins them): the three above, the card, the dismissal."""
    cur = _walk(_carrier("notification_dispatches"))
    assert len(cur.ran) == 4 and cur.carried == []
    assert {c.name for c in PROPERTY_CARRIERS if getattr(c, "at", None)} == {
        "collection_properties", "property_tags", "property_notes"}


def test_the_pipeline_folds_each_losing_card_from_where_it_came_and_moves_the_winner():
    """S's losing card is folded from the property its standing carry row names (7, an earlier
    step's), else from S; R's losing card from R; the rest move."""
    fold_s, fold_r, move = pipeline_identity.STATEMENTS
    snap = {"stage_id": 1}
    cur = _walk(_carrier("pipeline"), {fold_s: [(A, T1, snap, None), (B, T2, snap, 7)],
                                       fold_r: [(A, T3, snap)], move: [(B, T4)]})
    card = "property_pipeline"
    assert cur.carried == [Carried(card, None, T1, A, 10, "folded", snap),
                           Carried(card, None, T2, B, 7, "folded", snap),
                           Carried(card, None, T3, A, 20, "folded", snap),
                           Carried(card, None, T4, B, 20, "moved", None)]


def test_one_dismissal_one_carry_row_its_last_kind_wins():
    """1 is lifted 'merge' on R, then moved: folded. 2 moves, then a card lifts it: folded from R.
    S's own 3 came from 7 by an earlier merge, S's 4 never moved: folded from 7 and from S."""
    lift_twin, repoint, lift_deal = carriers.Dismissals.sql
    s1, s2, s3, s4 = ({"id": i} for i in range(1, 5))
    cur = _walk(_carrier("dismissals"), {
        lift_twin: [(1, T1, A, s1)], repoint: [(1, T1, A), (2, T2, B)],
        lift_deal: [(2, T2, B, s2, None), (3, T3, A, s3, 7), (4, T4, A, s4, None)]})
    row = "property_dismissals"
    assert cur.carried == [Carried(row, 1, T1, A, 20, "folded", s1),
                           Carried(row, 2, T2, B, 20, "folded", s2),
                           Carried(row, 3, T3, A, 7, "folded", s3),
                           Carried(row, 4, T4, A, 10, "folded", s4)]


def test_every_carrier_statement_is_in_the_prepare_corpus():
    """The PREPARE sweep (migrations.yml) reaches the f-string statements through CARRIER_SQL."""
    corpus = {_n(i.sql) for i in sql_corpus.discover(resolve_imports=True)}
    missing = [_n(s)[:100] for s in CARRIER_SQL if _n(s) not in corpus]
    assert not missing, missing


# --- routing (MS17, MS18): the plan over canned rows, the writes through a strict cursor -----

L, R1, R2 = 10, 20, 30
ME, THEM = "acc-me", "acc-them"


def _row(kind: str, table: str, key: Any, acc: Any, item: str, *, ad: int | None = None,
         origin: int | None = None, ids: tuple[int, ...] = (), twin: bool | None = None,
         to: int | None = None, live: bool | None = None, stage: int | None = None) -> tuple:
    """One `_ROUTES_SQL` row: kind, table, key, account, item, label, stage, live, the note's ad
    on `left`, the fold's to-property, the came-from property, the carry ids, the fold's twin."""
    return (kind, table, key, acc, item, item, stage, live, ad, to, origin, list(ids), twin)


class _RouteCur:
    """Answers the routing read with canned rows; refuses any statement outside `ROUTE_SQL` and
    the dismissal lift, and any named param not supplied. A write changes one row, but a write
    whose (table, key, account) `held` names, which changes none."""

    def __init__(self, rows: list[tuple] = (), held: set[tuple] = frozenset()) -> None:
        self.rows, self.ran, self.held, self.rowcount = list(rows), [], held, 0
        self.known = {_n(s) for s in (*carriers.ROUTE_SQL,
                                      carriers._DISMISSAL_LIFT_LIVE_DEAL_SQL)}

    def execute(self, sql: str, params: Any = None) -> None:
        s = _n(sql)
        assert s in self.known, f"routing ran SQL it does not declare: {s[:120]}"
        assert set(_NAMED.findall(s)) <= set(params or {}), s[:120]
        self.ran.append((s, params))
        table = s.split()[1 if s.startswith("UPDATE") else 2]
        key = (table, (params or {}).get("key"), (params or {}).get("account"))
        self.rowcount = 0 if key in self.held else 1

    def fetchall(self) -> list[tuple]:
        return self.rows


def _plan(rows: list[tuple], movers: dict[int, int | None], **kw: Any) -> list[carriers.Route]:
    return carriers.curation_plan(_RouteCur(rows), left=L, movers=movers, **kw)


def _where(routes: list[carriers.Route]) -> dict[str, tuple]:
    return {r.item: (r.action, r.anchor, r.carry_ids, r.skipped) for r in routes}


def test_the_preselection_routes_notes_with_their_ad_and_the_rest_along_the_carry_record():
    """2 goes home to 20 (its anchor: the lowest ad going there), 3 is born new, 1 stays. A note
    follows its ad on `left` (consuming its carry rows only when it leaves); an item goes back
    where its oldest standing carry row says when that property gets ads back, else stays."""
    rows = [_row("move", "property_notes", 1, ME, "note:1", ad=2, ids=(7,)),
            _row("move", "property_notes", 2, ME, "note:2", ad=1, ids=(8,)),
            _row("move", "property_notes", 3, ME, "note:3", origin=R1, ids=(9,)),
            _row("move", "property_pipeline", None, ME, "pipeline", origin=R1, ids=(5, 6),
                 live=True, stage=4),
            _row("move", "collection_properties", 50, ME, "collection:50", origin=R2, ids=(4,)),
            _row("move", "property_tags", 60, THEM, "tag:60")]
    assert _where(_plan(rows, {2: R1, 4: R1, 3: None})) == {
        "note:1": ("move", 2, (7,), None), "note:2": ("move", 1, (), None),
        "note:3": ("move", 2, (9,), None), "pipeline": ("move", 2, (5, 6), None),
        "collection:50": ("move", None, (), None), "tag:60": ("move", None, (), None)}


def test_folds_are_recreated_where_they_came_from_only_while_their_twin_is_on_left():
    """A fold from 20 (which gets ads back) is re-created there while its twin stands on `left`;
    with the twin gone it is not ('gone'), but its carry row is spent. A fold of `left`'s own item
    is re-created on `left` once the item it folded into leaves ('held' while it stays); a fold
    from a property that gets nothing back is not routed."""
    rows = [_row("move", "property_pipeline", None, ME, "pipeline", origin=R1, ids=(5,),
                 live=True, stage=4),
            _row("fold", "collection_properties", 50, ME, "fold:11", origin=R1, ids=(11,),
                 twin=True, to=L),
            _row("fold", "property_tags", 60, ME, "fold:12", origin=R1, ids=(12,), twin=False,
                 to=L),
            _row("fold", "property_pipeline", None, ME, "fold:13", origin=L, ids=(13,),
                 twin=True, to=L, live=True, stage=3),
            _row("fold", "property_tags", 61, ME, "fold:14", origin=R2, ids=(14,), twin=True,
                 to=L)]
    assert _where(_plan(rows, {2: R1})) == {
        "pipeline": ("move", 2, (5,), None), "fold:11": ("recreate", 2, (11,), None),
        "fold:12": ("recreate", 2, (12,), "gone"), "fold:13": ("recreate", None, (13,), None)}
    # the card it folded into stays: the account still holds a card on `left`, nothing re-made
    stays = [rows[0][:10] + (None, [], None), rows[3]]
    assert _where(_plan(stays, {2: R1}))["fold:13"] == ("recreate", None, (), "held")


def test_choices_override_only_the_acting_accounts_items_and_add_copies():
    rows = [_row("move", "property_notes", 1, ME, "note:1", ad=2),
            _row("move", "property_pipeline", None, ME, "pipeline", origin=R1, ids=(5,),
                 live=True, stage=4),
            _row("move", "property_pipeline", None, THEM, "pipeline", origin=R1, ids=(6,),
                 live=False, stage=9),
            _row("move", "collection_properties", 50, ME, "collection:50", ids=(4,))]
    plan = _plan(rows, {2: R1, 3: None}, account=ME,
                 choices={"note:1": (None, (3,)), "pipeline": (3, ()),
                          "collection:50": (None, ())})
    assert [(r.item, r.account_id, r.action, r.anchor, r.carry_ids) for r in plan] == [
        ("note:1", ME, "move", None, ()), ("pipeline", ME, "move", 3, (5,)),
        ("pipeline", THEM, "move", 2, (6,)), ("collection:50", ME, "move", None, ()),
        ("note:1", ME, "copy", 3, ())]
    # overriding the preselection spends the carry rows even when the item stays
    plan = _plan(rows, {2: R1, 3: None}, account=ME, choices={"pipeline": (None, ())})
    assert _where([r for r in plan if r.account_id == ME])["pipeline"] == (
        "move", None, (5,), None)
    assert _plan(rows, {2: R1}, account=None, choices={"pipeline": (None, ())})[1].anchor == 2


def test_the_conflict_pass_never_lands_a_copy_or_a_fold_on_a_twin_and_no_dismissal_on_a_deal():
    rows = [_row("move", "property_pipeline", None, ME, "pipeline", origin=R1, ids=(5,),
                 live=True, stage=4),
            _row("move", "collection_properties", 50, ME, "collection:50", origin=R1, ids=(4,)),
            _row("move", "property_dismissals", 70, THEM, "dismissal"),
            _row("fold", "property_pipeline", None, ME, "fold:13", origin=R1, ids=(13,),
                 twin=True, to=L, live=False, stage=3),
            _row("fold", "property_dismissals", 71, ME, "fold:14", origin=R1, ids=(14,),
                 twin=True, to=L)]
    plan = _plan(rows, {2: R1, 3: None}, account=ME,
                 choices={"collection:50": (2, (None, 3))})
    assert _where(plan) == {
        "pipeline": ("move", 2, (5,), None),
        "collection:50": ("copy", 3, (), None),
        "dismissal": ("move", None, (), None),
        "fold:13": ("recreate", 2, (13,), "held"),   # 20 gets my card back: no second one
        "fold:14": ("recreate", 2, (14,), "card")}   # nor a dismissal where my live deal lands
    copies = [(r.anchor, r.skipped) for r in plan if r.action == "copy"]
    assert copies == [(None, None), (3, None)], "a copy to the property left, once its source went"


def test_the_writes_run_in_order_and_spend_only_what_moved():
    """Moves, copies, re-creations, the carry stamp, then the lift on `left` and every landing;
    a route whose anchor did not move routes and spends nothing; counts as the dry run's. A move
    that changes no row (its account holds the item where it would land: an origin active again)
    is neither counted nor spends its carry rows."""
    Route = carriers.Route
    routes = [Route("property_notes", 1, ME, "move", 2, None, None, (7,), "note:1", None),
              Route("property_pipeline", None, ME, "move", 2, R1, 4, (5,), "pipeline", None),
              Route("property_tags", 60, ME, "move", 4, R2, None, (8,), "tag:60", None),
              Route("property_notes", 1, ME, "copy", None, None, None, (), "note:1", None),
              Route("collection_properties", 50, ME, "recreate", 2, R1, None, (11,), "fold:11",
                    None),
              Route("property_tags", 61, ME, "recreate", 2, R1, None, (12,), "fold:12", None,
                    skipped="gone"),
              Route("property_tags", 63, ME, "recreate", 4, R2, None, (13,), "fold:13", None)]
    cur = _RouteCur()
    counts = carriers.route_curation(cur, routes, left=L, landed={2: R1})
    ran = [(f"{s.split()[0]} {s.split()[1 if s.startswith('UPDATE') else 2]}", p.get("to"))
           for s, p in cur.ran]
    assert ran == [("UPDATE property_notes", R1), ("UPDATE property_pipeline", R1),
                   ("INSERT property_notes", L), ("INSERT collection_properties", R1),
                   ("UPDATE property_merge_carries", None), ("UPDATE property_dismissals", None),
                   ("UPDATE property_dismissals", None)]
    assert cur.ran[4][1] == {"ids": [5, 7, 11, 12]}, "what rides an ad that did not move: nothing"
    assert [p for _s, p in cur.ran[5:]] == [{"s": L}, {"s": R1}]
    assert counts == {"note_moves": 1, "carry_rows": 2}
    held = _RouteCur(held={("property_pipeline", None, ME)})
    assert carriers.route_curation(held, routes, left=L, landed={2: R1}) == {
        "note_moves": 1, "carry_rows": 1}
    assert held.ran[4][1] == {"ids": [7, 11, 12]}, "the card that did not move keeps its row"


@pytest.mark.usefixtures("ledger_carriers")
def test_the_dry_run_counts_the_live_plan_per_property_left(monkeypatch):
    """`curation_preview` plans exactly what the undo would: the group-scoped movers on the
    property they sit on, the routing read along that merge's carry rows only."""
    import toolkit.property_identity as pi
    from tests._property_ledger import _Ledger

    db = _Ledger({1: 10, 2: 20, 3: 20})
    group = pi.merge_property_set(db, [10, 20], source="autodedup", reason="r")["data"][
        "merge_group_id"]
    seen: list[dict[str, Any]] = []
    Route = carriers.Route
    canned = [Route("property_notes", 1, ME, "move", 2, None, None, (), "note:1", None),
              Route("property_pipeline", None, ME, "move", 3, R1, 4, (5,), "pipeline", None),
              Route("property_tags", 61, ME, "recreate", 2, R1, None, (12,), "fold:12", None),
              Route("property_tags", 62, ME, "move", None, None, None, (), "tag:62", None)]
    monkeypatch.setattr(carriers, "curation_plan",
                        lambda cur, **kw: seen.append(kw) or canned)
    assert carriers.curation_preview(db, group, [3, 2, 1]) == {"carry_rows": 2, "note_moves": 1}
    assert seen == [{"left": 10, "movers": {2: 20, 3: 20}, "group": group}]


def test_every_routing_statement_is_in_the_prepare_corpus_and_none_deletes():
    corpus = {_n(i.sql) for i in sql_corpus.discover(resolve_imports=True)}
    assert not [_n(s)[:100] for s in carriers.ROUTE_SQL if _n(s) not in corpus]
    assert not [s for s in carriers.ROUTE_SQL if re.search(r"\bDELETE\b", s, re.I)]
    assert set(carriers._MOVE_SQL) == set(carriers._INSERT_SQL) == {
        "property_notes", "property_pipeline", "collection_properties", "property_tags",
        "property_dismissals"}
