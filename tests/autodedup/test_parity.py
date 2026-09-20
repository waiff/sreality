"""`--mode rt_parity`: the instrument has to find the fact gap, and find nothing when there is
none.

The fixture builds ONE cohort twice — the artifact through the export's record builders, the
live side through `SqlFacts` over the same fake `public` rows — so a clean run is the control
and every defect below is introduced on purpose, one at a time.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from autodedup import parity
from autodedup.export import build_image_record, build_listing_record, tag_pairs
from autodedup.fingerprint import build_fingerprint
from autodedup.incremental import Calibration
from autodedup.dataset import Listing
from autodedup.settings import Settings
from tests.autodedup.fake_pg import FakePg

GENERATION = "rt"
EXPORTED_AT = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
SOURCES = ("sreality", "bezrealitky", "remax")
DESCRIPTION = (
    "Prodej krasneho bytu 2+kk v cihlovem dome po kompletni rekonstrukci, evidencni cislo "
    "zakazky N115423, orientace na jih, sklep a balkon, klidna lokalita u parku. "
)


def _listing_row(conn: FakePg, listing_id: int, source: str, phashes: list[int]) -> None:
    conn.listings[listing_id] = {
        "id": listing_id, "source": source, "source_id_native": f"n{listing_id}",
        "source_url": f"https://example.test/{listing_id}", "category_main": "byt",
        "category_type": "prodej", "subtype": None, "disposition": "2+kk",
        "area_m2": 62.0 + (listing_id % 3), "floor": 3, "total_floors": 6,
        "price_czk": 6_200_000 + 1_000 * (listing_id % 5),
        "price_unit": "celkem", "area_basis": "uzitna", "has_balcony": True,
        "has_parking": None, "has_lift": True, "building_type": "cihlova",
        "condition": "po_rekonstrukci", "energy_rating": "C", "estate_area": None,
        "usable_area": 62.0, "garden_area": None, "category_sub_cb": None,
        "furnished": "castecne", "terrace": None, "cellar": True, "garage": None,
        "parking_lots": None, "ownership": "osobni",
        "published_at": "2026-02-01T08:00:00+00:00",
        "description": DESCRIPTION * 4,
        "first_seen_at": EXPORTED_AT - timedelta(days=30),
        "last_seen_at": EXPORTED_AT - timedelta(days=1),
        "inactive_at": None, "is_active": True,
        "broker_identity_id": 77, "broker_firm_id": 9,
        "broker_phone": "+420 777 123 456", "broker_email": "a@agency.cz",
    }
    conn.locations[listing_id] = {
        "listing_id": listing_id, "obec_kod": 563510, "obec_name": "Liberec",
        "cast_obce_kod": None, "cast_obce_name": None, "granularity": "address",
        "granularity_rank": 6, "is_address_grain": True, "lat": 50.77, "lon": 15.05,
        "uncertainty_radius_m": 5.0, "street_name": "Kolbenova",
        "house_number_cp": "12", "house_number_co": None, "psc": "46001",
        "ruian_adm_kod": 22_349_841, "country_status": "cz",
    }
    conn.snapshots.append({"id": listing_id, "listing_id": listing_id,
                           "scraped_at": EXPORTED_AT - timedelta(days=2),
                           "price_czk": 6_500_000})
    for index, phash in enumerate(phashes):
        image_id = listing_id * 100 + index
        conn.image_rows.append({"image_id": image_id, "listing_id": listing_id,
                                "sequence": index, "storage_path": f"img/{image_id}.jpg",
                                "phash": phash})
        conn.clip_tags.append({"image_id": image_id, "fine_tag": "living_room_modern",
                               "logical_tag": "living_room", "confidence": 0.8})


def seed(conn: FakePg, n: int = 9) -> None:
    """`n` listings over three portals. Consecutive listings SHARE their photo hashes, so the
    draw carries pairs with real image agreement — the only pairs whose K-C the instrument can
    say anything about. One hash is carried by every listing: the catalogue photo E9 subtracts."""
    catalogue = 111_000
    for index in range(n):
        listing_id = 4_000 + index
        base = 900_000 + 10 * (index // 2)
        _listing_row(conn, listing_id, SOURCES[index % len(SOURCES)],
                     [catalogue, base, base + 1, base + 2, base + 3])


def true_population(conn: FakePg) -> dict[int, int]:
    """What the export's corpus-wide scan returns: distinct carriers per hash, never below 1."""
    carriers: dict[int, set[int]] = {}
    for row in conn.image_rows:
        if row.get("phash") is not None:
            carriers.setdefault(int(row["phash"]), set()).add(int(row["listing_id"]))
    return {phash: len(ids) for phash, ids in carriers.items()}


