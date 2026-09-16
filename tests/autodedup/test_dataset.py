"""The local cohort reader, exercised against a synthetic artifact in the exported shape."""

from __future__ import annotations

import array
import base64
import gzip
import inspect
import io
import json
import math
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from autodedup import dataset as ds
from autodedup import export
from autodedup import harness

REPO_ROOT = Path(__file__).resolve().parents[2]

META: dict[str, Any] = {
    "t": "meta",
    "exported_at": "2026-09-16T10:00:00+00:00",
    "generator_version": "w1",
    "blocks": [
        {"key": "turnov", "grain": "town", "code": 577626, "label": "Turnov"},
        {"key": "negctl", "grain": "assembled", "code": 554782, "label": "Praha negative control"},
    ],
    "counts": {"listings": 3, "images": 4, "clip_vectors": 3, "snapshots": 5},
    "params": {"catalog_df": 8},
}


def _clip(seed: float) -> str:
    values = [math.sin(seed + i * 0.01) for i in range(ds.CLIP_DIM)]
    return base64.b64encode(struct.pack(ds.CLIP_STRUCT, *values)).decode("ascii")


def _listing(listing_id: int, block: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "t": "listing",
        "id": listing_id,
        "block": block,
        "source": "sreality",
        "source_id_native": f"n{listing_id}",
        "source_url": f"https://example.test/{listing_id}",
        "category_main": "byt",
        "category_type": "prodej",
        "subtype": "2+kk",
        "disposition": "2+kk",
        "area_m2": 62.5,
        "floor": 3,
        "total_floors": 5,
        "price": 4_500_000,
        "attrs": {"has_lift": True, "energy": "B"},
        "description": "Prodej bytu 2+kk v cihlovem dome.",
        "first_seen_at": "2025-01-02T08:00:00+00:00",
        "last_seen_at": "2026-09-01T08:00:00+00:00",
        "inactive_at": None,
        "is_active": True,
        "broker_key": "a" * 64,
        "broker_identity_id": 11,
        "broker_firm_id": 7,
        "location": {
            "obec_kod": 577626,
            "obec_name": "Turnov",
            "cast_obce_kod": None,
            "cast_obce_name": None,
            "granularity": "street",
            "granularity_rank": 4,
            "is_address_grain": False,
            "lat": 50.5875,
            "lon": 15.1583,
            "uncertainty_radius_m": 120.0,
            "street_key": "turnov|hluboka",
            "house_number": None,
            "psc": "51101",
            "ruian_adm_kod": None,
            "country_status": "cz",
        },
        "price_history": [
            ["2025-01-02T08:00:00+00:00", 4_800_000],
            ["2025-06-02T08:00:00+00:00", 4_500_000],
        ],
    }
    row.update(over)
    return row


def _image(listing_id: int, image_id: int, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "t": "image",
        "listing_id": listing_id,
        "image_id": image_id,
        "seq": 0,
        "storage_path": f"img/{image_id}.jpg",
        "phash": 1234567890123,
        "pop": 1,
        "clip": _clip(float(image_id)),
        "tags": [["kitchen", 0.81], ["interior", 0.42]],
    }
    row.update(over)
    return row


RECORDS: list[dict[str, Any]] = [
    META,
    _listing(101, "turnov"),
    _listing(
        102, "turnov",
        source="bazos", area_m2=None, disposition=None, floor=None, broker_key=None,
        is_active=False, inactive_at="2026-02-01T08:00:00+00:00",
        category_type="pronajem", price_history=[],
        location={"obec_kod": 577626, "obec_name": "Turnov", "granularity": "obec",
                  "granularity_rank": 1, "lat": None, "lon": None, "street_key": None,
                  "ruian_adm_kod": None},
    ),
    _listing(
        201, "negctl", source="remax", category_main="dum",
        location={"obec_kod": 554782, "obec_name": "Praha", "cast_obce_kod": 490245,
                  "cast_obce_name": "Vysocany", "granularity": "address",
                  "granularity_rank": 6, "is_address_grain": True, "lat": 50.1,
                  "lon": 14.5, "uncertainty_radius_m": 5.0, "street_key": "praha|kolbenova",
                  "house_number": "12", "psc": "19000", "ruian_adm_kod": 22349841,
                  "country_status": "cz"},
        price_history=[["2025-03-01T08:00:00+00:00", 9_000_000]],
    ),
    _image(101, 5001, seq=0, phash=-1, pop=12),
    _image(101, 5002, seq=1, phash=0, pop=2),
    _image(102, 5003, seq=0, phash=None, pop=None, clip=None, tags=[]),
    _image(201, 5004, seq=0, phash=42, pop=3),
]


