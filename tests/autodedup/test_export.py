"""The cohort export: the block rules, the PII posture, and the artifact's record contract.

The artifact shape is a contract between this exporter and the offline dataset builder, so
the ordering, the discriminators and the meta counts are pinned here rather than left to the
first reader to discover.
"""

from __future__ import annotations

import gzip
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from autodedup import cohort, export, export_sql
from scraper.db import LISTING_COLUMNS
from toolkit.room_taxonomy import ROOM_FAMILIES, family_of


# --- cohort -----------------------------------------------------------------------------


def test_default_cohort_is_the_four_ruled_blocks() -> None:
    assert [b.key for b in cohort.BLOCKS] == ["jablonec", "turnov", "vysocany", "negctl"]
    assert [b.grain for b in cohort.BLOCKS] == ["town", "town", "quarter", "assembled"]
    assert [b.code for b in cohort.BLOCKS] == [563510, 577626, 490245, 554782]
    assert cohort.parse_blocks("") == cohort.BLOCKS
    assert cohort.parse_blocks(None) == cohort.BLOCKS


def test_negative_control_rule_excludes_the_dense_quarter() -> None:
    rule = cohort.NEGATIVE_CONTROL
    assert rule.obec_kod == cohort.PRAHA_OBEC_KOD == 554782
    assert rule.exclude_cast_obce_kod == 490245
    assert (rule.min_group_size, rule.min_distinct_floors, rule.max_listings) == (3, 2, 800)


@pytest.mark.parametrize("raw", ["town:563510 quarter:490245", "town:563510,quarter:490245"])
def test_blocks_override_parses_both_separators(raw: str) -> None:
    blocks = cohort.parse_blocks(raw)
    assert [(b.grain, b.code) for b in blocks] == [("town", 563510), ("quarter", 490245)]
    assert [b.key for b in blocks] == ["town563510", "quarter490245"]


def test_blocks_override_accepts_a_ruled_key() -> None:
    assert cohort.parse_blocks("negctl") == (cohort.block_by_key("negctl"),)


@pytest.mark.parametrize("raw", ["village:1", "town:abc", "negctl negctl", "nope"])
def test_blocks_override_rejects_nonsense(raw: str) -> None:
    with pytest.raises(ValueError):
        cohort.parse_blocks(raw)


def test_negative_control_takes_whole_groups_largest_first_under_the_cap() -> None:
    groups = [
        {"ruian_adm_kod": 1, "listing_ids": list(range(100, 106))},
        {"ruian_adm_kod": 2, "listing_ids": list(range(200, 204))},
        {"ruian_adm_kod": 3, "listing_ids": list(range(300, 303))},
    ]
    ids, taken = cohort.select_negative_control(groups, max_listings=10)
    assert ids == list(range(100, 106)) + list(range(200, 204))
    assert [t["ruian_adm_kod"] for t in taken] == [1, 2]
    assert [t["n_listings"] for t in taken] == [6, 4]


def test_negative_control_skips_an_oversized_group_rather_than_emptying_the_stratum() -> None:
    groups = [
        {"ruian_adm_kod": 9, "listing_ids": list(range(1000))},
        {"ruian_adm_kod": 2, "listing_ids": [1, 2, 3]},
    ]
    ids, taken = cohort.select_negative_control(groups, max_listings=800)
    assert ids == [1, 2, 3]
    assert [t["ruian_adm_kod"] for t in taken] == [2]


def test_dedupe_ids_gives_a_listing_to_the_first_block_that_claims_it() -> None:
    a, b = cohort.BLOCKS[0], cohort.BLOCKS[1]
    assert cohort.dedupe_ids([(a, [1, 2]), (b, [2, 3])]) == {1: a.key, 2: a.key, 3: b.key}


# --- PII --------------------------------------------------------------------------------


