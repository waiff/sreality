"""Migration 643's rails (Health W9): exactly the old health layer goes, in one transaction, and the arbiter stays."""

from __future__ import annotations

import re

import pytest

from scripts.migration_objects import _dropped, _strip_noise, parse_objects
from tests.migration_defs import latest_definition
from tests.test_630_one_browse_read_model import MIGRATIONS, _code
from tests.test_measure_registry_census import effective_sql_definitions
from tests.test_migration_rls_grants import _dynamic_ddl
from toolkit.system_alerts import _OPEN_SQL

SQL = (MIGRATIONS / "643_drop_legacy_health_layer.sql").read_text(encoding="utf-8")
S440 = (MIGRATIONS / "440_derived_artifacts_registry_completion.sql").read_text(encoding="utf-8")
S441 = (MIGRATIONS / "441_stamp_derived_artifact.sql").read_text(encoding="utf-8")
S621 = (MIGRATIONS / "621_platform_checks.sql").read_text(encoding="utf-8")

# The refresh's first lock first; scraper_health_checks_mv before snapshot_churn_24h_mv, the matview it reads.
MATVIEWS = ["health_summary_mv", "portal_health_mv", "scraper_health_checks_mv", "snapshot_churn_24h_mv",
            "category_trends_mv", "health_mv_refresh_stamp", "image_storage_overview_mv", "images_failure_overview_mv"]
VIEWS = ["derived_artifacts_public", "worker_liveness", "detail_latency_recent", "listing_presence_checks_public",
         "scrape_runs_public", "listing_detail_queue_public", "listing_fetch_failures_public", "parsed_url_activity",
         "portals_public", "portal_listing_counts", "data_quality_by_source", "pipeline_check_history_public"]
# ops_incident_runs last: dropping its FK locks ops_incidents.
TABLES = ["pipeline_check_results_retired_2026_09_22", "portal_coverage_gate", "dual_write_watermark",
          "ops_incident_runs"]
FUNCTIONS = ["refresh_health_matviews", "health_summary", "portal_health_summary", "scraper_health_checks",
             "category_trends", "image_storage_overview", "images_failure_overview", "recent_scrape_runs",
             "workflow_failure_summary", "recent_workflow_failures", "emit_verification_stale_alert"]
ENGINE_COLUMNS = ["workflow_paths", "origins", "sample_run_url", "sample_excerpt", "failure_count"]
COLUMNS = [f"ops_incidents.{c}" for c in ENGINE_COLUMNS] + ["portals.scrape_cadence_minutes"]
SETTINGS = ["pipeline_check_thresholds", "broker_resolution_last_complete", "property_sweep_last_complete"]
ARBITER = "ops_incidents_open_signature_uq"
JOB = "refresh-health-dashboard"
_TYPE_ALIASES = {"int": "integer", "int4": "integer"}


def _block(text: str, tag: str) -> str:
    at = text.index(f"do ${tag}$")
    return text[at: text.index(f"${tag}$;", at)]


def _names(sql_list: str) -> list[str]:
    return re.findall(r"'([a-z0-9_]+)'", sql_list)


def _in_lists(table: str, column: str) -> list[list[str]]:
    """The quoted names of each `delete from public.<table> where <column> in (...)`, in file order."""
    return [_names(m) for m in re.findall(rf"delete from public\.{table} where {column} in \(([^)]*)\);", _code(SQL))]


def _arg_types(text: str, fn: str) -> str:
    """The argument types of `fn`'s CREATE in `text`, normalised (`public.` optional: 274 spells it bare)."""
    m = re.search(rf"create\s+or\s+replace\s+function\s+(?:public\.)?{fn}\s*\(", text, re.I)
    depth, i = 1, m.end()
    while depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    args = " ".join(re.sub(r"--[^\n]*", "", text[m.end(): i - 1]).split())
    types = []
    for part in filter(None, (p.strip() for p in re.split(r",(?![^()]*\))", args))):
        words = part.split()
        if words[0].lower() in {"in", "out", "inout", "variadic"}:
            words = words[1:]
        typ = re.split(r"\s+default\s+|\s*=\s*", " ".join(words[1:]), flags=re.I)[0].lower()
        types.append(_TYPE_ALIASES.get(typ, typ))
    return ", ".join(types)


def _registry_seed() -> dict[str, str]:
    """440's derived_artifacts seed: {artifact name: producer}."""
    return dict(re.findall(r"\('([a-z0-9_]+)',\s*'([^']+)'", S440))


