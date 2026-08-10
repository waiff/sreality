"""VFR daily change files: the header contract, the chain proof, and the honest stub."""

from __future__ import annotations

import datetime
import io
import zipfile
from pathlib import Path

import pytest

from location_data import vfr_delta

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "location_data"
HEADER_XML = (FIXTURES / "vfr_header_sample.xml").read_text(encoding="utf-8")


def test_header_parses_the_six_scalars_the_chain_proof_needs():
    header = vfr_delta.parse_header(HEADER_XML)
    assert header.vfr_version == "3.1"
    assert header.batch_type == "Prirustek"
    assert header.file_type == "ST_ZZSZ"
    assert header.stamp == "2026-08-08T00:00:01"
    assert header.previous_file == "20260806_ST_ZZSZ.xml.zip"
    assert header.transaction_from == 8217302
    assert header.transaction_to == 8220073


def test_missing_version_is_schema_drift():
    with pytest.raises(vfr_delta.SchemaDrift):
        vfr_delta.parse_header("<vf:Hlavicka></vf:Hlavicka>")


def test_version_drift_stops_the_lane():
    header = vfr_delta.parse_header(HEADER_XML.replace("3.1", "4.0"))
    with pytest.raises(vfr_delta.SchemaDrift):
        vfr_delta.assert_supported(header)


def test_chain_verification_accepts_the_expected_predecessor():
    header = vfr_delta.parse_header(HEADER_XML)
    vfr_delta.verify_chain(header, "20260806_ST_ZZSZ.xml.zip")


def test_chain_break_is_a_hard_stop():
    header = vfr_delta.parse_header(HEADER_XML)
    with pytest.raises(vfr_delta.ChainBreak):
        vfr_delta.verify_chain(header, "20260805_ST_ZZSZ.xml.zip")


def test_first_delta_after_a_baseline_has_nothing_to_chain_to():
    vfr_delta.verify_chain(vfr_delta.parse_header(HEADER_XML), None)


@pytest.mark.parametrize(
    "day,size,expected",
    [
        (datetime.date(2026, 8, 2), 1_943, True),      # Sunday, measured near-empty
        (datetime.date(2026, 8, 8), 129_599, True),    # Saturday, measured near-empty
        (datetime.date(2026, 8, 7), 443_020, False),   # Friday, a real file
        (datetime.date(2026, 8, 9), 1_774_366, False),  # Sunday but a genuine payload
    ],
)
def test_weekend_near_empty_files_are_normal(day, size, expected):
    assert vfr_delta.is_weekend_empty(day, size) is expected


def test_summarize_counts_both_signals():
    counts = vfr_delta.summarize(HEADER_XML)
    assert counts == {"address_points": 1, "retirements": 1}


def test_read_xml_from_a_zip():
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as zf:
        zf.writestr("20260807_ST_ZZSZ.xml", HEADER_XML)
    assert "PredchoziSoubor" in vfr_delta.read_xml(blob.getvalue())


def test_applying_a_delta_fails_loudly_rather_than_guessing():
    header = vfr_delta.parse_header(HEADER_XML)
    with pytest.raises(vfr_delta.DeltaApplyNotImplemented) as exc:
        vfr_delta.apply_delta(None, header, HEADER_XML)
    message = str(exc.value)
    assert "TypPrvkuKod" in message and "NOT NULL" in message


def test_url_grammar():
    assert vfr_delta.ZZSZ_URL.format(date="20260809").endswith(
        "/vymenny_format/soucasna/20260809_ST_ZZSZ.xml.zip"
    )
