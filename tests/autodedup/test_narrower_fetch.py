"""E932: the probe-key neighbourhood is read without CLIP vectors; the claim and the scored
endpoints carry them.

The world is `lane_world`'s nine listings with a CLIP vector on every photograph, plus two
listings that share every probe key but sit seven and eight floors up: the rule floor vetoes
every pair they could form with the nine, so once both are stored they are in every later
pass's neighbourhood and the sides of one wanted pair — their own, whose decision stands. That
is the read E932 removes, and the parity below is that removing it moves no decision.
"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace
from typing import Any

import pytest

from autodedup import harness, incremental, incremental_lane
from autodedup.dataset import Dataset, Image, Listing, Meta
from autodedup.export import build_image_record, build_listing_record, encode_clip, tag_pairs
from autodedup.export_sql import COHORT_CLIP_SQL, COHORT_IMAGES_SQL
from autodedup.fingerprint import build_fingerprint
from autodedup.incremental import (
    EvidenceHold,
    Keyer,
    Limits,
    PassDeadline,
    _Working,
    evidence_of,
    fp_digest,
    guard_row,
    run_pass_bounded,
)
from autodedup.incremental_lane import SqlFacts, SqlStore
from autodedup.incremental_sql import RT_PAIR_UPSERT_SQL
from autodedup.incremental_store import CohortFacts, MemoryStore, Schedule
from autodedup.model import hand_initialised
from autodedup.score_lane import families_of_bitmask
from autodedup.settings import Settings
from tests.autodedup import lane_world
from tests.autodedup.fake_pg import FakePg, _Cursor
from tests.autodedup.test_one_lane import _Session

GEN = "rt"
OUTLIERS = {4_100: 10, 4_101: 11}
ORDER = [4_000, 4_001, 4_100, 4_002, 4_003, 4_101, 4_004, 4_005, 4_006, 4_007, 4_008]


class _Recording(FakePg):
    """The FakePg, keeping each statement's parameters beside its text."""

    def __init__(self) -> None:
        super().__init__(now=lane_world.EXPORTED_AT + lane_world.timedelta(days=1))
        self.issued: list[tuple[str, dict[str, Any]]] = []

    def cursor(self) -> "_RecordingCursor":
        return _RecordingCursor(self)


class _RecordingCursor(_Cursor):
    def execute(self, sql: str, params=None) -> None:
        self.conn.issued.append((sql, dict(params or {})))
        super().execute(sql, params)


def _vector(seed: int) -> str:
    rng = random.Random(seed)
    return "[" + ",".join(f"{rng.uniform(-0.1, 0.1):.4f}" for _ in range(512)) + "]"


def _world() -> _Recording:
    conn = _Recording()
    lane_world.seed(conn)
    for listing_id, floor in OUTLIERS.items():
        lane_world._listing_row(conn, listing_id, "sreality",
                                [700_000 + listing_id * 10 + k for k in range(3)])
        conn.listings[listing_id]["floor"] = floor
    for row in conn.image_rows:
        conn.clip_vectors[int(row["image_id"])] = _vector(int(row["phash"]))
    conn.phash_pop = lane_world.true_population(conn)
    return conn


def _dataset(conn: FakePg) -> Dataset:
    """The same rows as an exported cohort, built with the export's own record builders."""
    tags: dict[int, list[dict[str, Any]]] = {}
    for row in conn.clip_tags:
        tags.setdefault(int(row["image_id"]), []).append(row)
    history: dict[int, list[dict[str, Any]]] = {}
    for row in conn.snapshots:
        history.setdefault(int(row["listing_id"]), []).append(row)
    listings: dict[int, Listing] = {}
    images: dict[int, list[Image]] = {}
    for listing_id in sorted(conn.listings):
        listings[listing_id] = Listing.from_json(build_listing_record(
            conn.listings[listing_id], block="", location=conn.locations[listing_id],
            history=history.get(listing_id, ())))
        images[listing_id] = [Image.from_json(build_image_record(
            row, clip=encode_clip(conn.clip_vectors.get(int(row["image_id"]))),
            tags=tag_pairs(tags.get(int(row["image_id"]), [])), pop=conn.phash_pop))
            for row in conn.image_rows if row["listing_id"] == listing_id]
    return Dataset(meta=Meta(), listings=listings, images_by_listing=images)