def test_exactly_the_old_layer_goes_and_nothing_cascades() -> None:
    """RED by: a CASCADE, an EXECUTE-built drop, a dropped arbiter index or history table, a new object."""
    assert _dropped(SQL) == ({("relation", r) for r in MATVIEWS + VIEWS + TABLES}
                             | {("function", f) for f in FUNCTIONS} | {("column", c) for c in COLUMNS})
    assert parse_objects(SQL) == [], "643 creates nothing; the apply receipt has nothing to probe"
    code = _code(SQL)
    for absent in ("cascade", "drop index", "execute", "to_regproc("):
        assert absent not in code, absent
    assert not re.search(r"\btruncate\b", _strip_noise(SQL), re.I), "a TRUNCATE statement"
    assert not _dynamic_ddl(SQL)
    assert ("relation", "data_quality_snapshots") not in _dropped(SQL), "ruling Q5 keeps it"
    assert ("relation", ARBITER) not in _dropped(SQL)
    drops = sorted(" ".join(m.split()).lower() for m in re.findall(
        r"\b(?:drop|alter\s+table)\b[^;]*", _strip_noise(SQL), re.I))
    assert drops == sorted(
        [f"drop materialized view if exists public.{r}" for r in MATVIEWS]
        + [f"drop view if exists public.{r}" for r in VIEWS]
        + [f"drop table if exists public.{r}" for r in TABLES]
        + [f"drop function if exists public.{f}({_arg_types(latest_definition(f).read_text(encoding='utf-8'), f)})"
           for f in FUNCTIONS]
        + ["alter table public.ops_incidents " + ", ".join(f"drop column if exists {c}" for c in ENGINE_COLUMNS),
           "alter table public.portals drop column if exists scrape_cadence_minutes"])


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_each_function_is_dropped_by_its_latest_definitions_signature(fn: str) -> None:
    """RED by: a signature typo, which DROP FUNCTION IF EXISTS turns into a silent notice."""
    types = _arg_types(latest_definition(fn).read_text(encoding="utf-8"), fn)
    assert _code(SQL).count(f"drop function if exists public.{fn}({types});") == 1
    assert f"to_regprocedure('public.{fn}({types})')" in _block(SQL, "pre"), "the overload guard spells another one"


def test_the_census_sees_each_matview_drop() -> None:
    """The per-m2 census supersedes a CREATE through the FIRST name of a DROP only, and scraper_health_checks_mv
    carried a registered prose hit (354): each matview is its own statement, so its definition leaves the census."""
    assert not re.search(r"drop (?:materialized view|view|table) if exists [^;]*,", _code(SQL))
    effective = effective_sql_definitions()
    survivors = [k for k in (f"materialized view:{m}" for m in MATVIEWS) if k in effective]
    assert survivors == []


def test_the_steps_run_in_order_inside_one_transaction() -> None:
    """RED by: a DROP before the guard or the unschedule, a registry row deleted before its matview (the stamp's lock
    order), a view before the matview that reads it, a hot table locked before the rest, or a second transaction."""
    code = _code(SQL)
    steps = (["set lock_timeout = '5s';", "set statement_timeout = '120s';", "begin;", "do $pre$", "do $cron$",
              "drop materialized view if exists public.health_summary_mv;",
              "delete from public.derived_artifacts where name in ('health_summary_mv',"]
             + [f"drop materialized view if exists public.{m};" for m in MATVIEWS[1:]]
             + ["delete from public.derived_artifacts where name in ('image_storage_overview_mv',"]
             + [f"drop view if exists public.{v};" for v in VIEWS]
             + [f"drop function if exists public.{f}(" for f in FUNCTIONS]
             + [f"drop table if exists public.{t};" for t in TABLES]
             + ["alter table public.ops_incidents drop column", "alter table public.portals drop column",
                "delete from public.app_settings", "update public.ops_incidents",
                "comment on table public.ops_incidents is", f"comment on index public.{ARBITER} is",
                "comment on table public.data_quality_snapshots is",
                "comment on table public.pipeline_check_results is",
                "comment on view public.measure_plausibility_by_source is", "do $post$",
                "notify pgrst, 'reload schema';", "commit;", "reset statement_timeout;", "reset lock_timeout;"])
    for step in steps:
        assert code.count(step) == 1, step
    at = [code.index(step) for step in steps]
    assert at == sorted(at), [s for s, a, b in zip(steps, at, at[1:]) if a > b]


def test_the_unschedule_is_guarded_for_the_replay_and_a_missing_job() -> None:
    """By id from its name (633's idiom): the name form raises when the job is absent, so a re-run would fail."""
    cron = _code(_block(SQL, "cron"))
    unschedule = f"perform cron.unschedule(j.jobid) from cron.job j where j.jobname = '{JOB}';"
    guard = "to_regclass('cron.job') is null"
    assert unschedule in cron and guard in cron
    assert cron.index(guard) < cron.index(unschedule)
    assert "cron.unschedule('" not in _code(SQL)


