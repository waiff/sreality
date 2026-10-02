"""The readings half (W3), EXECUTED against the replayed schema — CI's migrations lane
(`TEST_DATABASE_URL`); every test rolls back; locally it skips. Only a real database shows which
reading is CURRENT (the hash in SQL, migration 581's stamp, the lane's version) and what the one
write deletes, so each trace the stamp exists for is walked: a headline that reverts (A -> B -> A),
a model rolled back (M1 -> M2 -> M1), a contract bump, a delisted text never read, a wanted ad.
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
B = ("Prodej bytu 2+1, Palackého", A[1])                          # the HEADLINE alone differs
TOWN = ("Hradec Králové", "v Hradci Králové")
_MODEL = {"now": "m1"}


@pytest.fixture()
def conn(monkeypatch):
    import psycopg

    connection = psycopg.connect(_DB_URL, autocommit=True, options="-c statement_timeout=20000")
    monkeypatch.setattr(ci.text_lane, "resolve_model", lambda conn: _MODEL["now"])
    monkeypatch.setattr(ci.text_lane, "extractor_version", lambda model: f"2:x:{model}")
    _MODEL["now"] = "m1"
    with connection, connection.transaction(force_rollback=True):
        contracts.project(connection, next(
            c for c in contracts.load_all() if c.source == "bazos"), git_ref="ci")
        yield connection


def _listing(conn: Any, text: tuple[str, str], listing_id: int | None = None,
             active: bool = True) -> int:
    if listing_id is not None:
        conn.execute("UPDATE listings SET raw_json = jsonb_build_object('title', %s::text),"
                     " description = %s, is_active = %s WHERE id = %s", (*text, active, listing_id))
        return listing_id
    return int(conn.execute(
        "INSERT INTO listings (source, source_id_native, raw_json, description, category_main,"
        " category_type, price_czk, area_m2, is_active, last_seen_at) VALUES ('bazos', %s,"
        " jsonb_build_object('title', %s::text), %s, 'byt', 'prodej', 5000000, 60, true, now())"
        " RETURNING id", (f"w3-{uuid.uuid4()}", *text)).fetchone()[0])


def _reading(conn: Any, listing_id: int, model: str, **slots: Any) -> int:
    """A reading of the CURRENT text, keyed as the lane keys it; a slot: value or (value, quote)."""
    cells: dict[str, Any] = {"ad_kind": {"value": slots.pop("ad_kind", "offer")}}
    for slot, cell in slots.items():
        value, quote = cell if isinstance(cell, tuple) else (cell, cell)
        cells[slot] = {"value": value, "evidence_quote": quote}
    return int(conn.execute(
        f"INSERT INTO listing_description_enrichments (listing_id, text_hash, extractor_version,"
        f" extracted, filled, model) SELECT l.id, {ci.text_lane._HASH_EXPR}, %s,"
        f" jsonb_build_object('location', %s::jsonb), '{{}}'::jsonb, %s FROM listings l"
        f" WHERE l.id = %s RETURNING id",
        (f"2:x:{model}", Jsonb(cells), model, listing_id)).fetchone()[0])


def _mine(conn: Any) -> dict[str, Any]:
    stats: dict[str, Any] = dict.fromkeys(("readings_mined", "claims", "claims_inserted",
                                           "enqueued", "claims_superseded", "refusals"), 0)
    ci.drain_readings(conn, source="bazos", entries_by_source=ci.load_entries(conn),
                      statement_timeout=20, budget=ci._Budget(None), batch_id=1,
                      dry_run=False, stats=stats, refusals={})
    return stats


def _claims(conn: Any, listing_id: int, claim_type: str | None = None) -> set[Any]:
    rows = conn.execute("SELECT claim_type::text, value_text FROM location_claims"
                        " WHERE listing_id = %s", (listing_id,)).fetchall()
    return {v if claim_type else (t, v) for t, v in rows if claim_type in (None, t)}


def _stamps(conn: Any, listing_id: int) -> dict[int, str | None]:
    return dict(conn.execute("SELECT id, mined_contract_version FROM"
                             " listing_description_enrichments WHERE listing_id = %s",
                             (listing_id,)).fetchall())


def test_the_current_text_decides_and_supersession_spares_other_types_and_the_operator(conn):
    listing = _listing(conn, A)
    entries = {e.entry_id: e for e in ci.load_entries(conn)["bazos"]}
    row = ci.ListingRow(listing, "bazos", "n", {}, datetime.now(UTC))
    stale = ci._base(entries["bzs.txt.town"], row, value_text="Švihov")   # the post-office town
    kept = [replace(stale, value_text="Kbel", contract_entry_id=None, licence_class="operator",
                    surface="operator_input", extraction_method="operator_manual"),
            ci._base(entries["bzs.det.psc"], row, value_text="50011")]
    with conn.cursor() as cur:
        ci.write_result(cur, ci.IntakeResult(claims=[stale, *kept]))
    kept_rows = {("obec_name", "Kbel"), ("psc", "50011")}
    ra = _reading(conn, listing, "m1", town=TOWN, street="Husova")

    assert _mine(conn)["readings_mined"] == 1
    assert _claims(conn, listing) == kept_rows | {("obec_name", "Hradec Králové"),
                                                  ("street_name", "Husova")}
    assert _stamps(conn, listing) == {ra: "bazos@8"}
    assert conn.execute("SELECT 1 FROM dirty_locations WHERE listing_id = %s",
                        (listing,)).fetchone()
    assert _mine(conn)["readings_mined"] == 0                  # a stamped reading is settled

    _listing(conn, B, listing)                                 # A -> B, B not read yet:
    assert _mine(conn)["readings_mined"] == 0                  # no reading, the claims stay
    rb = _reading(conn, listing, "m1", town=TOWN, street="Palackého")
    assert _mine(conn)["claims_superseded"] == 1
    assert _claims(conn, listing, "street_name") == {"Palackého"}
    assert _stamps(conn, listing) == {ra: "bazos@8~", rb: "bazos@8"}

    _listing(conn, A, listing)                                 # B -> A: A's reading again
    assert _mine(conn)["readings_mined"] == 1
    assert _claims(conn, listing, "street_name") == {"Husova"}
    assert _stamps(conn, listing) == {ra: "bazos@8", rb: "bazos@8~"}

    _listing(conn, ("Koupím byt", "Koupím byt v Hradci Králové."), listing)
    _reading(conn, listing, "m1", ad_kind="wanted", town=TOWN)
    _mine(conn)                                                # V3: it names nothing, so
    assert _claims(conn, listing) == kept_rows                 # the town falls to the PSČ


def test_a_model_rolled_back_publishes_its_own_reading_again(conn):
    """M1 -> M2 -> M1 over ONE text; with no reading at the lane's version (m3), the newest."""
    listing = _listing(conn, A)
    r1 = _reading(conn, listing, "m1", street="Husova")
    r2 = _reading(conn, listing, "m2", street="Palackého")
    streets = []
    for model in ("m1", "m2", "m1", "m3"):
        _MODEL["now"] = model
        _mine(conn)
        streets.append(_claims(conn, listing, "street_name"))
    assert streets == [{"Husova"}, {"Palackého"}, {"Husova"}, {"Palackého"}]
    assert _stamps(conn, listing) == {r1: None, r2: "bazos@8"}


