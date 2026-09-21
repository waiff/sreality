"""Hermetic rails for `scraper/reas_parser.py`. No network, no DB.

The fixtures are real records, hand-trimmed (see the banner at the top of
`tests/fixtures/reas/sold_list.html`). Everything asserted below was measured over 308
saved sold records; the numbers in the docstrings are that census, not guesses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scraper.reas_parser import (
    ReasPayloadError,
    SoldTransaction,
    parse_sold_html,
    parse_sold_payload,
)
from toolkit.filter_registry import DISPOSITION_OPTIONS, SUBTYPE_OPTIONS

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _ROOT / "tests" / "fixtures" / "reas"
_MIGRATION = _ROOT / "migrations" / "542_sold_transactions.sql"

# The lat/lng box the Olomouc page was fetched with (half-side 10 km), as
# `geoLinks.ts` writes it: south-west lat/lng, north-east lat/lng.
_OLOMOUC_BOX = (49.503969, 17.112315, 49.683631, 17.389485)
_OLOMOUC_RECORD = "108473925010_unit_22906291-105-37"


def _page():
    return parse_sold_html((_FIXTURES / "sold_list.html").read_text(encoding="utf-8"))


def _by_id() -> dict[str, SoldTransaction]:
    return {row.source_record_id: row for row in _page().rows}


def _next_data() -> dict:
    html = (_FIXTURES / "sold_list.html").read_text(encoding="utf-8")
    blob = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    assert blob is not None
    return json.loads(blob.group(1))


def _record(map_pointer_id: str) -> dict:
    records = _next_data()["props"]["pageProps"]["adsListResult"]["data"]
    return next(r for r in records if r["mapPointerId"] == map_pointer_id)


def _payload(records: list[dict], **envelope) -> dict:
    return {
        "props": {
            "pageProps": {
                "adsListParams": {"linkedToTransfer": True},
                "adsListResult": {
                    "count": len(records), "nextPage": None, "data": records, **envelope
                },
            }
        }
    }


# --- the envelope -----------------------------------------------------------


def test_envelope_is_the_sources_own_numbers_not_derived_from_data():
    """`count` and `nextPage` are what the source declared — a caller paginates on them
    and writes them to the fetch ledger. The fixture's `data` is trimmed to 13 records
    against a real `count` of 89, so a parser that returned `len(rows)` would fail here."""
    page = _page()
    assert page.count == 89
    assert page.next_page == 2
    assert len(page.rows) == 11


# --- the refusals -----------------------------------------------------------


def test_the_active_catalogue_served_under_a_200_is_refused():
    """reas's `_next/data/<buildId>/prodane/...` route loses the `/prodane` rewrite and
    answers 200 with the ACTIVE catalogue: `linkedToTransfer: false`, zero soldPrice on
    every record. Stored blind it would be a page of live asking prices filed as sales."""
    trap = json.loads(
        (_FIXTURES / "active_catalog_next_data.json").read_text(encoding="utf-8")
    )
    assert trap["pageProps"]["adsListParams"]["linkedToTransfer"] is False
    assert all(r.get("soldPrice") is None for r in trap["pageProps"]["adsListResult"]["data"])

    with pytest.raises(ReasPayloadError, match="linkedToTransfer"):
        parse_sold_payload(trap)


def test_an_unknown_estate_type_is_refused():
    """The sold catalogue is flats and houses only. A fifth type is a contract change."""
    record = dict(_record(_OLOMOUC_RECORD), type="parcel", subType="building_plot")
    with pytest.raises(ReasPayloadError, match="unknown reas type"):
        parse_sold_payload(_payload([record]))


def test_a_record_without_a_cadastral_transfer_identity_is_refused():
    """A transferId that is neither a number nor an ObjectId means the identity grammar
    moved. Storing the row anyway would file a sale under a key we cannot explain."""
    record = dict(_record(_OLOMOUC_RECORD), mapPointerId="AD-1234_unit_9-9-9")
    with pytest.raises(ReasPayloadError, match="no cadastral transfer id"):
        parse_sold_payload(_payload([record]))


def test_broker_reported_records_are_dropped_and_counted_not_refused():
    """~1% of the feed is reas's own self-reported sale rather than a cadastre transfer,
    recognisable because its transferId is a 24-hex Mongo ObjectId instead of a number.
    A different kind of claim, so out of a table of registered transfers — but normal,
    so it must not fail the page."""
    page = _page()
    assert page.dropped_broker_reported == 2
    assert not [
        row for row in page.rows if not row.source_record_id.split("_", 1)[0].isdigit()
    ]


def test_an_eleven_digit_transfer_id_is_still_cadastre_derived():
    """Two of 308 records carry an 11-digit transferId, not the 12 digits the source
    survey generalised to. The rule is "all digits", never a length."""
    row = _by_id()["99964975010_unit_22948236-565-54"]
    assert row.price_czk == 2850000


# --- the mapping table ------------------------------------------------------


def test_type_and_subtype_map_onto_the_existing_vocabulary():
    rows = _by_id()
    assert rows[_OLOMOUC_RECORD].category_main == "byt"
    assert rows[_OLOMOUC_RECORD].subtype is None
    assert rows["108651789010_building_23214937"].category_main == "dum"

    observed = {
        "108651789010_building_23214937": "rodinny_dum",
        "107862090010_building_21802688": "vila",
        "104087196010_building_26503794": "chata",
        "108626338010_building_15362680": "chalupa",
    }
    for map_pointer_id, subtype in observed.items():
        assert rows[map_pointer_id].subtype == subtype

    targets = {option.value for option in SUBTYPE_OPTIONS}
    assert {s for s in observed.values()} <= targets


def test_every_row_is_a_sale():
    assert {row.category_type for row in _page().rows} == {"prodej"}


def test_dispositions_pass_through_and_the_two_unmappable_ones_become_null():
    """Ten of reas's twelve dispositions are our spelling exactly. `larger` ("5 and
    above, room count not stated") and `atypic` have no target, and widening the shared
    enum for 0.68% of one source would push both into Browse, the watchdog matcher and
    the comparables agent."""
    rows = _by_id()
    assert rows[_OLOMOUC_RECORD].disposition == "3+1"
    assert _record("106193482010_unit_24834289-2091-14")["disposition"] == "larger"
    assert rows["106193482010_unit_24834289-2091-14"].disposition is None
    assert _record("100108315010_unit_22311769-810-648")["disposition"] == "atypic"
    assert rows["100108315010_unit_22311769-810-648"].disposition is None

    allowed = {option.value for option in DISPOSITION_OPTIONS}
    assert {row.disposition for row in _page().rows} - {None} <= allowed


def test_a_house_has_no_disposition():
    """NULL on 84/84 buildings — reas's model, not sparsity: a house has no disposition."""
    rows = _by_id()
    for map_pointer_id, row in rows.items():
        if row.category_main == "dum":
            assert row.disposition is None, map_pointer_id