def test_pii_exclusion_list_is_pinned() -> None:
    """broker_name is never selected; phone and e-mail are read ONLY to build the salted
    key and are dropped before a record exists (E28)."""
    assert export_sql.EXCLUDED_PII_COLUMNS == ("broker_name",)
    assert export_sql.BROKER_HASH_INPUT_COLUMNS == ("broker_phone", "broker_email")
    assert "broker_name" not in export_sql.COHORT_LISTINGS_SQL
    for sql in (
        export_sql.COHORT_LOCATION_SQL,
        export_sql.COHORT_IMAGES_SQL,
        export_sql.COHORT_PRICE_HISTORY_SQL,
        export_sql.COHORT_CLIP_SQL,
        export_sql.COHORT_CLIP_TAGS_SQL,
    ):
        for column in ("broker_name", "broker_phone", "broker_email"):
            assert column not in sql


def test_exported_columns_cover_listing_columns_exactly_once() -> None:
    promoted = set(export_sql.PROMOTED_COLUMNS)
    attrs = set(export_sql.ATTR_COLUMNS)
    assert not promoted & attrs
    assert promoted | attrs == set(LISTING_COLUMNS)
    assert "description" in promoted  # exported, but only after scrubbing


def test_broker_key_prefers_identity_then_phone_then_domain() -> None:
    by_id = export.broker_key(broker_identity_id=7, phone="777123456", email="a@b.cz")
    assert by_id == export.broker_key(broker_identity_id=7)
    by_phone = export.broker_key(phone="+420 777 123 456")
    assert by_phone == export.broker_key(phone="777123456") != by_id
    by_domain = export.broker_key(email="Petr.Novak@Reality.CZ")
    assert by_domain == export.broker_key(email="jana@reality.cz")
    assert export.broker_key() is None
    assert len(by_id or "") == 64


def test_broker_key_is_salted() -> None:
    assert export.broker_key(broker_identity_id=7, salt="x") != export.broker_key(
        broker_identity_id=7, salt="y"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "volejte +420 777 123 456 kdykoliv",
        "tel: 777123456",
        "mobil 777 123 456",
        "kontakt 603-123-456",
        "volejte 603.123.456 prosím",
        "Tel: 00420777123456",
        "tel 00420 777 123 456",
        "volejte 420777123456",
    ],
)
def test_scrubber_removes_phone_shapes(raw: str) -> None:
    out = export.scrub_description(raw)
    assert export.PHONE_TOKEN in (out or "")
    assert "123456" not in (out or "").replace(" ", "")


def test_scrubber_removes_emails_and_names() -> None:
    raw = "Napište na petr.novak+re@reality.cz nebo Ing. Petr Novák, makléř Jana Malá."
    out = export.scrub_description(raw) or ""
    assert "@" not in out
    assert "Novák" not in out
    assert "Jana Malá" not in out
    assert export.EMAIL_TOKEN in out and export.NAME_TOKEN in out


def test_scrubber_keeps_prices_and_the_rest_of_the_text() -> None:
    raw = "Prodej bytu 3+kk, 68,5 m², cena 4 500 000 Kč, po rekonstrukci."
    assert export.scrub_description(raw) == raw
    assert export.scrub_description(None) is None
    assert export.scrub_description("") == ""


def test_normalize_phone_folds_the_czech_country_code_and_only_that() -> None:
    assert export.normalize_phone("+420 777 123 456") == "777123456"
    assert export.normalize_phone("00420777123456") == "777123456"
    assert export.normalize_phone("12345") is None
    assert export.normalize_phone(None) is None
    # A foreign number must not be truncated onto a Czech-shaped key: broker_key is the BRK
    # evidence family's identity (E11) and a collision manufactures a same_broker.
    assert export.normalize_phone("447700900123") is None
    assert export.normalize_phone("12345678901234") is None
    assert export.broker_key(phone="+44 7700 900123") != export.broker_key(phone="700900123")


# --- derived fields ----------------------------------------------------------------------