def test_a_contract_bump_re_mines_the_current_reading_and_settles_a_delisted_unread_text(conn):
    listing = _listing(conn, A)
    older = _reading(conn, listing, "m0", street="Palackého")
    current = _reading(conn, listing, "m1", street="Husova")
    _mine(conn)
    conn.execute("UPDATE portal_contracts SET version = 9 WHERE source = 'bazos' AND is_active")
    stats = _mine(conn)
    assert (stats["readings_mined"], stats["claims_superseded"]) == (1, 1)
    assert _stamps(conn, listing) == {older: None, current: "bazos@9"}
    assert _claims(conn, listing, "street_name") == {"Husova"}
    _listing(conn, B, listing, active=False)             # edited, then delisted before its read:
    conn.execute("UPDATE portal_contracts SET version = 10 WHERE source = 'bazos' AND is_active")
    assert _mine(conn)["readings_mined"] == 0            # checked once, its claims kept, and
    assert _stamps(conn, listing) == {older: "bazos@10~", current: "bazos@10~"}  # settled
    assert _claims(conn, listing, "street_name") == {"Husova"}
    _listing(conn, A, listing)                           # relisted as A: mined at @10
    assert _mine(conn)["readings_mined"] == 1
    assert _stamps(conn, listing) == {older: None, current: "bazos@10"}
