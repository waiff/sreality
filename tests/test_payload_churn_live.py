"""The churn counters' arithmetic AND the readout's, executed against the replayed schema.

The whole deliverable of W2a is a NUMBER the operator takes a tens-of-GB storage
decision on. The offline suite (tests/test_payload_churn_write.py) proves the hook
is invisible when off and unkillable when on, and CI's PREPARE sweep proves the
statement compiles — but neither ever runs the `ON CONFLICT` arithmetic, so a
wrong-direction comparison or an off-by-one would ship green and only surface a
week later as a nonsense readout.

This module runs both halves:

  * the WRITE side — first fetch, identical refetch, changed refetch, a replayed
    batch, and a normaliser-version bump;
  * the READ side — `scripts.location_payload_churn_report._CHURN_SURFACE_SQL`
    executed over seeded rows, so each aggregate is pinned to the FIELD it lands in.
    That statement is read positionally (`surface_from_row`), so two same-typed
    expressions swapped under unchanged aliases would invert raw-vs-normalised — the
    entire signal of the instrument — and still PREPARE, still parse, still pass every
    hermetic test. Only executing it against known rows answers that.

Gated on TEST_DATABASE_URL exactly like tests/test_sql_schema_prepare.py, so a normal
local `pytest` skips it.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from scraper import db
from scripts import location_payload_churn_report as rep

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — live churn arithmetic runs only in the CI DB job",
)

_JSON = "application/json"


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_DB_URL, autocommit=True) as c:
        yield c


def _row(conn: psycopg.Connection, key: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fetches, raw_changes, norm_changes, normalizer_version, "
            "       first_seen_at, last_seen_at "
            "FROM portal_payload_churn WHERE source_id_native = %s "
            "ORDER BY normalizer_version",
            (key,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, rows
    r = rows[0]
    return {
        "fetches": r[0], "raw_changes": r[1], "norm_changes": r[2],
        "version": r[3], "first_seen_at": r[4], "last_seen_at": r[5],
    }


def _record(conn: psycopg.Connection, key: str, body: bytes, observation: str) -> None:
    db.record_payload_churn(
        conn,
        source="sreality",
        source_id_native=key,
        page_kind="detail",
        body=body,
        content_type=_JSON,
        observation=observation,
    )


def test_counters_follow_the_hashes(conn: psycopg.Connection) -> None:
    key = f"live-{uuid.uuid4().hex}"

    _record(conn, key, b'{"price": 1}', "obs-1")
    assert (_row(conn, key)["fetches"], _row(conn, key)["norm_changes"]) == (1, 0)

    # Byte-different, content-identical: JSON canonicalisation must absorb it, so
    # the raw hash moves and the normalised one does not.
    _record(conn, key, b'{"price":   1}', "obs-2")
    after = _row(conn, key)
    assert (after["fetches"], after["raw_changes"], after["norm_changes"]) == (2, 1, 0)

    # Genuinely changed body: both move.
    _record(conn, key, b'{"price": 2}', "obs-3")
    after = _row(conn, key)
    assert (after["fetches"], after["raw_changes"], after["norm_changes"]) == (3, 2, 1)

    # Identical body AND a new fetch: counted as a fetch, not as a change.
    _record(conn, key, b'{"price": 2}', "obs-4")
    after = _row(conn, key)
    assert (after["fetches"], after["raw_changes"], after["norm_changes"]) == (4, 2, 1)


def test_a_replayed_observation_bumps_nothing(conn: psycopg.Connection) -> None:
    # What _flush_drain_batch does on a transient pooler drop: re-run the whole
    # write op with the same DrainItems, i.e. the same per-fetch tokens.
    key = f"live-{uuid.uuid4().hex}"

    _record(conn, key, b'{"price": 1}', "obs-1")
    _record(conn, key, b'{"price": 2}', "obs-2")
    before = _row(conn, key)

    _record(conn, key, b'{"price": 2}', "obs-2")
    _record(conn, key, b'{"price": 2}', "obs-2")

    assert _row(conn, key) == before


def test_a_normaliser_bump_opens_a_clean_cohort(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A profile tweak mid-measurement must not relabel accumulated counters onto
    # the new version (blended cohort) nor register a phantom change on the first
    # fetch under it (the hash moved because the normaliser moved).
    from location_data import payload_norm

    # The label names the ENGINE and the contract version that supplied the profile
    # (W2a-3e), so a bump of either opens a clean cohort. This bumps the engine; the
    # contract half is pinned in tests/location_data/test_volatile_paths_contract.py.
    contract = payload_norm.CONTRACT_PROFILE_SUFFIX + str(
        payload_norm.contract_profiles().versions["sreality"])
    shipped = payload_norm.NORMALIZER_VERSION + contract
    key = f"live-{uuid.uuid4().hex}"
    _record(conn, key, b'{"price": 1}', "obs-1")
    _record(conn, key, b'{"price": 2}', "obs-2")

    monkeypatch.setattr(payload_norm, "NORMALIZER_VERSION", "payload_norm@test")
    _record(conn, key, b'{"price": 2}', "obs-3")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT normalizer_version, fetches, raw_changes, norm_changes "
            "FROM portal_payload_churn WHERE source_id_native = %s",
            (key,),
        )
        cohorts = {r[0]: tuple(r[1:]) for r in cur.fetchall()}

    assert cohorts == {shipped: (2, 1, 1), "payload_norm@test" + contract: (1, 0, 0)}


def test_the_hook_writes_through_the_flag(conn: psycopg.Connection) -> None:
    # End-to-end through the wrapper the portals actually call, including the
    # thunked body and the app_settings gate.
    key = f"live-{uuid.uuid4().hex}"
    setting = db.PAYLOAD_SHADOW_HASH_SETTING
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app_settings WHERE key = %s", (setting,))

    db.clear_app_settings_flag_cache()
    db.record_payload_churn_if_enabled(
        conn, source="sreality", source_id_native=key, page_kind="detail",
        body=lambda: b'{"price": 1}', content_type=_JSON, observation="obs-1",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM portal_payload_churn WHERE source_id_native = %s",
            (key,),
        )
        assert cur.fetchone()[0] == 0

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES (%s, 'true'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (setting,),
        )
    db.clear_app_settings_flag_cache()
    try:
        db.record_payload_churn_if_enabled(
            conn, source="sreality", source_id_native=key, page_kind="detail",
            body=lambda: b'{"price": 1}', content_type=_JSON, observation="obs-1",
        )
        assert _row(conn, key)["fetches"] == 1
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE key = %s", (setting,))
        db.clear_app_settings_flag_cache()


# ----------------------------------------------------- the readout, executed

_SEED_SQL = """
    INSERT INTO portal_payload_churn
        (source, source_id_native, page_kind, normalizer_version,
         first_seen_at, last_seen_at, fetches, raw_changes, norm_changes,
         last_byte_size, last_norm_byte_size, last_observation)
    VALUES (%(source)s, %(key)s, %(page_kind)s::location_page_kind, %(version)s,
            %(first_seen_at)s, %(last_seen_at)s, %(fetches)s, %(raw_changes)s,
            %(norm_changes)s, %(byte_size)s, %(norm_byte_size)s, %(observation)s)
