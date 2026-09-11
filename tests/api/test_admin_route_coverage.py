"""Standing gate: admin-class API routes carry `require_admin`, and no route ships
unauthenticated by accident.

The DB side now has a generalizing gate (tests/test_tenant_isolation_live.py), but
nothing checked the API side, so a new /admin or /labeling route that forgot
`Depends(require_admin)` would ship un-caught. This walks the live FastAPI app and
buckets each route by the auth dependency reachable from its dependant tree —
router-level ``dependencies=[...]``, per-parameter ``Depends(...)``, and their nested
deps, so ``require_admin``'s inner ``verify_jwt`` is reached.

Residual gap, deliberate: this asserts admin-gating for routes under the KNOWN admin
prefixes. A brand-new admin surface mounted under a brand-new prefix is only caught by
the unauthenticated check below (if it has no auth at all) — it is the API analogue of
the DB gate's shared-market blind spot. Add the prefix here when you add the router.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.routing import APIRoute  # noqa: E402

from api import dependencies as deps  # noqa: E402
from api import tenant_pool  # noqa: E402
from api.main import app  # noqa: E402

# Routes that legitimately authenticate some other way. Every entry is a deliberate,
# reviewable decision — adding one should take an argument, not a reflex.
_PUBLIC_ALLOWLIST: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/health"),               # liveness probe
    ("GET", "/images/{key:path}"),    # R2 image proxy, opaque keys
    # Authenticates in-handler by verifying a Stripe HMAC over the raw request body.
    ("POST", "/billing/webhook"),
    # Authenticates in-handler by verifying a Svix (Resend) HMAC over the raw body.
    ("POST", "/webhooks/resend"),
    # RFC 8058 one-click unsubscribe — the HMAC token in the path IS the auth, and it
    # must render for a logged-out recipient (no session).
    ("GET", "/u/{token}"),
    ("POST", "/u/{token}"),
})

# Every route under these must resolve to require_admin. `/properties` as a whole is
# NOT an admin prefix (notes/tags under it ride the plain bearer gate), so the merge
# mechanics are listed by their own sub-paths — "/properties/merge" covers /merge,
# /merges and /merged.
_ADMIN_PREFIXES: tuple[str, ...] = (
    "/admin", "/properties/merge", "/properties/assets", "/labeling",
    "/outreach", "/broker-review", "/skill-refinements", "/location",
)

# Sentinels proving those routers actually mounted. Without this, a mis-mounted router
# would leave an empty route table and every assertion below would pass vacuously.
_MOUNT_SENTINELS: tuple[str, ...] = (
    "/admin", "/properties/merge", "/labeling", "/notifications",
)


def _reachable_calls(dependant) -> set:
    """Every dependency callable reachable from a route, including nested ones."""
    seen: set[int] = set()
    out: set = set()
    stack = list(dependant.dependencies)
    while stack:
        d = stack.pop()
        call = getattr(d, "call", None)
        if call is not None and id(call) not in seen:
            seen.add(id(call))
            out.add(call)
        stack.extend(d.dependencies)
    return out


def _bucket(route: APIRoute) -> str:
    calls = _reachable_calls(route.dependant)
    if deps.require_admin in calls:
        return "admin"
    if tenant_pool.tenant_conn in calls or deps.verify_jwt in calls:
        return "tenant"
    if deps.require_token in calls:
        return "token"
    # account_scope is an EITHER gate: it accepts the static token OR a real
    # Supabase JWT on the one Authorization header, and resolves the caller's
    # read scope. It is strictly stronger than require_token (same secret, same
    # timing-safe compare, plus an account predicate), so it buckets with it.
    if deps.account_scope in calls:
        return "token"
    return "public"


def _collect(routes) -> list[APIRoute]:
    """Flatten the app's route table across FastAPI versions.

    Older versions splice an included router's routes directly into app.routes; since
    0.13x `include_router` instead appends one `_IncludedRouter` wrapper holding the
    original router. Recurse through either shape, or this returns almost nothing and
    every assertion below passes vacuously (which is what the mount sentinel catches).
    """
    out: list[APIRoute] = []
    for r in routes:
        if isinstance(r, APIRoute):
            out.append(r)
            continue
        nested = getattr(getattr(r, "original_router", None), "routes", None)
        if nested is None:
            nested = getattr(r, "routes", None)
        if nested:
            out.extend(_collect(nested))
    return out


def _api_routes() -> list[tuple[str, str, APIRoute]]:
    return [
        (method, r.path, r)
        for r in _collect(app.routes)
        for method in sorted(r.methods - {"HEAD", "OPTIONS"})
    ]


def test_admin_routers_are_mounted() -> None:
    """If include_router silently mounted nothing, the assertions below would hold
    over an empty route table — so fail loudly instead of passing vacuously."""
    paths = {p for _, p, _ in _api_routes()}
    missing = [s for s in _MOUNT_SENTINELS if not any(p.startswith(s) for p in paths)]
    assert not missing, (
        f"no routes mounted under {missing} — the coverage assertions below would be "
        f"vacuous. Check the fastapi/python versions before trusting a green run."
    )


def test_admin_surfaces_require_admin() -> None:
    """Every route under an admin prefix must resolve to require_admin."""
    offenders = sorted(
        f"  {method} {path} -> {_bucket(route)}"
        for method, path, route in _api_routes()
        if path.startswith(_ADMIN_PREFIXES) and _bucket(route) != "admin"
    )
    assert not offenders, (
        "admin-prefixed route(s) are not admin-gated. Add "
        "Depends(deps.require_admin) — SPA route-gating is a client affordance, not a "
        "security boundary:\n" + "\n".join(offenders)
    )


def test_no_route_is_unauthenticated_by_accident() -> None:
    """A new route with no auth dependency at all lands in `public`; only the three
    known-public endpoints may."""
    unexpected = sorted(
        f"  {method} {path}"
        for method, path, route in _api_routes()
        if _bucket(route) == "public" and (method, path) not in _PUBLIC_ALLOWLIST
    )
    assert not unexpected, (
        "route(s) carry NO authentication dependency. Add one, or allowlist with a "
        "reason if it is deliberately public:\n" + "\n".join(unexpected)
    )


# --- the route-scope census (W5) -------------------------------------------
# The three tests above prove a route is AUTHENTICATED. Nothing proved one was
# SCOPED — which is how `POST /listings/lookup` shipped for seven weeks (PR #917,
# 2026-07-23) resolving no account at all while its SQL bound NULL into three
# `account_id IS NOT DISTINCT FROM %s` predicates.
#
# The doctrine this encodes (full version:
# `.claude/skills/database/references/tenancy.md`): reads on a tenant connection are
# scoped by RLS alone — `current_account_ids()` is the ONE definition of who the
# caller is; writes carry exactly ONE account, resolved once at the route edge by
# `tenant_pool.require_account_id` (400 "no account for caller" on none).
#
# SCOPE, stated honestly. The subject is routes reaching `tenant_pool.tenant_conn`.
# `_bucket` says "tenant" for anything reaching `verify_jwt`, which also covers the
# twelve `/brokers/*` + `POST /estimations` routes — those run on the service-role
# `get_db_conn`, where `verify_jwt` is authentication only and RLS is not the scope.
# They are EXCLUDED structurally, not by twelve copies of one sentence: an allowlist
# that doubles in size for a class it cannot judge is a gate that cries wolf, and a
# gate that cries wolf gets disabled. The account each of them resolves by hand is
# the subject of the second arm of `tests/api/test_account_scope_census.py`.

# Every entry is a deliberate, reviewable decision — adding one should take an
# argument, not a reflex. The reason must say what scopes the route INSTEAD.
_RLS_ONLY_ALLOWLIST: dict[str, str] = {
    # The flagship, and the route the outage was about.
    "POST /listings/lookup": (
        "RLS-only by design (W3): membership must match the SPA's plural "
        "current_account_ids() read. The route takes no account and its SQL carries no "
        "account predicate, so the extension's answer IS the SPA's by construction — a "
        "second, explicitly-bound definition is exactly what made them disagree for "
        "seven weeks. Re-adding an account argument here is the regression, not the fix."
    ),
    # Child-grain INSERTs: the account is derived in the DB, not at the route. The
    # most robust shape in the repo — migrations/292_child_grain_account_scoping.sql
    # puts a BEFORE INSERT/UPDATE trigger on the child that reads account_id off the
    # PARENT under the caller's own RLS, and WITH CHECK runs AFTER the trigger, so a
    # row pointing at another tenant's parent gets NULL back and fails closed. The
    # route cannot mis-scope an account it never names.
    "POST /collections/{collection_id}/properties": (
        "account trigger-derived from the parent `collections` row (migration 292)"
    ),
    "POST /properties/{property_id}/tags": (
        "account trigger-derived from the parent `tags` row (migration 292)"
    ),
    # Curation + notifications: reads, and UPDATE/DELETE of a row that already exists.
    # An INSERT must NAME its owner (WITH CHECK can validate the account a row claims,
    # never choose one); an UPDATE/DELETE by id is scoped by the SAME policy's USING
    # clause, so an account predicate there would be a second definition of the caller,
    # not a gate. That is why `POST /collections` carries require_account_id and
    # `PATCH /collections/{id}` does not.
    "GET /collections": "RLS-only read (migration 290 policy on `collections`)",
    "GET /collections/{collection_id}": "RLS-only read (migration 290)",
    "PATCH /collections/{collection_id}": "UPDATE by id — migration 290 USING clause",
    "DELETE /collections/{collection_id}": "DELETE by id — migration 290 USING clause",
    "DELETE /collections/{collection_id}/properties/{property_id}": (
        "DELETE by id on a child-grain table — migration 292 USING clause"
    ),
    "GET /properties/{property_id}/notes": "RLS-only read (migration 290 on `property_notes`)",
    "PATCH /properties/{property_id}/notes/{note_id}": "UPDATE by id — migration 290 USING clause",
    "DELETE /properties/{property_id}/notes/{note_id}": "DELETE by id — migration 290 USING clause",
    "GET /tags": "RLS-only read (migration 290 policy on `tags`)",
    "PATCH /tags/{tag_id}": "UPDATE by id — migration 290 USING clause",
    "DELETE /tags/{tag_id}": "DELETE by id — migration 290 USING clause",
    "DELETE /properties/{property_id}/tags/{tag_id}": (
        "DELETE by id on a child-grain table — migration 292 USING clause"
    ),
    "GET /notifications/subscriptions": (
        "RLS-only read (migration 290 on `notification_subscriptions`)"
    ),
    "GET /notifications/subscriptions/{subscription_id}": "RLS-only read (migration 290)",
    "PUT /notifications/subscriptions/{subscription_id}": "UPDATE by id — migration 290 USING clause",
    "DELETE /notifications/subscriptions/{subscription_id}": "DELETE by id — migration 290 USING clause",
    "GET /notifications/dispatches": "RLS-only read (migration 292 on `notification_dispatches`)",
    "GET /notifications/unread-count": "RLS-only count over the same policy (migration 292)",
    "POST /notifications/mark-all-seen": (
        "UPDATE over the caller's own rows — migration 292's USING clause IS the row set; "
        "an account predicate here would be a second definition of it"
    ),
    "POST /notifications/dispatches/{dispatch_id}/mark-seen": (
        "UPDATE by id — migration 292 USING clause"
    ),
    "GET /estimations/{run_id}": (
        "RLS-only read — `estimation_runs` carries the THREE-arm policy of migration 291 "
        "(own account OR SYSTEM OR platform admin), which no route-side `= %s` can restate"
    ),
    "PATCH /estimations/{run_id}/scenario": "UPDATE by id — migration 291 USING clause",
    "GET /pipeline/stages": (
        "pure display read; `pipeline.list_stages` took no account as of W3 — migration "
        "294's policy on `pipeline_stages` is the scope"
    ),
    "GET /billing/me": (
        "read that deliberately resolves a NULLABLE account by hand, because no account "
        "must yield the default plan rather than a 400 (see test_account_scope_census.py)"
    ),
}


def _tenant_conn_routes() -> list[tuple[str, str, set]]:
    """(method, path, reachable calls) for every route on the TENANT CONNECTION."""
    return [
        (method, path, calls)
        for method, path, route in _api_routes()
        if _bucket(route) == "tenant"
        and tenant_pool.tenant_conn in (calls := _reachable_calls(route.dependant))
    ]


def test_tenant_routes_are_account_scoped() -> None:
    """Every tenant-connection route either resolves ONE account at the edge or says
    in writing why RLS alone is its scope.

    This is the standing gate the #917 SHAPE had no answer to: a tenant write route
    that forwards no account. Drop `Depends(require_account_id)` from any of the
    eleven write routes and this names it — the one-line revert that shipped the
    outage is no longer a green diff.

    Its one honest edge: `POST /listings/lookup` is itself allowlisted now, and
    correctly so — W3 removed its account argument entirely, so #917's exact hunk is
    no longer expressible there. What holds that route is the reason string beside
    it, which a reviewer has to contradict in prose before re-adding an account. What
    this test holds MECHANICALLY is every other tenant write, and every new one.
    """
    offenders = sorted(
        f"  {method} {path}"
        for method, path, calls in _tenant_conn_routes()
        if tenant_pool.require_account_id not in calls
        and f"{method} {path}" not in _RLS_ONLY_ALLOWLIST
    )
    assert not offenders, (
        "tenant-connection route(s) neither resolve an account nor declare why RLS is "
        "their only scope. A WRITE takes Depends(tenant_pool.require_account_id) — one "
        "account, resolved once at the route edge, 400 on none. A READ (or an "
        "UPDATE/DELETE by id, scoped by its policy's USING clause) goes in "
        "_RLS_ONLY_ALLOWLIST with a reason naming what scopes it INSTEAD:\n"
        + "\n".join(offenders)
    )


def test_route_scope_census_is_not_vacuous() -> None:
    """The census's own mount sentinel (same job as `_MOUNT_SENTINELS` above).

    If `_bucket` stopped returning "tenant", or the tenant routers stopped mounting,
    the assertion above would hold over an empty list — green and worthless. Pin the
    population, one known-good member of it, and the allowlist's own freshness.
    """
    tenant = _tenant_conn_routes()
    assert tenant, (
        "no tenant-connection routes found — test_tenant_routes_are_account_scoped "
        "would pass vacuously. Check _bucket and the router mounts."
    )
    cards = [c for m, p, c in tenant if (m, p) == ("POST", "/pipeline/cards")]
    assert cards, "POST /pipeline/cards is not bucketed as a tenant-connection route"
    assert tenant_pool.require_account_id in cards[0], (
        "POST /pipeline/cards no longer declares require_account_id — the census above "
        "can no longer detect an unscoped write"
    )
    stale = sorted(set(_RLS_ONLY_ALLOWLIST) - {f"{m} {p}" for m, p, _c in tenant})
    assert not stale, (
        "_RLS_ONLY_ALLOWLIST entries match no tenant-connection route — delete them "
        "rather than leave a stale exemption that could cover a future route:\n  "
        + "\n  ".join(stale)
    )
