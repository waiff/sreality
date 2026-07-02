"""The weekly stored-vs-recomputed street_name_key parity check (audit PR-E)."""

from __future__ import annotations

from scripts.check_street_key_parity import find_mismatches


def test_find_mismatches_flags_only_divergent_rows() -> None:
    rows = [
        (1, "ul. Koterovská 12", "koterovska"),   # correct stored key
        (2, "Hlavní", "hlavni"),                  # correct
        (3, "Hlavní", "WRONG"),                   # stale/wrong -> mismatch
        (4, "náměstí Míru", None),                # missing key -> mismatch
    ]
    out = find_mismatches(rows)
    assert [(sid, stored, expected) for sid, _s, stored, expected in out] == [
        (3, "WRONG", "hlavni"),
        (4, None, "miru"),
    ]


def test_find_mismatches_clean_sample_is_empty() -> None:
    assert find_mismatches([(1, "Absolonova", "absolonova")]) == []
