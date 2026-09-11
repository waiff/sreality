"""scripts.reconcile_source_url — the pure decision, the counters that gate a write, and
the shape of its SQL (narrow projection, no source predicate inside the keyset page)."""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from scraper import sreality_url
from scripts import reconcile_source_url as mod

NOW = _dt.datetime(2026, 9, 11, tzinfo=_dt.timezone.utc)
CANON = "https://www.sreality.cz/detail/prodej/dum/rodinny/praha-michle-pod-sychrovem-i/1915215948"


def _row(**overrides) -> mod.Row:
    base = dict(
        id=1, source="sreality", sreality_id=1915215948, category_type="prodej",
        category_main="dum", category_sub_cb=37, locality="Praha - Michle",
        street="Pod Sychrovem I", street_source="parser", source_url=None,
        is_active=True, last_seen_at=NOW,
    )
    base.update(overrides)
    return mod.Row(**base)


# --- decide() ------------------------------------------------------------------------------


def test_a_non_sreality_row_is_never_touched() -> None:
    d = mod.decide(_row(source="bazos", source_url="https://reality.bazos.cz/inzerat/1/x.php"),
                   clear=True)
    assert d.action == mod.SKIP and d.url is None


def test_a_null_url_gets_the_canonical() -> None:
    d = mod.decide(_row(), clear=False)
    assert d == mod.Decision(mod.WRITE, CANON, None)


def test_the_operator_reported_404_form_is_replaced() -> None:
    stored = "https://www.sreality.cz/detail/prodej/dum/rodinny-dum/x/1915215948"
    assert mod.decide(_row(source_url=stored), clear=False).action == mod.WRITE


def test_an_already_canonical_row_is_unchanged() -> None:
    assert mod.decide(_row(source_url=CANON), clear=False).action == mod.UNCHANGED


def test_the_stored_type_vocabulary_is_remapped() -> None:
    d = mod.decide(_row(category_type="drazba", category_main="komercni", category_sub_cb=31,
                        locality="Týček", street=None, street_source=None,
                        sreality_id=3147342668), clear=False)
    assert d.url == "https://www.sreality.cz/detail/drazby/komercni/zemedelsky/tycek-tycek-/3147342668"


def test_an_unknown_code_declines_and_is_never_guessed() -> None:
    d = mod.decide(_row(category_sub_cb=41), clear=False)
    assert d.action == mod.DECLINED and d.url is None
    assert d.reason is sreality_url.Declined.SUB_UNKNOWN


def test_a_declining_row_with_a_stored_sreality_url_needs_an_explicit_clear() -> None:
    stored = "https://www.sreality.cz/detail/prodej/dum/rodinny/x/1915215948"
    assert mod.decide(_row(category_sub_cb=None, source_url=stored), clear=False).action == mod.WOULD_CLEAR
    d = mod.decide(_row(category_sub_cb=None, source_url=stored), clear=True)
    assert d.action == mod.CLEAR and d.url is None


def test_clear_never_touches_a_stored_url_that_is_not_srealitys() -> None:
    # Defensive: a sreality row somehow carrying a foreign URL is reported, not erased.
    d = mod.decide(_row(category_sub_cb=None, source_url="https://example.cz/1"), clear=True)
    assert d.action == mod.DECLINED


def test_never_a_placeholder() -> None:
    # locality NULL declines: canonical string or nothing, never `/x/`.
    d = mod.decide(_row(locality=None), clear=False)
    assert d.action == mod.DECLINED and d.reason is sreality_url.Declined.LOCALITY_NULL


# --- the gates -----------------------------------------------------------------------------


def test_structural_soundness_is_the_id_at_the_end() -> None:
    assert mod.structurally_sound(CANON, 1915215948)
    assert not mod.structurally_sound(CANON, 1915215949)
    assert not mod.structurally_sound(CANON, None)


def test_report_flags_an_unknown_code_on_a_recent_row_as_a_write_blocker() -> None:
    r = mod.Report()
    r.record(_row(category_sub_cb=41, last_seen_at=NOW - _dt.timedelta(days=1)),
             mod.decide(_row(category_sub_cb=41), clear=False), now=NOW)
    r.record(_row(category_sub_cb=55, category_main="byt", is_active=False,
                  last_seen_at=NOW - _dt.timedelta(days=400)),
             mod.decide(_row(category_sub_cb=55, category_main="byt"), clear=False), now=NOW)
    assert r.unknown_cb == {"41:dum": 1, "55:byt": 1}
    assert r.unknown_cb_recent == 1          # only the row seen in the last 7 days
    assert r.check_status() == "fail"


def test_report_counts_and_parity_status() -> None:
    r = mod.Report()
    r.record(_row(), mod.decide(_row(), clear=False), now=NOW)                      # write, active
    r.record(_row(id=2, is_active=False, street_source="resolver"),
             mod.decide(_row(is_active=False, street_source="resolver"), clear=False), now=NOW)
    r.record(_row(id=3, source_url=CANON), mod.decide(_row(source_url=CANON), clear=False), now=NOW)
    r.record(_row(id=4, source="idnes"), mod.decide(_row(source="idnes"), clear=False), now=NOW)
    r.record(_row(id=5, locality="Jižní, Olomouc - Slavonín", category_main="byt",
                  category_sub_cb=4),
             mod.decide(_row(locality="Jižní, Olomouc - Slavonín", category_main="byt",
                             category_sub_cb=4), clear=False), now=NOW)
    assert r.examined == 5 and r.sreality == 4
    assert r.actions[mod.WRITE] == 3 and r.actions[mod.UNCHANGED] == 1
    assert r.written_split == {"active": 2, "inactive": 1}
    assert r.written_street_source == {"parser": 2, "resolver": 1}
    assert r.locality_format == {"structured": 3, "legacy_comma": 1}
    assert r.disagreeing == 3 and r.check_status() == "warn"
    assert r.details()["actions"] == {"write": 3, "unchanged": 1}


def test_parity_is_ok_when_nothing_disagrees_and_fails_past_the_threshold() -> None:
    r = mod.Report()
    assert r.check_status() == "ok"
    r.disagreeing = mod.PARITY_FAIL_ROWS + 1
    assert r.check_status() == "fail"


@pytest.mark.parametrize(
    "status,location,expected",
    [
        (200, None, True),
        (301, CANON, True),                       # a redirect to exactly the derived URL
        (301, CANON.replace("rodinny", "vila"), False),
        (404, None, False),
        (302, "https://login.seznam.cz/...", False),
    ],
)
def test_conformance_accepts_200_or_a_redirect_to_itself(status, location, expected) -> None:
    assert mod.conforms(status, location, CANON) is expected


# --- the SQL shape --------------------------------------------------------------------------


def test_the_keyset_read_names_no_wide_column_and_no_sparse_predicate() -> None:
    assert "raw_json" not in mod._SELECT_SQL
    where = mod._SELECT_SQL.split("WHERE", 1)[1]
    assert "source" not in where.split("ORDER BY")[0]   # id > cursor and nothing else
    assert re.search(r"LIMIT %\(page\)s", mod._SELECT_SQL)


def test_the_write_is_a_no_op_on_replay() -> None:
    assert "IS DISTINCT FROM" in mod._UPDATE_SQL
    assert "SET source_url = v.url" in mod._UPDATE_SQL


def test_the_conformance_sample_is_active_sreality_only() -> None:
    assert "l.source = 'sreality' AND l.is_active" in mod._SAMPLE_SQL
    assert "raw_json" not in mod._SAMPLE_SQL
