"""A `public` the lane can be seeded from and run over, in the FakePg.

Nine listings over three portals in one town, consecutive ones sharing their photographs, one
catalogue frame carried by all of them — the world the W9g gate tests were built on, now seeded
the way production is (A10): `rt_seed` walks the block and cuts the calibration from these rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from autodedup.incremental_lane import run_rt_seed
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



SCOPE = "obec:563510"
SEED_ARGS = {"settings": "default", "model": "prior", "rt_scope": SCOPE}


def world(now: datetime | None = None, n: int = 9) -> FakePg:
    conn = FakePg(now=now or EXPORTED_AT + timedelta(days=1))
    seed(conn, n)
    return conn


def seed_lane(conn: FakePg, out_dir: Path, **args: Any) -> dict[str, Any]:
    """`rt_seed` over the fake's `public`, the scorer named as the lane requires (E90a)."""
    return run_rt_seed(lambda: conn, {**SEED_ARGS, **args}, out_dir)
