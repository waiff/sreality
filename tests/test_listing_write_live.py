"""The ONE listing write (rule 2), executed against the replayed schema: every per-source
upsert text runs, the latest-snapshot tiebreak and the statement_timestamp() stamp are
observed, and every side effect (media, failure clear, dirty marks, Gate 2) is read back
from its table. Runs in CI's migrations job (`TEST_DATABASE_URL`); every test rolls back.
"""

from __future__ import annotations

import itertools
import os
import uuid
from datetime import date, datetime, timezone
from typing import Any

import pytest

from scraper import listing_write
from scraper.portal import _DEFAULTS
from scraper.scraped_listing import ScrapedListing
from toolkit.broker_sources import BROKER_SOURCE_NAMES

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)

PORTALS = sorted(_DEFAULTS)
GATE2 = "gate2_null_sreality_id_enabled"
_SIDS = itertools.count(9_300_000_001)
_IMG = "https://cdn.example.test/{native}/{i}.jpg"
_VIDEO = "https://cdn.example.test/{native}/tour.mp4"


def add_media_arbiters(conn: Any) -> None:
    """The media upserts arbitrate on (listing_id, sequence). Production carries those two
    unique guards from scripts/apply_r2_unique_guards.py (built CONCURRENTLY, so outside
    migrations), which the replayed schema therefore lacks: add them in this transaction."""
    from scripts.apply_r2_unique_guards import UNIQUE_GUARDS

    with conn.cursor() as cur:
        for g in UNIQUE_GUARDS:
            if g["table"] in ("images", "listing_videos"):
                cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {g['name']} "
                            f"ON {g['table']} {g['cols_sql']}")


@pytest.fixture()
def conn():
    """Non-autocommit and already inside a transaction, so every write_listings call nests as
    a savepoint and the final rollback undoes all of it."""
    import psycopg

    c = psycopg.connect(
        _DB_URL,
        options="-c statement_timeout=20000 -c lock_timeout=5000"
        " -c idle_in_transaction_session_timeout=30000",
    )
    try:
        with c.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
        add_media_arbiters(c)
        yield c
    finally:
        c.rollback()
        c.close()


def _one(conn: Any, sql: str, params: Any = None) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row is not None and len(row) == 1 else row


