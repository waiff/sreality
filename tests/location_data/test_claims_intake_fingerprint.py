"""`claim_fingerprint` parity — why it is computed in SQL, and what the mirror is for.

01 §4.2.1 defines the fingerprint over a tuple that includes
`coalesce(value_norm, value_text, '')`, and `value_norm` is written by migration 382's
`location_value_norm()`:

    nullif(btrim(regexp_replace(lower(unaccent(p_value)), '[^a-z0-9]+', ' ', 'g')), '')

The fingerprint carries a UNIQUE index, so it is the dedup mechanism for an APPEND-ONLY
table. If a Python computation of it disagreed with the SQL one by a single byte, the
index would stop matching and the same value would be re-inserted on every run — silently,
and unrecoverably.

Python cannot guarantee that agreement. PostgreSQL's `unaccent` is a DICTIONARY, not a
Unicode decomposition: it additionally maps ß→ss, æ→ae, ø→o, đ→d, ł→l, þ→th …, which an
NFKD combining-mark strip leaves untouched (and the `[^a-z0-9]+` class then folds to a
space). Those characters are rare in Czech and NOT rare in this corpus — remax 442804 is
genuinely in Poland, bazos carries an 835-row `Zahraničí` bucket, and foreign-address
detection is a stated D1 goal.

DECISION (recorded in the PR): the fingerprint is computed IN SQL, inside the same
statement that writes the claim, from the same `location_value_norm()` the column uses.
`value_norm_mirror()` stays as a DIAGNOSTIC, and this test is what keeps it honest —
including by naming the exact character set on which it may not be trusted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from location_data.claims_intake import (
    _CLAIM_FINGERPRINT_SQL,
    _CLAIM_WRITE_SQL,
    MIRROR_UNSAFE_CHARS,
    mirror_is_faithful,
    value_norm_mirror,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_382 = REPO_ROOT / "migrations" / "382_location_w1_claims.sql"

# The body as migration 382 (PR-A) declares it. Pinned here so a change to the definition
# on either side is a failing test rather than a silent divergence.
EXPECTED_VALUE_NORM_BODY = (
    "select case when p_value is null then null else "
    "nullif(btrim(regexp_replace(lower(unaccent(p_value)), '[^a-z0-9]+', ' ', 'g')), '') end"
)

# Diacritics, punctuation, case, whitespace — the four axes the SQL body touches.
CZECH_BATTERY = (
    ("náměstí Jiřího z Poděbrad", "namesti jiriho z podebrad"),
    ("Křižíkova 148/34", "krizikova 148 34"),
    ("  Údolní   ", "udolni"),
    ("ŽIŽKOV", "zizkov"),
    ("Plzeň-město", "plzen mesto"),
    ("U Smaltovny 22a", "u smaltovny 22a"),
    ("Vršovické náměstí", "vrsovicke namesti"),
    ("28. října", "28 rijna"),
    ("Krásný Les u Frýdlantu", "krasny les u frydlantu"),
    ("Hořice v Podkrkonoší", "horice v podkrkonosi"),
    ("Ústí nad Labem", "usti nad labem"),
    ("Praha 3 – Žižkov", "praha 3 zizkov"),
    ("---", None),
    ("", None),
    ("Ďáblice/Ďáblická", "dablice dablicka"),
    ("Nová zlatá míle", "nova zlata mile"),
)


def _extract_sql_body(text: str) -> str:
    match = re.search(
        r"create function location_value_norm\(p_value text\) returns text\s*"
        r"language sql stable as \$fn\$(?P<body>.*?)\$fn\$",
        text, re.DOTALL | re.IGNORECASE)
    assert match, "location_value_norm is not declared as expected in migration 382"
    return " ".join(match.group("body").split())


@pytest.mark.parametrize("value,expected", CZECH_BATTERY)
def test_python_mirror_matches_the_sql_semantics_on_czech_text(value, expected):
    assert mirror_is_faithful(value)
    assert value_norm_mirror(value) == expected


def test_migration_382_still_declares_the_body_this_mirror_was_written_against():
    if not MIGRATION_382.exists():
        pytest.skip("migration 382 (location W1 PR-A) is not merged into this branch yet")
    assert _extract_sql_body(MIGRATION_382.read_text(encoding="utf-8")) == (
        EXPECTED_VALUE_NORM_BODY)


def test_the_mirror_declares_where_it_cannot_be_trusted():
    """These are the characters PostgreSQL's unaccent dictionary EXPANDS and Python's NFKD
    strip does not — the reason the fingerprint is not computed in Python."""
    assert "ß" in MIRROR_UNSAFE_CHARS
    assert not mirror_is_faithful("Straße")
    assert not mirror_is_faithful("Wiechowice, ul. Główna 12ł")
    assert mirror_is_faithful("Hlavní město Praha")
    # NFKD leaves ß intact, so the punctuation class folds it to a space; PostgreSQL's
    # dictionary would have produced 'strasse'. Demonstrated, not asserted as correct.
    assert value_norm_mirror("Straße") == "stra e"


def test_the_write_path_computes_the_fingerprint_in_sql_from_the_same_function():
    """The mirror is diagnostic; the claim writer must never use it."""
    assert "location_value_norm(i.value_text) AS value_norm" in _CLAIM_WRITE_SQL
    assert "sha256(convert_to(jsonb_build_array(" in _CLAIM_FINGERPRINT_SQL
    assert _CLAIM_FINGERPRINT_SQL.strip() in _CLAIM_WRITE_SQL
    assert "ON CONFLICT (claim_fingerprint) DO NOTHING" in _CLAIM_WRITE_SQL


def test_the_fingerprint_tuple_is_the_one_01_declares_and_is_time_free():
    """01 §4.2.1: values dedupe, occurrences are their own series. Time in the tuple would
    mean one claim row per unchanged value per snapshot — order 10 M+ rows for sreality
    alone."""
    tuple_sql = _CLAIM_FINGERPRINT_SQL
    for column in (
        "t.listing_id", "t.source", "t.source_id_native", "t.claim_type", "t.surface",
        "t.page_kind", "t.extraction_method", "t.extractor_id", "t.extractor_version",
        "t.contract_entry_id", "coalesce(t.value_norm, t.value_text, '')", "t.value_num",
        "ST_AsEWKB(t.geom)", "ST_AsEWKB(t.shape)", "t.value_jsonb", "t.distance_m",
        "t.travel_mode", "t.target_text", "t.declared_precision_label",
        "t.declared_confidence", "t.declared_radius_m", "t.legacy_source_column",
    ):
        assert column in tuple_sql, column
    for forbidden in ("first_observed_at", "extracted_at", "payload_sha256", "snapshot_id",
                      "span_start", "span_end", "batch_id"):
        assert forbidden not in tuple_sql, forbidden
