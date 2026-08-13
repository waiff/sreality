"""The contract SHADOW mechanism — migration 404 + `contracts.set_shadow` (W2-4).

06 §6.4.0(2): a contract that cannot meet its frozen-sample precision floors ships in
*shadow* — claims written, excluded from resolution — until it can. Every per-portal W2
contract merges shadowed and is un-shadowed only when its labelled sample passes, so the
mechanism has to be provable BEFORE the first portal contract PR: a failing gate needs
somewhere to land that is not "revert the branch".

Two properties carry the whole design and are asserted here textually, and again against a
live schema in `test_contract_shadow_live.py`:

  * shadow is enforced in `location_claims_live`, never in resolver code (01 §A.2 check 9
    — 03 never selects from `location_claims`), so no resolver read can forget to ask;
  * un-shadowing is one UPDATE and needs no backfill, because the claims were being
    written the whole time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from location_data import contracts
from location_data.contracts import ContractError

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = _ROOT / "migrations"
_SHADOW_MIGRATION = _MIGRATIONS / "404_location_w2_contract_shadow.sql"
_CLAIMS_MIGRATION = _MIGRATIONS / "382_location_w1_claims.sql"
_RESOLVER = _ROOT / "location_data" / "resolver"

# `location_claims_live` starts with `location_claims`, so the word boundary is what makes
# this a check and not a tautology: `_` is a word character, so the view name never matches.
_BASE_TABLE = re.compile(r"\blocation_claims\b")


def _strip_sql_comments(text: str) -> str:
    return "\n".join(line.split("--")[0] for line in text.splitlines())


def _view_body(migration: Path) -> str:
    sql = _strip_sql_comments(migration.read_text(encoding="utf-8"))
    start = sql.lower().index("view location_claims_live as")
    return " ".join(sql[start:sql.index(";", start)].split())


# ------------------------------------------------------------------ the migration

def test_the_header_carries_a_defaulted_shadow_flag():
    """NOT NULL DEFAULT false: every already-projected contract stays live, and a column
    that could be NULL would make "unknown" read as "not shadowed" in some queries and as
    "not not-shadowed" in others."""
    sql = " ".join(_strip_sql_comments(
        _SHADOW_MIGRATION.read_text(encoding="utf-8")).split()).lower()
    assert re.search(
        r"alter table portal_contracts\s+add column (if not exists )?shadow "
        r"boolean not null default false", sql), sql


def test_shadow_is_header_state_and_never_touches_the_immutable_entries():
    """The frozen sample scores a CONTRACT VERSION (06 §6.4.0(1)), so a half-shadowed
    contract would be a mixture no gate could be expressed against. Entries also stay
    immutable once loaded (02 §2.1.8) — an ALTER on them would be a second lifecycle."""
    sql = _strip_sql_comments(_SHADOW_MIGRATION.read_text(encoding="utf-8")).lower()
    assert "alter table portal_contract_entries" not in sql
    assert "portal_contract_entries" in _view_body(_SHADOW_MIGRATION)  # only via the view


def test_the_replaced_view_keeps_the_column_list_so_create_or_replace_is_legal():
    """CREATE OR REPLACE VIEW may append columns but may not rename, reorder or drop one.
    Both revisions project `c.*` off `location_claims`, which is the same list."""
    for migration in (_CLAIMS_MIGRATION, _SHADOW_MIGRATION):
        body = _view_body(migration)
        assert "select c.* from location_claims c" in body.lower(), migration.name
    assert "create or replace view location_claims_live" in \
        _strip_sql_comments(_SHADOW_MIGRATION.read_text(encoding="utf-8")).lower()


def test_shadow_composes_with_the_retraction_predicate_rather_than_replacing_it():
    """Two independent exclusions, ANDed: a retraction says claims are WRONG (permanent,
    reasoned, append-only), shadow says a contract is UNPROVEN (reversible). Re-stating the
    view is exactly where one could silently eat the other."""
    old = _view_body(_CLAIMS_MIGRATION).lower()
    new = _view_body(_SHADOW_MIGRATION).lower()
    retraction = old[old.index("where not exists"):]
    assert retraction in new, "the 382 retraction predicate must survive verbatim"
    shadow = new[new.index(retraction) + len(retraction):].strip()
    assert shadow.startswith("and not exists"), shadow
    assert "pc.shadow" in shadow and "pce.id = c.contract_entry_id" in shadow


def test_a_contractless_claim_is_never_shadowed():
    """`contract_entry_id` is nullable — a legacy-column or operator claim has no contract
    entry at all. NOT EXISTS (rather than a join, or `pc.shadow = false`) is what keeps
    those rows live: shadow is a statement about a contract, and they have none."""
    shadow = _view_body(_SHADOW_MIGRATION).lower()
    tail = shadow[shadow.rindex("and not exists"):]
    assert "not exists" in tail
    assert " join portal_contracts" in tail
    assert "left join" not in tail


# ------------------------------------------------------------------ where it is enforced

def test_the_resolver_reads_the_view_and_never_the_base_claim_table():
    """01 §A.2 check 9. Enforcing shadow in `location_claims_live` is only sound while this
    holds — the day a resolver module reads `location_claims` directly it inherits neither
    the retraction predicate nor the shadow one, in silence."""
    offenders = [
        p.name for p in sorted(_RESOLVER.glob("*.py"))
        if _BASE_TABLE.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "resolver module(s) name location_claims directly; every claim read must go "
        f"through location_claims_live: {offenders}")


# ------------------------------------------------------------------ the projection

def test_every_shipped_contract_is_unshadowed():
    """W2-4 ships the mechanism and zero policy: no contract on disk is shadowed yet, so
    this PR cannot change what any live extractor produces."""
    assert {c.source: c.shadow for c in contracts.load_all()} == {
        s: False for s in contracts.EXTRACTOR_PREFIXES}


def test_a_yaml_without_the_key_projects_as_live(tmp_path: Path):
    contract = _rewritten("maxima", tmp_path, shadow=None)
    assert contract.shadow is False
    assert _header_params(contract)["shadow"] is False


def test_a_yaml_shadow_key_reaches_the_header_insert(tmp_path: Path):
    """The one thing a per-portal contract PR has to do to ship dark: add `shadow: true`."""
    contract = _rewritten("maxima", tmp_path, shadow=True)
    assert contract.shadow is True
    assert _header_params(contract)["shadow"] is True


def test_reprojecting_an_unchanged_contract_never_re_shadows_it(tmp_path: Path):
    """The un-shadow decision is the OPERATOR's and outlives the deploy that follows it.
    `shadow` is written on the header INSERT only, so re-projecting the same bytes — which
    every deploy does — cannot quietly put a passed contract back in the dark."""
    contract = _rewritten("maxima", tmp_path, shadow=True)
    conn = _FakeConn(existing_sha=contract.sha256.hex())
    contracts.project(conn, contract, git_ref="deadbeef")
    assert not [s for s, _ in conn.executed if "shadow" in s.lower()]


# ------------------------------------------------------------------ flipping the flag

def test_unshadowing_is_one_update_and_writes_no_claim():
    """"Needs no backfill" is the whole point: the claims are already on disk and the view
    joins the header, so clearing the flag is the entire operation."""
    conn = _FakeConn(existing_sha="", shadow_was=True)
    assert contracts.set_shadow(conn, source="maxima", version=1, shadow=False) is True
    statements = [s for s, _ in conn.executed]
    assert len(statements) == 1
    assert statements[0].lower().startswith("update portal_contracts")
    assert not any(w in s.lower() for s in statements
                   for w in ("insert", "delete", "location_claims"))


def test_setting_the_flag_to_what_it_already_is_reports_no_movement():
    conn = _FakeConn(existing_sha="", shadow_was=True)
    assert contracts.set_shadow(conn, source="maxima", version=1, shadow=True) is False


def test_flipping_an_unprojected_version_is_an_error_not_a_silent_noop():
    """A typo'd portal or version must not read as "done" — the operator would then wait
    for claims that are still dark."""
    conn = _FakeConn(existing_sha="", shadow_was=None)
    with pytest.raises(ContractError, match="not projected"):
        contracts.set_shadow(conn, source="maxima", version=9, shadow=False)


def test_the_cli_refuses_both_directions_at_once_before_it_opens_a_connection():
    """Both flags set is an operator typo with two opposite meanings; guessing one would
    flip a live contract. The check runs before `db.connect()`, so this needs no DB."""
    assert contracts.main(["--shadow", "maxima@1", "--unshadow", "maxima@1"]) == 2


def test_retraction_stays_a_separate_mechanism():
    """Shadow must not become a soft retraction: a retracted claim is permanently wrong and
    says so with a reason and an author, and neither statement may be spelled as the other."""
    for sql in (contracts._RETRACT_SQL, contracts._RETIRE_SQL):
        assert "shadow" not in sql.lower()
    assert "location_claim_retractions" not in contracts._SET_SHADOW_SQL


# ------------------------------------------------------------------ helpers

def _rewritten(portal: str, tmp_path: Path, *, shadow: bool | None) -> contracts.PortalContract:
    """A real contract, re-emitted with (or without) a top-level `shadow` key.

    Round-tripping a shipped file rather than hand-rolling a minimal one keeps this test
    honest about the actual format — and proves the key is additive to it.
    """
    import yaml

    doc = yaml.safe_load((contracts.CONTRACT_DIR / f"{portal}.yaml").read_text("utf-8"))
    doc.pop("shadow", None)
    if shadow is not None:
        doc["shadow"] = shadow
    path = tmp_path / f"{portal}.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return contracts.parse_contract(path)


def _header_params(contract: contracts.PortalContract) -> dict[str, Any]:
    conn = _FakeConn(existing_sha="")
    contracts.project(conn, contract, git_ref="deadbeef")
    return next(params for sql, params in conn.executed
                if "INSERT INTO portal_contracts" in sql)


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._sql = ""

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))
        self._sql = " ".join(sql.split())

    def fetchone(self) -> tuple[Any, ...] | None:
        if "RETURNING was.shadow" in self._sql:
            return None if self._conn.shadow_was is None else (self._conn.shadow_was,)
        if "FROM portal_contracts WHERE source" in self._sql:
            return (7, self._conn.existing_sha, False) if self._conn.existing_sha else None
        return (7,)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _FakeConn:
    """Enough psycopg surface to assert on statement order and bound params. It cannot see
    a CHECK, a UNIQUE or the view's predicate — those are test_contract_shadow_live.py."""

    def __init__(self, existing_sha: str, shadow_was: bool | None = None) -> None:
        self.existing_sha = existing_sha
        self.shadow_was = shadow_was
        self.executed: list[tuple[str, object]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def transaction(self) -> _FakeCursor:
        return _FakeCursor(self)