def test_street_key_deaccents_and_folds() -> None:
    assert export.street_key("Náměstí Míru 3") == "namesti miru 3"
    assert export.street_key("  ") is None
    assert export.street_key(None) is None


def test_house_number_joins_cp_and_co() -> None:
    assert export.house_number("123", "4a") == "123/4a"
    assert export.house_number("123", None) == "123"
    assert export.house_number(None, "4") == "4"
    assert export.house_number(None, None) is None


def test_price_events_keeps_distinct_consecutive_changes_newest_end_first() -> None:
    rows = [
        {"scraped_at": "2024-01-01T00:00:00+00:00", "price_czk": 100},
        {"scraped_at": "2024-01-02T00:00:00+00:00", "price_czk": 100},
        {"scraped_at": "2024-01-03T00:00:00+00:00", "price_czk": 90},
        {"scraped_at": "2024-01-04T00:00:00+00:00", "price_czk": None},
        {"scraped_at": "2024-01-05T00:00:00+00:00", "price_czk": 80},
    ]
    assert export.price_events(rows) == [
        ["2024-01-01T00:00:00+00:00", 100],
        ["2024-01-03T00:00:00+00:00", 90],
        ["2024-01-05T00:00:00+00:00", 80],
    ]
    capped = export.price_events(rows, limit=2)
    assert capped == [["2024-01-03T00:00:00+00:00", 90], ["2024-01-05T00:00:00+00:00", 80]]


def test_clip_float16_round_trip() -> None:
    values = [i / 1000 for i in range(export.CLIP_DIMS)]
    encoded = export.encode_clip("[" + ",".join(str(v) for v in values) + "]")
    assert encoded is not None
    assert len(__import__("base64").b64decode(encoded)) == 2 * export.CLIP_DIMS
    decoded = export.decode_clip(encoded)
    assert decoded is not None and len(decoded) == export.CLIP_DIMS
    for original, back in zip(values, decoded):
        assert abs(original - back) <= 0.001
    assert export.encode_clip(values) == encoded
    assert export.decode_clip(export.encode_clip(tuple(values))) == decoded


def test_clip_encoder_refuses_anything_that_is_not_512_numbers() -> None:
    assert export.encode_clip(None) is None
    assert export.encode_clip("[]") is None
    assert export.encode_clip("[1,2,3]") is None
    assert export.encode_clip("[oops]") is None
    assert export.encode_clip(42) is None
    assert export.decode_clip(None) is None
    # float16 cannot hold these, and a NaN would poison every cosine downstream.
    too_big = [0.0] * export.CLIP_DIMS
    too_big[3] = 1e5
    assert export.encode_clip(too_big) is None
    nan = [0.0] * export.CLIP_DIMS
    nan[0] = float("nan")
    assert export.encode_clip(nan) is None
    assert export.encode_clip([float("inf")] * export.CLIP_DIMS) is None


def test_clip_encoding_is_little_endian_half_floats() -> None:
    values = [0.0] * export.CLIP_DIMS
    values[0] = 1.0
    raw = __import__("base64").b64decode(export.encode_clip(values) or "")
    assert raw[:2] == struct.pack("<e", 1.0)


def test_tag_pairs_put_the_logical_room_type_first() -> None:
    """`image_clip_tags.logical_tag` is a ROOM TYPE (`site_plan`), never a family
    (`plan`) — the family is `room_taxonomy.family_of()` over it (E10)."""
    rows = [{"logical_tag": "kitchen", "fine_tag": "kitchenette", "confidence": 0.5}]
    assert export.tag_pairs(rows) == [["kitchen", 0.5], ["kitchenette", 0.5]]
    same = [{"logical_tag": "site_plan", "fine_tag": "site_plan", "confidence": None}]
    assert export.tag_pairs(same) == [["site_plan", None]]


def test_emitted_logical_tags_resolve_to_a_room_family() -> None:
    rows = [{"logical_tag": tag, "fine_tag": tag, "confidence": 1.0} for tag in ROOM_FAMILIES]
    emitted = [name for name, _ in export.tag_pairs(rows)]
    assert emitted == list(ROOM_FAMILIES)
    assert {family_of(name) for name in emitted} <= set(ROOM_FAMILIES.values())


