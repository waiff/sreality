"""E962: the streamed calibration cut is the whole cut to the byte, and holds one chunk.

`Calibration.build` (the decision module's, unchanged) is the oracle: every test cuts the same
scope both ways and compares every statistic, the digest and the serialised payload. The worlds
are built to reach each statistic's edges — prices tied on a decile boundary (0.0 against -0.0
among them, which only the oracle's insertion order sorts), non-integral prices, keys that
explode only once chunks are summed, a broker key that explodes only under its decile, codes
whose carriers sit in different chunks — and then cut in chunks of every size.
"""

from __future__ import annotations

import gc
import json
import random
from dataclasses import fields, replace
from typing import Any

import pytest

from autodedup import calibration_cut, incremental_lane
from autodedup.calibration_cut import CalibrationCut
from autodedup.dataset import Image, Listing
from autodedup.export_sql import COHORT_PHASH_POP_SQL
from autodedup.fingerprint import Fingerprint, build_fingerprint
from autodedup.harness import named_settings
from autodedup.incremental import Calibration
from autodedup.incremental_lane import SqlFacts, cut_calibration
from autodedup.incremental_sql import RT_PHASH_POP_WRITE_SQL
from autodedup.settings import Settings
from autodedup.text_facts import MAX_CODE_POPULATION
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.lane_world import GENERATION, seed_lane, true_population
from tests.autodedup.lane_world import world as lane_world

STATISTICS = ("price_cuts", "exploded", "block_docs", "block_token_df", "corpus_docs",
              "corpus_token_df", "block_attr_df", "pin_pop", "certifying_codes")
WORDS = ("prodej", "bytu", "cihlovy", "dum", "balkon", "sklep", "park", "metro", "rekonstrukce",
         "jih", "klidna", "lokalita", "garaz", "terasa", "vytah", "novostavba", "zahrada")
TOWNS = ((554782, 500054), (554782, 490067), (563510, None), (582786, 411582), (500011, None))


def _record(listing_id: int, rnd: random.Random) -> dict[str, Any]:
    obec, cast = rnd.choice(TOWNS)
    code = rnd.random()
    words = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(0, 25)))
    if code < 0.25:
        words += f" evidencni cislo zakazky N{rnd.randint(11000, 11012)} "
    elif code < 0.3:
        words += f" [id {rnd.randint(700000, 700003)}] "
    price = rnd.choice((None, 0.0, -0.0, 4_990_000, 5_000_000, 5_000_000, 6_250_000.5,
                        0.1 + 0.2, 1e-9, 12_345_678.901, float(rnd.randint(1, 40) * 250_000)))
    return {
        "id": listing_id, "block": "", "source": rnd.choice(("sreality", "idnes", "bazos")),
        "category_main": rnd.choice(("byt", "byt", "dum")),
        "category_type": rnd.choice(("prodej", "prodej", "pronajem")),
        "disposition": rnd.choice((None, "2+kk", "3+1", "1+kk")),
        "area_m2": rnd.choice((None, 0.0, 61.0, 62.5, 63.9, 80.0, 120.25)),
        "floor": rnd.choice((None, 1, 3)), "total_floors": rnd.choice((None, 6)),
        "price": price,
        "attrs": {key: rnd.choice(values) for key, values in (
            ("building_type", ("cihlova", "panelova", None)),
            ("condition", ("po_rekonstrukci", "novostavba", None)),
            ("energy_rating", ("B", "C", "G", None)), ("ownership", ("osobni", "druzstevni")),
            ("has_balcony", (True, False, None)), ("price_unit", ("celkem", "za_mesic")))
            if rnd.random() < 0.8},
        "description": words or None,
        "broker_key": rnd.choice((None, "b:1", "b:2", "b:3")),
        "location": {"obec_kod": obec, "cast_obce_kod": cast, "granularity": "address",
                     "granularity_rank": rnd.choice((4, 6)), "is_address_grain": True,
                     "lat": 50.0 + rnd.randint(0, 3) / 1000, "lon": 14.4,
                     "street_key": rnd.choice((None, "kolbenova", "vinohradska")),
                     "house_number": rnd.choice((None, "cp:12", "cp:14")),
                     "house_number_cp": "12", "psc": "11000", "country_status": "cz"},
        "price_history": [["2026-09-01T00:00:00+00:00", 5_100_000]] if rnd.random() < 0.3 else [],
    }


