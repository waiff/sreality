"""The shadow mechanism executed against the replayed schema (W2-4).

The offline half (tests/location_data/test_contract_shadow.py) reads the migration text and
proves `contracts.py` binds the flag; CI's PREPARE sweep proves the statements compile. But
the entire deliverable of W2-4 is one predicate inside one view, and no static check can
answer the only question that matters: does a shadowed contract's claim actually disappear
from `location_claims_live`, and does clearing the flag actually bring the SAME rows back
with no backfill?

This module runs it: written-but-dark, un-shadowing that rewrites no claim, the retraction
predicate still biting, the two exclusions composing, a contract-less claim never caught by
either, the replaced view still projecting exactly the base table's columns, `live` and
`shadow` partitioning the unretracted claims, and the flip actually queueing the contract's
listings for re-resolution (the half that no view predicate can do). Gated on
TEST_DATABASE_URL exactly like tests/test_sql_schema_prepare.py, so a normal local `pytest`
skips it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from location_data import contracts

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — the live view predicate runs only in the CI DB job",
)


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection]:
    """NOT autocommit: every test rolls its fixtures back, so the claim store stays empty."""
    with psycopg.connect(_DB_URL) as c:
        yield c
        c.rollback()


def _contract(conn: psycopg.Connection, *, shadow: bool) -> tuple[str, int]:
    """A projected contract version, returning (source, entry_id)."""
    source = f"ci-shadow-{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO portal_contracts "
            "  (source, version, contract_sha256, git_ref, shadow) "
            "VALUES (%s, 1, %s, 'ci', %s) RETURNING id",
            (source, uuid.uuid4().bytes, shadow),
        )
        contract_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO portal_contract_entries "
            "  (contract_id, entry_id, surface, page_kind, locator, claim_type, "
            "   extraction_method) "
            "VALUES (%s, 'ci.det.street', 'api_json', 'detail', '{}'::jsonb, "
            "        'street_name', 'portal_structured_field') RETURNING id",
            (contract_id,),
        )
        return source, cur.fetchone()[0]


def _claim(conn: psycopg.Connection, *, listing_id: int, source: str,
           entry_id: int | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO location_claims "
            "  (listing_id, source, source_id_native, snapshot_anchor, first_observed_at, "
            "   claim_type, surface, page_kind, extraction_method, extractor_id, "
            "   extractor_version, contract_entry_id, value_text, licence_class, "
            "   claim_fingerprint) "
            "VALUES (%s, %s, 'n1', 'unanchored_latest_fetch', now(), 'street_name', "
            "        'api_json', 'detail', 'portal_structured_field', 'ci.det.street', "
            "        'contract:ci@1', %s, 'Krátká', 'portal', %s) RETURNING id",
            (listing_id, source, entry_id, uuid.uuid4().bytes),
        )
        return cur.fetchone()[0]


_STORED_SQL = "SELECT id FROM location_claims WHERE listing_id = %s"
_LIVE_SQL = "SELECT id FROM location_claims_live WHERE listing_id = %s"
_SHADOW_SQL = "SELECT id FROM location_claims_shadow WHERE listing_id = %s"
_UNRETRACTED_SQL = "SELECT id FROM location_claims_unretracted WHERE listing_id = %s"


def _ids(conn: psycopg.Connection, sql: str, listing_id: int) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(sql, (listing_id,))
        return {r[0] for r in cur.fetchall()}


def _listing_id() -> int:
    return int(uuid.uuid4().int % 1_000_000_000)


def test_a_shadowed_contract_writes_its_claims_and_hides_them(conn: psycopg.Connection) -> None:
    """06 §6.4.0(2): "claims written, excluded from resolution". Both halves matter — a
    mechanism that skipped the write would make the frozen-sample scoring impossible, which
    is the very thing the shadow exists to wait for."""
    listing_id = _listing_id()
    source, entry_id = _contract(conn, shadow=True)
    claim_id = _claim(conn, listing_id=listing_id, source=source, entry_id=entry_id)

    assert _ids(conn, _STORED_SQL, listing_id) == {claim_id}
    assert _ids(conn, _LIVE_SQL, listing_id) == set()


def test_unshadowing_rewrites_no_claim(conn: psycopg.Connection) -> None:
    """The claims are already on disk and the view joins the header, so clearing the flag
    changes no claim row: the same ids appear, with their original created_at."""
    listing_id = _listing_id()
    source, entry_id = _contract(conn, shadow=True)
    claim_id = _claim(conn, listing_id=listing_id, source=source, entry_id=entry_id)
    with conn.cursor() as cur:
        cur.execute("SELECT created_at FROM location_claims WHERE id = %s", (claim_id,))
        created_at = cur.fetchone()[0]

    assert contracts.set_shadow(conn, source=source, version=1, shadow=False).moved is True

    assert _ids(conn, _LIVE_SQL, listing_id) == {claim_id}
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), min(created_at) FROM location_claims "
                    "WHERE listing_id = %s", (listing_id,))
        assert cur.fetchone() == (1, created_at)

    # Idempotent: flipping to the value it already holds reports no movement.
    assert contracts.set_shadow(conn, source=source, version=1, shadow=False).moved is False

    # ...and it is reversible, which retraction deliberately is not.
    assert contracts.set_shadow(conn, source=source, version=1, shadow=True).moved is True
    assert _ids(conn, _LIVE_SQL, listing_id) == set()


def test_the_flip_requeues_the_contracts_listings(conn: psycopg.Connection) -> None:
    """MAJOR 1: the view flips instantly, but `listing_location_current` — what every
    consumer, the dashboard and the scorecard read — is built only by the drain, and no
    other producer would ever re-queue these listings. `'contract_shadow'` also has to be
    an accepted `dirty_locations.reason`, which is a CHECK and not free text."""
    listing_id = _listing_id()
    other = _listing_id()
    source, entry_id = _contract(conn, shadow=True)
    _claim(conn, listing_id=listing_id, source=source, entry_id=entry_id)
    _claim(conn, listing_id=other, source=source, entry_id=entry_id)

    flip = contracts.set_shadow(conn, source=source, version=1, shadow=False)
    assert flip.enqueued == 2
    with conn.cursor() as cur:
        cur.execute("SELECT listing_id, reason FROM dirty_locations "
                    "WHERE listing_id = ANY(%s)", ([listing_id, other],))
        assert sorted(cur.fetchall()) == sorted(
            [(listing_id, "contract_shadow"), (other, "contract_shadow")])


def test_a_second_flip_is_still_a_working_button(conn: psycopg.Connection) -> None:
    """Unconditional, like the operator-correction lane: an operator re-running
    `--unshadow` because the drain failed must not be told "nothing moved" while the
    projections are still stale. A listing the queue already holds is left as it is."""
    listing_id = _listing_id()
    source, entry_id = _contract(conn, shadow=True)
    _claim(conn, listing_id=listing_id, source=source, entry_id=entry_id)
    contracts.set_shadow(conn, source=source, version=1, shadow=False)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM dirty_locations WHERE listing_id = %s", (listing_id,))

    flip = contracts.set_shadow(conn, source=source, version=1, shadow=False)
    assert (flip.moved, flip.enqueued) == (False, 1)


def test_a_failed_flip_leaves_no_queue_rows(conn: psycopg.Connection) -> None:
    """The UPDATE and the enqueue are one transaction: a version that was never projected
    must not leave the queue holding work for a decision that did not happen."""
    listing_id = _listing_id()
    source, entry_id = _contract(conn, shadow=True)
    _claim(conn, listing_id=listing_id, source=source, entry_id=entry_id)
    with pytest.raises(contracts.ContractError):
        contracts.set_shadow(conn, source=source, version=9, shadow=False)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dirty_locations WHERE listing_id = %s",
                    (listing_id,))
        assert cur.fetchone() == (0,)


def test_a_retraction_still_hides_a_claim_under_a_live_contract(
    conn: psycopg.Connection,
) -> None:
    listing_id = _listing_id()
    source, entry_id = _contract(conn, shadow=False)
    claim_id = _claim(conn, listing_id=listing_id, source=source, entry_id=entry_id)
    assert _ids(conn, _LIVE_SQL, listing_id) == {claim_id}

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO location_claim_retractions (scope, claim_id, reason, retracted_by) "
            "VALUES ('claim', %s, 'extractor_bug', 'ci')",
            (claim_id,),
        )
    assert _ids(conn, _LIVE_SQL, listing_id) == set()


def test_the_two_exclusions_compose_and_neither_overrides_the_other(
    conn: psycopg.Connection,
) -> None:
    """Un-shadowing a contract must not resurrect a claim that was separately retracted: one
    says "unproven", the other says "wrong", and clearing the first answers only the first."""
    listing_id = _listing_id()
    source, entry_id = _contract(conn, shadow=True)
    kept = _claim(conn, listing_id=listing_id, source=source, entry_id=entry_id)
    retracted = _claim(conn, listing_id=listing_id, source=source, entry_id=entry_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO location_claim_retractions (scope, claim_id, reason, retracted_by) "
            "VALUES ('claim', %s, 'extractor_bug', 'ci')",
            (retracted,),
        )

    assert _ids(conn, _LIVE_SQL, listing_id) == set()
    contracts.set_shadow(conn, source=source, version=1, shadow=False)
    assert _ids(conn, _LIVE_SQL, listing_id) == {kept}


def test_a_claim_with_no_contract_entry_is_never_shadowed(conn: psycopg.Connection) -> None:
    """`contract_entry_id` is nullable — legacy-column and operator claims have no contract.
    A join-based predicate (or a `pc.shadow = false` filter) would silently delete them from
    the resolver's input; NOT EXISTS keeps them."""
    listing_id = _listing_id()
    source, _entry_id = _contract(conn, shadow=True)
    orphan = _claim(conn, listing_id=listing_id, source=source, entry_id=None)
    assert _ids(conn, _LIVE_SQL, listing_id) == {orphan}