class _EveryVector:
    """The read before E932: every gallery the pass fetches carries its vectors."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def facts(self, ids, *, clip=True):
        return self.inner.facts(ids, clip=True)

    def vectors(self, image_ids):
        return self.inner.vectors(image_ids)


class _NoVector(_EveryVector):
    """A fetch that lost the vectors: the control the parity's evidence column must catch."""

    def facts(self, ids, *, clip=True):
        return self.inner.facts(ids, clip=False)

    def vectors(self, image_ids):
        return {}


def _drain(store: Any, facts: Any, conn: _Recording | None = None
           ) -> tuple[list[Any], list[list[tuple[str, dict[str, Any]]]]]:
    settings = Settings()
    calibration = harness.calibrated(_dataset(_world()), settings)[1]
    hold = EvidenceHold(now=_world().now.timestamp(), horizon_s=48 * 3600.0)
    work = Schedule(ORDER)
    passes, statements = [], []
    while not work.exhausted():
        mark = len(conn.issued) if conn is not None else 0
        passes.append(run_pass_bounded(store, facts, work, settings, hand_initialised(),
                                       calibration, limits=Limits(max_listings=2,
                                                                  max_pairs=10 ** 9),
                                       hold=hold))
        statements.append(conn.issued[mark:] if conn is not None else [])
    return passes, statements


def _memory_rows(store: MemoryStore) -> dict[tuple[int, int], tuple]:
    return {key: (row.zone, round(row.score, 9), tuple(sorted(row.families)), row.veto,
                  row.certificate, row.reason, row.fp_lo, row.fp_hi,
                  tuple(sorted((str(k), str(v)) for k, v in row.evidence.items())))
            for key, row in store.pairs.items()}


def _sql_rows(conn: FakePg) -> dict[tuple[int, int], tuple]:
    rows = SqlStore(conn, GEN).pairs_within(sorted(conn.listings))
    return {(row.lo, row.hi): (row.zone, round(row.score, 9), tuple(sorted(row.families)),
                               row.veto, row.certificate, row.reason, row.fp_lo, row.fp_hi,
                               tuple(sorted(row.evidence.items())))
            for row in rows}


def _memory_evidence(store: MemoryStore) -> dict[int, tuple[int, int, int, int]]:
    return {i: (row.evidence.n_images, row.evidence.n_phash, row.evidence.n_clip,
                row.evidence.n_tags) for i, row in store.fp.items()}


def _sql_evidence(conn: FakePg) -> dict[int, tuple[int, int, int, int]]:
    return {i: (row["ev_images"], row["ev_phash"], row["ev_clip"], row["ev_tags"])
            for (gen, i), row in conn.rt_fp.items() if gen == GEN}


def _sql_groups(conn: FakePg) -> list[list[int]]:
    groups: dict[int, list[int]] = {}
    for gen, key, listing_id in conn.cluster_members:
        if gen == GEN:
            groups.setdefault(key, []).append(listing_id)
    return sorted(sorted(members) for members in groups.values())


def _images_of(conn: FakePg, ids: set[int]) -> set[int]:
    return {int(row["image_id"]) for row in conn.image_rows if row["listing_id"] in ids}


def _ids(statements: list[tuple[str, dict[str, Any]]], sql: str) -> set[int]:
    return {int(i) for text, params in statements if text == sql for i in params["ids"]}


# ------------------------------------------------------------------ the one-path parity