def _world(seed: int, n: int, settings: Settings
           ) -> tuple[dict[int, Fingerprint], dict[int, Listing]]:
    rnd = random.Random(seed)
    listings: dict[int, Listing] = {}
    fps: dict[int, Fingerprint] = {}
    ids = sorted(rnd.sample(range(1_000, 1_000 + 40 * n), n))
    for listing_id in ids:
        listing = Listing.from_json(_record(listing_id, rnd))
        images = [Image(listing_id=listing_id, image_id=listing_id * 100 + seq, seq=seq,
                        phash=rnd.choice((0x1F2E3D4C5B6A7980, 0x0123456789ABCDEF,
                                          rnd.getrandbits(63))),
                        pop=rnd.randint(1, 30))
                  for seq in range(rnd.randint(0, 6))]
        listings[listing_id] = listing
        fps[listing_id] = build_fingerprint(listing, images, settings)
    return fps, listings


def _streamed(fps: dict[int, Fingerprint], listings: dict[int, Listing], settings: Settings,
              chunk: int) -> Calibration:
    cut = CalibrationCut(settings)
    ids = sorted(fps)
    for start in range(0, len(ids), chunk):
        part = ids[start:start + chunk]
        cut.fold({i: fps[i] for i in part}, {i: listings[i] for i in part})
    return cut.build(GENERATION)


def _payload(calibration: Calibration) -> str:
    raw = calibration.to_json()
    raw["built_at"] = "fixed"
    return json.dumps(raw, ensure_ascii=False, sort_keys=True)


def _same(whole: Calibration, streamed: Calibration) -> None:
    for name in STATISTICS + ("n_listings", "generation", "feature_version"):
        assert getattr(streamed, name) == getattr(whole, name), name
    assert streamed.digest() == whole.digest()
    assert _payload(streamed) == _payload(whole)


SETTINGS = {
    "defaults": replace(Settings(), max_block_size=3),
    "w31": replace(named_settings("w31"), max_block_size=4),
}


@pytest.mark.parametrize("name", sorted(SETTINGS))
@pytest.mark.parametrize("seed", range(6))
def test_the_streamed_cut_is_the_whole_cut_at_every_chunk_size(name: str, seed: int) -> None:
    settings = SETTINGS[name]
    fps, listings = _world(seed, 90, settings)
    whole = Calibration.build(fps, listings, settings, GENERATION)
    exploded = [probe for probe, keys in whole.exploded.items() if keys]
    assert exploded and len(exploded) < len(whole.exploded), "some probes explode, some not"
    assert whole.certifying_codes and whole.price_cuts and whole.block_attr_df
    for chunk in (1, 2, 7, 33, 90):
        _same(whole, _streamed(fps, listings, settings, chunk))


def test_an_empty_scope_and_a_single_advert() -> None:
    settings = SETTINGS["w31"]
    _same(Calibration.build({}, {}, settings, GENERATION), CalibrationCut(settings).build())
    fps, listings = _world(11, 1, settings)
    _same(Calibration.build(fps, listings, settings, GENERATION),
          _streamed(fps, listings, settings, 1))


def test_ties_on_a_decile_boundary_sort_as_the_oracle_adds_them() -> None:
    """0.0 and -0.0 compare equal, so which one a cut picks is the order they entered; the
    stream folds in the oracle's order (ascending id), and so picks the same one."""
    settings = SETTINGS["defaults"]
    fps, listings = _world(3, 40, settings)
    prices = [0.0 if i % 2 else -0.0 for i in range(20)] + [5e6] * 10 + [0.1 + 0.2] * 10
    for listing_id, price in zip(sorted(fps), prices):
        fps[listing_id] = replace(fps[listing_id], price=price, cat_group="byt",
                                  category_type="prodej")
    whole = Calibration.build(fps, listings, settings, GENERATION)
    assert "-0.0" in _payload(whole) and "0.30000000000000004" in _payload(whole)
    for chunk in (1, 3, 40):
        _same(whole, _streamed(fps, listings, settings, chunk))


