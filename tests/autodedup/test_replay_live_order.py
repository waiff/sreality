"""W9m — the replay proof covers the ORDER and the CLAIM SIZE production uses (E116).

For four waves the proof ran the incremental path in `first_seen_at` order at a batch size of
200 and declared equivalence. A real build claims entrants `order by s.listing_id`
(`RT_SCOPE_ENTRANTS_SQL`) five hundred at a time; the 2026-09-21 build claimed 108, then 507,
then 507, then 500 nine times. So the proof passed while the live path was ordered differently
and clustered from a store the proof never wrote through — and when the live store then
disagreed, there was no arm to point at.

Two things are proved here: the live arm exists and is not opt-in, and it reaches the batch
engine's state. The second one is the claim; the first one is what makes it a proof.
"""

from __future__ import annotations

import json
import gzip
from pathlib import Path
from typing import Any

from autodedup import replay
from autodedup.dataset import Dataset, Image, Listing, Location, Meta
from autodedup.incremental_store import MemoryStore
from autodedup.replay import arrival_order, entrant_order
from autodedup.settings import Settings

SETTINGS = Settings()


def _listing(listing_id: int, index: int) -> Listing:
    return Listing(
        id=listing_id, block="town:1", source="sreality", source_id_native=f"n{listing_id}",
        category_main="byt", category_type="prodej", disposition="2+kk",
        area_m2=55.0 + (index % 5), floor=3, price=5_000_000.0,
        description="Prodej bytu 2+kk o vymere 60 m2 v cihlovem dome po rekonstrukci. " * 6,
        # Arrival order and id order are DELIBERATELY inverted: the newest id arrived first,
        # so an arm that only ran `first_seen_at` order never ran this edge stream.
        first_seen_at=f"2026-01-{28 - index:02d}T08:00:00+00:00",
        last_seen_at="2026-03-01T08:00:00+00:00", is_active=True,
        broker_key=f"b{listing_id % 3}",
        location=Location(obec_kod=554782, cast_obce_kod=490245, street_key="hlavni",
                          house_number_cp="12", lat=50.1, lon=14.4, ruian_adm_kod=111),
    )


def _dataset(n: int = 18) -> Dataset:
    listings: dict[int, Listing] = {}
    images: dict[int, list[Image]] = {}
    for index in range(n):
        listing_id = 1000 + index * 7
        listings[listing_id] = _listing(listing_id, index)
        images[listing_id] = [
            Image(listing_id=listing_id, image_id=listing_id * 10 + seq, seq=seq,
                  phash=(index % 4) * 977 + seq, pop=1)
            for seq in range(3)
        ]
    return Dataset(meta=Meta(), listings=listings, images_by_listing=images)


def _artifact(tmp_path: Path, ds: Dataset) -> Path:
    path = tmp_path / "cohort.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "meta", **_meta(ds)}) + "\n")
        for listing_id, listing in sorted(ds.listings.items()):
            handle.write(json.dumps({"type": "listing", **_listing_json(listing)}) + "\n")
            for image in ds.images(listing_id):
                handle.write(json.dumps({"type": "image", **_image_json(image)}) + "\n")
    return path


def _meta(ds: Dataset) -> dict[str, Any]:
    return {"n_listings": len(ds.listings), "exported_at": "2026-09-20T20:03:50+00:00"}


def _listing_json(listing: Listing) -> dict[str, Any]:
    location = listing.location
    return {
        "id": listing.id, "block": listing.block, "source": listing.source,
        "source_id_native": listing.source_id_native, "category_main": listing.category_main,
        "category_type": listing.category_type, "disposition": listing.disposition,
        "area_m2": listing.area_m2, "floor": listing.floor, "price": listing.price,
        "description": listing.description, "first_seen_at": listing.first_seen_at,
        "last_seen_at": listing.last_seen_at, "is_active": listing.is_active,
        "broker_key": listing.broker_key,
        "location": {"obec_kod": location.obec_kod, "cast_obce_kod": location.cast_obce_kod,
                     "street_key": location.street_key,
                     "house_number_cp": location.house_number_cp,
                     "lat": location.lat, "lon": location.lon,
                     "ruian_adm_kod": location.ruian_adm_kod},
    }


def _image_json(image: Image) -> dict[str, Any]:
    return {"listing_id": image.listing_id, "image_id": image.image_id, "seq": image.seq,
            "phash": image.phash, "pop": image.pop}


# ------------------------------------------------------------------ the two orders differ


def test_the_entrant_order_is_the_one_a_build_claims_in() -> None:
    """`RT_SCOPE_ENTRANTS_SQL` is `order by s.listing_id`: the bootstrap phase walks the scope
    snapshot by id, not by arrival, so an existing corpus enters in id order."""
    ds = _dataset()

    assert entrant_order(ds) == sorted(ds.listings)
    assert entrant_order(ds) != arrival_order(ds)


# ------------------------------------------------------------------ the arm, and the claim


def test_the_live_arm_runs_without_being_asked_for(tmp_path: Path) -> None:
    """Not a flag. A proof whose hardest arm is opt-in is a proof nobody runs (E116)."""
    ds = _dataset()
    assert replay.run(["--artifact", str(_artifact(tmp_path, ds)),
                       "--out", str(tmp_path / "out"), "--batch-size", "4"]) == 0
    report = json.loads((tmp_path / "out" / "replay.json").read_text())

    assert report["live_order"]["claim_size"] == 500
    assert report["live_order"]["order"].startswith("listing_id ascending")
    assert report["live_order"]["score_column"] == "double precision"


def test_the_live_order_at_the_live_claim_size_reaches_the_batch_state(tmp_path: Path) -> None:
    ds = _dataset()
    assert replay.run(["--artifact", str(_artifact(tmp_path, ds)),
                       "--out", str(tmp_path / "out"), "--batch-size", "4"]) == 0
    report = json.loads((tmp_path / "out" / "replay.json").read_text())

    assert report["live_order"]["pairs_vs_batch"]["identical"] is True
    assert report["live_order"]["clusters_vs_batch"]["member_sets_identical"] is True
    assert report["live_order"]["clusters_vs_arrival_order"]["member_sets_identical"] is True


def test_the_arm_is_written_through_the_store_it_names(tmp_path: Path) -> None:
    """`--score-column real` reproduces `autodedup.pairs.score` as it was before migration
    541, so the proof can now be pointed at the precision the live store actually had —
    which is the one thing the four passing waves never exercised (E114)."""
    ds = _dataset()
    assert replay.run(["--artifact", str(_artifact(tmp_path, ds)),
                       "--out", str(tmp_path / "out"), "--batch-size", "4",
                       "--score-column", "real"]) == 0
    report = json.loads((tmp_path / "out" / "replay.json").read_text())

    assert report["live_order"]["score_column"] == "real"


def test_the_twin_the_arm_writes_through_is_the_one_the_column_declares() -> None:
    assert MemoryStore().score_sql_type == "double precision"
    assert MemoryStore(score_sql_type="real").score_sql_type == "real"