def write_artifact(conn: FakePg, path: Path, pop: dict[int, int]) -> Path:
    tags: dict[int, list[dict[str, Any]]] = {}
    for row in conn.clip_tags:
        tags.setdefault(int(row["image_id"]), []).append(row)
    history: dict[int, list[dict[str, Any]]] = {}
    for row in conn.snapshots:
        history.setdefault(int(row["listing_id"]), []).append(row)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "t": "meta", "exported_at": EXPORTED_AT.isoformat(), "generator_version": "w1",
            "blocks": [{"key": "town:563510", "grain": "town", "code": 563510}],
            "counts": {}, "phash_pop_ok": True, "errors": [], "params": {}}) + "\n")
        for listing_id in sorted(conn.listings):
            handle.write(json.dumps(build_listing_record(
                conn.listings[listing_id], block="town:563510",
                location=conn.locations[listing_id],
                history=history.get(listing_id, ())), default=str) + "\n")
        for row in conn.image_rows:
            handle.write(json.dumps(build_image_record(
                row, clip=None, tags=tag_pairs(tags.get(int(row["image_id"]), [])),
                pop=pop), default=str) + "\n")
    return path


def seed_calibration(conn: FakePg, settings: Settings) -> None:
    from autodedup.export import build_listing_record as _record

    listings = {
        listing_id: Listing.from_json(_record(
            conn.listings[listing_id], block="town:563510",
            location=conn.locations[listing_id]))
        for listing_id in sorted(conn.listings)
    }
    fps = {i: build_fingerprint(listing, [], settings) for i, listing in listings.items()}
    calibration = Calibration.build(fps, listings, settings, GENERATION)
    conn.calibration[GENERATION] = {
        "digest": calibration.digest(), "n_listings": len(fps),
        "payload": calibration.to_json(), "artifact_url": "out/cohort.jsonl.gz",
        "settings": settings.to_dict(), "model_version": "hand_v1"}


@pytest.fixture()
def world(tmp_path: Path):
    """`world(**args) -> report`, with `.conn` and `.artifact` attached."""
    settings = Settings()
    conn = FakePg(now=EXPORTED_AT + timedelta(days=1))
    seed(conn)
    seed_calibration(conn, settings)
    pop = true_population(conn)
    conn.phash_pop.update(pop)
    artifact = write_artifact(conn, tmp_path / "cohort.jsonl.gz", pop)

    def run(**args: Any) -> dict[str, Any]:
        merged = {"cohort": str(artifact), "n": "9", "pairs": "40", "seed": "1", **args}
        return parity.run_parity(lambda: conn, merged, tmp_path / "out")

    run.conn = conn          # type: ignore[attr-defined]
    run.artifact = artifact  # type: ignore[attr-defined]
    return run


def test_a_matched_world_reports_no_field_difference(world) -> None:
    report = world()
    assert report["sample"]["sampled"] == 9
    assert report["facts"]["listing_fields"] == {}
    assert report["facts"]["image_fields"] == {}
    assert report["facts"]["counts"]["images_only_live"] == 0
    assert report["facts"]["counts"]["images_only_artifact"] == 0
    assert report["pairs"]["drawn"] > 0
    assert report["pairs"]["score_differs_gt_0_01"] == 0
    assert report["pairs"]["zone_moves"] == {} and report["pairs"]["certificate_moves"] == {}
    assert report["pairs"]["features"] == {}


def test_the_instrument_writes_nothing(world) -> None:
    world()
    conn = world.conn
    written = [sql for sql in conn.statements
               if sql.strip().split()[0].lower() in {"insert", "update", "delete"}]
    assert written == [], "the read-only instrument issued a write"
    assert conn.transactions == 0 and not conn.pairs and not conn.rt_fp