# --- records ----------------------------------------------------------------------------


def _listing_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 11,
        "source": "sreality",
        "source_id_native": "42",
        "source_url": "https://www.sreality.cz/detail/42",
        "category_main": "byt",
        "category_type": "prodej",
        "subtype": "3+kk",
        "disposition": "3+kk",
        "area_m2": 68.5,
        "floor": 3,
        "total_floors": 5,
        "price_czk": 4_500_000,
        "price_unit": "total",
        "condition": "dobry",
        "description": "Volejte 777 123 456, e-mail petr@reality.cz",
        "first_seen_at": "2024-01-01T00:00:00+00:00",
        "last_seen_at": "2024-02-01T00:00:00+00:00",
        "inactive_at": None,
        "is_active": True,
        "broker_identity_id": 5,
        "broker_firm_id": 6,
        "broker_phone": "777123456",
        "broker_email": "petr@reality.cz",
    }
    row.update(overrides)
    return row


def test_listing_record_carries_no_contact_detail() -> None:
    record = export.build_listing_record(_listing_row(), block="jablonec", location=None)
    blob = json.dumps(record, ensure_ascii=False)
    assert "777123456" not in blob and "777 123 456" not in blob
    assert "petr@reality.cz" not in blob
    assert set(record) & {"broker_phone", "broker_email", "broker_name"} == set()
    assert record["broker_key"] and record["broker_identity_id"] == 5
    assert record["t"] == "listing" and record["block"] == "jablonec"
    assert record["price"] == 4_500_000
    assert record["attrs"] == {"price_unit": "total", "condition": "dobry"}
    assert record["location"]["obec_kod"] is None
    assert record["price_history"] == []


def test_image_record_carries_the_corpus_wide_phash_population() -> None:
    row = {"image_id": 1, "listing_id": 11, "sequence": 0, "storage_path": "a/b.jpg", "phash": 7}
    record = export.build_image_record(row, clip=None, tags=[["kitchen", 0.5]], pop={7: 19})
    assert record == {
        "t": "image",
        "listing_id": 11,
        "image_id": 1,
        "seq": 0,
        "storage_path": "a/b.jpg",
        "phash": 7,
        "pop": 19,
        "clip": None,
        "tags": [["kitchen", 0.5]],
    }
    unknown = export.build_image_record({**row, "phash": None}, clip=None, pop={})
    assert unknown["pop"] is None
    # pop=None is "the probe did not run": UNKNOWN, never "this photo is unique".
    assert export.build_image_record(row, clip=None, pop=None)["pop"] is None


# --- the artifact ------------------------------------------------------------------------


class _Noop:
    def __enter__(self) -> "_Noop":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_conn(handler: Any) -> Any:
    class Cur:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def execute(self, sql: str, params: Any = None) -> None:
            if "statement_timeout" in sql:
                self.rows = []
                return
            self.rows = handler(sql, params or {})

        @property
        def description(self) -> list[tuple[str]]:
            return [(k,) for k in (self.rows[0] if self.rows else {})]

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [tuple(r.values()) for r in self.rows]

        def fetchone(self) -> tuple[Any, ...] | None:
            return tuple(self.rows[0].values()) if self.rows else None

        def __enter__(self) -> "Cur":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    class Conn:
        def cursor(self) -> Cur:
            return Cur()

        def transaction(self) -> _Noop:
            return _Noop()

        def close(self) -> None:
            return None

    return Conn


_VECTOR = "[" + ",".join(["0.5"] * export.CLIP_DIMS) + "]"