def test_missing_street_id_is_null_not_a_failure():
    """`streetId` is absent on 9.7% of records; the obec and cadastral-area codes are
    always there, and those are what a cell fetch and a cohort read key on."""
    row = _by_id()["108696291010_unit_17223261-41-1"]
    assert row.ulice_kod is None
    assert (row.obec_kod, row.ku_kod) == (552666, 691917)


# --- areas ------------------------------------------------------------------


def test_the_headline_area_is_usable_first_through_the_shared_resolver():
    """`utilityArea` -> usable, `floorArea` -> floor, fed to `derive_headline_area`. On
    the 3% of flats carrying both and disagreeing, usable wins — reas's own headline
    takes the SMALLER, which is up to a 30% area and 43% per-m² gap in one direction."""
    rows = _by_id()
    flat = rows["108627247010_unit_23333413-695-11"]
    assert (_record(flat.source_record_id)["utilityArea"],
            _record(flat.source_record_id)["floorArea"]) == (23, 28)
    assert (flat.area_m2, flat.area_basis) == (23.0, "usable")

    house = rows["108702555010_building_4459091"]
    assert (house.area_m2, house.area_basis) == (133.0, "usable")


def test_a_building_without_a_utility_area_falls_to_floor_with_an_honest_basis():
    """8 of 84 buildings carry only `floorArea`. The basis stamp is what keeps the
    per-m² measure honest about which physical area its denominator is."""
    row = _by_id()["104087196010_building_26503794"]
    assert _record(row.source_record_id).get("utilityArea") is None
    assert (row.area_m2, row.area_basis) == (34.0, "floor")
    assert row.usable_area is None


def test_land_area_lands_in_estate_area_and_only_on_houses():
    """`landArea` is 84/84 buildings and 0/224 flats, so `plot_area_m2` returns the
    parcel for a house and NULL for a flat with no further work."""
    rows = _by_id()
    assert rows["108626338010_building_15362680"].estate_area == 896.0
    for row in rows.values():
        if row.category_main == "byt":
            assert row.estate_area is None, row.source_record_id