"""


def _seed(conn: psycopg.Connection, source: str, **row: Any) -> None:
    params: dict[str, Any] = {
        "source": source,
        "page_kind": "detail",
        "version": "payload_norm@live",
        "observation": uuid.uuid4().hex,
        "norm_byte_size": 1_000,
        **row,
    }
    with conn.cursor() as cur:
        cur.execute(_SEED_SQL, params)


def _surface(conn: psycopg.Connection, source: str) -> rep.Surface:
    measurement = rep.measure(conn, statement_timeout_s=30, with_inventory=False)
    mine = [s for s in measurement.surfaces if s.row.source == source]
    assert len(mine) == 1, [s.row for s in mine]
    return mine[0]


def test_the_readout_lands_every_aggregate_in_its_declared_field(
    conn: psycopg.Connection,
) -> None:
    """Deliberately asymmetric numbers: no two aggregates share a value, so a swapped
    pair of same-typed expressions cannot pass by coincidence. Every mean differs from
    its own median, and raw differs from norm on both the counters and the sizes."""
    source = f"live-{uuid.uuid4().hex[:12]}"
    start = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)

    def hours(n: float) -> datetime.datetime:
        return start + datetime.timedelta(hours=n)

    # Three keys refetched, one seen once: 3 + 5 + 2 + 1 = 11 fetches, 11 - 4 = 7 repeats.
    _seed(conn, source, key="a", first_seen_at=start, last_seen_at=hours(12), fetches=3,
          raw_changes=2, norm_changes=1, byte_size=100_000, norm_byte_size=1_000)
    _seed(conn, source, key="b", first_seen_at=start, last_seen_at=hours(8), fetches=5,
          raw_changes=4, norm_changes=2, byte_size=200_000, norm_byte_size=2_000)
    _seed(conn, source, key="d", first_seen_at=start, last_seen_at=hours(1), fetches=2,
          raw_changes=1, norm_changes=1, byte_size=700_000, norm_byte_size=9_000)
    _seed(conn, source, key="c", first_seen_at=start, last_seen_at=start, fetches=1,
          raw_changes=0, norm_changes=0, byte_size=300_000, norm_byte_size=4_000)

    surface = _surface(conn, source)
    row = surface.row
    assert (row.page_kind, row.normalizer_version) == ("detail", "payload_norm@live")
    assert (row.keys, row.artefacts, row.keys_repeated) == (4, 4, 3)
    assert row.fetches == 11
    # The pair a swap would invert, and the whole signal of the instrument.
    assert (row.raw_changes, row.norm_changes) == (7, 4)
    assert surface.repeat_fetches == 7
    assert surface.raw_change_rate == pytest.approx(1.0)
    assert surface.norm_change_rate == pytest.approx(4 / 7)
    assert row.mean_raw_bytes == pytest.approx(325_000.0)
    assert row.median_raw_bytes == pytest.approx(250_000.0)
    assert row.mean_norm_bytes == pytest.approx(4_000.0)
    assert row.median_norm_bytes == pytest.approx(3_000.0)
    # Averaged PER KEY over the repeated keys only — (6 + 2 + 1) / 3 h — never
    # (12 + 8 + 1 + 0) / 4 and never the summed span over the summed fetches.
    assert row.mean_interval_s == pytest.approx(3 * 3_600.0)
    assert row.median_interval_s == pytest.approx(2 * 3_600.0)
    assert (row.window_start, row.window_end) == (start, hours(12))


def test_a_week_stamped_index_position_counts_once_however_many_weeks(
    conn: psycopg.Connection,
) -> None:
    """The index archivers key `…/{offset}/{week}` (db.index_archive_week), so rows grow
    with the measurement window while one CYCLE still touches each position once.

    The week suffixes come from `db.index_archive_week` itself, so the shape the readout
    strips can never drift from the shape the three archivers write.
    """
    source = f"live-{uuid.uuid4().hex[:12]}"
    start = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)
    weeks = [
        db.index_archive_week(start + datetime.timedelta(weeks=w)) for w in range(5)
    ]
    assert len(set(weeks)) == 5, weeks
    offsets = (0, 20, 40, 60)
    for week in weeks:
        for offset in offsets:
            _seed(conn, source, page_kind="index", key=f"byt/prodej/all/{offset}/{week}",
                  first_seen_at=start, last_seen_at=start + datetime.timedelta(hours=24),
                  fetches=25, raw_changes=24, norm_changes=12, byte_size=500_000)

    surface = _surface(conn, source)
    assert surface.row.keys == 20, "five ISO weeks x four page positions"
    assert surface.row.artefacts == 4, "one pass over the index touches four positions"
    assert surface.artefacts == 4
    assert surface.norm_change_rate == pytest.approx(0.5)
    # 4 positions x 50 % x 500 KB per pass — NOT the 5x that number `keys` would give,
    # which is the factor that would have grown with every week the instrument stayed on.
    assert surface.gb_per_cycle() == pytest.approx(4 * 0.5 * 500_000 / 1e9)
    assert surface.gb_per_cycle() != pytest.approx(20 * 0.5 * 500_000 / 1e9)
