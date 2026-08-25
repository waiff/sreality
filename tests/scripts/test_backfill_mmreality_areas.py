"""derive() rules of the mmreality area heal — the plot that was stored as the
headline while the parcel column stayed empty.

mmreality stores its whole source object in `raw_json`, so the heal is a
re-derive, not a refetch: these are the exact inputs the fixed parser sees.
"""

from __future__ import annotations

from scripts.backfill_mmreality_areas import (
    _COLS,
    _COUNT_SQL,
    _PAGE_IDS_SQL,
    _PAYLOAD_SQL,
    _RAW_KEYS,
    changed_columns,
    derive,
)


def _stored(**over):
    row = {c: None for c in _COLS}
    row.update(over)
    return row


def test_house_headline_becomes_the_usable_area():
    out = derive("dum", {"usableArea": 130, "totalArea": 905})
    assert out["area_m2"] == 130.0
    assert out["area_basis"] == "usable"
    assert out["usable_area"] == 130.0


def test_house_plot_is_rescued_into_estate_area():
    # The parcel was thrown away at the same moment it was being misused as the
    # denominator: count(estate_area) was ZERO across all 11,218 mmreality rows.
    out = derive("dum", {"usableArea": 130, "totalArea": 905})
    assert out["estate_area"] == 905.0


def test_house_without_usable_area_lands_null_not_the_parcel():
    # 13 of 3,601 active houses. A parcel stamped 'total' would be a worse lie
    # than a missing value, so the headline goes NULL and the plot still lands.
    out = derive("dum", {"totalArea": 1200})
    assert out["area_m2"] is None
    assert out["area_basis"] is None
    assert out["estate_area"] == 1200.0


def test_flat_total_area_is_a_real_interior_measure():
    # Only `dum` mis-used totalArea: on byt/komercni/ostatni mmreality's two
    # labels carry the same number (measured: 0 rows where they differ).
    out = derive("byt", {"totalArea": 68})
    assert out["area_m2"] == 68.0
    assert out["area_basis"] == "total"
    assert out["estate_area"] is None


def test_flat_prefers_usable_over_total():
    out = derive("byt", {"usableArea": 64, "totalArea": 68})
    assert (out["area_m2"], out["area_basis"]) == (64.0, "usable")


def test_land_headline_is_the_parcel_and_stays_out_of_estate_area():
    out = derive("pozemek", {"totalArea": 2400})
    assert (out["area_m2"], out["area_basis"]) == (2400.0, "plot")
    assert out["estate_area"] is None


def test_structured_plot_beats_the_house_total_area():
    out = derive("dum", {"usableArea": 140, "totalArea": 900, "landArea": 875})
    assert out["estate_area"] == 875.0


def test_garden_area_rides_along():
    out = derive("dum", {"usableArea": 140, "totalArea": 900, "gardenArea": 320})
    assert out["garden_area"] == 320.0


def test_string_measures_parse_like_the_parser():
    out = derive("dum", {"usableArea": "130,5", "totalArea": "905"})
    assert out["area_m2"] == 130.5


def test_changed_columns_names_only_the_disagreements():
    raw = {"usableArea": 130, "totalArea": 905}
    stored = _stored(area_m2=905.0, usable_area=130.0)
    changed = changed_columns("dum", raw, stored)
    assert changed == {"area_m2": 130.0, "area_basis": "usable", "estate_area": 905.0}


def test_changed_columns_is_empty_once_healed():
    raw = {"usableArea": 130, "totalArea": 905}
    stored = _stored(area_m2=130.0, area_basis="usable",
                     usable_area=130.0, estate_area=905.0)
    assert changed_columns("dum", raw, stored) == {}


def test_a_basis_only_stamp_is_a_change():
    # area_basis is NULL on all of mmreality today and is OUT of _HASH_FIELDS,
    # so stamping it heals the label without churning a snapshot.
    raw = {"totalArea": 68}
    stored = _stored(area_m2=68.0)
    assert changed_columns("byt", raw, stored) == {"area_basis": "total"}


def test_the_projected_keys_are_the_ones_derive_reads():
    # The SELECT projects five raw_json keys instead of the whole 17 kB object;
    # a key dropped from _RAW_KEYS would silently read as absent, so the
    # projection and the derive have to be checked against each other.
    from scripts.backfill_mmreality_areas import _RAW_KEYS
    assert set(_RAW_KEYS) == {
        "usableArea", "totalArea", "landArea", "plotArea", "gardenArea",
    }


def test_derive_reads_the_projected_string_shape():
    # `raw_json->>'k'` hands back TEXT, never a JSON number, so that is the shape
    # production actually feeds derive().
    raw = dict(zip(
        ("usableArea", "totalArea", "landArea", "plotArea", "gardenArea"),
        ("130.0", "905.0", None, None, "320.0"),
    ))
    out = derive("dum", raw)
    assert (out["area_m2"], out["estate_area"], out["garden_area"]) == (130.0, 905.0, 320.0)


def _payload_select_list() -> list[str]:
    """The output column aliases of _PAYLOAD_SQL, in order."""
    body = _PAYLOAD_SQL.split("SELECT", 1)[1].split("FROM", 1)[0]
    out = []
    for item in body.split(","):
        item = item.strip()
        if not item:
            continue
        out.append(item.split(" AS ")[-1].strip() if " AS " in item
                   else item.split(".")[-1].strip())
    return out


def test_payload_projection_matches_the_positional_unpacking():
    # main() slices the payload row positionally: row[3:8] -> _COLS,
    # row[8:13] -> _RAW_KEYS, row[13] -> the resume marker. Nothing else pins
    # that mapping, and getting it wrong would silently write one area column's
    # value into another. Re-ordering the SELECT must fail here, loudly.
    cols = _payload_select_list()
    assert cols[:3] == ["id", "category_main", "property_id"]
    assert tuple(cols[3:8]) == _COLS
    assert tuple(cols[8:13]) == tuple(f"raw_{k}" for k in _RAW_KEYS)
    assert cols[13] == "marker"
    assert len(cols) == 14


def test_page_ids_query_touches_no_wide_column():
    # The whole point of the two-statement page: step 1 must not name raw_json,
    # or the planner is free to detoast the corpus before the LIMIT applies —
    # which is exactly what got the single-statement form cancelled in prod.
    assert "raw_json" not in _PAGE_IDS_SQL
    assert "ORDER BY l.id" in _PAGE_IDS_SQL
    assert "LIMIT" in _PAGE_IDS_SQL


def test_corpus_count_does_not_filter_on_the_marker():
    # Counting the un-stamped set costs a full detoast of every mmreality row;
    # that count(*) is what the first live dispatch died on.
    assert "area_reparse_v2" not in _COUNT_SQL
