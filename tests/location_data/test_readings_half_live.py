"""The readings half (location reader W3), EXECUTED against the replayed schema.

Runs in CI's migrations lane (`TEST_DATABASE_URL`, `DB_RAILS_REQUIRED=1`); every test rolls
back; locally it skips. Only a real database can show the current-reading selector (the hash
in SQL, the stamp of migration 578, the preference for the lane's version) and the one write
(insert, supersession, stamp, enqueue), so each trace the stamp exists for is walked here: a
text that changes and reverts (A -> B -> A), a model rolled back (M1 -> M2 -> M1), a contract
bump, a reading that names nothing — and what supersession must never touch.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from location_data import claims_intake as ci
from location_data import contracts

_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DB_URL and os.environ.get("DB_RAILS_REQUIRED") != "1",
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job")

A = ("Prodej bytu 2+1, Husova", "Byt v ulici Husova v Hradci Králové, kousek od Palackého.")
B = ("Prodej bytu 2+1, Palackého", "Byt v ulici Palackého v Hradci Králové.")


@pytest.fixture()
def conn(monkeypatch):
    import psycopg

    connection = psycopg.connect(_DB_URL, autocommit=True,
                                 options="-c statement_timeout=20000 -c lock_timeout=5000")
    monkeypatch.setattr(ci.text_lane, "resolve_model", lambda conn: _MODEL["now"])
    monkeypatch.setattr(ci.text_lane, "extractor_version", lambda model: f"2:x:{model}")
    try:
        with connection.transaction(force_rollback=True):
            bazos = next(c for c in contracts.load_all() if c.source == "bazos")
            contracts.project(connection, bazos, git_ref="ci")
            yield connection
    finally:
        connection.close()


_MODEL = {"now": "m1"}


def _listing(conn: Any, text: tuple[str, str]) -> int:
    row = conn.execute(
        "INSERT INTO listings (source, source_id_native, raw_json, description, category_main,"
        " category_type, price_czk, area_m2, is_active, last_seen_at) VALUES ('bazos', %s,"
        " jsonb_build_object('title', %s::text), %s, 'byt', 'prodej', 5000000, 60, true, now())"
        " RETURNING id", (f"w3-{uuid.uuid4()}", *text)).fetchone()
    return int(row[0])


def _retext(conn: Any, listing_id: int, text: tuple[str, str]) -> None:
    conn.execute("UPDATE listings SET raw_json = jsonb_build_object('title', %s::text),"
                 " description = %s WHERE id = %s", (*text, listing_id))


def _reading(conn: Any, listing_id: int, model: str, **slots: Any) -> int:
    """A successful reading of the listing's CURRENT text, keyed as the text lane keys it.
    A slot is `value` (its own quote) or `(value, quote)`."""
    cells: dict[str, Any] = {"ad_kind": {"value": slots.pop("ad_kind", "offer")}}
    for slot, cell in slots.items():
        value, quote = cell if isinstance(cell, tuple) else (cell, cell)
        cells[slot] = {"value": value, "evidence_quote": quote}
    row = conn.execute(
        f"INSERT INTO listing_description_enrichments"
        f" (listing_id, text_hash, extractor_version, extracted, filled, model)"
        f" SELECT l.id, {ci.text_lane._HASH_EXPR}, %s, jsonb_build_object('location', %s::jsonb),"
        f" '{{}}'::jsonb, %s FROM listings l WHERE l.id = %s RETURNING id",
        (f"2:x:{model}", Jsonb(cells), model, listing_id)).fetchone()
    return int(row[0])


def _mine(conn: Any) -> dict[str, Any]:
    stats: dict[str, Any] = {k: 0 for k in (
        "readings_mined", "claims", "claims_reading", "claims_inserted", "enqueued",
        "claims_superseded", "lock_retries")}
    ci.drain_readings(conn, source="bazos", entries_by_source=ci.load_entries(conn),
                      statement_timeout=20, budget=ci._Budget(None), batch_id=1,
                      dry_run=False, stats=stats, refusals={})
    return stats


def _claims(conn: Any, listing_id: int) -> set[tuple[str, str]]:
    rows = conn.execute("SELECT claim_type::text, value_text FROM location_claims"
                        " WHERE listing_id = %s", (listing_id,)).fetchall()
    return {(t, v) for t, v in rows}


def _stamps(conn: Any, listing_id: int) -> dict[int, str | None]:
    rows = conn.execute("SELECT id, mined_contract_version FROM listing_description_enrichments"
                        " WHERE listing_id = %s", (listing_id,)).fetchall()
    return {int(i): s for i, s in rows}


def _seed(conn: Any, listing_id: int) -> None:
    """What mining must supersede (the post-office town the href used to claim) and what it
    must never touch: another type (the page's PSČ) and an operator correction."""
    entries = {e.entry_id: e for e in ci.load_entries(conn)["bazos"]}
    row = ci.ListingRow(listing_id, "bazos", "n", {}, datetime.now(UTC))
    town = ci._base(entries["bzs.txt.town"], row, value_text="Švihov")
    operator = replace(town, value_text="Kbel", contract_entry_id=None, licence_class="operator",
                       surface="operator_input", extraction_method="operator_manual")
    psc = ci._base(entries["bzs.det.psc"], row, value_text="50011")
    with conn.cursor() as cur:
        ci.write_result(cur, ci.IntakeResult(claims=[town, operator, psc]))


def test_the_current_text_decides_and_supersession_spares_other_types_and_the_operator(conn):
    listing = _listing(conn, A)
    _seed(conn, listing)
    kept = {("psc", "50011"), ("obec_name", "Kbel")}
    town = ("Hradec Králové", "v Hradci Králové")
    ra = _reading(conn, listing, "m1", town=town, street="Husova")

    assert _mine(conn)["readings_mined"] == 1
    assert _claims(conn, listing) == kept | {("obec_name", "Hradec Králové"),
                                             ("street_name", "Husova")}
    assert _stamps(conn, listing) == {ra: "bazos@8"}
    assert conn.execute("SELECT 1 FROM dirty_locations WHERE listing_id = %s",
                        (listing,)).fetchone()
    assert _mine(conn)["readings_mined"] == 0                  # a stamped reading is settled

    _retext(conn, listing, B)                                  # A -> B, B not read yet
    assert _mine(conn)["readings_mined"] == 0                  # no reading: claims stay
    rb = _reading(conn, listing, "m1", town=town, street="Palackého")
    assert _mine(conn)["claims_superseded"] == 1
    assert ("street_name", "Palackého") in _claims(conn, listing)
    assert ("street_name", "Husova") not in _claims(conn, listing)
    assert _stamps(conn, listing) == {ra: None, rb: "bazos@8"}

    _retext(conn, listing, A)                                  # B -> A: A's reading again
    assert _mine(conn)["readings_mined"] == 1
    assert ("street_name", "Husova") in _claims(conn, listing)
    assert _stamps(conn, listing) == {ra: "bazos@8", rb: None}

    _retext(conn, listing, ("Koupím byt", "Koupím byt v Hradci Králové."))
    _reading(conn, listing, "m1", ad_kind="wanted", town=town)
    _mine(conn)                                                # V3: names nothing
    assert _claims(conn, listing) == kept                      # the town falls to the PSČ


def test_a_model_rolled_back_publishes_its_own_reading_again(conn):
    """M1 -> M2 -> M1 over ONE text: the lane's current version picks the reading; with no
    reading at that version the newest one of the text stands."""
    listing = _listing(conn, A)
    r1 = _reading(conn, listing, "m1", street="Husova")
    r2 = _reading(conn, listing, "m2", street="Palackého")
    streets = []
    for model in ("m1", "m2", "m1", "m3"):
        _MODEL["now"] = model
        _mine(conn)
        streets.append({v for t, v in _claims(conn, listing) if t == "street_name"})
    _MODEL["now"] = "m1"
    assert streets == [{"Husova"}, {"Palackého"}, {"Husova"}, {"Palackého"}]
    assert _stamps(conn, listing) == {r1: None, r2: "bazos@8"}


def test_a_contract_bump_re_mines_the_current_reading_and_retires_the_old_versions(conn):
    listing = _listing(conn, A)
    older = _reading(conn, listing, "m0", street="Palackého")
    current = _reading(conn, listing, "m1", street="Husova")
    _mine(conn)
    before = conn.execute("SELECT count(*) FROM location_claims WHERE listing_id = %s",
                          (listing,)).fetchone()[0]
    conn.execute("UPDATE portal_contracts SET version = 9 WHERE source = 'bazos' AND is_active")
    stats = _mine(conn)
    assert stats["readings_mined"] == 1 and stats["claims_superseded"] == before
    assert _stamps(conn, listing) == {older: None, current: "bazos@9"}
    assert {v for t, v in _claims(conn, listing) if t == "street_name"} == {"Husova"}
