"""Hermetic tests for the W2a index-coverage audit (02 §2.3.2 P2, 06 W2a gate (c)).

The audit exists because PR #1060 found that a call site can EXIST and still not
fire, so the failure modes worth pinning are:

  * **Collapsing three states into two.** `gated` (a call site behind the
    freshness skip) must never render as `wired` — that is the whole finding — and
    must never render as `absent` either, since one is owed a code fix and the
    other a build.
  * **Misreading a deliberate skip as a gap.** sreality's `probe_category` and
    remax's `_max_pages` guard archive nothing BY DESIGN.
  * **A registry that rots.** The classification is re-derived from each module at
    run time and any disagreement with the baked registry has to surface.
  * **Reading `count(*) > 0` as "it works".** Index archiving was switched off in
    June 2026, so old rows are not accumulation.
  * **Touching a body.** The audit may not project `portal_raw_pages.html` or
    `portal_raw_payloads.body`, and may not write.
"""

from __future__ import annotations

import ast
import datetime
import re
from pathlib import Path

from scripts import location_index_archive_audit as aud
from tests.sql_corpus import first_keyword

_SOURCE_PATH = Path(aud.__file__)
_SOURCE = _SOURCE_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE, _SOURCE_PATH.name)

_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)

_INDEX_CALL_SITE = """
import scraper.db as db


def walk(conn, pages):
    for offset, html in pages:
        db.upsert_portal_raw_page(
            conn, source="x", source_id_native=str(offset), source_url="u",
            page_kind="index", html=html, http_status=200,
        )
"""

_GATED_CALL_SITE = """
import scraper.db as db


def walk(conn, pages):
    fresh = db.fresh_index_page_keys(conn, "x", hours=22.0)
    for offset, html in pages:
        if str(offset) in fresh:
            continue
        db.upsert_portal_raw_page(
            conn, source="x", source_id_native=str(offset), source_url="u",
            page_kind="index", html=html, http_status=200,
        )
"""

_DETAIL_ONLY = """
import scraper.db as db


def drain(conn, items):
    for item in items:
        db.upsert_portal_raw_page(
            conn, source="x", source_id_native=item.id, source_url="u",
            page_kind="detail", html=item.html, http_status=200,
        )
"""


def _facts(
    *, entries: int = 0, declares: bool = False, archive: bool | None = None,
) -> aud.ContractFacts:
    return aud.ContractFacts(
        index_entries=entries, declares_index_surface=declares, declared_archive=archive,
    )


def _portal(
    *,
    source: str = "sreality",
    contract: aud.ContractFacts | None = None,
    site: aud.CallSite | None = None,
    observed: str | None = None,
    staging: aud.StagingRows | None = None,
    payloads: aud.PayloadRows | None = None,
) -> aud.PortalAudit:
    return aud.PortalAudit(
        source=source,
        contract=contract or _facts(),
        site=site or aud.CallSite(module="main.py", state=aud.STATE_ABSENT),
        observed_state=observed,
        staging=staging,
        payloads=payloads,
        now=_NOW,
    )


# --------------------------------------------- 1. three states, never collapsed


def test_a_call_site_behind_the_freshness_skip_is_gated_not_wired() -> None:
    # THE finding this audit was built for: `if key in fresh: return` sits BEFORE
    # upsert_portal_raw_page, so the call site exists and still does not fire.
    assert aud.classify_module_source(_GATED_CALL_SITE) == aud.STATE_GATED


def test_a_call_site_with_no_skip_set_is_wired() -> None:
    # The same call site with the skip removed classifies differently — which is
    # what proves `gated` is a real distinction and not a label on every call site.
    assert aud.classify_module_source(_INDEX_CALL_SITE) == aud.STATE_WIRED


def test_a_module_with_no_index_call_site_is_absent() -> None:
    assert aud.classify_module_source(_DETAIL_ONLY) == aud.STATE_ABSENT
    assert aud.classify_module_source("x = 1\n") == aud.STATE_ABSENT


def test_the_three_states_are_three_distinct_verdicts() -> None:
    wants = _facts(entries=2, declares=True, archive=True)
    verdicts = {
        state: _portal(contract=wants, observed=state).verdict
        for state in (aud.STATE_WIRED, aud.STATE_GATED, aud.STATE_ABSENT)
    }
    assert len(set(verdicts.values())) == 3, verdicts
    assert verdicts[aud.STATE_WIRED] == aud.VERDICT_YES
    assert verdicts[aud.STATE_GATED] == aud.VERDICT_PARTIAL
    assert verdicts[aud.STATE_ABSENT] == aud.VERDICT_NO_CALL_SITE


