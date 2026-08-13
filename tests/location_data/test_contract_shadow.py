"""The contract SHADOW mechanism — migration 404 + `contracts.set_shadow` (W2-4).

06 §6.4.0(2): a contract that cannot meet its frozen-sample precision floors ships in
*shadow* — claims written, excluded from resolution — until it can. Every per-portal W2
contract merges shadowed and is un-shadowed only when its labelled sample passes, so the
mechanism has to be provable BEFORE the first portal contract PR: a failing gate needs
somewhere to land that is not "revert the branch".

Four properties carry the whole design and are asserted here textually, and again against a
live schema in `test_contract_shadow_live.py`:

  * shadow is enforced in `location_claims_live`, never in resolver code (01 §A.2 check 9
    — 03 never selects from `location_claims`), so no resolver read can forget to ask;
  * un-shadowing rewrites no claim, because the claims were being written the whole time;
  * but it DOES re-queue the contract's listings, because `listing_location_current` — the
    relation every consumer, the dashboard and the scorecard read — is built by the drain
    and nothing else would ever rebuild it;
  * and a shadowed contract stays scoreable, through `location_claims_shadow`, or the
    un-shadow gate would be unsatisfiable and the flag a one-way door.
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
_QUEUE_MIGRATION = _MIGRATIONS / "384_location_w1_serving.sql"
_RESOLVER = _ROOT / "location_data" / "resolver"

# `location_claims_live` starts with `location_claims`, so the word boundary is what makes
# this a check and not a tautology: `_` is a word character, so the view name never matches.
_BASE_TABLE = re.compile(r"\blocation_claims\b")
# The scoring relation and the shared retraction relation are equally off-limits to the
# resolver: reading either would bypass the shadow predicate in exactly the way the view
# exists to prevent, and `_BASE_TABLE` cannot see them (same underscore reason).
_NON_RESOLVER_RELATIONS = re.compile(r"\blocation_claims_(shadow|unretracted)\b")


def _strip_sql_comments(text: str) -> str:
    return "\n".join(line.split("--")[0] for line in text.splitlines())


def _view_body(migration: Path, name: str = "location_claims_live") -> str:
    sql = _strip_sql_comments(migration.read_text(encoding="utf-8"))
    start = sql.lower().index(f"view {name} as")
    return " ".join(sql[start:sql.index(";", start)].split())


def _migration_sql(migration: Path) -> str:
    return " ".join(_strip_sql_comments(
        migration.read_text(encoding="utf-8")).split()).lower()


# ------------------------------------------------------------------ the migration

def test_the_header_carries_a_defaulted_shadow_flag():
    """NOT NULL DEFAULT false: every already-projected contract stays live, and a column
    that could be NULL would make "unknown" read as "not shadowed" in some queries and as
    "not not-shadowed" in others."""
    sql = _migration_sql(_SHADOW_MIGRATION)
    assert re.search(
        r"alter table portal_contracts\s+add column (if not exists )?shadow "
        r"boolean not null default false", sql), sql


def test_shadow_is_header_state_and_never_touches_the_immutable_entries():
    """The frozen sample scores a CONTRACT VERSION (06 §6.4.0(1)), so a half-shadowed
    contract would be a mixture no gate could be expressed against. Entries also stay
    immutable once loaded (02 §2.1.8) — an ALTER on them would be a second lifecycle."""
    sql = _migration_sql(_SHADOW_MIGRATION)
    assert "alter table portal_contract_entries" not in sql
    assert "portal_contract_entries" in _view_body(_SHADOW_MIGRATION)  # only via the view


def test_the_replaced_view_keeps_the_column_list_so_create_or_replace_is_legal():
    """CREATE OR REPLACE VIEW may append columns but may not rename, reorder or drop one.
    382 projects `c.*` off `location_claims`; 404 projects `u.*` off a view that is itself
    `c.*` off `location_claims`, which is the same list in the same order."""
    assert "select c.* from location_claims c" in _view_body(_CLAIMS_MIGRATION).lower()
    assert "select u.* from location_claims_unretracted u" in \
        _view_body(_SHADOW_MIGRATION).lower()
    assert "select c.* from location_claims c" in \
        _view_body(_SHADOW_MIGRATION, "location_claims_unretracted").lower()
    assert "create or replace view location_claims_live" in _migration_sql(_SHADOW_MIGRATION)


def test_the_retraction_predicate_is_stated_exactly_once_and_verbatim():
    """Two independent exclusions, composed: a retraction says claims are WRONG (permanent,
    reasoned, append-only), shadow says a contract is UNPROVEN (reversible). Re-stating the
    correlated retraction predicate per consumer is where one would silently eat the other,
    so it moves into `location_claims_unretracted` and both consumers select from that."""
    old = _view_body(_CLAIMS_MIGRATION).lower()
    retraction = old[old.index("where not exists"):]
    unretracted = _view_body(_SHADOW_MIGRATION, "location_claims_unretracted").lower()
    assert retraction in unretracted, "the 382 retraction predicate must survive verbatim"
    assert _migration_sql(_SHADOW_MIGRATION).count("location_claim_retractions") == 1


def test_live_and_shadow_partition_the_unretracted_claims():
    """Complementary predicates over the SAME relation: every unretracted claim is in
    exactly one of them, so un-shadowing can neither lose a claim nor double one."""
    live = _view_body(_SHADOW_MIGRATION).lower()
    shadow = _view_body(_SHADOW_MIGRATION, "location_claims_shadow").lower()
    for body in (live, shadow):
        assert "from location_claims_unretracted u" in body
        assert "pce.id = u.contract_entry_id and pc.shadow" in body
    assert "where not exists" in live
    assert "where exists" in shadow and "not exists" not in shadow


def test_a_contractless_claim_is_never_shadowed():
    """`contract_entry_id` is nullable — a legacy-column or operator claim has no contract
    entry at all. NOT EXISTS (rather than a join, or `pc.shadow = false`) is what keeps
    those rows live: shadow is a statement about a contract, and they have none."""
    live = _view_body(_SHADOW_MIGRATION).lower()
    tail = live[live.rindex("not exists"):]
    assert " join portal_contracts" in tail
    assert "left join" not in tail


def test_the_flip_can_find_the_contracts_claims_without_a_sequential_scan():
    """`set_shadow` enqueues by `pce.contract_id`, which reaches `location_claims` through
    `contract_entry_id` — the one claim column 382 does not index."""
    sql = _migration_sql(_SHADOW_MIGRATION)
    assert "create index if not exists location_claims_contract_entry on location_claims " \
           "(contract_entry_id)" in sql
    leading = re.findall(r"create index \w+ on location_claims (?:using \w+ )?\((\w+)",
                         _migration_sql(_CLAIMS_MIGRATION))
    assert leading and "contract_entry_id" not in leading, leading


def test_the_queue_learns_the_new_reason():
    """`dirty_locations.reason` is a CHECK, not free text, so the enqueue below is a
    constraint violation until the migration widens it — and the widening drops 384's
    generated constraint by name so a rename cannot leave two checks fighting."""
    sql = _migration_sql(_SHADOW_MIGRATION)
    assert "alter table dirty_locations drop constraint dirty_locations_reason_check" in sql
    assert "'contract_shadow'" in sql
    # every reason 384 allowed must survive the re-statement
    old = _migration_sql(_QUEUE_MIGRATION)
    reasons = re.search(r"reason text not null check \(reason in \((.*?)\)\)", old).group(1)
    for reason in re.findall(r"'([a-z_]+)'", reasons):
        assert f"'{reason}'" in sql, reason


def test_the_scoring_relation_is_revoked_from_the_browser_roles():
    """The location tables are backend-only; a new view carries Supabase's auto-GRANT."""
    sql = _migration_sql(_SHADOW_MIGRATION)
    for relation in ("location_claims_live", "location_claims_unretracted",
                     "location_claims_shadow"):
        assert f"revoke all on {relation} from anon, authenticated" in sql