def _export_handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    if sql is export_sql.COHORT_TOWN_IDS_SQL:
        return [{"listing_id": 11}] if params["code"] == 563510 else [{"listing_id": 12}]
    if sql is export_sql.COHORT_QUARTER_IDS_SQL:
        return [{"listing_id": 13}]
    if sql is export_sql.COHORT_NEGCTL_GROUPS_SQL:
        return [{"ruian_adm_kod": 99, "n_listings": 3, "n_floors": 2, "listing_ids": [14, 15, 16]}]
    if sql is export_sql.COHORT_LISTINGS_SQL:
        return [_listing_row(id=i) for i in params["ids"]]
    if sql is export_sql.COHORT_LOCATION_SQL:
        return [
            {
                "listing_id": params["ids"][0],
                "obec_kod": 563510,
                "obec_name": "Jablonec nad Nisou",
                "cast_obce_kod": None,
                "cast_obce_name": None,
                "granularity": "street",
                "granularity_rank": 6,
                "is_address_grain": False,
                "lat": 50.7,
                "lon": 15.2,
                "uncertainty_radius_m": 120,
                "street_name": "Náměstí Míru",
                "house_number_cp": "12",
                "house_number_co": "3",
                "psc": "46601",
                "ruian_adm_kod": 99,
                "country_status": "cz",
            }
        ]
    if sql is export_sql.COHORT_PRICE_HISTORY_SQL:
        return [
            {"listing_id": 11, "scraped_at": "2024-01-01T00:00:00+00:00", "price_czk": 100},
            {"listing_id": 11, "scraped_at": "2024-02-01T00:00:00+00:00", "price_czk": 90},
        ]
    if sql is export_sql.COHORT_IMAGES_SQL:
        return [
            {"image_id": 1, "listing_id": 11, "sequence": 0, "storage_path": "a.jpg", "phash": 7},
            {"image_id": 2, "listing_id": 11, "sequence": 1, "storage_path": "b.jpg", "phash": None},
        ]
    if sql is export_sql.COHORT_PHASH_POP_SQL:
        return [{"phash": 7, "n_listings": 19}]
    if sql is export_sql.COHORT_CLIP_COUNT_SQL:
        return [{"n": 1}]
    if sql is export_sql.COHORT_CLIP_SQL:
        return [{"image_id": 1, "embedding": _VECTOR}]
    if sql is export_sql.COHORT_CLIP_TAGS_SQL:
        assert params["model"] == export.DEFAULT_CLIP_MODEL
        return [
            {"image_id": 1, "fine_tag": "kitchenette", "logical_tag": "kitchen", "confidence": 0.9}
        ]
    raise AssertionError(f"unexpected statement: {sql[:60]}")


