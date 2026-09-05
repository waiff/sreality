"""Hermetic tests for the W2 per-portal gate report (06 §6.4.0, W2-13).

Four properties carry the weight here, and they are the four groups below:

  * **It reads, and it never asks for a page's bytes.** The report exists to be dispatched
    while a corpus-wide sweep is in flight; a statement that detoasted 14 GB of archived
    markup to produce a handful of integers would be the outage it is meant to observe.
    Pinned statically over the whole module, prose included.
  * **Its yield denominator is the population the lane actually walks.** A gate stated over
    a different population than the one that was swept is a fabricated percentage, so the
    minable-population statement is pinned substring-by-substring against the lane's own
    scan.
  * **The verdict is pure and undecidability is not failure.** "No sample yet" is the
    correct reading for every W2 portal until the O8 labelling is done; a report that called
    that a FAIL would train the operator to ignore it.
  * **Every omission is visible.** House number is not scored, the old system's precision
    class cannot be scored, and a skipped denominator is a missing citation — all three are
    printed rather than left as blank cells that read like measurements.
"""

from __future__ import annotations

import ast
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from location_data import claims_remine_archive
from scripts import location_archive_denominator as denominator
from scripts import location_w2_gate_report as gate
from tests.sql_corpus import first_keyword
from toolkit import location_labels

_SOURCE_PATH = Path(gate.__file__)
_SOURCE = _SOURCE_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE, _SOURCE_PATH.name)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


# --------------------------------------------------------------- 1. read-only, no bodies


def test_the_report_never_names_a_body_column() -> None:
    # The archive is ~14 GB and effectively all of it is TOASTed out of line or in R2, so a
    # body column named anywhere would be a round trip per row to produce integers.
    for forbidden in ("body_r2_key", "p.body", "portal_raw_pages.html", ", html"):
        assert forbidden not in _SOURCE, forbidden


def test_the_report_only_ever_reads() -> None:
    # Prose included, so the guard needs no judgement call about which occurrence was "only
    # a comment" — the same rule the archive denominator lives under.
    offenders = re.findall(
        r"\b(insert|update|delete|create|drop|alter|truncate|grant|revoke)\b",
        _SOURCE, re.I)
    assert not offenders, offenders


def test_every_statement_is_a_module_level_sql_constant_and_a_select() -> None:
    """Also what keeps them discoverable by tests/sql_corpus.py — an f-string or a
    concatenation would be invisible to the placeholder guard and to the CI PREPARE sweep."""
    names = {name for name, value in vars(gate).items()
             if name.endswith("_SQL") and isinstance(value, str)}
    assert names, "the SQL constant scan found nothing — the module or the scan moved"
    for name in names:
        assert first_keyword(getattr(gate, name)) == "SELECT", name


def test_the_minable_population_mirrors_the_lane_scan() -> None:
    """A yield measured over a different population than the one that was swept is a
    fabricated percentage, so the two statements must share their predicates."""
    lane = _norm(claims_remine_archive._PAYLOAD_SCAN_FULL_SQL)
    mine = _norm(gate._MINABLE_SQL)
    for fragment in (
        "http_status IS NULL OR",
        "(n.first_observed_at, n.id) > (p.first_observed_at, p.id)",
        "l.source_id_native = p.source_id_native",
    ):
        assert fragment in lane, fragment
        assert fragment in mine, fragment
    # `portal_raw_payloads.listing_id` is nullable and populated by nothing; a join on it
    # matches zero rows and the report would print a confident 0 %.
    assert "p.listing_id" not in mine


def test_the_lane_identifiers_are_imported_not_retyped() -> None:
    assert gate.ARCHIVE_LANE == claims_remine_archive.LANE
    assert gate.ARCHIVE_SURFACE == claims_remine_archive.ARCHIVE_SURFACE
    assert gate.ARCHIVE_EXTRACTOR_VERSION == claims_remine_archive.REMINE_VERSION


def test_the_report_declares_no_lease_constants() -> None:
    """It takes no lease. A module-level `LANE` / `JOB_NAME` / `CONCURRENCY_GROUP` string
    here would make tests/location_data/test_lane_identifiers.py see the archive lane's
    identity claimed twice."""
    declared = {
        target.id
        for node in _TREE.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets if isinstance(target, ast.Name)
    }
    assert not declared & {"LANE", "JOB_NAME", "CONCURRENCY_GROUP"}, declared