def _write(path: Path, records: list[dict[str, Any]]) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


@pytest.fixture()
def artifact(tmp_path: Path) -> Path:
    return _write(tmp_path / "cohort.jsonl.gz", RECORDS)


def test_load_reads_the_contract(artifact: Path) -> None:
    data = ds.load(artifact)
    assert data.meta.generator_version == "w1"
    assert [b.key for b in data.meta.blocks] == ["turnov", "negctl"]
    assert data.meta.counts["listings"] == 3
    assert set(data.listings) == {101, 102, 201}
    assert [img.image_id for img in data.images(101)] == [5001, 5002]
    listing = data.listings[101]
    assert listing.area_m2 == pytest.approx(62.5)
    assert listing.location.street_key == "turnov|hluboka"
    assert listing.price_history[0] == ("2025-01-02T08:00:00+00:00", 4_800_000.0)
    assert data.listings[102].location.ruian_adm_kod is None


def test_load_is_order_tolerant(tmp_path: Path) -> None:
    shuffled = list(reversed(RECORDS))
    scrambled = _write(tmp_path / "scrambled.jsonl.gz", shuffled)
    ordered = ds.load(_write(tmp_path / "ordered.jsonl.gz", RECORDS))
    other = ds.load(scrambled)
    assert set(other.listings) == set(ordered.listings)
    assert other.meta.exported_at == ordered.meta.exported_at
    assert [i.image_id for i in other.images(101)] == [i.image_id for i in ordered.images(101)]
    assert [l.id for l in other.block("turnov")] == [l.id for l in ordered.block("turnov")]


def test_blank_lines_and_unknown_record_types_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "noisy.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(META) + "\n\n")
        handle.write(json.dumps({"t": "pair", "lo": 1, "hi": 2}) + "\n")
        handle.write(json.dumps(_listing(101, "turnov")) + "\n")
    data = ds.load(path)
    assert set(data.listings) == {101}


def test_clip_vector_round_trips_and_caches(artifact: Path) -> None:
    image = ds.load(artifact).images(101)[0]
    vector = image.clip_vector()
    assert vector is not None and len(vector) == ds.CLIP_DIM
    expected = [math.sin(5001.0 + i * 0.01) for i in range(ds.CLIP_DIM)]
    for got, want in zip(vector[:16], expected[:16]):
        assert got == pytest.approx(want, abs=1e-3)
    assert image.clip_vector() is vector


def test_clip_vector_is_none_without_a_payload_and_rejects_a_short_one(artifact: Path) -> None:
    data = ds.load(artifact)
    assert data.images(102)[0].clip_vector() is None
    broken = ds.Image(listing_id=1, image_id=1, clip=base64.b64encode(b"\x00" * 8).decode())
    with pytest.raises(ValueError, match="expected 1024"):
        broken.clip_vector()


def test_cosine_and_hamming64() -> None:
    assert ds.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert ds.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert ds.cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert ds.cosine([3.0, 4.0], [6.0, 8.0]) == pytest.approx(1.0)
    assert ds.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    with pytest.raises(ValueError):
        ds.cosine([1.0], [1.0, 2.0])

    assert ds.hamming64(0, 0) == 0
    assert ds.hamming64(0b1011, 0b1001) == 1
    assert ds.hamming64(-1, 0) == 64
    assert ds.hamming64(-1, -1) == 0
    assert ds.hamming64(1 << 63, 0) == 1


