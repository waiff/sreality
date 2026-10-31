"""THE ACTING-ROUTES CENSUS — a route that acts for another account writes curation only.

Migration 641 lets a pipeline share's recipient ACT AS the owner on eleven curation tables
(docs/design/pipeline-sharing.md §3.2). A tenant write route declares which account it writes:
`tenant_pool.require_acting_account_id` (the caller's own, or under X-Act-As the owner) or
`tenant_pool.require_account_id` (always the caller's own; 403 under the header). This walks the
live app and, for every route declaring either, reads the SQL its handler can reach and checks:

  * an ACTING route writes (INSERT / UPDATE / DELETE) only the eleven curation tables;
  * an OWN-ACCOUNT route writes none of them, except where `_OWN_ACCOUNT_CURATION_WRITES` says why;
  * an RLS-ONLY tenant route (neither dependency) acts whenever the header is present, so one that
    writes curation must write nothing else, and one that reaches the SERVICE ROLE must declare
    `require_account_id` (a curation gate followed by a privileged write would delegate it);
  * the population is what it should be (non-vacuous every way).

HOW THE SQL IS FOUND: the endpoint's AST, then every function it calls that lives under `api.`
(module attribute or bare name, followed transitively), each contributing its string literals
(f-string constant parts included, docstrings excluded) and every module-level string constant it
names (`_FOO_SQL`); an endpoint whose SQL is BUILT (the saved-set factory, api/saved_sets.py) carries it as
its `runs_sql` tag, read here too. THE NAMED BLIND SPOTS: SQL built from a variable assembled elsewhere and
passed in, helpers outside `api/` (toolkit reads only, today), and dynamic dispatch. A rail that
names its edges cannot manufacture confidence.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
import types
from typing import Any, Callable

import pytest

pytest.importorskip("fastapi")

from fastapi.routing import APIRoute  # noqa: E402

from api import tenant_pool  # noqa: E402
from tests.api._routes import method_routes, reachable_calls  # noqa: E402

CURATION = frozenset({
    "collections", "tags", "property_notes", "collection_properties", "property_tags",
    "property_attachments", "property_artifact_links", "property_dismissals",
    "property_pipeline", "pipeline_stages", "property_pipeline_events",
})

# Own-account routes that write curation anyway, each with the reason it must NOT act.
_OWN_ACCOUNT_CURATION_WRITES: dict[str, str] = {
    "POST /collections/{collection_id}/shares": (
        "its ownership gate touches collections.updated_at, scoped to current_account_ids(); "
        "sharing never delegates (spec §3.3)"
    ),
}

# Service-role openers: reaching one from a tenant route is a privileged write after the tenant gate.
_SERVICE_ROLE = frozenset({"open_background_conn", "get_db_conn"})

# Routes that must never act: identity-scoped tables (docs/design/pipeline-sharing.md §3.7) and
# sharing itself (only the owner shares their own board). A READ of an identity-scoped table is no
# member: under the header its policy answers the caller's own rows (`GET /stats/dashboard`).
_NEVER_ACTS: frozenset[str] = frozenset({
    "POST /notifications/subscriptions",
    "POST /filter-presets",
    "POST /filter-preset-groups",
    "PUT /stats/dashboard",
    "POST /stats/dashboards",
    "POST /pipeline/shares",
    "POST /collections/{collection_id}/shares",
    "POST /stats/dashboards/{dashboard_id}/shares",
    # Push on this device (migration 654): a device belongs to the signed-in user, so a delegate's phone
    # gets only the delegate's alerts. (GET /push/key names no account at all: it reads no row.)
    "PUT /push/subscription",
    "DELETE /push/subscription",
    "POST /push/opened",
})

_WRITE = re.compile(r"\b(?:insert\s+into|update|delete\s+from)\s+([a-z_][a-z0-9_]*)", re.I)
_NOT_TABLES = frozenset({"set", "of"})  # UPDATE ... SET; SELECT ... FOR UPDATE OF <alias>


def _routes() -> list[tuple[str, APIRoute]]:
    return [(f"{m} {p}", r) for m, p, r in method_routes()]


def _strings(fn: Callable[..., Any], tree: ast.AST) -> list[str]:
    body = tree.body[0].body if isinstance(tree, ast.Module) else []
    docstring = body[0].value if body and isinstance(body[0], ast.Expr) else None
    out: list[str] = []
    for node in ast.walk(tree):
        if node is docstring:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.Name):
            value = fn.__globals__.get(node.id)
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, dict):
                out.extend(v for v in value.values() if isinstance(v, str))
    return out


def _callees(fn: Callable[..., Any], tree: ast.AST) -> list[Callable[..., Any]]:
    out: list[Callable[..., Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        target: Any = None
        if isinstance(f, ast.Name):
            target = fn.__globals__.get(f.id)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            owner = fn.__globals__.get(f.value.id)
            if isinstance(owner, types.ModuleType):
                target = getattr(owner, f.attr, None)
        if isinstance(target, types.FunctionType) and target.__module__.startswith("api."):
            out.append(target)
    return out


def _reach(endpoint: Callable[..., Any]) -> tuple[set[str], bool]:
    """(tables the reachable SQL writes, whether a service-role opener is named on the way)."""
    tables: set[str] = set()
    service_role = False
    seen: set[Callable[..., Any]] = set()
    stack = [endpoint]
    while stack:
        fn = stack.pop()
        if fn in seen:
            continue
        seen.add(fn)
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for text in [*_strings(fn, tree), *getattr(fn, "runs_sql", ())]:
            tables |= {m.group(1).lower() for m in _WRITE.finditer(text)} - _NOT_TABLES
        service_role |= any(
            (isinstance(n, ast.Attribute) and n.attr in _SERVICE_ROLE)
            or (isinstance(n, ast.Name) and n.id in _SERVICE_ROLE)
            for n in ast.walk(tree)
        )
        stack.extend(_callees(fn, tree))
    return tables, service_role


def _written_tables(endpoint: Callable[..., Any]) -> set[str]:
    return _reach(endpoint)[0]


def _by_dependency() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    acting: dict[str, set[str]] = {}
    own: dict[str, set[str]] = {}
    for key, route in _routes():
        deps = reachable_calls(route)
        if tenant_pool.require_acting_account_id in deps:
            acting[key] = _written_tables(route.endpoint)
        elif tenant_pool.require_account_id in deps:
            own[key] = _written_tables(route.endpoint)
    return acting, own


def _rls_only() -> dict[str, tuple[set[str], bool]]:
    """Tenant routes declaring neither write-account dependency: the header acts through RLS alone."""
    out: dict[str, tuple[set[str], bool]] = {}
    for key, route in _routes():
        deps = reachable_calls(route)
        if (tenant_pool.tenant_conn in deps
                and tenant_pool.require_acting_account_id not in deps
                and tenant_pool.require_account_id not in deps):
            out[key] = _reach(route.endpoint)
    return out


def test_an_acting_route_writes_only_the_eleven_curation_tables() -> None:
    acting, _own = _by_dependency()
    offenders = sorted(
        f"  {key} writes {sorted(tables - CURATION)}"
        for key, tables in acting.items() if tables - CURATION
    )
    assert not offenders, (
        "route(s) declaring require_acting_account_id write outside the eleven curation tables "
        "— a delegate would write there AS THE OWNER. Declare require_account_id instead:\n"
        + "\n".join(offenders)
    )


def test_an_own_account_route_writes_no_curation_table() -> None:
    _acting, own = _by_dependency()
    offenders = sorted(
        f"  {key} writes {sorted(tables & CURATION)}"
        for key, tables in own.items()
        if tables & CURATION and key not in _OWN_ACCOUNT_CURATION_WRITES
    )
    assert not offenders, (
        "route(s) declaring require_account_id write a curation table — under a share they "
        "refuse to act (403). Declare require_acting_account_id, or say why here:\n"
        + "\n".join(offenders)
    )


def test_an_rls_only_route_acts_on_curation_alone_and_never_reaches_the_service_role() -> None:
    offenders = []
    for key, (tables, service_role) in sorted(_rls_only().items()):
        if tables & CURATION and tables - CURATION:
            offenders.append(f"  {key} writes curation AND {sorted(tables - CURATION)}")
        if service_role and tables & CURATION:
            offenders.append(f"  {key} gates on curation, then reaches the service role")
    assert not offenders, (
        "RLS-only tenant route(s) act under X-Act-As (the header reaches every curation policy) but "
        "would carry that delegation into a non-curation or service-role write. Declare "
        "require_account_id (never acts) or require_acting_account_id:\n" + "\n".join(offenders)
    )


def test_the_census_is_not_vacuous() -> None:
    acting, own = _by_dependency()
    for key in ("POST /pipeline/cards", "PATCH /pipeline/cards/{property_id}",
                "POST /pipeline/stages", "POST /collections", "POST /dismissals"):
        assert key in acting, f"{key} no longer declares require_acting_account_id"
        assert acting[key], f"{key}: the census found no write SQL — the scanner went blind"
    assert {"property_pipeline", "property_pipeline_events"} <= acting["POST /pipeline/cards"]
    assert "property_dismissals" in acting["POST /pipeline/cards"], (
        "the scanner no longer follows a helper into another api module "
        "(dismissals.lift_dismissals_of_live_deals)"
    )
    missing = sorted(_NEVER_ACTS - set(own))
    assert not missing, f"identity-scoped write route(s) no longer declare require_account_id: {missing}"
    assert not set(_OWN_ACCOUNT_CURATION_WRITES) - set(own), "stale _OWN_ACCOUNT_CURATION_WRITES entry"
    # The saved-set factory builds its SQL from the kind; its `runs_sql` tag is what the census reads.
    assert own["POST /filter-presets"] == {"filter_presets"}
    assert own["POST /filter-preset-groups"] == {"filter_preset_groups"}
    assert own["POST /stats/dashboards"] == {"stats_dashboards"}
    assert {"stats_dashboards", "stats_dashboard_shares", "notification_dispatches"} <= own[
        "POST /stats/dashboards/{dashboard_id}/shares"]
    rls_only = _rls_only()
    assert "collections" in rls_only["PATCH /collections/{collection_id}"][0]
    assert rls_only["DELETE /collections/{collection_id}/shares/{account_id}"][0] == {"collection_shares"}
    assert _reach(_endpoint("POST /pipeline/shares"))[1], "the scanner lost the service-role opener"


def _endpoint(key: str) -> Callable[..., Any]:
    return dict(_routes())[key].endpoint