def test_each_real_gated_module_flips_state_when_its_gate_is_mutated_away() -> None:
    """Mutation test on the REAL modules, not a synthetic snippet: both edges of
    `gated` have to move, or the classification is a constant dressed as a check.

    * delete the freshness skip and the module must read `wired`;
    * delete the index call site and it must read `absent`.
    """
    for source, site in sorted(aud.INDEX_ARCHIVERS.items()):
        if site.state != aud.STATE_GATED:
            continue
        real = site.module_path.read_text(encoding="utf-8")
        assert aud.classify_module_source(real) == aud.STATE_GATED, source
        ungated = real.replace(aud.FRESHNESS_SKIP_TOKEN, "some_other_helper")
        assert aud.classify_module_source(ungated) == aud.STATE_WIRED, source
        uncalled = real.replace('page_kind="index"', 'page_kind="detail"')
        assert aud.classify_module_source(uncalled) == aud.STATE_ABSENT, source


def test_a_detail_call_site_does_not_make_a_portal_look_wired() -> None:
    # Seven portals archive detail pages through the same function; matching on
    # the call name alone would report every one of them as an index archiver.
    assert aud.has_index_archive_call(ast.parse(_DETAIL_ONLY)) is False
    assert aud.has_index_archive_call(ast.parse(_INDEX_CALL_SITE)) is True


def test_an_index_call_site_is_found_through_the_db_module_alias() -> None:
    bare = _INDEX_CALL_SITE.replace("db.upsert_portal_raw_page", "upsert_portal_raw_page")
    assert aud.classify_module_source(bare) == aud.STATE_WIRED


# ------------------------------------ 2. the registry agrees with the real code


def test_every_portal_with_a_contract_has_a_registry_entry() -> None:
    from location_data import contracts

    sources = {c.source for c in contracts.load_all()}
    assert sources <= set(aud.INDEX_ARCHIVERS), sorted(sources - set(aud.INDEX_ARCHIVERS))


def test_the_registry_matches_what_each_module_actually_does() -> None:
    """The drift check as a test, so the registry cannot rot silently: a PR that
    adds, removes or un-gates an index archiver fails here until it says so."""
    for source, site in sorted(aud.INDEX_ARCHIVERS.items()):
        assert site.module_path.exists(), site.module_path
        observed = aud.classify_module(site.module_path)
        assert observed == site.state, f"{source}: registry {site.state}, module {observed}"


def test_todays_fleet_is_three_gated_and_six_absent() -> None:
    # Pins the state PR #1060 documented. `wired` being empty is the finding, not
    # an oversight: all three call sites sit behind the freshness skip.
    by_state: dict[str, list[str]] = {}
    for source, site in aud.INDEX_ARCHIVERS.items():
        by_state.setdefault(site.state, []).append(source)
    assert sorted(by_state.get(aud.STATE_GATED, [])) == [
        "ceskereality", "remax", "sreality",
    ]
    assert sorted(by_state.get(aud.STATE_ABSENT, [])) == [
        "bazos", "bezrealitky", "idnes", "maxima", "mmreality", "realitymix",
    ]
    assert by_state.get(aud.STATE_WIRED, []) == []


def test_every_gated_call_site_names_its_gate_and_its_known_gap_comment() -> None:
    # The comment and the registry row have to stay in step — the comment is what
    # points the next reader here, and the row is what this audit prints.
    for source, site in aud.INDEX_ARCHIVERS.items():
        if site.state != aud.STATE_GATED:
            continue
        assert site.gate, source
        assert site.call_site, source
        module = site.module_path.read_text(encoding="utf-8")
        assert "KNOWN GAP" in module, source


def test_an_absent_portal_claims_no_call_site_or_gate() -> None:
    for source, site in aud.INDEX_ARCHIVERS.items():
        if site.state != aud.STATE_ABSENT:
            continue
        assert site.call_site is None, source
        assert site.gate is None, source


# ------------------------------- 3. an intentional skip is not read as a defect