# ------------------------------------------------------------------ where it is enforced

def test_the_resolver_reads_the_view_and_never_the_base_claim_table():
    """01 §A.2 check 9. Enforcing shadow in `location_claims_live` is only sound while this
    holds — the day a resolver module reads `location_claims` (or either of the two
    relations that do not carry the shadow predicate) it inherits nothing, in silence."""
    offenders = [
        p.name for p in sorted(_RESOLVER.glob("*.py"))
        if _BASE_TABLE.search(p.read_text(encoding="utf-8"))
        or _NON_RESOLVER_RELATIONS.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "resolver module(s) name a claim relation that is not location_claims_live; "
        f"every resolver claim read must go through the view: {offenders}")


def test_the_scorer_is_the_one_reader_of_the_shadow_relation():
    """MAJOR 2's fix: shadow only gates anything if the dark contract can be measured. The
    scorecard reads `location_claims_shadow`; if that reader disappears the flag becomes a
    one-way door again, so the coupling is asserted rather than assumed."""
    from toolkit import location_labels

    assert "location_claims_shadow" in location_labels._SHADOW_SCORE_SQL
    assert "listing_location_current" not in location_labels._SHADOW_SCORE_SQL
    # ...and it scores against the SAME floors and the SAME normalizer as the live one.
    assert set(location_labels.SHADOW_SCORED_FLOORS) <= set(location_labels.FLOORS)
    assert "location_value_norm(" in location_labels._SHADOW_SCORE_SQL


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