def test_an_empty_frozen_population_is_named_and_costs_every_catalogue_ratio(world) -> None:
    """The live defect: `autodedup.phash_pop` is materialised only for hashes on >= 3 listings
    (migration 528) and is in fact empty, while the export's scan returns >= 1 for every hash.
    `pop == 0` makes `pop_is_measured()` false, `catalog_ratio` None, and K-C — which requires a
    catalogue ratio to be PRESENT — structurally unreachable."""
    world.conn.phash_pop.clear()
    report = world()

    assert report["phash_pop_rows"] == 0
    pop = report["facts"]["image_fields"]["image.pop"]
    assert pop["differ"] == pop["compared"] > 0
    assert pop["differ_stable"] == pop["differ"], "nothing here drifted"
    assert set(report["facts"]["listing_fields"]) == set(), "only the images moved"
    classes = report["facts"]["image_field_classes"]
    assert set(classes) == {"frozen_statistic"}, "the frozen statistic moved and nothing else"

    features = report["pairs"]["features"]
    assert "catalog_ratio_max" in features
    artifact_certs = report["pairs"]["certificates"]["artifact"]
    live_certs = report["pairs"]["certificates"]["live"]
    assert artifact_certs.get("K-C", 0) > 0
    assert live_certs.get("K-C", 0) == 0
    assert any(move.startswith("K-C->") for move in report["pairs"]["certificate_moves"])


def test_a_listing_changed_since_the_export_is_drift_not_a_defect(world) -> None:
    conn = world.conn
    listing_id = min(conn.listings)
    conn.listings[listing_id]["price_czk"] = 9_999_000
    conn.snapshots.append({"id": 99_999, "listing_id": listing_id,
                           "scraped_at": EXPORTED_AT + timedelta(days=1),
                           "price_czk": 9_999_000})
    report = world()

    assert report["sample"]["drifted"] == 1
    assert report["sample"]["drifted_examples"][0]["listing_id"] == listing_id
    price = report["facts"]["listing_fields"]["price"]
    assert price["differ"] == 1 and price["differ_stable"] == 0


def test_an_image_added_after_the_export_is_counted_not_diffed(world) -> None:
    conn = world.conn
    listing_id = min(conn.listings)
    conn.image_rows.append({"image_id": 777_777, "listing_id": listing_id, "sequence": 9,
                            "storage_path": "img/777777.jpg", "phash": 123_456})
    report = world()
    assert report["facts"]["counts"]["images_only_live"] == 1
    assert report["facts"]["counts"]["images_only_artifact"] == 0
    assert report["facts"]["gallery_examples"][0]["only_live"] == [777_777]


def test_the_report_names_what_the_seeded_generation_was_cut_under(world) -> None:
    report = world()
    config = report["lane_config"]
    assert config["seeded"] is True
    assert config["seeded_model_version"] == "hand_v1"
    assert config["settings_differences"] == {}, "the instrument scored on the seeded settings"


def test_export_run_or_cohort_is_required() -> None:
    with pytest.raises(SystemExit):
        parity.parse_args({})
    with pytest.raises(SystemExit):
        parity.parse_args({"export_run": "not-a-run"})
    parsed = parity.parse_args({"export_run": "35200225251"})
    assert (parsed.n, parsed.pairs, parsed.seed, parsed.generation) == (200, 400, 1, "rt")


def test_the_instrument_can_read_the_population_a_seed_WOULD_write(world) -> None:
    """`population=artifact` is how the operator measures a seed before seeding: the live side
    reads the cohort's own counts — the exact rows `rt_seed` materialises into
    `autodedup.phash_pop` — so the certificates the fix restores are visible from a read-only
    run against a table that is still empty (E86)."""
    world.conn.phash_pop.clear()

    broken = world()
    assert broken["population_source"] == "frozen" and broken["phash_pop_rows"] == 0
    assert broken["pairs"]["certificates"]["live"].get("K-C", 0) == 0

    fixed = world(population="artifact")
    assert fixed["population_source"] == "artifact"
    assert fixed["facts"]["image_fields"] == {}, "the population is the one the export measured"
    assert (fixed["pairs"]["certificates"]["live"]
            == fixed["pairs"]["certificates"]["artifact"])
    assert fixed["pairs"]["certificate_moves"] == {}


def test_the_report_says_whether_the_GATE_would_refuse(world) -> None:
    """The instrument answers the question the parent asks before re-seeding: would the gate
    refuse this? A clean world says no; a stored fact moved with no snapshot behind it says
    yes, and names the listing."""
    clean = world()
    assert clean["gate"]["would_refuse"] is False and clean["gate"]["breaches"] == 0
    assert clean["gate"]["checked"] == 9

    world.conn.listings[4_000]["price_czk"] = 1
    broken = world()
    assert broken["gate"]["would_refuse"] is True
    assert broken["gate"]["breaches_by_kind"] == {"stored": 1}