def test_a_broker_key_explodes_under_its_decile_not_its_price() -> None:
    """The stream counts a broker's postings under each advert's PRICE (the deciles do not
    exist yet) and re-counts them under the decile at the end: five prices of one decile are
    five keys of one posting each, and one key of five once mapped."""
    settings = replace(Settings(), max_block_size=4)
    fps, listings = _world(5, 60, settings)
    for offset, listing_id in enumerate(sorted(fps)):
        fps[listing_id] = replace(fps[listing_id], broker_key="b:9", cat_group="byt",
                                  category_type="prodej", price=1_000_000.0 + offset)
    whole = Calibration.build(fps, listings, settings, GENERATION)
    assert whole.exploded["broker"], "the decile keys are over the limit"
    _same(whole, _streamed(fps, listings, settings, 1))


def test_a_code_is_judged_over_carriers_in_different_chunks() -> None:
    """Certifying is a verdict over ALL a code's carriers: too many of them, or two that name
    different dispositions, refuses the code wherever the chunks split them."""
    settings = SETTINGS["defaults"]
    fps, listings = _world(8, 30, settings)
    ids = sorted(listings)
    for index, listing_id in enumerate(ids):
        if index < MAX_CODE_POPULATION + 1:
            text, dispo = "evidencni cislo zakazky N55555", "2+kk"
        elif index < MAX_CODE_POPULATION + 3:
            text, dispo = "evidencni cislo zakazky N66666", ("2+kk", "3+1")[index % 2]
        elif index < MAX_CODE_POPULATION + 9:
            text, dispo = "evidencni cislo zakazky N77777", "2+kk"
        else:
            text, dispo = "bez cisla", "2+kk"
        listings[listing_id] = replace(listings[listing_id], description=text, disposition=dispo,
                                       area_m2=60.0 + (index % 2) * 3.0)
    whole = Calibration.build(fps, listings, settings, GENERATION)
    assert whole.certifying_codes == ["N77777"]
    for chunk in (1, 4, 30):
        _same(whole, _streamed(fps, listings, settings, chunk))


def test_a_listing_without_a_fingerprint_still_carries_its_codes() -> None:
    """`Calibration.build` judges the codes of every listing it is handed, fingerprinted or
    not; so does the stream, a fold with no fingerprint at all included."""
    settings = SETTINGS["defaults"]
    fps, listings = _world(8, 40, settings)
    for index, listing_id in enumerate(sorted(listings)):
        listings[listing_id] = replace(listings[listing_id], disposition="2+kk", area_m2=61.0,
                                       description=f"evidencni cislo zakazky N{88800 + index // 4}")
    ids = sorted(listings)
    for kept in (set(), set(ids[::3])):
        part = {i: fps[i] for i in kept}
        whole = Calibration.build(part, listings, settings, GENERATION)
        assert len(whole.certifying_codes) == 10
        cut = CalibrationCut(settings)
        for start in range(0, len(ids), 7):
            chunk = ids[start:start + 7]
            cut.fold({i: part[i] for i in chunk if i in part}, {i: listings[i] for i in chunk})
        _same(whole, cut.build(GENERATION))


def test_a_fold_below_the_last_id_is_refused() -> None:
    settings = SETTINGS["defaults"]
    fps, listings = _world(2, 6, settings)
    ids = sorted(fps)
    cut = CalibrationCut(settings)
    cut.fold({i: fps[i] for i in ids[3:]}, {i: listings[i] for i in ids[3:]})
    with pytest.raises(ValueError, match="not above"):
        cut.fold({i: fps[i] for i in ids[:3]}, {i: listings[i] for i in ids[:3]})