def test_the_deliberate_non_archiving_paths_are_recorded_as_intentional() -> None:
    # sreality's probe_category and remax's _max_pages guard archive nothing BY
    # DESIGN; without this the audit's own reader files each of them as a gap.
    assert any(
        "probe_category" in skip
        for skip in aud.INDEX_ARCHIVERS["sreality"].intentional_skips
    )
    assert any(
        "_max_pages" in skip
        for skip in aud.INDEX_ARCHIVERS["remax"].intentional_skips
    )


def test_an_intentional_skip_never_changes_the_state_or_the_verdict() -> None:
    # It is documentation printed beside the verdict, never an input to it: remax
    # is `gated` because of the freshness skip, and the probe guard is separate.
    bare = aud.CallSite(module="remax_main.py", state=aud.STATE_GATED)
    documented = aud.CallSite(
        module="remax_main.py", state=aud.STATE_GATED,
        intentional_skips=("the page-capped probe never archives",),
    )
    wants = _facts(entries=2, declares=True, archive=True)
    assert _portal(contract=wants, site=bare).verdict == aud.VERDICT_PARTIAL
    assert _portal(contract=wants, site=documented).verdict == aud.VERDICT_PARTIAL


def test_the_readout_prints_the_intentional_skips_beside_the_gap() -> None:
    lines = "\n".join(aud.render([
        _portal(source="remax", contract=_facts(entries=2), observed=aud.STATE_GATED,
                site=aud.INDEX_ARCHIVERS["remax"]),
    ]))
    assert "intentional (NOT a gap)" in lines
    assert "_max_pages" in lines


# ---------------------------------------- 4. contract axis: asked vs. built vs. off


def test_a_portal_declaring_index_entries_with_no_call_site_is_a_gap() -> None:
    # The plan's own acceptance test: a `page_kind: index` contract entry with
    # nothing archiving that body is a build the operator is owed.
    audit = _portal(contract=_facts(entries=1), observed=aud.STATE_ABSENT)
    assert audit.verdict == aud.VERDICT_NO_CALL_SITE


def test_a_declared_archive_true_surface_with_no_call_site_is_also_a_gap() -> None:
    # bezrealitky's live shape: the fetch config promises an archived index
    # surface and no code writes one.
    audit = _portal(contract=_facts(declares=True, archive=True), observed=aud.STATE_ABSENT)
    assert audit.verdict == aud.VERDICT_DECLARED_UNBUILT


def test_a_declared_archive_false_surface_is_a_decision_not_a_gap() -> None:
    # bazos declares `archive: false` on its index surface; reporting that as a
    # gap would make the audit's gap list unreadable.
    audit = _portal(contract=_facts(declares=True, archive=False), observed=aud.STATE_ABSENT)
    assert audit.verdict == aud.VERDICT_DECLARED_OFF


def test_a_portal_that_asks_for_nothing_is_not_a_gap() -> None:
    audit = _portal(contract=_facts(), observed=aud.STATE_ABSENT)
    assert audit.verdict == aud.VERDICT_NOT_ASKED


def test_a_contract_that_contradicts_itself_is_marked_either_way() -> None:
    entries_only = _portal(contract=_facts(entries=1), observed=aud.STATE_GATED)
    assert "no index surface" in (entries_only.contract_note or "")
    surface_only = _portal(
        contract=_facts(declares=True, archive=True), observed=aud.STATE_ABSENT,
    )
    assert "no index claim entry" in (surface_only.contract_note or "")
    consistent = _portal(
        contract=_facts(entries=2, declares=True, archive=True), observed=aud.STATE_GATED,
    )
    assert consistent.contract_note is None


def test_the_contract_axis_is_read_from_the_real_contract_files() -> None:
    from location_data import contracts

    by_source = {c.source: aud.contract_facts(c) for c in contracts.load_all()}
    assert by_source["sreality"].index_entries == 3
    assert by_source["sreality"].declared_archive is True
    assert by_source["bazos"].declared_archive is False
    assert by_source["idnes"].declares_index_surface is False
    assert by_source["idnes"].declared_archive is None


# ------------------------------------------- 5. data axis: fresh, not merely present