# --- price, date, point -----------------------------------------------------


def test_the_price_is_the_sold_price_and_the_asking_price_is_labelled_as_one():
    row = _by_id()["107862090010_building_21802688"]
    source = _record(row.source_record_id)
    assert (source["soldPrice"], source["price"]) == (29300000, 29990000)
    assert row.price_czk == 29300000
    assert row.asking_last_czk == 29990000


def test_sold_at_is_a_date():
    """The source sends a full ISO UTC timestamp whose time component is its
    batch-ingest clock (03:00-17:00 UTC, Czech business hours), not a legal-effects
    time. The cadastre records a date."""
    row = _by_id()[_OLOMOUC_RECORD]
    assert _record(_OLOMOUC_RECORD)["soldAt"] == "2026-08-04T07:30:56.000Z"
    assert str(row.sold_at) == "2026-08-04"
    assert not hasattr(row.sold_at, "hour")


def test_the_point_is_read_as_lng_lat():
    """`point.coordinates` is GeoJSON order. A transposed pair would still parse, still
    store and still answer a radius query — with nothing in it. The Olomouc record must
    land inside the lat/lng box its page was fetched with."""
    row = _by_id()[_OLOMOUC_RECORD]
    lng, lat = (float(v) for v in row.geom.removeprefix("SRID=4326;POINT(").rstrip(")").split())
    south, west, north, east = _OLOMOUC_BOX
    assert south <= lat <= north
    assert west <= lng <= east


def test_photo_urls_are_the_sources_own_urls_in_its_own_order():
    row = _by_id()[_OLOMOUC_RECORD]
    assert row.photo_urls == [
        image["original"] for image in _record(_OLOMOUC_RECORD)["imagesWithMetadata"]
    ]
    assert all(url.startswith("https://") for url in row.photo_urls)


# --- what is never stored ---------------------------------------------------


def test_the_modelled_price_and_area_reach_neither_a_column_nor_raw():
    """`histogramPrice` is `soldPrice` indexed to today (identity within 12 months,
    x1.10-1.28 beyond); `originalPrice` is corrupt (one record carries 1 Kc);
    `displayArea` is reas's own headline under a rule that is not ours. Keeping any of
    them in `raw` would leave a future session one mapping away from the defect."""
    fields = set(SoldTransaction.__dataclass_fields__)
    assert not fields & {"display_area", "histogram_price", "original_price"}

    for row in _page().rows:
        assert not set(row.raw) & {"displayArea", "histogramPrice", "originalPrice"}
        assert row.price_czk == row.raw["soldPrice"]


def test_seller_and_broker_identity_is_dropped_at_parse_time():
    """This repo is public and so is the fixture. The identity is not masked, it is
    removed — from the row, from `raw`, and from the committed bytes."""
    for row in _page().rows:
        assert not set(row.raw) & {"sellerDetails", "companyDetails"}

    payload = _next_data()["props"]["pageProps"]["adsListResult"]["data"]
    serialised = json.dumps(payload)
    for key in ("sellerDetails", "companyDetails", "avatarUrl", "logoUrl"):
        assert key not in serialised


def test_a_record_that_arrives_with_identity_still_loses_it():
    """The fixture is scrubbed, so the drop itself is proven on a record built here."""
    record = dict(
        _record(_OLOMOUC_RECORD),
        sellerDetails={"name": "Jan Novák", "slug": "jan-novak"},
        companyDetails={"name": "Reality s.r.o."},
    )
    (row,) = parse_sold_payload(_payload([record])).rows
    assert not set(row.raw) & {"sellerDetails", "companyDetails"}
    assert "Novák" not in json.dumps(row.raw, ensure_ascii=False)


# --- the row IS the table ---------------------------------------------------


def test_the_row_fields_are_exactly_the_sold_transactions_columns():
    """The parser's output shape is the store's shape; a column added on one side and
    not the other is a writer that silently drops a field."""
    body = re.search(
        r"create table if not exists sold_transactions \((.*?)\n\);",
        _MIGRATION.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert body is not None
    columns = []
    for line in body.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("--") or line.startswith("primary key"):
            continue
        columns.append(line.split()[0])

    # `fetched_at` is the writer's stamp (a column default); the parser fetched nothing.
    assert set(columns) - {"fetched_at"} == set(SoldTransaction.__dataclass_fields__)