def test_summary_numbers(artifact: Path) -> None:
    summary = ds.load(artifact).summary()
    assert list(summary) == ["turnov", "negctl"]
    turnov = summary["turnov"]
    assert turnov["grain"] == "town" and turnov["code"] == 577626
    assert turnov["n_listings"] == 2
    assert turnov["active_share"] == pytest.approx(0.5)
    assert turnov["sources"] == {"bazos": 1, "sreality": 1}
    assert turnov["category_type"] == {"prodej": 1, "pronajem": 1}
    assert turnov["with_area"] == pytest.approx(0.5)
    assert turnov["with_disposition"] == pytest.approx(0.5)
    assert turnov["with_floor"] == pytest.approx(0.5)
    assert turnov["with_broker_key"] == pytest.approx(0.5)
    assert turnov["with_street_key"] == pytest.approx(0.5)
    assert turnov["with_point"] == pytest.approx(0.5)
    assert turnov["with_ruian_adm_kod"] == 0.0
    assert turnov["n_images"] == 3
    assert turnov["images_per_listing_mean"] == pytest.approx(1.5)
    assert turnov["images_per_listing_median"] == pytest.approx(1.5)
    assert turnov["with_any_clip"] == pytest.approx(0.5)
    assert turnov["with_any_phash"] == pytest.approx(0.5)
    assert turnov["catalog_image_share"] == pytest.approx(1 / 3)
    assert turnov["price_history_depth"] == {"0": 1, "2": 1}

    negctl = summary["negctl"]
    assert negctl["n_listings"] == 1
    assert negctl["with_ruian_adm_kod"] == 1.0
    assert negctl["catalog_image_share"] == 0.0
    assert negctl["price_history_depth"] == {"1": 1}


def test_summary_survives_a_block_with_no_listings(tmp_path: Path) -> None:
    path = _write(tmp_path / "empty.jsonl.gz", [META])
    summary = ds.load(path).summary()
    assert summary["turnov"]["n_listings"] == 0
    assert "active_share" not in summary["turnov"]


def test_block_keys_include_blocks_absent_from_meta(tmp_path: Path) -> None:
    path = _write(tmp_path / "extra.jsonl.gz", [META, _listing(303, "jablonec")])
    data = ds.load(path)
    assert data.block_keys() == ["turnov", "negctl", "jablonec"]
    assert [l.id for l in data.block("jablonec")] == [303]


def test_cli_stats_exit_code_and_json(artifact: Path, tmp_path: Path) -> None:
    out = io.StringIO()
    target = tmp_path / "nested" / "summary.json"
    assert harness.main(["stats", str(artifact), "--json", str(target)], out=out) == 0
    text = out.getvalue()
    assert "turnov" in text and "negctl" in text and "listings" in text
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["turnov"]["n_listings"] == 2


def test_cli_sample_exit_codes(artifact: Path, tmp_path: Path) -> None:
    out = io.StringIO()
    assert harness.main(["sample", str(artifact), "--block", "turnov", "--n", "5"], out=out) == 0
    text = out.getvalue()
    assert "listing 101" in text and "listing 102" in text
    assert "phash=" in text

    assert harness.main(["sample", str(artifact), "--block", "nope"], out=io.StringIO()) == 1
    assert harness.main(["stats", str(tmp_path / "missing.jsonl.gz")], out=io.StringIO()) == 1
    with pytest.raises(SystemExit) as excinfo:
        harness.main(["frobnicate", str(artifact)], out=io.StringIO())
    assert excinfo.value.code == 2


def test_cli_runs_as_a_module(artifact: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "autodedup.harness", "stats", str(artifact)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "turnov" in done.stdout


def test_clip_cache_is_a_compact_array_and_is_droppable(artifact: Path) -> None:
    image = ds.load(artifact).images(101)[0]
    vector = image.clip_vector()
    assert isinstance(vector, array.array) and vector.typecode == "f"
    assert vector.itemsize * len(vector) == 2 * ds.CLIP_BYTES
    assert image.clip_norm() == pytest.approx(ds.norm(vector))
    image.drop_clip_cache()
    again = image.clip_vector()
    assert again is not vector and list(again) == list(vector)


def test_clip_cache_is_not_an_init_parameter() -> None:
    params = inspect.signature(ds.Image).parameters
    assert "_clip_vector" not in params and "_clip_norm" not in params


def test_cosine_norm_matches_cosine(artifact: Path) -> None:
    data = ds.load(artifact)
    a, b = data.images(101)[0], data.images(201)[0]
    va, vb = a.clip_vector(), b.clip_vector()
    assert va is not None and vb is not None
    assert ds.cosine_norm(va, vb, a.clip_norm(), b.clip_norm()) == pytest.approx(ds.cosine(va, vb))
    assert ds.cosine_norm([1.0, 0.0], [1.0, 1.0], 0.0, 1.0) == 0.0


def test_non_finite_numbers_degrade_to_absence(tmp_path: Path) -> None:
    path = tmp_path / "nonfinite.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write('{"t":"listing","id":1,"block":"turnov","area_m2":NaN,"price":Infinity,'
                     '"location":{"lat":NaN,"lon":14.5}}\n')
    listing = ds.load(path).listings[1]
    assert listing.area_m2 is None and listing.price is None
    assert listing.location.lat is None and listing.location.has_point() is False


