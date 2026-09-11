"""scripts.verify_outbound_urls — the rotating sample, the conformance rule, and the
concentration threshold (pure; the HEAD probe and the DB are not exercised here)."""

from __future__ import annotations

from scraper import sreality_url
from scripts import verify_outbound_urls as mod
from scripts.verify_pipeline import DEFAULT_THRESHOLDS

T = DEFAULT_THRESHOLDS
CANON = "https://www.sreality.cz/detail/prodej/dum/rodinny/praha-michle-pod-sychrovem-i/1915215948"


def _s(**kw) -> mod.Sample:
    base = dict(id=1, source="sreality", category_main="dum", category_sub_cb=37, source_url=CANON)
    base.update(kw)
    return mod.Sample(**base)


def test_the_rotation_covers_the_whole_codebook_every_four_weeks() -> None:
    seen: set[int] = set()
    for week in range(1, 5):
        seen |= set(mod.codes_for_week(week))
    assert seen == set(sreality_url.SUB_SLUG)
    assert 10 <= len(mod.codes_for_week(37)) <= 14
    assert mod.codes_for_week(37) == mod.codes_for_week(41)   # same quarter, four weeks on


def test_conformance_for_sreality_accepts_a_locality_only_redirect() -> None:
    assert mod.conforms("sreality", 200, None, CANON)
    drifted = CANON.replace("praha-michle-pod-sychrovem-i", "praha-michle-")
    assert mod.conforms("sreality", 301, drifted, CANON)
    assert not mod.conforms("sreality", 301, CANON.replace("rodinny", "vila"), CANON)
    assert not mod.conforms("sreality", 404, None, CANON)
    assert not mod.conforms("sreality", 302, "https://login.seznam.cz/x", CANON)


def test_conformance_for_a_crawler_accepts_its_own_same_host_canonicalisation() -> None:
    stored = "https://www.remax-czech.cz/reality/detail/440872/old-slug"
    assert mod.conforms("remax", 301, "https://www.remax-czech.cz/reality/detail/440872/new-slug", stored)
    assert not mod.conforms("remax", 301, "https://elsewhere.example/x", stored)
    assert not mod.conforms("remax", 410, None, stored)


def test_a_whole_cell_failing_is_a_slug_defect_and_fails() -> None:
    samples = [(_s(id=i), False) for i in range(3)] + [(_s(id=i, category_sub_cb=39), True) for i in range(3, 6)]
    r = mod.classify(samples, T)
    assert r["status"] == "fail" and r["check_key"] == mod.CHECK_KEY
    assert r["details"]["failed_cells"] == [
        {"source": "sreality", "category_main": "dum", "category_sub_cb": 37, "sampled": 3, "failed": 3}
    ]


def test_scattered_failures_warn_by_share_and_never_fail() -> None:
    # one 404 in each of four cells of three: 33% per cell (< 67%), 33% overall (> 5%)
    samples = []
    for i, cb in enumerate((2, 3, 4, 5)):
        samples += [(_s(id=10 * i, category_main="byt", category_sub_cb=cb), False),
                    (_s(id=10 * i + 1, category_main="byt", category_sub_cb=cb), True),
                    (_s(id=10 * i + 2, category_main="byt", category_sub_cb=cb), True)]
    r = mod.classify(samples, T)
    assert r["status"] == "warn" and r["details"]["failed_cells"] == []
    assert r["value"] == 33.33


def test_a_single_bad_row_in_a_small_cell_is_not_a_cell_failure() -> None:
    r = mod.classify([(_s(), False), (_s(id=2, category_sub_cb=39), True)] + [(_s(id=i, category_sub_cb=33), True) for i in range(3, 40)], T)
    assert r["status"] == "ok"   # 1/39 = 2.6% < 5%, and the failing cell has < cell_min samples


def test_all_clean_is_ok_with_the_share_as_value() -> None:
    r = mod.classify([(_s(id=i), True) for i in range(5)], T)
    assert r["status"] == "ok" and r["value"] == 0.0 and r["details"]["sampled"] == 5