def _records(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def test_export_writes_meta_then_listings_then_images(tmp_path: Path) -> None:
    summary = export.run_export(_fake_conn(_export_handler), {}, tmp_path)
    records = _records(tmp_path / export.ARTIFACT_NAME)

    kinds = [r["t"] for r in records]
    assert kinds[0] == "meta"
    assert kinds.count("meta") == 1
    assert kinds == ["meta"] + ["listing"] * kinds.count("listing") + ["image"] * kinds.count("image")

    meta = records[0]
    assert meta["generator_version"] == export.GENERATOR_VERSION
    assert [b["key"] for b in meta["blocks"]] == ["jablonec", "turnov", "vysocany", "negctl"]
    assert meta["counts"] == {
        "listings": 6, "images": 2, "clip_vectors": 1, "snapshots": 2, "price_events": 2
    }
    assert meta["phash_pop_ok"] is True and meta["errors"] == []
    assert meta["counts"]["listings"] == kinds.count("listing")
    assert meta["counts"]["images"] == kinds.count("image")
    assert summary["counts"] == meta["counts"]
    assert summary["bytes"] > 0 and summary["errors"] == []
    assert [b["n_listings"] for b in summary["blocks"]] == [1, 1, 1, 3]
    # the stream is renamed onto its final path only once it closes cleanly
    assert not (tmp_path / (export.ARTIFACT_NAME + ".part")).exists()


def test_export_record_fields_match_the_contract(tmp_path: Path) -> None:
    export.run_export(_fake_conn(_export_handler), {}, tmp_path)
    records = _records(tmp_path / export.ARTIFACT_NAME)
    listings = {r["id"]: r for r in records if r["t"] == "listing"}
    images = [r for r in records if r["t"] == "image"]

    assert {r["block"] for r in listings.values()} == {"jablonec", "turnov", "vysocany", "negctl"}
    first = listings[11]
    assert first["price_history"] == [
        ["2024-01-01T00:00:00+00:00", 100],
        ["2024-02-01T00:00:00+00:00", 90],
    ]
    assert first["location"]["street_key"] == "namesti miru"
    assert first["location"]["house_number"] == "12/3"
    assert first["location"]["granularity_rank"] == 6
    assert set(first["location"]) == {
        "obec_kod", "obec_name", "cast_obce_kod", "cast_obce_name", "granularity",
        "granularity_rank", "is_address_grain", "lat", "lon", "uncertainty_radius_m",
        "street_key", "house_number", "psc", "ruian_adm_kod", "country_status",
    }
    assert export.PHONE_TOKEN in first["description"]

    assert [i["image_id"] for i in images] == [1, 2]
    assert images[0]["pop"] == 19 and images[1]["pop"] is None
    assert export.decode_clip(images[0]["clip"])[:1] == [0.5]
    assert images[0]["tags"] == [["kitchen", 0.9], ["kitchenette", 0.9]]
    assert images[1]["clip"] is None


def test_export_blocks_argument_narrows_the_cohort(tmp_path: Path) -> None:
    summary = export.run_export(
        _fake_conn(_export_handler), {"blocks": "town:563510"}, tmp_path
    )
    assert summary["counts"]["listings"] == 1
    meta = _records(tmp_path / export.ARTIFACT_NAME)[0]
    assert [b["key"] for b in meta["blocks"]] == ["town563510"]


def test_export_survives_a_phash_population_timeout(tmp_path: Path) -> None:
    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if sql is export_sql.COHORT_PHASH_POP_SQL:
            raise RuntimeError("canceling statement due to statement timeout")
        return _export_handler(sql, params)

    summary = export.run_export(_fake_conn(handler), {}, tmp_path)
    records = _records(tmp_path / export.ARTIFACT_NAME)
    images = [r for r in records if r["t"] == "image"]
    assert summary["errors"] and "phash_pop" in summary["errors"][0]
    assert summary["phash_pop_ok"] is False
    # E9's catalog subtraction has no input, and the ARTIFACT says so — a 0 would read as
    # "every photo is unique" to a consumer who only ever sees the file.
    assert all(image["pop"] is None for image in images)
    meta = records[0]
    assert meta["phash_pop_ok"] is False
    assert meta["errors"] and "phash_pop" in meta["errors"][0]


def test_a_failed_write_leaves_no_artifact_to_mistake_for_a_whole_one(tmp_path: Path) -> None:
    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if sql is export_sql.COHORT_CLIP_SQL:
            raise RuntimeError("server closed the connection unexpectedly")
        return _export_handler(sql, params)

    with pytest.raises(RuntimeError):
        export.run_export(_fake_conn(handler), {}, tmp_path)
    assert not (tmp_path / export.ARTIFACT_NAME).exists()


@pytest.mark.parametrize(
    "args",
    [
        {"nope": "1"},
        {"timeout_s": "x"},
        {"timeout_s": "0"},
        {"batch": "0"},
        {"negctl_max": "-1"},
        {"negctl_max": "80000"},
    ],
)
def test_export_args_are_validated(args: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        export.parse_export_args(args)


def test_export_args_defaults() -> None:
    params = export.parse_export_args({})
    assert params["clip_model"] == export.DEFAULT_CLIP_MODEL
    assert params["batch"] == 1000
    assert params["negctl_max"] == cohort.NEGATIVE_CONTROL.max_listings