def _all(conn: Any, sql: str, params: Any = None) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _exec(conn: Any, sql: str, params: Any = None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _property(conn: Any, *, active: bool = True) -> int:
    return int(_one(conn, "INSERT INTO properties (is_active) VALUES (%s) RETURNING id", (active,)))


def _link(conn: Any, listing_id: int, pid: int) -> None:
    _exec(conn, "UPDATE listings SET property_id = %s WHERE id = %s", (pid, listing_id))


def _clear_marks(conn: Any) -> None:
    _exec(conn, "DELETE FROM dirty_properties")
    _exec(conn, "DELETE FROM dirty_broker_listings")


def _dirty(conn: Any, pid: int) -> bool:
    return bool(_one(conn, "SELECT count(*) FROM dirty_properties WHERE property_id = %s", (pid,)))


def _broker_marked(conn: Any, listing_id: int) -> bool:
    return bool(_one(
        conn, "SELECT count(*) FROM dirty_broker_listings WHERE listing_id = %s", (listing_id,)))


def _snapshots(conn: Any, listing_id: int) -> int:
    return int(_one(conn, "SELECT count(*) FROM listing_snapshots WHERE listing_id = %s",
                    (listing_id,)))


def _snapshot(conn: Any, listing_id: int, content_hash: str, *, at: str = "now()") -> int:
    return int(_one(
        conn,
        "INSERT INTO listing_snapshots (listing_id, scraped_at, price_czk, content_hash, raw_json) "
        f"VALUES (%s, {at}, NULL, %s, '{{}}'::jsonb) RETURNING id",
        (listing_id, content_hash)))


def _seed_sreality_row(conn: Any, sid: int) -> int:
    return int(_one(
        conn,
        "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, "
        "category_main, category_type) "
        "VALUES (%s, 'sreality', %s, '{}'::jsonb, 'byt', 'prodej') RETURNING id",
        (sid, str(sid))))


def _sreality(sid: int | None = None, *, k: str = "a", images: int = 1,
              **row: Any) -> listing_write.ListingWrite:
    sid = sid if sid is not None else next(_SIDS)
    raw = {"hash_id": sid, "k": k}
    base = {"sreality_id": sid, "category_main": "byt", "category_type": "prodej",
            "price_czk": 5_000_000, "area_m2": 70.0,
            "source_url": "https://www.sreality.cz/detail/x"}
    base.update(row)
    imgs = [{"url": _IMG.format(native=sid, i=i), "sequence": i} for i in range(images)]
    return listing_write.from_sreality(raw, base, imgs)


def _scraped(source: str, native: str | None = None, *, images: int = 1, video: bool = False,
             broker: dict[str, Any] | None = None, discovered_at: datetime | None = None,
             **kw: Any) -> listing_write.ListingWrite:
    native = native or f"lw-{uuid.uuid4()}"
    urls = [_IMG.format(native=native, i=i) for i in range(images)]
    if video:
        urls.append(_VIDEO.format(native=native))
    raw: dict[str, Any] = {"image_urls": urls}
    if broker is not None:
        raw["broker"] = broker
    fields: dict[str, Any] = {"category_main": "byt", "category_type": "prodej",
                              "price_czk": 4_500_000, "area_m2": 60.0}
    fields.update(kw)
    listing = ScrapedListing(
        source=source, source_id_native=native,
        source_url=f"https://{source}.example.test/{native}", raw=raw, **fields)
    return listing_write.from_scraped(listing, discovered_at=discovered_at)


def _write(conn: Any, *writes: listing_write.ListingWrite) -> list[listing_write.WriteOutcome]:
    return listing_write.write_listings(conn, list(writes))


@pytest.mark.parametrize("source", PORTALS)
def test_a_new_row_lands_bare_with_one_snapshot_and_its_media(conn, source):
    """Executes every per-source upsert text: a new row, its natural key, its area_basis,
    one snapshot, its media on the surrogate, property_id NULL and no dirty mark (rule 19)."""
    crawler = source != "sreality"
    w = (_scraped(source, images=2, video=True, area_basis="usable") if crawler
         else _sreality(images=2, area_basis="usable"))
    dirty_before = _one(conn, "SELECT count(*) FROM dirty_properties")

    [o] = _write(conn, w)

    assert o.result == "new" and o.source_id_native == w.source_id_native
    assert _all(conn, "SELECT id, listing_id FROM listing_snapshots WHERE listing_id = %s",
                (o.listing_id,)) == [(o.snapshot_id, o.listing_id)]
    assert _one(conn, "SELECT property_id, source, source_id_native, is_active, inactive_at "
                      "FROM listings WHERE id = %s", (o.listing_id,)) == (
        None, source, w.source_id_native, True, None)
    assert _one(conn, "SELECT area_basis FROM listings WHERE id = %s", (o.listing_id,)) == "usable"
    assert _one(conn, "SELECT count(*) FROM images WHERE listing_id = %s", (o.listing_id,)) == 2
    assert o.images_inserted == 2
    if crawler:
        assert _all(conn, "SELECT source_url, sequence FROM listing_videos WHERE listing_id = %s",
                    (o.listing_id,)) == [(_VIDEO.format(native=w.source_id_native), 2)]
    assert _one(conn, "SELECT count(*) FROM dirty_properties") == dirty_before


def test_b_a_replay_is_unchanged_and_still_a_sighting(conn):
    w = _scraped("bazos", images=2)
    [first] = _write(conn, w)
    _exec(conn, "UPDATE listings SET last_seen_at = now() - interval '1 day' WHERE id = %s",
          (first.listing_id,))

    [again] = _write(conn, w)

    assert again.result == "unchanged"
    assert (again.listing_id, again.snapshot_id) == (first.listing_id, first.snapshot_id)
    assert again.images_inserted == 0
    assert _snapshots(conn, first.listing_id) == 1
    assert _one(conn, "SELECT last_seen_at = now() FROM listings WHERE id = %s",
                (first.listing_id,)) is True


@pytest.mark.parametrize("source", ["sreality", "bazos"])
def test_c_a_content_change_appends_and_dirty_marks(conn, source):
    sid = next(_SIDS)
    native = f"lw-{uuid.uuid4()}"
    make = ((lambda price: _sreality(sid, k=str(price), price_czk=price)) if source == "sreality"
            else (lambda price: _scraped(source, native, price_czk=price)))
    [o] = _write(conn, make(5_000_000))
    pid = _property(conn)
    _link(conn, o.listing_id, pid)
    _clear_marks(conn)

    [changed] = _write(conn, make(4_800_000))

    assert changed.result == "updated" and changed.snapshot_id != o.snapshot_id
    assert _snapshots(conn, o.listing_id) == 2
    assert _dirty(conn, pid)
    assert _broker_marked(conn, o.listing_id) == (source in BROKER_SOURCE_NAMES)


def test_d_a_revival_with_unchanged_content_dirty_marks(conn):
    w = _sreality()
    [o] = _write(conn, w)
    pid = _property(conn)
    _link(conn, o.listing_id, pid)
    _exec(conn, "UPDATE listings SET is_active = false, inactive_at = now() WHERE id = %s",
          (o.listing_id,))
    _clear_marks(conn)

    [again] = _write(conn, w)

    assert again.result == "unchanged"
    assert _one(conn, "SELECT is_active, inactive_at FROM listings WHERE id = %s",
                (o.listing_id,)) == (True, None)
    assert _dirty(conn, pid)


def test_e_an_unchanged_write_under_an_inactive_property_dirty_marks(conn):
    w = _scraped("idnes")
    [o] = _write(conn, w)
    pid = _property(conn, active=False)
    _link(conn, o.listing_id, pid)
    _clear_marks(conn)

    [again] = _write(conn, w)

    assert again.result == "unchanged"
    assert _dirty(conn, pid)


def test_f_a_fingerprinted_broker_change_marks_the_broker_queue_only(conn):
    native = f"lw-{uuid.uuid4()}"
    [o] = _write(conn, _scraped("idnes", native, broker={"account_oid": "a", "name": "A"}))
    _clear_marks(conn)

    [again] = _write(conn, _scraped("idnes", native, broker={"account_oid": "b", "name": "B"}))

    assert again.result == "unchanged"
    assert _snapshots(conn, o.listing_id) == 1
    assert _broker_marked(conn, o.listing_id)


def test_g_an_unfingerprinted_source_never_reads_or_marks_the_broker(conn):
    assert "NULL::jsonb AS stored_broker" in listing_write._upsert_sql("bezrealitky")
    native = f"lw-{uuid.uuid4()}"
    [o] = _write(conn, _scraped("bezrealitky", native, broker={"broker_id": "a"}))
    _clear_marks(conn)

    [again] = _write(conn, _scraped("bezrealitky", native, broker={"broker_id": "b"}))

    assert again.result == "unchanged"
    assert not _broker_marked(conn, o.listing_id)


def test_h_a_mixed_batch_reports_per_native(conn):
    a, b = f"lw-{uuid.uuid4()}", f"lw-{uuid.uuid4()}"
    _write(conn, _scraped("remax", a), _scraped("remax", b))
    c = f"lw-{uuid.uuid4()}"

    outcomes = _write(conn, _scraped("remax", a), _scraped("remax", b, price_czk=3_900_000),
                      _scraped("remax", c, images=3))

    assert [(o.source_id_native, o.result) for o in outcomes] == [
        (a, "unchanged"), (b, "updated"), (c, "new")]
    assert listing_write.tally(outcomes) == {
        "new": 1, "updated": 1, "unchanged": 1, "images_discovered": 3}


def test_i_an_exact_scraped_at_tie_resolves_on_id(conn):
    """Two snapshots stamped in one transaction share scraped_at; the higher id is latest."""
    sid = next(_SIDS)
    lid = _seed_sreality_row(conn, sid)
    w_a, w_b = _sreality(sid, k="a"), _sreality(sid, k="b")
    assert w_a.content_hash != w_b.content_hash
    snap_a = _snapshot(conn, lid, w_a.content_hash)
    snap_b = _snapshot(conn, lid, w_b.content_hash)
    assert snap_a < snap_b

    [same] = _write(conn, w_b)
    assert (same.result, same.snapshot_id) == ("unchanged", snap_b)

    [moved] = _write(conn, w_a)
    assert moved.result == "updated" and moved.snapshot_id > snap_b


def test_j_the_snapshot_is_stamped_at_statement_time(conn):
    """A snapshot committed later in wall-clock time than this transaction began must not
    outrank the one this write appends: the append is stamped statement_timestamp()."""
    sid = next(_SIDS)
    lid = _seed_sreality_row(conn, sid)
    _snapshot(conn, lid, "seeded-later-than-now", at="clock_timestamp()")

    [o] = _write(conn, _sreality(sid))

    assert o.result == "updated"
    latest = listing_write.latest_snapshot(conn, "sreality", str(sid))
    assert latest is not None and latest.id == o.snapshot_id
    assert latest.content_hash == o.content_hash


def _seq_step(conn: Any) -> int:
    return int(_one(conn, "SELECT increment_by FROM pg_sequences "
                          "WHERE sequencename = 'synthetic_listing_id_seq'"))


def test_k_gate2_off_mints_one_synthetic_id_per_first_sight_only(conn):
    _exec(conn, "DELETE FROM app_settings WHERE key = %s", (GATE2,))
    step = _seq_step(conn)
    v0 = int(_one(conn, "SELECT nextval('synthetic_listing_id_seq')"))
    a, b = _scraped("ceskereality"), _scraped("ceskereality")

    first = _write(conn, a, b)
    v1 = int(_one(conn, "SELECT nextval('synthetic_listing_id_seq')"))
    assert v1 - v0 == 3 * step
    minted = {o.listing_id: _one(conn, "SELECT sreality_id FROM listings WHERE id = %s",
                                 (o.listing_id,)) for o in first}
    assert set(minted.values()) == {v0 + step, v0 + 2 * step}

    _write(conn, a, b)
    v2 = int(_one(conn, "SELECT nextval('synthetic_listing_id_seq')"))
    assert v2 - v1 == step, "a refetch must draw no synthetic id"


def test_l_gate2_on_lands_a_null_sreality_id(conn):
    _exec(conn, "INSERT INTO app_settings (key, value) VALUES (%s, 'true'::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (GATE2,))
    w = _scraped("maxima")

    [o] = _write(conn, w)

    assert _one(conn, "SELECT id, sreality_id FROM listings WHERE source = 'maxima' "
                      "AND source_id_native = %s", (w.source_id_native,)) == (o.listing_id, None)
    assert _all(conn, "SELECT sreality_id FROM listing_snapshots WHERE id = %s",
                (o.snapshot_id,)) == [(None,)]


def test_m_a_successful_write_clears_its_own_failure_row_only(conn):
    sid, other = next(_SIDS), next(_SIDS)
    _exec(conn, "INSERT INTO listing_fetch_failures (sreality_id, last_error) "
                "VALUES (%s, 'x'), (%s, 'y')", (sid, other))

    _write(conn, _sreality(sid))
    _write(conn, _scraped("realitymix"))

    rows = _all(conn, "SELECT sreality_id FROM listing_fetch_failures "
                      "WHERE sreality_id = ANY(%s)", ([sid, other],))
    assert rows == [(other,)]


def test_n_preserve_if_null_follows_the_contract(conn):
    native = f"lw-{uuid.uuid4()}"
    [b] = _write(conn, _scraped("bazos", native, condition="dobrý"))
    _write(conn, _scraped("bazos", native, condition=None))
    assert _one(conn, "SELECT condition FROM listings WHERE id = %s", (b.listing_id,)) == "dobrý"

    sid = next(_SIDS)
    [s] = _write(conn, _sreality(sid, condition="dobrý"))
    _write(conn, _sreality(sid, condition=None))
    assert _one(conn, "SELECT condition FROM listings WHERE id = %s", (s.listing_id,)) is None


def test_o_discovered_at_is_set_once_and_discovery_seq_is_never_written(conn):
    """MS19 (W5): nothing writes discovery_seq any more; the column stays until W6."""
    native = f"lw-{uuid.uuid4()}"
    t5 = datetime(2026, 9, 1, 5, tzinfo=timezone.utc)
    t9 = datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
    [o] = _write(conn, _scraped("idnes", native, discovered_at=t5))
    _write(conn, _scraped("idnes", native, discovered_at=t9))

    assert _one(conn, "SELECT discovery_seq, discovered_at FROM listings WHERE id = %s",
                (o.listing_id,)) == (None, t5)


def test_p_float_numerics_are_coerced_not_rejected(conn):
    [o] = _write(conn, _scraped("mmreality", price_czk=4_500_000.0, area_m2=float("nan")))

    assert _one(conn, "SELECT price_czk, area_m2 FROM listings WHERE id = %s",
                (o.listing_id,)) == (4_500_000, None)
    assert _one(conn, "SELECT price_czk FROM listing_snapshots WHERE id = %s",
                (o.snapshot_id,)) == 4_500_000


def test_q_a_date_lands_as_utc_midnight(conn):
    [o] = _write(conn, _scraped("bazos", published_at=date(2026, 7, 2)))

    assert _one(conn, "SELECT published_at = '2026-07-02 00:00:00+00'::timestamptz "
                      "FROM listings WHERE id = %s", (o.listing_id,)) is True


def test_r_a_duplicate_native_keeps_the_last_payload(conn):
    native = f"lw-{uuid.uuid4()}"

    outcomes = _write(conn, _scraped("remax", native, price_czk=1_000_000),
                      _scraped("remax", native, price_czk=2_000_000))

    assert len(outcomes) == 1
    assert _one(conn, "SELECT price_czk FROM listings WHERE id = %s",
                (outcomes[0].listing_id,)) == 2_000_000


def test_s_mixed_sources_write_nothing(conn):
    a, b = _scraped("bazos"), _scraped("idnes")

    with pytest.raises(ValueError):
        _write(conn, a, b)

    assert _one(conn, "SELECT count(*) FROM listings WHERE source_id_native = ANY(%s)",
                ([a.source_id_native, b.source_id_native],)) == 0


def _skew_property_ids_past(conn: Any, listing_id: int) -> None:
    """listings.id and properties.id are independent sequences; make them disjoint so a
    stamp assertion can tell a listing id from a property id (setval, never a nextval walk)."""
    seq = _one(conn, "SELECT pg_get_serial_sequence('properties', 'id')")
    _one(conn, "SELECT setval(%s::regclass, greatest(nextval(%s::regclass), %s), true)",
         (seq, seq, listing_id + 1))


def test_t_the_straggler_attach_births_recomputes_and_browse_syncs(conn, monkeypatch):
    import scripts.recompute_property_stats as rps

    synced: list[int] = []
    original = rps.sync_browse_list

    def _spy(c: Any, ids: Any) -> None:
        ids = list(ids)
        synced.extend(ids)
        original(c, ids)

    monkeypatch.setattr(rps, "sync_browse_list", _spy)
    outcomes = _write(conn, _scraped("bazos"), _scraped("bazos"))
    lids = [o.listing_id for o in outcomes]
    _skew_property_ids_past(conn, max(lids))

    rps._attach_stragglers(conn)

    pids = [_one(conn, "SELECT property_id FROM listings WHERE id = %s", (lid,)) for lid in lids]
    assert None not in pids and len(set(pids)) == 2
    for lid, pid in zip(lids, pids):
        assert _one(conn, "SELECT repr_listing_ref_id FROM properties WHERE id = %s", (pid,)) == lid
    assert set(pids) <= set(synced)
