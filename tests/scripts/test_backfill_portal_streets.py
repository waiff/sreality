"""derive() rules of the portal-streets backfill — the realitymix arm (2026-07):
first segment of the index-card locality_text, morphology-REQUIRED like the
parser's own data-address path (a leading místní část must never become a street)."""

from __future__ import annotations

from scripts.backfill_portal_streets import _COLS, _INPUT_PREDICATE, _SOURCES, derive


def _row(**over):
    row = {c: None for c in _COLS}
    row.update(over)
    return row


def test_realitymix_is_wired():
    assert "realitymix" in _SOURCES
    assert "locality_text" in _INPUT_PREDICATE["realitymix"]
    assert "loc_text" in _COLS


def test_realitymix_street_first_segment_with_morphology():
    s, hn, zp, improved = derive("realitymix", _row(
        loc_text="Čerpadlová, Praha", obec="Praha",
    ))
    assert s == "Čerpadlová" and improved
    assert hn is None and zp is None


def test_realitymix_rejects_non_street_morphology():
    # A leading místní část ("Jindřichov") is not caught by reject_as_town when it
    # isn't the row's own obec — the morphology gate is what stops it.
    s, _hn, _zp, improved = derive("realitymix", _row(
        loc_text="Jindřichov, Skorošice", obec="Skorošice",
    ))
    assert s is None and not improved


def test_realitymix_rejects_own_town_as_street():
    s, _hn, _zp, improved = derive("realitymix", _row(
        loc_text="Ostrava, Moravskoslezský kraj", obec="Ostrava",
    ))
    assert s is None and not improved


def test_realitymix_town_only_text_yields_null():
    s, _hn, _zp, improved = derive("realitymix", _row(
        loc_text="Rokytnice nad Jizerou", obec="Rokytnice nad Jizerou",
    ))
    assert s is None and not improved