def test_catalog_share_is_none_when_the_population_probe_did_not_run(tmp_path: Path) -> None:
    records = [
        META,
        _listing(101, "turnov"),
        _image(101, 5001, phash=99, pop=0),
        _image(101, 5002, phash=None, pop=None),
    ]
    summary = ds.load(_write(tmp_path / "unmeasured.jsonl.gz", records)).summary()
    turnov = summary["turnov"]
    assert turnov["catalog_image_share"] is None
    assert turnov["catalog_share_curve"] == {"3": None, "5": None, "8": None, "12": None}
    assert turnov["catalog_pop_unmeasured"] == 1

    out = io.StringIO()
    assert harness.main(["stats", str(tmp_path / "unmeasured.jsonl.gz")], out=out) == 0
    assert "UNMEASURED" in out.getvalue()


def test_catalog_threshold_is_a_dial(artifact: Path) -> None:
    data = ds.load(artifact)
    assert data.summary(pop_min=2)["turnov"]["catalog_image_share"] == pytest.approx(2 / 3)
    assert data.summary(pop_min=20)["turnov"]["catalog_image_share"] == 0.0
    assert data.summary()["turnov"]["catalog_share_curve"]["3"] == pytest.approx(1 / 3)
    out = io.StringIO()
    assert harness.main(["stats", str(artifact), "--catalog-df", "2"], out=out) == 0
    assert "images pop>=2" in out.getvalue()


def test_unassigned_listings_stay_visible(tmp_path: Path) -> None:
    path = _write(tmp_path / "unassigned.jsonl.gz", [META, _listing(404, "")])
    data = ds.load(path)
    assert data.block_keys()[-1] == ds.UNASSIGNED_BLOCK
    assert [l.id for l in data.block(ds.UNASSIGNED_BLOCK)] == [404]
    summary = data.summary()
    assert summary[ds.UNASSIGNED_BLOCK]["n_listings"] == 1
    assert sum(s["n_listings"] for s in summary.values()) == len(data.listings)


def test_integrity_counters_and_the_stats_integrity_block(tmp_path: Path) -> None:
    records = [
        META,
        _listing(101, "turnov"),
        _listing(101, "turnov"),
        {"t": "pair", "lo": 1, "hi": 2},
        _image(101, 5001),
        _image(101, 5001),
        _image(999, 5009),
        _image(101, 5010, clip=base64.b64encode(b"\x00" * 8).decode("ascii")),
    ]
    path = _write(tmp_path / "dirty.jsonl.gz", records)
    data = ds.load(path)
    assert data.integrity.duplicate_listing_ids == 1
    assert data.integrity.duplicate_image_ids == 1
    assert data.integrity.orphan_images == 1
    assert data.integrity.skipped_types == {"pair": 1}
    assert data.clip_payload_errors() == [(5010, 8)]

    out = io.StringIO()
    assert harness.main(["stats", str(path)], out=out) == 0
    text = out.getvalue()
    assert "integrity" in text and "listings in blocks" in text
    assert "duplicate image ids" in text and "bad CLIP payloads" in text
    assert "e.g. [(5010, 8)]" in text and "pair 1" in text


def test_sample_prints_attrs(artifact: Path) -> None:
    out = io.StringIO()
    assert harness.main(["sample", str(artifact), "--block", "turnov", "--n", "1"], out=out) == 0
    assert '"has_lift": true' in out.getvalue()


