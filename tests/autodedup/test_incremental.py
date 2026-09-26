"""The real-time lane's mechanisms, offline (PROGRAM.md E70/E71/E72, E64).

Five things a replay over one cohort cannot prove on its own, and each is a rail rather than a
restatement: retrieval matches `BlockIndex.candidates` EXACTLY rather than approximately; an
unchanged listing costs nothing (idempotence); a re-probed neighbourhood makes the fan-out cap
order-insensitive; the E64 rail is owed only where a census limb is on; and the dark switch is
off unless the worker's interval says otherwise.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

import pytest

from autodedup.blocking import BlockIndex, generate_pairs
from autodedup.dataset import Dataset, Image, Listing, Location, Meta
from autodedup.fingerprint import build_all
from autodedup.incremental import (
    Calibration,
    Keyer,
    Limits,
    guard_row,
    key_token,
    retrieve,
    run_pass,
)
from autodedup.incremental_store import MemoryStore
from autodedup.model import hand_initialised
from autodedup.replay import DatasetFacts, ScheduleWork, arrival_order, batch_state
from autodedup.settings import Settings


def _listing(listing_id: int, **kw) -> Listing:
    base = dict(
        block="town:1", source="sreality", source_id_native=f"n{listing_id}",
        category_main="byt", category_type="prodej", disposition="2+kk",
        area_m2=60.0, floor=3, price=5_000_000.0,
        description="Prodej bytu 2+kk o vymere 60 m2 v cihlovem dome po rekonstrukci. "
                    * 6,
        first_seen_at=f"2026-01-{(listing_id % 27) + 1:02d}T08:00:00+00:00",
        last_seen_at="2026-03-01T08:00:00+00:00", is_active=True,
        broker_key=f"b{listing_id % 3}",
        location=Location(obec_kod=554782, cast_obce_kod=490245, street_key="hlavni",
                          house_number_cp="12", lat=50.1, lon=14.4, ruian_adm_kod=111),
    )
    base.update(kw)
    return Listing(id=listing_id, **base)


def _dataset(n: int = 24) -> Dataset:
    listings = {}
    images = {}
    for index in range(n):
        listing_id = 1000 + index * 7
        listings[listing_id] = _listing(listing_id, area_m2=55.0 + (index % 5))
        images[listing_id] = [
            Image(listing_id=listing_id, image_id=listing_id * 10 + seq, seq=seq,
                  phash=(index % 4) * 977 + seq, pop=1)
            for seq in range(3)
        ]
    return Dataset(meta=Meta(), listings=listings, images_by_listing=images)


def _settings() -> Settings:
    return Settings()


def _calibration(ds: Dataset, settings: Settings) -> Calibration:
    return Calibration.build(build_all(ds, settings), ds.listings, settings)


def _drain(ds: Dataset, settings: Settings, calibration: Calibration,
           order: Iterable[int], batch: int = 5) -> tuple[MemoryStore, list]:
    store = MemoryStore()
    facts = DatasetFacts(ds)
    work = ScheduleWork(list(order))
    model = hand_initialised()
    passes = []
    limits = Limits(max_listings=batch, max_pairs=10 ** 9, max_component=400)
    while not work.exhausted():
        passes.append(run_pass(store, facts, work, settings, model, calibration,
                               limits=limits))
    return store, passes


# --------------------------------------------------------------------------- E71 retrieval


def test_retrieval_matches_the_cohort_pass_exactly() -> None:
    """The one rail that stops the SQL restatement of `BlockIndex.candidates` from drifting."""
    ds = _dataset()
    settings = _settings()
    fps = build_all(ds, settings)
    calibration = _calibration(ds, settings)
    store, _passes = _drain(ds, settings, calibration, arrival_order(ds))

    index = BlockIndex(settings)
    for listing_id in sorted(fps):
        index.add(fps[listing_id])
    index.finalize()

    keyer = Keyer(settings, calibration)
    guards = {i: guard_row(fp) for i, fp in fps.items()}
    for listing_id in sorted(fps):
        expected = index.candidates(fps[listing_id])
        got = retrieve(fps[listing_id], keyer, store, guards, settings, {})
        assert got == expected, listing_id


def test_index_keys_come_from_the_block_index_not_a_restatement() -> None:
    ds = _dataset(6)
    settings = _settings()
    fps = build_all(ds, settings)
    calibration = _calibration(ds, settings)
    keyer = Keyer(settings, calibration)
    index = BlockIndex(settings)
    for listing_id in sorted(fps):
        index.add(fps[listing_id])
    index.finalize()
    for fp in fps.values():
        assert keyer.index_keys(fp) == [
            (probe, key_token(key)) for probe, key in index.index_keys(fp)
        ]


# ------------------------------------------------------------------------- the watermark


def test_an_unchanged_listing_costs_nothing() -> None:
    """Idempotence: a second drain over the same corpus scores and deletes nothing."""
    ds = _dataset(12)
    settings = _settings()
    calibration = _calibration(ds, settings)
    store, first = _drain(ds, settings, calibration, arrival_order(ds))
    before = {key: (row.zone, row.score) for key, row in store.pairs.items()}

    facts = DatasetFacts(ds)
    work = ScheduleWork(arrival_order(ds))
    model = hand_initialised()
    again = []
    while not work.exhausted():
        again.append(run_pass(store, facts, work, settings, model, calibration,
                              limits=Limits(max_listings=5, max_pairs=10 ** 9)))
    assert sum(p.pairs_scored for p in again) == 0
    assert sum(p.pairs_deleted for p in again) == 0
    assert {key: (row.zone, row.score) for key, row in store.pairs.items()} == before


def test_the_watermark_only_advances_over_what_it_claimed() -> None:
    work = ScheduleWork([1, 2, 3, 4, 5])
    first = work.claim(2)
    assert [item.listing_id for item in first] == [1, 2]
    assert work.commit(first)["arrivals_done"] == 2
    second = work.claim(2)
    assert [item.listing_id for item in second] == [3, 4]
    # A CLAIM moves nothing: only the items a pass actually decided advance the cursor.
    assert work.cursor == 2
    assert work.commit(second)["arrivals_done"] == 4
    assert not work.exhausted()
    third = work.claim(2)
    assert [item.listing_id for item in third] == [5]
    work.commit(third)
    assert work.exhausted()


def test_a_refused_claim_leaves_the_schedule_where_it_was() -> None:
    """E75: an aborted pass commits nothing, so the same arrivals come back next time."""
    work = ScheduleWork([1, 2, 3])
    claimed = work.claim(3)
    work.commit([])
    assert work.cursor == 0
    assert [item.listing_id for item in work.claim(3)] == [item.listing_id
                                                           for item in claimed]


# --------------------------------------------------------------------- E72 and the replay


def test_the_incremental_final_state_equals_the_cohort_pass() -> None:
    ds = _dataset(24)
    settings = _settings()
    calibration = _calibration(ds, settings)
    reference, batch_clusters, _timings = batch_state(ds, settings, hand_initialised())
    store, _passes = _drain(ds, settings, calibration, arrival_order(ds))
    assert set(store.pairs) == set(reference)
    for key, row in store.pairs.items():
        assert (row.zone, row.reason) == (reference[key]["zone"], reference[key]["reason"]), key
    assert {k: sorted(v) for k, v in store.clusters.items()} == {
        k: sorted(v) for k, v in batch_clusters.items()
    }


def test_arrival_order_does_not_change_the_final_state() -> None:
    """E71/E72 in one assertion: two orders, one state."""
    ds = _dataset(24)
    settings = _settings()
    calibration = _calibration(ds, settings)
    forward, _a = _drain(ds, settings, calibration, arrival_order(ds))
    backward, _b = _drain(ds, settings, calibration, list(reversed(arrival_order(ds))), batch=3)
    assert {k: (r.zone, round(r.score, 9)) for k, r in forward.pairs.items()} == {
        k: (r.zone, round(r.score, 9)) for k, r in backward.pairs.items()
    }
    assert {k: sorted(v) for k, v in forward.clusters.items()} == {
        k: sorted(v) for k, v in backward.clusters.items()
    }


# ------------------------------------------------------------------------------ E64 rail


def test_the_rail_is_owed_only_where_a_census_limb_is_on() -> None:
    ds = _dataset(12)
    calibration = _calibration(ds, _settings())
    off = replace(_settings(), context_rule_block_min=None,
                  context_rule_image_population_min=None)
    _store, passes = _drain(ds, off, calibration, arrival_order(ds))
    assert all(p.rail.get("owed", 0) == 0 for p in passes)
    assert all(p.rail.get("reopened", 0) == 0 for p in passes)


def test_a_certificate_is_never_re_opened_by_the_rail() -> None:
    """E64's exemption, read off the plan rather than off a comment: a stamped promotion that
    carries a certificate is passed to `rail_plan` in `exempt` and so can never be an action."""
    from autodedup.hazard_context import ContextStamp, rail_plan, ContextIndex

    ds = _dataset(6)
    stamp = ContextStamp(1, 1)
    index = ContextIndex(cells={}, listing_phashes={}, phash_population={},
                         from_price=frozenset())
    ids = sorted(ds.listings)
    merges = [(ids[0], ids[1], stamp, "blk")]
    actions, _counters = rail_plan(merges, index, ds.listings, block_min=1,
                                   image_population_min=None, max_per_block=8,
                                   exempt=frozenset({(ids[0], ids[1])}))
    assert actions == []


# --------------------------------------------------------------------------- one switch


def test_the_worker_interval_is_the_only_switch() -> None:
    """E914: the pass is not a dispatch mode and takes no switch argument — the worker's
    `realtime_autodedup_interval_seconds` (0 = stop) is the one control."""
    import inspect

    from autodedup import incremental_lane, lane

    assert "incremental" not in lane.MODES and "rt_parity" not in lane.MODES
    params = inspect.signature(incremental_lane.run_incremental).parameters
    assert set(params) == {"conn_factory", "deadline_s"}
    for gone in ("ENV_FLAG", "DB_FLAG", "env_enabled", "db_enabled", "parity_gate"):
        assert not hasattr(incremental_lane, gone), gone


def test_the_pass_spends_nothing(tmp_path) -> None:
    """D19: no LLM judge has merge authority on the band, so this lane calls no provider."""
    ds = _dataset(12)
    settings = _settings()
    calibration = _calibration(ds, settings)
    _store, passes = _drain(ds, settings, calibration, arrival_order(ds))
    assert all(p.spent_usd == 0.0 for p in passes)
    assert all(p.to_json()["spent_usd"] == 0.0 for p in passes)


# ----------------------------------------------------------------- the schema boundary


def test_no_statement_of_this_lane_writes_outside_schema_autodedup() -> None:
    """D4, asserted over the file rather than trusted to a reading.

    Every write verb in `incremental_sql` must name a table in `autodedup`; `public` appears
    there only in SELECTs. A new statement that forgets this fails here, which is the point."""
    import re

    from autodedup import incremental_sql

    # `(?<!do )` so an upsert's own `on conflict ... do update set` is not read as a write.
    writes = re.compile(r"(?<!do )\b(insert\s+into|update|delete\s+from)\s+([a-z_.]+)", re.I)
    seen = 0
    for name in dir(incremental_sql):
        if not name.endswith("_SQL"):
            continue
        # Comments are stripped first: prose about an update is not an update.
        statement = getattr(incremental_sql, name)
        sql = "\n".join(line.split("--")[0] for line in statement.splitlines())
        for verb, table in writes.findall(sql):
            seen += 1
            assert table.startswith("autodedup."), f"{name} writes {verb} {table}"
    assert seen >= 10, "the audit found no write statements — did the constants move?"


def test_every_feed_is_bounded_and_settle_lagged() -> None:
    """E73: a feed without a LIMIT is an unbounded pass; one without a lag skips late commits."""
    from autodedup import incremental_sql as sql_module

    for name in ("RT_NEW_LISTINGS_SQL", "RT_CHANGED_LISTINGS_SQL", "RT_FLIPPED_LISTINGS_SQL",
                 "RT_NEW_STRAGGLERS_SQL", "RT_REVIVED_SQL"):
        assert "limit" in getattr(sql_module, name).lower(), name
    for name in ("RT_NEW_LISTINGS_SQL", "RT_CHANGED_LISTINGS_SQL", "RT_FLIPPED_LISTINGS_SQL"):
        assert "make_interval(secs => %(lag)s)" in getattr(sql_module, name), name
    # The delisting feed pages on the FULL key, never on the timestamp alone.
    assert "(l.inactive_at, l.id) >" in sql_module.RT_FLIPPED_LISTINGS_SQL


def test_the_lane_never_prepares_a_statement_with_a_bare_percent() -> None:
    """The repo's PREPARE gate: a `%` that is not a named parameter breaks psycopg's Parse."""
    import re

    from autodedup import incremental_sql

    named = re.compile(r"%\([a-z_0-9]+\)s")
    for name in dir(incremental_sql):
        if not name.endswith("_SQL"):
            continue
        stripped = named.sub("", getattr(incremental_sql, name))
        assert "%" not in stripped, name