def test_the_shadow_key_is_outside_contract_sha256(tmp_path: Path):
    """Migration 404 calls clearing the flag "an operational UPDATE, not a
    contract_version bump" — which is false in git if the key is hashed with the rest of
    the file. It would then be impossible to tidy the obsolete `shadow: true` line away
    without `project()` demanding a version bump, and bumping the version would RE-SHADOW
    the contract and throw away the sample that had just passed."""
    off = _rewritten("maxima", tmp_path, shadow=None)
    on = _rewritten("maxima", tmp_path, shadow=True)
    assert off.sha256 == on.sha256
    assert _rewritten("maxima", tmp_path, shadow=False).sha256 == on.sha256


def test_a_file_with_no_shadow_key_hashes_to_its_plain_file_digest():
    """The filter is subtractive, so every already-projected contract keeps the hash it was
    projected under — otherwise the next `--load` would refuse all nine at once."""
    import hashlib

    for path in sorted(contracts.CONTRACT_DIR.glob("*.yaml")):
        body = path.read_bytes()
        assert b"\nshadow:" not in b"\n" + body, path.name
        assert contracts.contract_body_hash(body) == hashlib.sha256(body).digest(), path.name


def test_an_indented_shadow_key_is_still_hashed(tmp_path: Path):
    """The filter is anchored to column 0 because only a top-level key is the flag. A
    `shadow:` nested inside an extraction is ordinary contract content and must keep
    changing the hash."""
    path = tmp_path / "nested.yaml"
    base = "portal: maxima\ncontract_version: 1\nextractions: []\n"
    a = contracts.contract_body_hash(base.encode())
    b = contracts.contract_body_hash((base + "fetch:\n  shadow: true\n").encode())
    assert a != b


def test_a_typod_top_level_key_is_refused_rather_than_ignored(tmp_path: Path):
    """`shaddow: true` used to ship the contract LIVE — the exact state the mechanism
    exists to prevent. Unknown top-level keys now fail closed."""
    path = tmp_path / "maxima.yaml"
    source = (contracts.CONTRACT_DIR / "maxima.yaml").read_text("utf-8")
    path.write_text(source + "\nshaddow: true\n", encoding="utf-8")
    with pytest.raises(ContractError, match="unknown top-level key"):
        contracts.parse_contract(path)


