"""The weekly stored-vs-recomputed street_name_key parity check (audit PR-E)."""

from __future__ import annotations

from scripts.check_street_key_parity import find_mismatches


def test_find_mismatches_flags_only_divergent_rows() -> None:
    rows = [
        (101, 1, "ul. Koterovská 12", "koterovska"),   # correct stored key
        (102, 2, "Hlavní", "hlavni"),                  # correct
        (103, 3, "Hlavní", "WRONG"),                   # stale/wrong -> mismatch
        (104, 4, "náměstí Míru", None),                # missing key -> mismatch
    ]
    out = find_mismatches(rows)
    assert [(lid, sid, stored, expected) for lid, sid, _s, stored, expected in out] == [
        (103, 3, "WRONG", "hlavni"),
        (104, 4, None, "miru"),
    ]


def test_find_mismatches_clean_sample_is_empty() -> None:
    assert find_mismatches([(1, 1, "Absolonova", "absolonova")]) == []


def test_find_mismatches_null_sreality_id_does_not_crash() -> None:
    """A post-Gate-2 non-sreality row has sreality_id=NULL; a bare int(sreality_id)
    would raise TypeError and abort the whole sampled run instead of just flagging
    the row (listing_id, the surrogate PK, is never NULL)."""
    rows = [(205, None, "Hlavní", "WRONG")]
    assert find_mismatches(rows) == [(205, None, "Hlavní", "WRONG", "hlavni")]


def test_find_mismatches_null_sreality_id_clean_row_is_skipped() -> None:
    assert find_mismatches([(206, None, "Hlavní", "hlavni")]) == []