def test_w2_portals_excludes_the_two_json_portals() -> None:
    assert set(gate.W2_PORTALS).isdisjoint(denominator.NO_HTML_ARCHIVE)
    assert len(gate.W2_PORTALS) == 7


# --------------------------------------------------------------- 2. fixtures


def _block(det: int, asserted: int, matches: int, floor_key: str,
           *, old_asserted: int | None = None, old_matches: int = 0) -> dict[str, Any]:
    """One `_block()` output, produced by the REAL scorer so the fold cannot drift."""
    raw: dict[str, Any] = {
        "x_determinable": det, "x_new_asserted": asserted, "x_new_match": matches,
    }
    if old_asserted is not None:
        raw["x_old_asserted"] = old_asserted
        raw["x_old_match"] = old_matches
    return location_labels._block(raw, "x", floor_key, has_old=old_asserted is not None)


def _line(field: str, precision: float | None, *, old_precision: float | None = None,
          det: int = 100, asserted: int = 100) -> gate.FloorLine:
    if precision is None:
        block = _block(det, 0, 0, field,
                       old_asserted=None if old_precision is None else asserted)
    else:
        block = _block(
            det, asserted, round(asserted * precision / 100.0), field,
            old_asserted=None if old_precision is None else asserted,
            old_matches=0 if old_precision is None
            else round(asserted * old_precision / 100.0))
    return gate.floor_line(block, field, has_old=old_precision is not None)


def _served(street: float | None = 99.0, obec: float | None = 99.0,
            okres: float | None = 100.0, precision_class: float | None = 99.0,
            *, old: dict[str, float] | None = None) -> tuple[gate.FloorLine, ...]:
    old = old or {}
    return (
        _line("street", street, old_precision=old.get("street")),
        _line("obec", obec, old_precision=old.get("obec")),
        _line("okres", okres, old_precision=old.get("okres")),
        _line("precision_class", precision_class),
    )


def _shadow(street: float | None = 97.0, obec: float | None = 99.0,
            okres: float | None = 100.0) -> tuple[gate.FloorLine, ...]:
    return (_line("street", street), _line("obec", obec), _line("okres", okres))


def _sample(labelled: int = 118, members: int = 120) -> dict[str, Any]:
    return {"id": 7, "source": "remax", "drawn_at": "2026-09-06T08:00:00+00:00",
            "n": members, "members": members, "labelled": labelled}


def _decide(**overrides: Any) -> tuple[str, tuple[str, ...]]:
    kwargs: dict[str, Any] = {
        "source": "remax", "sample": _sample(), "shadowed_versions": (),
        "archived_claims": 118_442, "served": _served(), "shadow": _shadow(),
        "shadow_gate_pass": None,
    }
    kwargs.update(overrides)
    return gate.decide(**kwargs)


# --------------------------------------------------------------- 3. the verdict


def test_no_sample_is_undecidable_not_a_fail() -> None:
    verdict, reasons = _decide(sample=None)
    assert verdict == gate.VERDICT_NO_SAMPLE
    assert verdict not in gate.FAILING_VERDICTS
    assert "O8" in reasons[0]


def test_a_thinly_labelled_sample_is_undecidable() -> None:
    verdict, reasons = _decide(sample=_sample(labelled=40, members=120))
    assert verdict == gate.VERDICT_SAMPLE_UNLABELLED
    assert verdict not in gate.FAILING_VERDICTS
    assert "40 of 120 labelled" in reasons[0]
    assert str(gate.MIN_LABELLED) in reasons[0]


def test_a_portal_with_no_archived_claim_is_not_mined() -> None:
    verdict, reasons = _decide(archived_claims=0)
    assert verdict == gate.VERDICT_NOT_MINED
    assert verdict not in gate.FAILING_VERDICTS
    assert "archived_html" in reasons[0]


def test_a_shadowed_contract_that_clears_its_three_floors_passes() -> None:
    verdict, reasons = _decide(shadowed_versions=(3,), shadow_gate_pass=True)
    assert verdict == gate.VERDICT_SHADOW_PASS
    assert any("--unshadow remax@3" in reason for reason in reasons)


def test_a_shadowed_contract_below_a_floor_fails_and_names_the_field() -> None:
    verdict, reasons = _decide(
        shadowed_versions=(3,), shadow_gate_pass=False, shadow=_shadow(street=91.2))
    assert verdict == gate.VERDICT_SHADOW_FAIL
    assert any(reason.startswith("street 91.") for reason in reasons), reasons