def test_export_records_load_as_a_dataset(tmp_path: Path) -> None:
    """The artifact shape is a contract: build it with the exporter, read it with the reader."""
    vector = [math.sin(i * 0.01) for i in range(export.CLIP_DIMS)]
    listing_row = {
        "id": 7001,
        "source": "sreality",
        "source_id_native": "1234567",
        "source_url": "https://www.sreality.cz/detail/1234567",
        "category_main": "byt",
        "category_type": "prodej",
        "subtype": "3+1",
        "disposition": "3+1",
        "area_m2": 74.5,
        "floor": 2,
        "total_floors": 4,
        "price_czk": 5_900_000,
        "description": "Prodej bytu, volejte +420 777 123 456 nebo pis na jan@example.cz.",
        "first_seen_at": "2025-02-01T08:00:00+00:00",
        "last_seen_at": "2026-09-01T08:00:00+00:00",
        "inactive_at": None,
        "is_active": True,
        "broker_identity_id": 4242,
        "broker_firm_id": 88,
        "broker_phone": "+420 777 123 456",
        "broker_email": "jan@example.cz",
        "has_lift": True,
        "condition": "dobry",
    }
    location_row = {
        "obec_kod": 577626,
        "obec_name": "Turnov",
        "granularity": "address",
        "granularity_rank": 6,
        "is_address_grain": True,
        "lat": 50.5875,
        "lon": 15.1583,
        "uncertainty_radius_m": 5.0,
        "street_name": "Hluboká",
        "house_number_cp": "12",
        "house_number_co": "3",
        "psc": "51101",
        "ruian_adm_kod": 22349841,
        "country_status": "cz",
    }
    history = [
        {"scraped_at": "2025-02-01T08:00:00+00:00", "price_czk": 6_200_000},
        {"scraped_at": "2025-05-01T08:00:00+00:00", "price_czk": 6_200_000},
        {"scraped_at": "2025-08-01T08:00:00+00:00", "price_czk": 5_900_000},
    ]
    image_row = {"listing_id": 7001, "image_id": 9001, "sequence": 0,
                 "storage_path": "img/9001.jpg", "phash": -12345}
    tags = export.tag_pairs([{"logical_tag": "kitchen", "fine_tag": "kitchen_modern",
                              "confidence": 0.77}])
    records = [
        {"t": "meta", "exported_at": "2026-09-16T10:00:00+00:00",
         "generator_version": export.GENERATOR_VERSION,
         "blocks": [{"key": "turnov", "grain": "town", "code": 577626, "label": "Turnov"}],
         "counts": {"listings": 1, "images": 1}, "params": {"salt": export.AUTODEDUP_BROKER_SALT}},
        export.build_listing_record(listing_row, block="turnov", location=location_row,
                                    history=history),
        export.build_image_record(image_row, clip=export.encode_clip(vector), tags=tags,
                                  pop={-12345: 9}),
    ]
    path = tmp_path / "roundtrip.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    data = ds.load(path)
    listing = data.listings[7001]
    assert listing.block == "turnov" and listing.source == "sreality"
    assert listing.source_id_native == "1234567"
    assert listing.area_m2 == pytest.approx(74.5) and listing.price == pytest.approx(5_900_000)
    assert listing.subtype == "3+1" and listing.total_floors == 4
    assert listing.attrs == {"has_lift": True, "condition": "dobry"}
    assert listing.broker_key and len(listing.broker_key) == 64
    assert listing.broker_identity_id == 4242 and listing.broker_firm_id == 88
    assert "777" not in (listing.description or "") and "@" not in (listing.description or "")
    assert listing.location.street_key == "hluboka"
    assert listing.location.house_number == "12/3"
    assert listing.location.ruian_adm_kod == 22349841
    assert listing.price_history == [
        ("2025-02-01T08:00:00+00:00", 6_200_000.0),
        ("2025-08-01T08:00:00+00:00", 5_900_000.0),
    ]
    image = data.images(7001)[0]
    assert image.image_id == 9001 and image.seq == 0 and image.phash == -12345
    assert image.pop == 9 and image.is_catalog_candidate()
    assert image.tags == [("kitchen", 0.77), ("kitchen_modern", 0.77)]
    decoded = image.clip_vector()
    assert decoded is not None
    assert list(decoded) == pytest.approx(export.decode_clip(image.clip))
    assert data.summary()["turnov"]["n_listings"] == 1
