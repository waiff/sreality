"""The backup lane dumps exactly what a destructive migration names, and nothing else."""

from __future__ import annotations

import pytest

from scripts.backup_before_drop import Target, _copy_rows, column_copy_sql, parse_targets


def test_a_target_is_a_table_or_a_key_plus_columns() -> None:
    assert parse_targets(["property_tags", "listings:id,mf_gross_yield_pct"]) == [
        Target("property_tags", ()),
        Target("listings", ("id", "mf_gross_yield_pct")),
    ]
    assert Target("property_tags", ()).filename == "property_tags.sql.gz"
    assert Target("listings", ("id", "x")).filename == "listings.csv.gz"


@pytest.mark.parametrize("spec", [
    "listings:id",                 # a key with nothing to back up
    "public.listings",             # the schema is fixed: public
    "listings:id,mf; drop table",  # anything outside the identifier charset
    "Listings:id,x",
    "listings:",
])
def test_a_malformed_target_is_refused(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_targets([spec])


def test_a_table_named_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="twice"):
        parse_targets(["listings:id,a", "listings:id,b"])


def test_the_column_dump_keeps_only_rows_that_hold_a_value() -> None:
    """An absent row restores as NULL, which is what it was, so only set rows are kept."""
    target = parse_targets(["properties:id,mf_reference_rent_czk,mf_reference_rent"])[0]
    assert column_copy_sql(target).as_string(None) == (
        'COPY (SELECT "id", "mf_reference_rent_czk", "mf_reference_rent" FROM public."properties"'
        ' WHERE "mf_reference_rent_czk" IS NOT NULL OR "mf_reference_rent" IS NOT NULL'
        ' ORDER BY "id") TO STDOUT WITH (FORMAT csv, HEADER)'
    )


def test_the_table_dump_row_count_is_read_out_of_the_dump() -> None:
    dump = b"SET x;\nCOPY public.t (a) FROM stdin;\n1\n2\n\\.\nCOPY public.u (b) FROM stdin;\n3\n\\.\n"
    assert _copy_rows(dump) == 3
