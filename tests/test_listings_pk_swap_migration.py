"""Offline guard: the migrations chain must reach the Gate 1 PK swap.

R2 Gate 1 moved listings' PRIMARY KEY from sreality_id onto the surrogate id, but
it ran live out-of-band (scripts/apply_listings_pk_swap.py) and was never written
as a numbered file. migrations/ and prod diverged silently: the CI schema replay
(.github/workflows/migrations.yml) rebuilt a schema whose PK was still on
sreality_id, so the Gate 2 migration — which needs sreality_id nullable — would
have aborted in replay with "column sreality_id is in a primary key" while being
a no-op against prod.

No DB here: this is the fast offline floor that runs in the normal pytest job.
The replay itself proves the SQL applies; these tests prove it exists at all and
that it cannot touch a database that has already been swapped.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS = _ROOT / "migrations"
_CATCHUP = _MIGRATIONS / "348_listings_pk_swap_catchup.sql"

_DO_BLOCK_RE = re.compile(r"DO\s*\$\$.*?\$\$\s*;", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _squashed(path: Path) -> str:
    return _WS_RE.sub(" ", path.read_text()).lower()


def _chain() -> list[tuple[str, str]]:
    return [(p.name, _squashed(p)) for p in sorted(_MIGRATIONS.glob("*.sql"))]


def test_001_still_declares_the_legacy_primary_key():
    # The divergence's source, and the reason a catch-up file is needed at all:
    # 001 is append-only history and must NOT be rewritten to fix the replay.
    assert "sreality_id bigint primary key" in _squashed(
        _MIGRATIONS / "001_initial.sql"
    )


def test_chain_promotes_the_primary_key_onto_the_surrogate_id():
    promoters = [
        name
        for name, sql in _chain()
        if "add constraint listings_pkey primary key using index listings_id_pk_idx"
        in sql
    ]
    assert promoters, (
        "no migration promotes listings' PK onto the surrogate id — the replayed "
        "schema still has sreality_id as the PRIMARY KEY while prod has it as a "
        "nullable column. Gate 2 will fail CI with 'column sreality_id is in a "
        "primary key'."
    )


def test_chain_makes_sreality_id_nullable():
    droppers = [
        name
        for name, sql in _chain()
        if "alter table listings alter column sreality_id drop not null" in sql
    ]
    assert droppers, (
        "no migration drops NOT NULL from listings.sreality_id — dropping a "
        "PRIMARY KEY leaves attnotnull set, so the replayed column stays NOT NULL "
        "and every Gate 2 insert of a NULL sreality_id would fail."
    )


def test_catchup_touches_listings_only_from_inside_a_guarded_do_block():
    # It must be a no-op against prod: an unguarded ALTER/CREATE INDEX would take
    # ACCESS EXCLUSIVE / SHARE on a 566k-row table with an always-on writer.
    outside = _DO_BLOCK_RE.sub(" ", _CATCHUP.read_text()).lower()
    for stmt in ("alter table listings", "create unique index", "create index"):
        assert stmt not in outside, (
            f"{_CATCHUP.name} runs {stmt!r} outside the guarded DO block — it "
            "would no longer be a no-op against production."
        )


def test_catchup_guards_read_the_live_catalog():
    sql = _squashed(_CATCHUP)
    assert "contype = 'p'" in sql, "PK swap step is not guarded on the current PK"
    assert "attnotnull" in sql, "DROP NOT NULL step is not guarded on attnotnull"