def test_the_narrower_fetch_decides_exactly_what_the_full_fetch_decides() -> None:
    """Both fact sources, both stores, the read before E932 and the read after: the pairs —
    zone, score, families, veto, both digests and the evidence — and the groups are equal row
    for row. The evidence column is the one with teeth: a gallery whose vectors were skipped
    reads `n_clip = 0` and HOLDS a photo-dependent merge (E93), which the control shows."""
    ds = _dataset(_world())
    now = _world().now.timestamp()

    twin = MemoryStore(now=now)
    twin_passes, _ = _drain(twin, CohortFacts(ds))
    twin_full = MemoryStore(now=now)
    _drain(twin_full, _EveryVector(CohortFacts(ds)))
    assert _memory_rows(twin) == _memory_rows(twin_full)
    assert {key: row.feats for key, row in twin.pairs.items()} == {
        key: row.feats for key, row in twin_full.pairs.items()}, "every feature, CLIP's too"
    assert sorted(map(sorted, twin.clusters.values())) == sorted(
        map(sorted, twin_full.clusters.values()))
    assert _memory_evidence(twin) == _memory_evidence(twin_full), "the refresh's evidence"

    conn = _world()
    passes, _ = _drain(SqlStore(conn, GEN, store_floor=0.0), SqlFacts(conn), conn)
    full = _world()
    _drain(SqlStore(full, GEN, store_floor=0.0), _EveryVector(SqlFacts(full)), full)
    assert _sql_rows(conn) == _sql_rows(full)
    assert {key: row["features"] for key, row in conn.pairs.items()} == {
        key: row["features"] for key, row in full.pairs.items()}
    assert _sql_groups(conn) == _sql_groups(full)
    assert _sql_evidence(conn) == _sql_evidence(full)
    assert all(n_clip == n_images > 0 for n_images, _p, n_clip, _t in
               _sql_evidence(conn).values()), "every claim recorded its vectors' presence"

    assert _sql_rows(conn) == _memory_rows(twin), "the lane's facts are the cohort's"
    assert _sql_groups(conn) == sorted(map(sorted, twin.clusters.values()))
    assert _sql_evidence(conn) == _memory_evidence(twin)
    assert sum(p.zones.get("merge", 0) for p in passes) > 0, "the world must merge"
    assert sum(p.held for p in passes + twin_passes) == 0

    control = MemoryStore(now=now)
    held = sum(p.held for p in _drain(control, _NoVector(CohortFacts(ds)))[0])
    assert held > 0 and _memory_rows(control) != _memory_rows(twin), (
        "skipped vectors read as photographs the producers have not delivered")


def test_the_vector_read_names_the_claim_and_the_scored_endpoints_only() -> None:
    """Per pass, `COHORT_CLIP_SQL` reads the photographs of the claimed listings (their refresh
    records the vectors' presence) and of the two sides of every pair the pass DECIDES — never
    those of a neighbourhood listing, nor of the sides of a wanted pair whose stored decision
    stands. The read before E932 carried all of them, every pass."""
    conn = _world()
    passes, statements = _drain(SqlStore(conn, GEN, store_floor=0.0), SqlFacts(conn), conn)
    full = _world()
    _, before = _drain(SqlStore(full, GEN, store_floor=0.0), _EveryVector(SqlFacts(full)),
                       full)
    assert (GEN, *sorted(OUTLIERS)) in conn.pairs, "the outliers pair with each other"
    assert all(set(key[1:]) <= set(OUTLIERS) or not set(key[1:]) & set(OUTLIERS)
               for key in conn.pairs), "and with nothing else"

    skipped = 0
    for result, issued, issued_before in zip(passes, statements, before):
        claimed = set(result.claimed)
        decided = {int(params[side]) for text, params in issued
                   if text == RT_PAIR_UPSERT_SQL and params.get("features") is not None
                   for side in ("listing_lo", "listing_hi")}
        vectored = _ids(issued, COHORT_CLIP_SQL)
        assert _images_of(conn, claimed) <= vectored, "the claim's presence is read"
        assert _images_of(conn, claimed | decided) == vectored
        for outlier in set(OUTLIERS) - claimed - decided:
            if outlier in _ids(issued, COHORT_IMAGES_SQL):
                skipped += 1
                assert not _images_of(conn, {outlier}) & vectored
                assert _images_of(conn, {outlier}) <= _ids(issued_before, COHORT_CLIP_SQL)
    assert skipped > 0, "the outliers were read as neighbourhood, without vectors"


# ------------------------------------------------------------------ what a bare gallery is