def test_621s_resolve_is_repeated_verbatim() -> None:
    def resolve(text: str) -> str:
        at = text.index("update public.ops_incidents")
        return " ".join(text[at: text.index(";", at) + 1].split())

    assert resolve(SQL) == resolve(S621)
    assert resolve(SQL) == ("update public.ops_incidents set resolved_at = now(), resolve_reason = 'retired: signature"
                            " engine' where resolved_at is null and not starts_with(signature, 'cell:');")
    assert _code(SQL).count("update public.ops_incidents") == 1


def test_the_rows_deleted_are_exactly_the_dropped_ones() -> None:
    """The six the pg_cron refresh stamps go with its unschedule, the image pair after its matviews, nothing else."""
    seed = _registry_seed()
    by_refresh = sorted(n for n, producer in seed.items() if producer == "refresh_health_matviews")
    by_images = sorted(n for n, producer in seed.items() if producer == "scripts/refresh_image_stats.py")
    stamped = re.findall(r"perform public\.stamp_derived_artifact\('([a-z0-9_]+)'",
                         S441[S441.index("create or replace function public.refresh_health_matviews()"):])
    first, second = _in_lists("derived_artifacts", "name")
    assert sorted(first) == by_refresh == sorted(stamped)
    assert sorted(second) == by_images
    assert sorted(first + second) == sorted(MATVIEWS)
    assert _in_lists("app_settings", "key") == [SETTINGS]


def test_pre_guards_exactly_what_goes_and_requires_the_arbiter() -> None:
    """RED by: a name added to the DROPs but not to the guard (its readers would go unchecked)."""
    pre = _block(SQL, "pre")
    lists = dict(re.findall(r"(c_[a-z_]+) constant text\[\] := array\[([^\]]*)\]", pre))
    assert _names(lists["c_rels"]) == MATVIEWS + VIEWS + TABLES
    assert _names(lists["c_fns"]) == FUNCTIONS
    assert _names(lists["c_engine_cols"]) == ENGINE_COLUMNS
    flat = _code(pre)
    for probe in ("pg_views", "pg_matviews", "p.prosrc", "pg_depend", "cron.job", "scrape_cadence_minutes",
                  f"to_regclass('public.{ARBITER}') is null"):
        assert probe in flat, probe
    assert flat.index(f"to_regclass('public.{ARBITER}') is null") < flat.index("raise notice '643: no pg_cron here")


def test_post_names_what_goes_and_what_stays() -> None:
    post = _block(SQL, "post")
    for name in MATVIEWS + VIEWS + TABLES + FUNCTIONS + ENGINE_COLUMNS + SETTINGS:
        assert f"'{name}'" in post, name
    for kept in (ARBITER, "data_quality_snapshots", "pipeline_checks_public", f"'{JOB}'",
                 "starts_with(signature, 'cell:')", "relrowsecurity"):
        assert kept in post, kept
    assert "count(*) from public.derived_artifacts" not in _code(post), (
        "a replay holds other registry rows at this point than production: assert the eight names gone, never a count")


def test_the_ladder_still_needs_the_arbiter() -> None:
    """If the ladder ever opens incidents without ON CONFLICT, the index may go too: deliberately, not by this file."""
    assert "ON CONFLICT (signature) WHERE resolved_at IS NULL" in _OPEN_SQL
    assert "(signature) WHERE (resolved_at IS NULL)" in _block(SQL, "post")


def test_the_comments_stop_naming_what_goes() -> None:
    """Each restated comment leaves the dropped views and the engine's vocabulary behind; 633's own rail still holds."""
    code = _code(SQL)

    def comment(head: str) -> str:
        at = code.index(head)
        return code[at: code.index("';", at)]

    incidents = comment("comment on table public.ops_incidents is")
    assert "cell:<check_key>:<subject>" in incidents and "failure signature" not in incidents
    assert "pipeline_check_history_public" not in comment("comment on table public.pipeline_check_results is")
    plausibility = comment("comment on view public.measure_plausibility_by_source is")
    assert "data_quality_by_source" not in plausibility
    assert "scripts/checks/quality.py" in plausibility and "the data_quality gauge" in plausibility
    for retired in ("ppm2_median_shift", "ppm2_basis_floor_share", "area_vs_usable_divergence", "verify_pipeline"):
        assert retired not in plausibility, retired


def test_no_revert_is_replayed() -> None:
    assert not list(MIGRATIONS.glob("643_revert*.sql")), "a revert beside 643 would be replayed by CI"


def test_the_dml_touches_only_the_registry_the_settings_and_the_incidents() -> None:
    """A file ruled to KEEP everything else must not slip in a write elsewhere."""
    found = sorted(re.findall(r"\b(?:delete\s+from|update|insert\s+into)\s+public\.\w+", _code(SQL), flags=re.I))
    assert found == sorted(["delete from public.derived_artifacts"] * 2 + ["delete from public.app_settings", "update public.ops_incidents"]), found