def test_the_replaced_view_still_projects_the_base_table_columns(
    conn: psycopg.Connection,
) -> None:
    """CREATE OR REPLACE VIEW freezes the `c.*` expansion at replace time. The resolver
    unpacks rows positionally (resolve_db._CLAIMS_SELECT), so a dropped or reordered column
    here is a silent mis-mapping, not an error."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, ordinal_position, column_name FROM information_schema.columns "
            "WHERE table_name IN ('location_claims', 'location_claims_live') "
            "ORDER BY table_name, ordinal_position"
        )
        by_table: dict[str, list[str]] = {}
        for table, _pos, column in cur.fetchall():
            by_table.setdefault(table, []).append(column)
    assert by_table["location_claims_live"] == by_table["location_claims"]


def test_flipping_a_version_that_was_never_projected_raises(conn: psycopg.Connection) -> None:
    with pytest.raises(contracts.ContractError, match="not projected"):
        contracts.set_shadow(conn, source=f"ci-missing-{uuid.uuid4().hex[:8]}",
                             version=1, shadow=False)


def test_live_and_shadow_partition_the_unretracted_claims(conn: psycopg.Connection) -> None:
    """MAJOR 2's relation, checked as a partition rather than by reading its text: every
    unretracted claim is in exactly one of the two, so the scorer sees precisely what the
    resolver is being denied — no more, no less."""
    listing_id = _listing_id()
    dark_source, dark_entry = _contract(conn, shadow=True)
    live_source, live_entry = _contract(conn, shadow=False)
    dark = _claim(conn, listing_id=listing_id, source=dark_source, entry_id=dark_entry)
    lit = _claim(conn, listing_id=listing_id, source=live_source, entry_id=live_entry)
    orphan = _claim(conn, listing_id=listing_id, source=live_source, entry_id=None)
    retracted = _claim(conn, listing_id=listing_id, source=dark_source, entry_id=dark_entry)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO location_claim_retractions (scope, claim_id, reason, retracted_by) "
            "VALUES ('claim', %s, 'extractor_bug', 'ci')",
            (retracted,),
        )

    live = _ids(conn, _LIVE_SQL, listing_id)
    shadow = _ids(conn, _SHADOW_SQL, listing_id)
    unretracted = _ids(conn, _UNRETRACTED_SQL, listing_id)

    assert shadow == {dark}
    assert live == {lit, orphan}
    assert live | shadow == unretracted
    assert live & shadow == set()
    assert retracted not in unretracted


def test_the_scorer_reads_a_shadowed_contract_the_resolver_cannot(
    conn: psycopg.Connection,
) -> None:
    """The un-shadow gate is decidable only if the dark contract can be measured against
    the frozen sample. `_SHADOW_SCORE_SQL` is the surface that does it; running it here
    proves it compiles against the real schema and joins the real relations (the sample is
    empty in CI, so the numbers are zero — the shape is what is under test)."""
    from toolkit import location_labels

    with conn.cursor() as cur:
        cur.execute(location_labels._SHADOW_SCORE_SQL, {"source": "ci-none"})
        row = cur.fetchone()
    assert row[0] == 0
