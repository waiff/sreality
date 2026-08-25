"""Rule 17 must survive the obec key swap (W5, migration 436).

**The schema used to enforce rule 17 for free, and W5 takes that away.**

`listings` has no `home_city_id`, so until now a city-quality clause on a listings-grain
query died at parse with `42703: column l.home_city_id does not exist` — before a row was
read, data-independent, unmissable. W5 re-keys membership onto `l.obec_id`, a column
`listings` DOES have. The identical bypass would now plan, execute, and silently return an
estimate narrowed by operator-curated, revision-versioned, *subjective* city scores — with a
`status='success'` row in `estimation_runs` and a full trace. Nothing would fail.

The design proposal claimed the rewrite removes this latent failure "structurally". **It is
the reverse.** These are the rails that replace what the schema was doing.

Two facts make this urgent rather than theoretical:

  * **Nothing tested the agenda gate for these filters before.** An exhaustive grep found
    `city_index_rules` asserted only in `tests/api/test_notifications.py`, and only for
    rendered watchdog SQL shape. `tests/toolkit/test_filter_registry.py` has the exact
    assertion needed — twice — but for `mf_gross_yield` and `category_main`, never for the
    rule-17 filters. Deleting the agenda gate today turned nothing red.
  * **The old safety argument was a docstring.** `_city_quality_clauses` closed with *"The
    listings-grain callers via `_shared_filter_where` never set these filters, so the whole
    helper is inert for them."* That is an assumption about caller behaviour with no
    mechanism behind it — and `_shared_filter_where` called the helper unconditionally, with
    no grain argument and no guard.

Offline; runs in the normal `pytest -q` lane.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import toolkit.filter_registry as fr
from toolkit.comparables import (
    ComparableFilters,
    TargetSpec,
    _assert_no_city_quality,
    _CITY_QUALITY_FIELDS,
    _city_quality_clauses,
    _shared_filter_where,
)

_TARGET = TargetSpec(lat=50.08, lng=14.42)

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _ROOT / "migrations" / "436_city_quality_obec_key.sql"


# --- the rule-17 re-arm ----------------------------------------------------


def test_city_quality_on_a_listings_grain_call_raises():
    """RED by: replacing the raise in `_shared_filter_where` with an inert branch.

    This is the assertion that replaces the 42703 the schema used to throw.
    """
    filters = ComparableFilters(
        city_index_rules=[{"index_name": "celkove_hodnoceni", "value": 6, "op": ">="}]
    )
    with pytest.raises(ValueError, match="rule 17 violation"):
        _shared_filter_where(_TARGET, filters)


@pytest.mark.parametrize("field", _CITY_QUALITY_FIELDS)
def test_every_city_quality_field_is_guarded(field: str):
    """Not just `city_index_rules` — every field of the family.

    RED by: removing any name from `_CITY_QUALITY_FIELDS`.
    """
    value: object = 1
    if field == "city_index_rules":
        value = [{"index_name": "x", "value": 1, "op": ">="}]
    elif field == "near_city_proximity":
        value = {"radius_km": 10, "index_rules": []}
    filters = ComparableFilters(**{field: value})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="rule 17 violation"):
        _assert_no_city_quality(filters)


def test_a_clean_listings_grain_call_still_passes():
    """The guard must not fire on ordinary comparables."""
    _assert_no_city_quality(ComparableFilters(category_main="byt"))
    where, _ = _shared_filter_where(_TARGET, ComparableFilters(category_main="byt"))
    assert where, "the ordinary path stopped rendering predicates"


def test_the_agenda_gate_excludes_city_quality_from_estimation():
    """The gate that keeps operator-curated scores out of a deterministic estimate.

    This assertion did not exist before W5 — deleting the gate turned nothing red.

    RED by: adding Agenda.COMPARABLES or Agenda.ESTIMATION to the city_index_rules
    FilterDef.
    """
    for agenda in (fr.Agenda.COMPARABLES, fr.Agenda.ESTIMATION):
        ids = {f.id for f in fr.filters_for_agenda(agenda)}
        leaked = ids.intersection(_CITY_QUALITY_FIELDS)
        assert not leaked, (
            f"rule 17: {sorted(leaked)} reachable from the {agenda} agenda — an estimate "
            "would depend on a city_index_* revision"
        )


# --- the one predicate -----------------------------------------------------


def test_the_watchdog_matcher_renders_one_obec_predicate():
    """Three hand-maintained copies collapse to one form (rule 16).

    RED by: restoring the nested-EXISTS tree.
    """
    where, params = _city_quality_clauses(
        ComparableFilters(
            city_index_rules=[{"index_name": "celkove_hodnoceni", "value": 6, "op": ">="}]
        )
    )
    city = [w for w in where if "curated_cities_matching" in w]
    assert len(city) == 1, f"expected one city-quality predicate, got {where}"
    assert "l.obec_id = ANY (ARRAY(SELECT curated_cities_matching(" in city[0]
    # ARRAY(SELECT ...) forces a once-per-statement InitPlan; IN (SELECT ...) can degrade
    # into the per-row correlated SubPlan that cost 1,778,259 blocks.
    assert "IN (SELECT" not in city[0].upper().replace("ANY (ARRAY(SELECT", "")
    assert "EXISTS" not in city[0]
    assert "home_city_id" not in city[0]
    assert params["city_index_rules"].obj[0]["index_name"] == "celkove_hodnoceni"


def test_no_operator_token_is_string_interpolated_any_more():
    """The op whitelist now lives in the function's CASE, not in Python string building.

    RED by: reintroducing `_index_rule_predicate`.
    """
    import toolkit.comparables as c

    assert not hasattr(c, "_index_rule_predicate"), (
        "_index_rule_predicate is back — the operator token is being interpolated again"
    )
    assert not hasattr(c, "_ALLOWED_OPS")


def test_near_city_proximity_is_retired_loudly():
    """A retired filter that silently returns everything WIDENS the cohort.

    RED by: deleting the raise (silently dropping the branch).
    """
    with pytest.raises(ValueError, match="near_city_proximity is retired"):
        _city_quality_clauses(
            ComparableFilters(near_city_proximity={"radius_km": 10, "index_rules": []})
        )


def test_near_city_proximity_is_gone_from_every_agenda():
    """RED by: leaving the FilterDef in the registry."""
    assert "near_city_proximity" not in fr.REGISTRY
    for agenda in fr.Agenda:
        ids = {f.id for f in fr.filters_for_agenda(agenda)}
        assert "near_city_proximity" not in ids, f"still offered to {agenda}"


# --- the migration ---------------------------------------------------------


def test_the_migration_asserts_the_obec_invariant():
    """The load-bearing assumption is PROVEN in the migration, not inferred.

    It asserts the RELATIONS, not the literal 206 — a legitimately added 207th city must
    not fail the migration, an UNLINKED one must.

    RED by: deleting the DO block.
    """
    body = _MIGRATION.read_text()
    assert "curated_cities obec-key invariant violated" in body
    for clause in ("v_total <> v_linked", "v_total <> v_obec", "v_dangling <> 0",
                   "v_total <> v_distinct"):
        assert clause in body, f"the invariant no longer checks {clause}"
    assert "206" not in body.split("raise exception")[0].split("do $$")[-1], (
        "the invariant hard-codes 206 — it must assert the relations so a 207th "
        "curated city does not fail the migration"
    )


def test_the_revision_fix_is_latest_per_pair_not_global_max():
    """A partial revision upload must not empty every other city's cohort.

    Measured: the view returns 6,798 rows today; under the global-max spelling a one-city
    revision 3 would make it return 33 — a 99.5% collapse, every other city silently
    failing every rule, with no error anywhere.

    RED by: restoring `source_revision = (select max(source_revision) from ...)`.
    """
    body = _MIGRATION.read_text()
    viewdef_at = body.index("create or replace view city_index_values_public")
    viewdef = body[viewdef_at : body.index(";", viewdef_at)]
    assert "newer.city_id" in viewdef and "newer.index_name" in viewdef, (
        "the latest-revision filter is not correlated to (city_id, index_name) — a "
        "partial upload would empty every other city"
    )
    assert "max(" not in viewdef.lower(), "the global-max spelling is back"


def test_the_migration_reads_the_public_views_not_the_base_tables():
    """The single most dangerous detail in the migration.

    `curated_cities` and `city_index_values` are RLS-on with ZERO policies. A SECURITY
    INVOKER function reading the BASE tables returns zero rows for `authenticated` —
    silently, not as an error — collapsing every city-quality cohort to empty.

    RED by: changing `curated_cities_public` to `curated_cities` in the function body.
    """
    body = _MIGRATION.read_text()
    fn_at = body.index("create or replace function public.curated_cities_matching")
    fn = body[fn_at : body.index("$function$;", fn_at)]
    assert "from curated_cities_public" in fn
    assert "from city_index_values_public" in fn
    assert not re.search(r"from\s+curated_cities\s", fn), (
        "the function reads the RLS-locked base table — it would return zero rows for "
        "authenticated and silently empty every cohort"
    )
    assert not re.search(r"from\s+city_index_values\s", fn)