def test_precision_class_is_deferred_under_shadow_and_never_counted_as_a_pass() -> None:
    _verdict, reasons = _decide(shadowed_versions=(3,), shadow_gate_pass=True)
    assert any("precision_class is DEFERRED" in reason for reason in reasons)
    assert len(_shadow()) == 3
    assert len(_served()) == 4
    assert location_labels.SHADOW_DEFERRED_FLOORS == ("precision_class",)


def test_a_field_asserting_nothing_is_undecidable_not_a_pass() -> None:
    served = _served(okres=None)
    assert served[2].passes is None
    verdict, reasons = _decide(served=served)
    assert verdict == gate.VERDICT_LIVE_FAIL
    assert any("okres: nothing asserted" in reason for reason in reasons)


def test_a_regression_against_the_old_system_is_named_even_on_a_pass() -> None:
    verdict, reasons = _decide(served=_served(obec=96.0, old={"obec": 98.0}))
    # 96 % clears no obec floor, so make the floors met and the regression still shown.
    assert verdict == gate.VERDICT_LIVE_FAIL
    assert any(reason.startswith("REGRESSION obec") for reason in reasons)

    verdict, reasons = _decide(served=_served(street=96.0, old={"street": 98.0}))
    assert verdict == gate.VERDICT_LIVE_PASS
    assert any(reason.startswith("REGRESSION street") for reason in reasons)


def test_multiple_shadowed_versions_are_flagged_as_a_blend() -> None:
    _verdict, reasons = _decide(shadowed_versions=(2, 3), shadow_gate_pass=True)
    assert any("keys on SOURCE, not on contract version" in reason for reason in reasons)


def test_the_newest_batches_are_kept_per_source_in_python() -> None:
    rows = [{"source": "remax", "note": str(i)} for i in range(5)]
    rows += [{"source": "bazos", "note": "b"}]
    kept = gate.newest_batches(rows, "remax")
    assert [row["note"] for row in kept] == ["0", "1", "2"]
    assert gate.newest_batches(rows, "maxima") == ()


# --------------------------------------------------------------- 4. the visible omissions


def _gate(**overrides: Any) -> gate.PortalGate:
    kwargs: dict[str, Any] = {
        "source": "remax", "active_version": 3, "shadowed_versions": (3,),
        "sample": _sample(), "served": _served(), "shadow": _shadow(),
        "shadow_gate_pass": True, "minable_bodies": {"detail": 43_910, "index": 122},
        "w2_0_archived_listings": 41_203, "w2_0_floor_verdict": denominator.VERDICT_NOT_REFUTED,
        "archived_claims": 118_442, "archived_claim_listings": 39_880,
        "archived_by_claim_type": {"street_name": (41_010, 39_102)},
        "shadow_claims": 118_442, "shadow_claim_listings": 39_880,
        "batches": (), "verdict": gate.VERDICT_SHADOW_PASS, "reasons": ("go",),
    }
    kwargs.update(overrides)
    return gate.PortalGate(**kwargs)


def test_the_header_says_house_number_is_not_scored() -> None:
    text = "\n".join(gate.render([_gate()], generated_at="2026-09-08T09:12:44+00:00"))
    assert "house number: NOT SCORED" in text
    assert "old precision class: n/a" in text


def test_the_header_cites_the_denominator_or_says_it_was_skipped() -> None:
    cited = "\n".join(gate.render([_gate()], generated_at="now"))
    assert "41,203" in cited
    skipped = "\n".join(gate.render(
        [_gate(w2_0_archived_listings=None, w2_0_floor_verdict=None)], generated_at="now"))
    assert "not measured (--skip-denominator)" in skipped


def test_the_two_denominators_are_printed_side_by_side_and_labelled() -> None:
    """`portal_raw_payloads` and `portal_raw_pages` are different tables (measured 6.1 %
    apart); reporting one as the other would be a fabricated percentage."""
    text = "\n".join(gate.render([_gate()], generated_at="now"))
    assert "43,910" in text and "41,203" in text
    assert "two tables" in text
    # the yield divides by the payload store, the population the lane actually walks
    assert "90.8 % of 43,910" in text