def test_every_shipped_contract_parses_under_the_known_key_set():
    """The allowlist is only safe while it covers the real format; a new key must be added
    here, and this is the test that says so."""
    assert len(contracts.load_all()) == len(contracts.EXTRACTOR_PREFIXES)


# ------------------------------------------------------------------ flipping the flag

def test_unshadowing_writes_no_claim_but_requeues_the_contracts_listings():
    """MAJOR 1. Clearing the flag makes the claims visible in `location_claims_live`
    instantly — and changes NOTHING a consumer sees, because they all read
    `listing_location_current`, which only the drain rebuilds. Nothing else would ever
    re-queue these listings: the claim rows are untouched (the intake enqueues only newly
    inserted claims) and the daily backstop keys on a missing projection or a stale version
    tuple, neither of which a flag flip moves."""
    conn = _FakeConn(existing_sha="", shadow_was=True)
    flip = contracts.set_shadow(conn, source="maxima", version=1, shadow=False)
    assert flip.moved is True

    statements = [s for s, _ in conn.executed]
    assert statements[0].lower().startswith("set local statement_timeout")
    assert statements[1].lower().startswith("update portal_contracts")
    assert "insert into dirty_locations" in statements[2].lower()
    assert len(statements) == 3

    enqueue = statements[2].lower()
    assert "'contract_shadow'" in enqueue
    assert "on conflict (listing_id) do nothing" in enqueue
    # It reads the claims to find the listings and writes none of them back.
    assert "insert into location_claims" not in enqueue
    assert "delete" not in enqueue


def test_re_shadowing_a_live_contract_requeues_it_too():
    """Symmetric hole: projections BUILT from a contract's claims are just as stale once it
    goes dark, so `--shadow` has to un-build them."""
    conn = _FakeConn(existing_sha="", shadow_was=False)
    flip = contracts.set_shadow(conn, source="maxima", version=1, shadow=True)
    assert flip.moved is True
    assert any("insert into dirty_locations" in s.lower() for s, _ in conn.executed)


def test_the_enqueue_is_unconditional_like_the_operator_correction_lane():
    """`operator_corrections` enqueues off the INPUT row, not the insert CTE, so a
    restatement is never a dead button. Same reasoning here: an operator re-running
    `--unshadow` because the drain failed must not be told "already unshadowed, nothing to
    do" while the projections are still stale."""
    conn = _FakeConn(existing_sha="", shadow_was=False)
    flip = contracts.set_shadow(conn, source="maxima", version=1, shadow=False)
    assert flip.moved is False
    assert any("insert into dirty_locations" in s.lower() for s, _ in conn.executed)
    assert flip.enqueued == _FakeConn.ENQUEUED


def test_the_flip_and_the_enqueue_are_one_transaction():
    """A flag that moved without its queue rows is the stale-projection bug with extra
    steps; a queue that moved without the flag re-resolves to the same answer."""
    conn = _FakeConn(existing_sha="", shadow_was=True)
    contracts.set_shadow(conn, source="maxima", version=1, shadow=False)
    assert conn.transactions == 1


def test_flipping_an_unprojected_version_is_an_error_not_a_silent_noop():
    """A typo'd portal or version must not read as "done" — the operator would then wait
    for claims that are still dark."""
    conn = _FakeConn(existing_sha="", shadow_was=None)
    with pytest.raises(ContractError, match="not projected"):
        contracts.set_shadow(conn, source="maxima", version=9, shadow=False)
    assert not any("dirty_locations" in s.lower() for s, _ in conn.executed)


def test_the_schema_preflight_covers_the_relations_the_flip_writes():
    """`missing_relations` is the operator-legible "schema not applied" message; if it
    does not know about the enqueue's tables the flip fails halfway with a raw
    UndefinedTable instead."""
    assert {"location_claims", "dirty_locations"} <= set(contracts._RELATIONS)


