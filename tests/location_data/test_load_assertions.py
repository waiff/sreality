"""The blocking load-time controls, exercised without a database."""

from __future__ import annotations

from dataclasses import replace

from location_data import krovak, load_assertions
from location_data.load_assertions import PriorLoad, StagingStats

GOOD = StagingStats(
    row_count=3_020_222,
    missing_psc=0,
    missing_coords=920,
    golden_distance_m=0.4,
    krovak_y_min=432_064.28, krovak_y_max=901_942.00,
    krovak_x_min=936_371.33, krovak_x_max=1_219_794.01,
    lat_min=48.6, lat_max=51.0, lon_min=12.1, lon_max=18.8,
    only_in_adr=3, only_in_chain=1,
)

PRIOR = PriorLoad(
    row_count=3_015_000, missing_psc=0, missing_coords=900,
    krovak_y_min=432_064.28, krovak_y_max=901_942.00,
    krovak_x_min=936_371.33, krovak_x_max=1_219_794.01,
    discrepancies=4, proj_pipeline="S-JTSK to WGS 84 (5)",
)

PIPELINE = "S-JTSK to WGS 84 (5)"


def _named(assertions, name):
    return next(a for a in assertions if a.name == name)


def test_a_clean_load_passes_everything():
    assertions = load_assertions.evaluate(GOOD, PRIOR, proj_pipeline=PIPELINE)
    assert load_assertions.blocking_failures(assertions) == []
    assert {a.name for a in assertions} >= {
        "row_count_sanity", "row_count_vs_prior", "missing_psc",
        "missing_coordinates_vs_prior", "golden_point", "krovak_super_envelope",
        "krovak_envelope_vs_prior", "wgs84_bbox", "proj_pipeline_unchanged",
        "product_skew_discrepancies",
    }


def test_first_load_skips_every_prior_anchored_control():
    assertions = load_assertions.evaluate(GOOD, None, proj_pipeline=PIPELINE)
    names = {a.name for a in assertions}
    assert "row_count_vs_prior" not in names
    assert "krovak_envelope_vs_prior" not in names
    assert load_assertions.blocking_failures(assertions) == []


def test_golden_point_beyond_five_metres_aborts():
    stats = replace(GOOD, golden_distance_m=12.0)
    failures = load_assertions.blocking_failures(
        load_assertions.evaluate(stats, PRIOR, proj_pipeline=PIPELINE)
    )
    assert [f.name for f in failures] == ["golden_point"]


def test_missing_golden_point_aborts():
    stats = replace(GOOD, golden_distance_m=None)
    assert not _named(
        load_assertions.evaluate(stats, PRIOR, proj_pipeline=PIPELINE), "golden_point"
    ).ok


def test_a_row_count_drop_is_the_suspicious_direction():
    shrunk = replace(GOOD, row_count=int(PRIOR.row_count * 0.99))
    assert not _named(
        load_assertions.evaluate(shrunk, PRIOR, proj_pipeline=PIPELINE), "row_count_vs_prior"
    ).ok
    grown = replace(GOOD, row_count=int(PRIOR.row_count * 1.01))
    assert _named(
        load_assertions.evaluate(grown, PRIOR, proj_pipeline=PIPELINE), "row_count_vs_prior"
    ).ok


def test_register_growth_never_becomes_a_false_failure():
    """The whole point of anchoring to the prior load: a register 30 % larger than the
    2026-08 measurement still passes, as long as it grew steadily."""
    prior = replace(PRIOR, row_count=3_900_000)
    stats = replace(GOOD, row_count=3_930_000)
    assert load_assertions.blocking_failures(
        load_assertions.evaluate(stats, prior, proj_pipeline=PIPELINE)
    ) == []


def test_truncated_download_fails_the_outer_sanity_bound():
    stats = replace(GOOD, row_count=1_000)
    failed = {f.name for f in load_assertions.blocking_failures(
        load_assertions.evaluate(stats, None, proj_pipeline=PIPELINE)
    )}
    assert "row_count_sanity" in failed


def test_any_missing_psc_aborts():
    stats = replace(GOOD, missing_psc=1)
    assert not _named(
        load_assertions.evaluate(stats, PRIOR, proj_pipeline=PIPELINE), "missing_psc"
    ).ok


def test_missing_coordinates_tolerate_the_slack_but_not_a_collapse():
    ok = replace(GOOD, missing_coords=PRIOR.missing_coords + 500)
    assert _named(
        load_assertions.evaluate(ok, PRIOR, proj_pipeline=PIPELINE),
        "missing_coordinates_vs_prior",
    ).ok
    bad = replace(GOOD, missing_coords=200_000)
    assert not _named(
        load_assertions.evaluate(bad, PRIOR, proj_pipeline=PIPELINE),
        "missing_coordinates_vs_prior",
    ).ok


def test_sign_error_shows_up_as_an_envelope_and_bbox_failure():
    stats = replace(
        GOOD,
        krovak_y_min=-901_942.0, krovak_y_max=-432_064.28,
        lat_min=52.2, lat_max=52.3, lon_min=9.4, lon_max=9.5,
    )
    failed = {f.name for f in load_assertions.blocking_failures(
        load_assertions.evaluate(stats, PRIOR, proj_pipeline=PIPELINE)
    )}
    assert {"krovak_super_envelope", "wgs84_bbox"} <= failed


def test_envelope_shift_beyond_slack_aborts():
    stats = replace(GOOD, krovak_y_max=GOOD.krovak_y_max + 5_000)
    assert not _named(
        load_assertions.evaluate(stats, PRIOR, proj_pipeline=PIPELINE),
        "krovak_envelope_vs_prior",
    ).ok


def test_pipeline_change_warns_but_never_blocks():
    assertions = load_assertions.evaluate(GOOD, PRIOR, proj_pipeline="S-JTSK to WGS 84 (1)")
    item = _named(assertions, "proj_pipeline_unchanged")
    assert not item.ok and not item.blocking and item.route == "warn"
    assert load_assertions.blocking_failures(assertions) == []


def test_product_skew_is_a_trend_signal_not_a_page():
    stats = replace(GOOD, only_in_adr=500, only_in_chain=500)
    item = _named(
        load_assertions.evaluate(stats, PRIOR, proj_pipeline=PIPELINE),
        "product_skew_discrepancies",
    )
    assert not item.ok and item.route == "digest" and not item.blocking


def test_golden_constants_match_the_recon_corpus():
    assert krovak.GOLDEN_KOD_ADM == 21690278
    assert krovak.GOLDEN_KROVAK_POSITIVE == (744384.54, 1042569.73)
    assert krovak.GOLDEN_WGS84 == (50.089480, 14.398606)