def test_the_json_envelope_declares_no_write() -> None:
    payload = gate.to_json([_gate()], generated_at="now")
    assert payload["metadata"]["writes"] == []
    assert payload["metadata"]["tool"] == "location_w2_gate_report"
    assert payload["data"]["floors"] == dict(location_labels.FLOORS)
    assert payload["data"]["house_number_scored"] is False
    assert payload["data"]["portals"][0]["verdict"] == gate.VERDICT_SHADOW_PASS


# --------------------------------------------------------------- 5. the wiring


class _FakeCursor:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        text = _norm(sql)
        self.state["executed"].append((text, params))
        if "set_config" in text:
            self._result = []
            return
        assert self.state["transactions"], "a statement ran outside a bounded transaction"
        hits = [rows for marker, rows in self.state["results"].items() if marker in text]
        assert len(hits) == 1, f"{len(hits)} fixture markers matched: {text[:140]}"
        self._result = hits[0]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


class _FakeConn:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.state)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.state["transactions"] += 1
        yield


@pytest.fixture
def state() -> dict[str, Any]:
    return {
        "executed": [], "transactions": 0,
        "results": {
            "FROM portal_raw_payloads p": [("remax", "detail", 43_910),
                                           ("remax", "index", 122)],
            "SELECT c.source, count(*) AS claims, count(DISTINCT c.listing_id) AS listings "
            "FROM location_claims_unretracted": [("remax", 118_442, 39_880)],
            "c.claim_type::text AS claim_type": [("remax", "street_name", 41_010, 39_102)],
            "FROM location_claims_shadow": [("remax", 118_442, 39_880, 118_442)],
            "FROM portal_contracts": [("remax", 3, True, True, None)],
            "FROM location_claim_batches": [
                ("remax", "full", "ok", "2026-09-07", None, 118_442, 900, None,
                 "payloads=43910 applicable=43910"),
            ],
        },
    }


def test_the_gate_row_is_folded_from_the_statements_and_the_scorers(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any],
) -> None:
    monkeypatch.setattr(location_labels, "current_sample",
                        lambda conn, source: _sample())
    monkeypatch.setattr(location_labels, "score_sample", lambda conn, source: {
        "data": {field: _block(100, 100, 99, field) for field in gate.SERVED_FIELDS}})
    monkeypatch.setattr(location_labels, "score_shadow_claims", lambda conn, source: {
        "data": {**{field: _block(100, 100, 100, field) for field in gate.SHADOW_FIELDS},
                 "gate_pass": True}})

    gates = gate.gather(_FakeConn(state), sources=("remax",), statement_timeout_s=37,
                        skip_denominator=True)
    assert len(gates) == 1
    row = gates[0]
    assert row.minable_bodies == {"detail": 43_910, "index": 122}
    assert (row.archived_claims, row.archived_claim_listings) == (118_442, 39_880)
    assert row.archived_by_claim_type == {"street_name": (41_010, 39_102)}
    assert (row.active_version, row.shadowed_versions) == (3, (3,))
    assert row.verdict == gate.VERDICT_SHADOW_PASS
    # --skip-denominator never omits the citation silently
    assert row.w2_0_archived_listings is None
    assert len(row.batches) == 1 and "applicable=" in row.batches[0]["note"]

    guards = [params for text, params in state["executed"] if "set_config" in text]
    assert guards and {g["statement_timeout"] for g in guards} == {"37s"}
    assert state["transactions"] == len(guards) == 6


def test_the_lane_is_the_only_batch_scope_the_report_reads(state: dict[str, Any]) -> None:
    """The W3 snapshot lane writes to the same table under a different `lane`; reading its
    rows here would report another substrate's coverage as this one's."""
    assert "%(lane)s" in gate._BATCHES_SQL
    assert gate.ARCHIVE_LANE == "location_claims_remine_archive"


def test_a_missing_dsn_is_refused_before_any_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setattr(gate.db, "connect",
                        lambda *a, **k: pytest.fail("connected without a DSN"))
    assert gate.main([]) == 2


def test_the_timeout_default_is_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert gate.DEFAULT_STATEMENT_TIMEOUT_S > 0
    monkeypatch.setenv(gate.STATEMENT_TIMEOUT_ENV, "45")
    from location_data import loader_db
    assert loader_db.env_timeout_s(
        gate.STATEMENT_TIMEOUT_ENV, gate.DEFAULT_STATEMENT_TIMEOUT_S) == 45