def test_old_index_rows_are_not_accumulation() -> None:
    # Index archiving was switched off in early June 2026, so a portal can hold
    # thousands of rows and archive nothing today.
    stale = aud.StagingRows(
        pages=41_000,
        oldest=_NOW - datetime.timedelta(days=90),
        newest=_NOW - datetime.timedelta(days=60),
    )
    audit = _portal(observed=aud.STATE_GATED, staging=stale)
    assert audit.accumulating is False
    assert "STALE" in (audit.data_note or "")


def test_a_recent_index_row_is_accumulation() -> None:
    fresh = aud.StagingRows(
        pages=120, oldest=_NOW - datetime.timedelta(days=7),
        newest=_NOW - datetime.timedelta(hours=3),
    )
    audit = _portal(observed=aud.STATE_GATED, staging=fresh)
    assert audit.accumulating is True
    assert audit.data_note is None


def test_the_staleness_limit_is_two_refresh_windows() -> None:
    from scraper import db

    assert aud.STALE_AFTER_HOURS == 2.0 * db.INDEX_ARCHIVE_REFRESH_HOURS
    just_inside = aud.StagingRows(
        pages=1, oldest=_NOW, newest=_NOW - datetime.timedelta(hours=43),
    )
    just_outside = aud.StagingRows(
        pages=1, oldest=_NOW, newest=_NOW - datetime.timedelta(hours=45),
    )
    assert _portal(observed=aud.STATE_GATED, staging=just_inside).accumulating is True
    assert _portal(observed=aud.STATE_GATED, staging=just_outside).accumulating is False


def test_a_portal_with_a_call_site_and_no_rows_at_all_is_marked() -> None:
    audit = _portal(observed=aud.STATE_GATED, staging=aud.StagingRows(0, None, None))
    assert audit.data_note == aud.NO_ROWS


def test_fresh_rows_with_no_call_site_found_are_flagged_as_a_missed_writer() -> None:
    # The one direction the static classification cannot self-check: if bodies are
    # still landing for a portal the audit calls `absent`, the audit is wrong.
    landing = aud.StagingRows(
        pages=900, oldest=_NOW - datetime.timedelta(days=30),
        newest=_NOW - datetime.timedelta(hours=2),
    )
    audit = _portal(observed=aud.STATE_ABSENT, staging=landing)
    assert audit.data_note == aud.UNEXPECTED_ROWS


def test_an_absent_portal_with_only_old_rows_is_not_flagged() -> None:
    # Six of nine portals hold pre-June-2026 index rows; that is history, not a
    # missed writer, and flagging it would bury the one case that matters.
    historic = aud.StagingRows(
        pages=900, oldest=_NOW - datetime.timedelta(days=120),
        newest=_NOW - datetime.timedelta(days=70),
    )
    audit = _portal(observed=aud.STATE_ABSENT, staging=historic)
    assert audit.data_note is None


def test_an_absent_portal_with_no_rows_needs_no_data_marker() -> None:
    # Its verdict already says there is no call site; a NO ROWS line beside it is
    # noise on six of nine portals.
    audit = _portal(observed=aud.STATE_ABSENT, staging=aud.StagingRows(0, None, None))
    assert audit.data_note is None


def test_the_data_axis_is_absent_not_zero_when_the_db_is_skipped() -> None:
    audit = _portal(observed=aud.STATE_GATED)
    assert audit.accumulating is None
    assert audit.staging_age_hours is None
    assert audit.data_note is None
    assert aud.audit_json(audit)["data"]["staging_pages"] is None


# ------------------------------------------------------- 6. the drift marker


def test_a_registry_that_disagrees_with_its_module_is_marked_not_believed() -> None:
    audit = _portal(
        site=aud.CallSite(module="main.py", state=aud.STATE_WIRED),
        observed=aud.STATE_GATED,
    )
    assert audit.state == aud.STATE_GATED
    assert "DRIFT" in (audit.drift or "")
    assert audit.verdict == aud.VERDICT_PARTIAL


def test_an_unreadable_module_falls_back_to_the_registry() -> None:
    site = aud.INDEX_ARCHIVERS["sreality"]
    audit = _portal(site=site, observed=None)
    assert audit.state == site.state
    assert audit.drift is None


# ------------------------------------------------- 7. read-only, and no bodies


def _sql_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(aud).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }


def test_every_executed_statement_is_a_module_level_sql_constant() -> None:
    # Also what keeps them discoverable by tests/sql_corpus.py, whose CI PREPARE
    # sweep is the only thing that type-checks them against the real schema.
    names = _sql_constants()
    assert sorted(names) == ["_PAYLOAD_INDEX_SQL", "_STAGING_INDEX_SQL"]
    for node in ast.walk(_TREE):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("execute", "executemany")
        ):
            first = node.args[0]
            assert isinstance(first, ast.Name), ast.dump(first)
            assert first.id in names, first.id


def test_every_statement_the_audit_runs_is_a_select() -> None:
    for name, sql in _sql_constants().items():
        assert first_keyword(sql) == "SELECT", name
        assert not re.search(r"\b(insert|update|delete|truncate|copy)\b", sql, re.I), name


def test_no_statement_ever_projects_a_body() -> None:
    # portal_raw_pages.html is the 14 GB TOASTed column the W2-0 denominator's own
    # rule forbids touching; portal_raw_payloads.body is the archive itself. Both
    # queries are counts and timestamps, so neither may detoast anything.
    for name, sql in _sql_constants().items():
        assert not re.search(r"\bhtml\b", sql, re.I), name
        assert not re.search(r"\bbody\b", sql, re.I), name


def test_the_reads_are_bounded_by_a_transaction_local_timeout() -> None:
    # db.connect() is autocommit on the transaction-mode pooler: a session-level
    # SET can land on a different backend than the statement it guards.
    assert "loader_db.bounded(conn, statement_timeout_s)" in _SOURCE
    assert aud.DEFAULT_STATEMENT_TIMEOUT_S > 0


def test_both_statements_filter_on_the_page_kind_the_gate_uses() -> None:
    # The SQL has to stay a plain literal for the sql_corpus PREPARE sweep, so it
    # cannot interpolate db.INDEX_PAGE_KIND — this is the seam that ties them.
    from scraper import db

    assert aud.INDEX == db.INDEX_PAGE_KIND
    for name, sql in _sql_constants().items():
        assert f"page_kind = '{db.INDEX_PAGE_KIND}'" in sql, name


def test_the_payload_count_strips_the_week_suffix_index_keys_carry() -> None:
    # Index keys are week-stamped (…/{offset}/{week}, db.index_archive_week), so
    # count(*) grows with the measurement window and only DISTINCT artefacts is
    # the number of page positions being archived.
    assert "regexp_replace" in aud._PAYLOAD_INDEX_SQL
    assert "count(DISTINCT" in aud._PAYLOAD_INDEX_SQL


# -------------------------------------------------------- 8. the readout itself


def test_the_audit_runs_with_no_database_and_covers_every_portal() -> None:
    from location_data import contracts

    audits = aud.audit(None, now=_NOW)
    assert [a.source for a in audits] == sorted(c.source for c in contracts.load_all())
    assert len(audits) == 9
    assert all(a.staging is None and a.payloads is None for a in audits)


def test_the_live_readout_reports_gated_portals_as_neither_wired_nor_absent() -> None:
    # End to end against the real modules and the real contracts: the state this
    # PR exists to make legible has to survive rendering.
    audits = {a.source: a for a in aud.audit(None, now=_NOW)}
    for source in ("sreality", "remax", "ceskereality"):
        assert audits[source].state == aud.STATE_GATED, source
        assert audits[source].verdict == aud.VERDICT_PARTIAL, source
    lines = "\n".join(aud.render(list(audits.values())))
    assert aud.STATE_GATED in lines
    assert "0 portal(s) unconditionally archive the index page" in lines


def test_the_summary_warns_that_flipping_the_flag_today_still_drops_bodies() -> None:
    # The operator-facing point of the whole audit: with every call site gated,
    # enabling payload_index_archive buys an archive with holes in it.
    lines = "\n".join(aud.render(list(aud.audit(None, now=_NOW))))
    assert "unrecoverable" in lines


def test_the_json_shape_carries_all_three_axes_and_the_verdict() -> None:
    payload = aud.to_json(aud.audit(None, now=_NOW))
    assert payload["states"] == [aud.STATE_WIRED, aud.STATE_GATED, aud.STATE_ABSENT]
    entry = next(p for p in payload["portals"] if p["source"] == "sreality")
    assert entry["contract"]["index_entries"] == 3
    assert entry["code"]["state"] == aud.STATE_GATED
    assert entry["code"]["gate"]
    assert entry["code"]["intentional_skips"]
    assert entry["verdict"] == aud.VERDICT_PARTIAL