@pytest.mark.parametrize("source", ["cohort", "sql"])
def test_a_fingerprint_reads_no_vector(source: str) -> None:
    """Fingerprint, digest, probe and index keys and guard row: the same from a gallery read
    without its vectors as from one read with them."""
    conn = _world()
    facts = CohortFacts(_dataset(conn)) if source == "cohort" else SqlFacts(conn)
    ids = sorted(conn.listings)
    settings = Settings()
    keyer = Keyer(settings, harness.calibrated(_dataset(conn), settings)[1])
    full = facts.facts(ids)
    bare = facts.facts(ids, clip=False)
    assert all(image.clip for _listing, images in full.values() for image in images)
    assert not any(image.clip for _listing, images in bare.values() for image in images)
    for listing_id in ids:
        (listing, with_vectors), (_same, without) = full[listing_id], bare[listing_id]
        a = build_fingerprint(listing, with_vectors, settings)
        b = build_fingerprint(listing, without, settings)
        assert a == b
        assert fp_digest(a, with_vectors) == fp_digest(b, without)
        assert keyer.probe_keys(a) == keyer.probe_keys(b)
        assert keyer.index_keys(a) == keyer.index_keys(b)
        assert guard_row(a) == guard_row(b)


@pytest.mark.parametrize("source", ["cohort", "sql"])
def test_the_top_up_gives_the_claim_and_the_endpoints_their_evidence(source: str) -> None:
    """E92's hold reads `n_clip` off the gallery: for a listing topped up it is what the full
    read gives, so no merge waits for photographs that were merely not fetched."""
    conn = _world()
    facts = CohortFacts(_dataset(conn)) if source == "cohort" else SqlFacts(conn)
    ids = sorted(conn.listings)
    full = facts.facts(ids)
    claimed, endpoints = [4_000, 4_001], {4_002, 4_003, 4_004}
    working = _Working(facts, Settings())
    working.ensure(claimed, clip=True)
    working.ensure(ids)
    working.ensure(endpoints, clip=True)
    for listing_id in ids:
        images = working.images[listing_id]
        if listing_id in set(claimed) | endpoints:
            assert evidence_of(images) == evidence_of(full[listing_id][1])
            assert [image.clip for image in images] == [
                image.clip for image in full[listing_id][1]]
            assert evidence_of(images).n_clip == len(images) > 0
        else:
            assert evidence_of(images).n_clip == 0, "the neighbourhood carries none"
    assert working.vectored == set(claimed) | endpoints


def test_the_top_up_counts_the_population_once() -> None:
    """The readout (`images_with_phash`, `images_unmeasured`) is counted per gallery READ; the
    top-up reads vectors only, so a bare read plus its top-up counts what one full read
    counts."""
    conn = _world()
    unknown = 900_000
    del conn.phash_pop[unknown]
    ids = sorted(conn.listings)
    once = SqlFacts(conn)
    once.facts(ids)
    topped = SqlFacts(conn)
    working = _Working(topped, Settings())
    working.ensure(ids)
    working.ensure(ids, clip=True)
    assert once.images_unmeasured > 0, "the readout must have something to double"
    assert (topped.reads, topped.images_with_phash, topped.images_unmeasured,
            topped.hashes_unmeasured) == (
        once.reads, once.images_with_phash, once.images_unmeasured, once.hashes_unmeasured)


def test_the_vector_read_is_sliced_and_reads_the_clock(monkeypatch) -> None:
    """The top-up rides the fact read's own path (E930, E931): FACT_CHUNK slices, the pass's
    deadline read before each, so a slow top-up stops at a slice boundary."""
    world = _world()
    image_ids = sorted(int(row["image_id"]) for row in world.image_rows)
    whole = SqlFacts(world).vectors(image_ids)
    assert len(whole) == len(image_ids)
    monkeypatch.setattr(incremental_lane, "FACT_CHUNK", 7)
    sliced = SqlFacts(world)
    assert sliced.vectors(image_ids) == whole
    assert sliced.statements == math.ceil(len(image_ids) / 7)

    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(incremental, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    crossing = SqlFacts(_Session(world, clock=clock, cost=30.0), deadline=clock.now + 100.0)
    with pytest.raises(PassDeadline):
        crossing.vectors(image_ids)
    assert 0 < crossing.statements < sliced.statements