def test_every_statistic_of_the_calibration_has_a_fold() -> None:
    """A seventh statistic added to `Calibration.build` must get its fold here too: the field
    set is pinned, and the lockstep tests above compare each one."""
    names = {f.name for f in fields(Calibration)} - {"_digest", "generation", "feature_version",
                                                     "built_at", "n_listings"}
    assert names == set(STATISTICS), (
        "Calibration gained or lost a statistic — fold it in autodedup/calibration_cut.py and "
        "name it in STATISTICS")


# ------------------------------------------------------------------- the lane's cut


@pytest.fixture()
def world() -> FakePg:
    return lane_world()


def test_the_lane_cut_is_the_same_calibration_at_every_chunk(world, tmp_path) -> None:
    out = seed_lane(world, tmp_path)
    seeded = dict(world.calibration[GENERATION])
    assert out["calibration"]["digest"] == seeded["digest"]
    for chunk in (1, 2, 4):
        assert cut_calibration(world, Settings(), "m", GENERATION,
                               chunk=chunk)["digest"] == seeded["digest"]
        written = json.loads(world.calibration[GENERATION]["payload"])
        assert {**written, "built_at": None} == {**json.loads(seeded["payload"]), "built_at": None}


def test_the_lane_cut_holds_one_chunk_of_facts_and_fingerprints(world, tmp_path,
                                                                 monkeypatch) -> None:
    """The memory the stream exists for: at every fold, and at the build, no fingerprint and
    no listing of an earlier chunk is alive — the cut holds counts, never the scope."""
    seed_lane(world, tmp_path)
    live: list[tuple[int, int]] = []
    fold, build = CalibrationCut.fold, CalibrationCut.build

    def alive() -> tuple[int, int]:
        gc.collect()
        objects = gc.get_objects()
        return (sum(1 for item in objects if isinstance(item, Fingerprint)),
                sum(1 for item in objects if isinstance(item, Listing)))

    def counted_fold(self: CalibrationCut, fps: Any, listings: Any) -> None:
        fp_count, listing_count = alive()
        live.append((fp_count - len(fps), listing_count - len(listings)))
        fold(self, fps, listings)

    def counted_build(self: CalibrationCut, *args: Any) -> Calibration:
        live.append(alive())
        return build(self, *args)

    monkeypatch.setattr(calibration_cut.CalibrationCut, "fold", counted_fold)
    monkeypatch.setattr(calibration_cut.CalibrationCut, "build", counted_build)
    before = alive()
    cut_calibration(world, Settings(), "m", GENERATION, chunk=2)
    assert len(live) == 5 + 1 and set(live) == {before}


def test_the_population_is_one_statement_read_off_the_cursor_and_written_in_chunks(
        world, tmp_path, monkeypatch) -> None:
    """E962: the population's rows are iterated, never fetched whole, and its upserts go out
    POP_CHUNK at a time from the sorted hashes, never as one list of every row."""
    seed_lane(world, tmp_path)
    world.phash_pop.clear()
    monkeypatch.setattr(incremental_lane, "POP_CHUNK", 2)
    fetched: list[str] = []
    batches: list[int] = []
    cursor = type(world.cursor())
    fetchall, executemany = cursor.fetchall, cursor.executemany

    def watched_fetchall(self: Any) -> list[tuple]:
        fetched.append(self.conn.statements[-1])
        return fetchall(self)

    def watched_executemany(self: Any, sql: str, seq: Any) -> None:
        if sql == RT_PHASH_POP_WRITE_SQL:
            batches.append(len(seq))
        executemany(self, sql, seq)

    monkeypatch.setattr(cursor, "fetchall", watched_fetchall)
    monkeypatch.setattr(cursor, "executemany", watched_executemany)
    world.statements.clear()
    out = cut_calibration(world, Settings(), "m", GENERATION)

    assert world.statements.count(COHORT_PHASH_POP_SQL) == 1
    assert COHORT_PHASH_POP_SQL not in fetched
    assert world.phash_pop == true_population(world)
    assert batches and max(batches) <= 2 and sum(batches) == len(world.phash_pop)
    assert out["hashes"] == out["hashes_measured"] == len(world.phash_pop)


def test_the_facts_hold_the_population_as_handed(world) -> None:
    population = {1: 2}
    assert SqlFacts(world, population=population).population is population