# ------------------------------------------------------------------ the CLI

def test_the_cli_refuses_both_directions_at_once_before_it_opens_a_connection():
    """Both flags set is an operator typo with two opposite meanings; guessing one would
    flip a live contract. The check runs before `db.connect()`, so this needs no DB."""
    assert contracts.main(["--shadow", "maxima@1", "--unshadow", "maxima@1"]) == 2


def test_the_cli_refuses_a_retraction_mixed_with_a_shadow_flip():
    """`--retract` used to be handled first and RETURN, silently discarding a `--shadow` in
    the same invocation — while `--shadow --unshadow` was refused. Retraction is the
    irreversible verb, so a mixed invocation is the worst place to guess."""
    assert contracts.main(["--retract", "maxima@1", "--shadow", "maxima@1"]) == 2


def test_the_cli_happy_path_flips_the_named_version(monkeypatch: pytest.MonkeyPatch):
    """Nothing executed `_parse_target` → `missing_relations` → `set_shadow` end to end."""
    conn = _FakeConn(existing_sha="", shadow_was=True)
    monkeypatch.setattr(contracts.db, "connect", lambda *a, **k: _CtxConn(conn))
    monkeypatch.setattr(contracts, "missing_relations", lambda _c: [])

    assert contracts.main(["--unshadow", "maxima@3"]) == 0
    update = next(p for s, p in conn.executed if s.lower().startswith("update portal"))
    assert update == {"source": "maxima", "version": 3, "shadow": False}
    assert any("dirty_locations" in s.lower() for s, _ in conn.executed)


def test_the_cli_reports_an_unapplied_schema_instead_of_flipping(
    monkeypatch: pytest.MonkeyPatch,
):
    conn = _FakeConn(existing_sha="", shadow_was=True)
    monkeypatch.setattr(contracts.db, "connect", lambda *a, **k: _CtxConn(conn))
    monkeypatch.setattr(contracts, "missing_relations", lambda _c: ["dirty_locations"])

    assert contracts.main(["--unshadow", "maxima@3"]) == 2
    assert conn.executed == []


def test_retraction_stays_a_separate_mechanism():
    """Shadow must not become a soft retraction: a retracted claim is permanently wrong and
    says so with a reason and an author, and neither statement may be spelled as the other."""
    for sql in (contracts._RETRACT_SQL, contracts._RETIRE_SQL):
        assert "shadow" not in sql.lower()
    assert "location_claim_retractions" not in contracts._SET_SHADOW_SQL
    assert "location_claim_retractions" not in contracts._SHADOW_ENQUEUE_SQL


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
        self.rowcount = -1

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._sql = " ".join(sql.split())
        self._conn.executed.append((self._sql, params))
        self.rowcount = (
            _FakeConn.ENQUEUED if "INSERT INTO dirty_locations" in self._sql else -1
        )

    def fetchone(self) -> tuple[Any, ...] | None:
        if "RETURNING was.id, was.shadow" in self._sql:
            return None if self._conn.shadow_was is None else (7, self._conn.shadow_was)
        if "FROM portal_contracts WHERE source" in self._sql:
            return (7, self._conn.existing_sha, False) if self._conn.existing_sha else None
        return (7,)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _FakeTransaction:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_FakeTransaction":
        self._conn.transactions += 1
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    """Enough psycopg surface to assert on statement order and bound params. It cannot see
    a CHECK, a UNIQUE or the view's predicate — those are test_contract_shadow_live.py."""

    ENQUEUED = 4

    def __init__(self, existing_sha: str, shadow_was: bool | None = None) -> None:
        self.existing_sha = existing_sha
        self.shadow_was = shadow_was
        self.executed: list[tuple[str, object]] = []
        self.transactions = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)


class _CtxConn:
    """`with db.connect() as conn:` — the CLI's connection context manager."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeConn:
        return self._conn

    def __exit__(self, *exc: object) -> bool:
        return False
