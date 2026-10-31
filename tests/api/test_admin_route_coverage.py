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

import inspect

import pytest

pytest.importorskip("fastapi")

from fastapi.routing import APIRoute  # noqa: E402

from api import dependencies as deps  # noqa: E402
from api import tenant_pool  # noqa: E402
from tests.api._routes import auth_bucket, method_routes, reachable_calls  # noqa: E402

# Routes that legitimately authenticate some other way. Every entry is a deliberate,
# reviewable decision — adding one should take an argument, not a reflex.
_PUBLIC_ALLOWLIST: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/health"),               # liveness probe
    ("GET", "/images/{key:path}"),    # R2 image proxy, opaque keys
    # Authenticates in-handler by verifying a Stripe HMAC over the raw request body.
    ("POST", "/billing/webhook"),
    # The propscout.cz landing page's waitlist form and its thank-you question: a visitor from an ad has
    # no account. Honeypot + rate limit + strict input checks; writes only waitlist_signups (mig 700).
    ("POST", "/waitlist"),
    ("POST", "/waitlist/answer"),
})

# Every route under these must resolve to require_admin. `/properties` as a whole is
# NOT an admin prefix (notes/tags under it ride the plain bearer gate), so the merge
# mechanics are listed by their own sub-paths — "/properties/merge" covers /merge.
_ADMIN_PREFIXES: tuple[str, ...] = (
    "/admin", "/properties/merge", "/labeling",
    "/outreach", "/broker-review", "/skill-refinements", "/location",
    "/autodedup",
    # Paste-a-building (rule 13) spends an extraction plus one estimation per unit and holds no
    # account of its own: operator-only since agent platform M0 (#2059).
    "/buildings",
)

# Sentinels proving those routers actually mounted. Without this, a mis-mounted router
# would leave an empty route table and every assertion below would pass vacuously.
_MOUNT_SENTINELS: tuple[str, ...] = (
    "/admin", "/properties/merge", "/labeling", "/notifications",
)


def test_admin_routers_are_mounted() -> None:
    """If include_router silently mounted nothing, the assertions below would hold
    over an empty route table — so fail loudly instead of passing vacuously."""
    paths = {p for _, p, _ in method_routes()}
    missing = [s for s in _MOUNT_SENTINELS if not any(p.startswith(s) for p in paths)]
    assert not missing, (
        f"no routes mounted under {missing} — the coverage assertions below would be "
        f"vacuous. Check the fastapi/python versions before trusting a green run."
    )


def test_admin_surfaces_require_admin() -> None:
    """Every route under an admin prefix must resolve to require_admin."""
    offenders = sorted(
        f"  {method} {path} -> {auth_bucket(route)}"
        for method, path, route in method_routes()
        if path.startswith(_ADMIN_PREFIXES) and auth_bucket(route) != "admin"
    )
    assert not offenders, (
        "admin-prefixed route(s) are not admin-gated. Add "
        "Depends(deps.require_admin) — SPA route-gating is a client affordance, not a "
        "security boundary:\n" + "\n".join(offenders)
    )


def _takes_force_refresh(route: APIRoute) -> bool:
    body = route.body_field.field_info.annotation if route.body_field is not None else None
    return "force_refresh" in getattr(body, "model_fields", {})


def test_a_forced_refresh_is_admin_gated_wherever_it_is_accepted() -> None:
    """A body field `force_refresh` re-buys a cached LLM answer: every route accepting it calls
    `deps.require_admin_to_force` (M0, #2059). The gate reads the body, so no dependency can carry it."""
    accepting = [(m, p, r) for m, p, r in method_routes() if _takes_force_refresh(r)]
    assert len(accepting) >= 2, "the census found no route taking force_refresh: check how it reads the body"
    ungated = sorted(f"  {m} {p}" for m, p, r in accepting
                     if "require_admin_to_force(" not in inspect.getsource(r.endpoint))
    assert not ungated, "route(s) accept force_refresh without the admin gate:\n" + "\n".join(ungated)


def test_no_route_is_unauthenticated_by_accident() -> None:
    """A new route with no auth dependency at all lands in `public`; only the three
    known-public endpoints may."""
    unexpected = sorted(
        f"  {method} {path}"
        for method, path, route in method_routes()
        if auth_bucket(route) == "public" and (method, path) not in _PUBLIC_ALLOWLIST
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
# `tenant_pool.require_account_id` (400 "no account for caller" on none) — or, for a write
# to the eleven curation tables, `tenant_pool.require_acting_account_id` (the same, or under
# X-Act-As the owner whose edit share migration 641 grants; tests/api/test_acting_routes_census.py).
#
# SCOPE, stated honestly. The subject is routes reaching `tenant_pool.tenant_conn`.
# `auth_bucket` says "tenant" for anything reaching `verify_jwt`, which also covers the
# `/brokers/*` + `POST /estimations` routes — those run on the service-role
# `get_db_conn`, where `verify_jwt` is authentication only and RLS is not the scope.
# They are EXCLUDED structurally, not by a copy of one sentence each: an allowlist
# that doubles in size for a class it cannot judge is a gate that cries wolf, and a
# gate that cries wolf gets disabled. The account each of them resolves by hand is
# the subject of the second arm of `tests/api/test_account_scope_census.py`.
# `POST /estimations/{run_id}/feedback` sits outside it the same way (it opens its own
# short `tenant_transaction`); the last test in this file holds that route instead.

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
    "DELETE /collections/{collection_id}/shares/{account_id}": (
        "DELETE by key — migration 610's party policy USING (owner revokes, recipient leaves)"
    ),
    "GET /properties/{property_id}/notes": "RLS-only read (migration 290 on `property_notes`)",
    "GET /viewings": "RLS-only read (migration 290 on `property_notes`), the caller's own rows",
    "GET /artifact-links": "RLS-only read (migration 599 on `property_artifact_links`)",
    "DELETE /properties/{property_id}/artifact-link": (
        "DELETE of the caller's own row by property — migration 599 USING clause"
    ),
    "PATCH /properties/{property_id}/notes/{note_id}": "UPDATE by id — migration 290 USING clause",
    "DELETE /properties/{property_id}/notes/{note_id}": "DELETE by id — migration 290 USING clause",
    "GET /properties/{property_id}/attachments": "RLS-only read (migration 660 on `property_attachments`)",
    "GET /properties/{property_id}/attachments/{attachment_id}": (
        "RLS-only read (migration 660 on `property_attachments`)"
    ),
    "DELETE /properties/{property_id}/attachments/{attachment_id}": (
        "DELETE by id — migration 660 USING clause"
    ),
    "GET /properties/{property_id}/download.zip": (
        "RLS-only read of the acting identity's own photos (migration 660 on "
        "`property_attachments`, `acting_account_ids()`); the ad's data on the service role"
    ),
    "GET /tags": "RLS-only read (migration 290 policy on `tags`)",
    "PATCH /tags/{tag_id}": "UPDATE by id — migration 290 USING clause",
    "DELETE /tags/{tag_id}": "DELETE by id — migration 290 USING clause",
    "DELETE /properties/{property_id}/tags/{tag_id}": (
        "DELETE by id on a child-grain table — migration 292 USING clause"
    ),
    "GET /filter-presets": "RLS-only read (migration 290 policy on `filter_presets`)",
    "GET /filter-presets/{preset_id}": "RLS-only read (migration 290)",
    "PUT /filter-presets/{preset_id}": "UPDATE by id — migration 290 USING clause",
    "DELETE /filter-presets/{preset_id}": "DELETE by id — migration 290 USING clause",
    "PUT /filter-presets/reorder": (
        "UPDATE by a list of ids — migration 290's USING clause is the row set, so another "
        "account's id matches nothing"
    ),
    "PUT /filter-presets/layout": (
        "UPDATE by a list of ids on both tables — the 290 and 672 USING clauses are the row sets, and "
        "672's (group_id, account_id) key refuses another account's group"
    ),
    "GET /filter-preset-groups": "RLS-only read (migration 672 policy on `filter_preset_groups`)",
    "GET /filter-preset-groups/{group_id}": "RLS-only read (migration 672)",
    "PUT /filter-preset-groups/{group_id}": "UPDATE by id — migration 672 USING clause",
    "DELETE /filter-preset-groups/{group_id}": "DELETE by id — migration 672 USING clause",
    "PUT /filter-preset-groups/reorder": (
        "UPDATE by a list of ids — migration 672's USING clause is the row set"
    ),
    "GET /stats/dashboards": (
        "RLS-only read — migration 626's tenant policy (own) plus 647's shared-read arm "
        "(dashboards shared with the caller); labels through share_party_emails()"
    ),
    "GET /stats/dashboard": (
        "RLS-only read of the working board — migration 626's tenant policy (current_account_ids(), "
        "the caller's own even under X-Act-As); 647's shared arm reads named rows only, so `name IS NULL` "
        "is the caller's one unnamed row"
    ),
    "GET /stats/dashboards/{dashboard_id}": "RLS-only read (migrations 626 + 647, as above)",
    "PUT /stats/dashboards/{dashboard_id}": (
        "UPDATE by id — migration 626's USING clause; a shared row is visible, never updatable (404)"
    ),
    "DELETE /stats/dashboards/{dashboard_id}": "DELETE by id — migration 626 USING clause",
    "PUT /stats/dashboards/{dashboard_id}/property-card": (
        "UPDATE by id under migration 626's USING clause, both statements also naming "
        "current_account_ids(): another account's or a shared dashboard matches nothing (404)"
    ),
    "PUT /stats/dashboards/reorder": (
        "UPDATE by a list of ids — migration 626's USING clause is the row set, so another "
        "account's (or a shared) dashboard matches nothing"
    ),
    "DELETE /stats/dashboards/{dashboard_id}/shares/{account_id}": (
        "DELETE by key — migration 647's party policy USING (owner revokes, recipient leaves)"
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
    "GET /estimations/{run_id}/trace/{step_n}/payload": (
        "RLS-only read (migration 292 on `estimation_trace_payloads`, the run's three arms)"
    ),
    "GET /estimations/{run_id}/feedback": (
        "RLS-only read (migration 292 on `estimation_feedback`) behind an RLS run probe"
    ),
    "GET /pipeline/shares": (
        "RLS-only read — migration 641's party policy on `pipeline_shares` (owner or recipient, "
        "never while acting); labels through share_party_emails()"
    ),
    "DELETE /pipeline/shares/{account_id}": (
        "DELETE by key — migration 641's party policy USING (owner revokes, recipient leaves); "
        "`side` names which of the caller's two lists the counterparty sits in"
    ),
    "GET /pipeline/stages": (
        "pure display read; `pipeline.list_stages` took no account as of W3 — migration "
        "294's policy on `pipeline_stages` is the scope"
    ),
    "DELETE /dismissals/{property_id}": (
        "UPDATE (lift) of the caller's own active rows — migration 536's USING clause is "
        "the row set, and it must stay plural: `property_dismissals_public` hides a "
        "property if ANY of the caller's accounts dismissed it, so undo has to lift them all"
    ),
    "GET /billing/me": (
        "read that deliberately resolves a NULLABLE account by hand, because no account "
        "must yield the default plan rather than a 400 (see test_account_scope_census.py)"
    ),
}


# The two write-account dependencies (migration 641): own account, or the account acted for.
_WRITE_ACCOUNT_DEPENDENCIES = frozenset({
    tenant_pool.require_account_id, tenant_pool.require_acting_account_id,
})


def _tenant_conn_routes() -> list[tuple[str, str, set]]:
    """(method, path, reachable calls) for every route on the TENANT CONNECTION."""
    return [
        (method, path, calls)
        for method, path, route in method_routes()
        if auth_bucket(route) == "tenant"
        and tenant_pool.tenant_conn in (calls := reachable_calls(route))
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
        if not calls & _WRITE_ACCOUNT_DEPENDENCIES
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

    If `auth_bucket` stopped returning "tenant", or the tenant routers stopped mounting,
    the assertion above would hold over an empty list — green and worthless. Pin the
    population, one known-good member of it, and the allowlist's own freshness.
    """
    tenant = _tenant_conn_routes()
    assert tenant, (
        "no tenant-connection routes found — test_tenant_routes_are_account_scoped "
        "would pass vacuously. Check auth_bucket and the router mounts."
    )
    cards = [c for m, p, c in tenant if (m, p) == ("POST", "/pipeline/cards")]
    assert cards, "POST /pipeline/cards is not bucketed as a tenant-connection route"
    assert tenant_pool.require_acting_account_id in cards[0], (
        "POST /pipeline/cards no longer declares require_acting_account_id — the census above "
        "can no longer detect an unscoped write"
    )
    stale = sorted(set(_RLS_ONLY_ALLOWLIST) - {f"{m} {p}" for m, p, _c in tenant})
    assert not stale, (
        "_RLS_ONLY_ALLOWLIST entries match no tenant-connection route — delete them "
        "rather than leave a stale exemption that could cover a future route:\n  "
        + "\n  ".join(stale)
    )


# The three estimation child routes the 2026-10-01 review found on the service-role
# connection behind the bundle-public static token: any holder could read any
# account's trace payloads and spend refiner LLM credit on any run. The two reads ride
# the request's tenant connection. The POST opens a SHORT tenant transaction in its
# handler instead (gate + insert, committed), then the admin-only refiner on a
# service-role connection — so it must reach neither the request-long `tenant_conn`
# (its transaction would sit open across the LLM call, the W1-1 boundary) nor a
# service-role dependency (opened before the gate could refuse a foreign run).
_ESTIMATION_CHILD_READS: tuple[tuple[str, str], ...] = (
    ("GET", "/estimations/{run_id}/trace/{step_n}/payload"),
    ("GET", "/estimations/{run_id}/feedback"),
)
_FEEDBACK_POST = ("POST", "/estimations/{run_id}/feedback")


def test_estimation_child_routes_ride_the_tenant_connection() -> None:
    routes = {(m, p): r for m, p, r in method_routes()}
    for key in (*_ESTIMATION_CHILD_READS, _FEEDBACK_POST):
        assert key in routes, f"{key} is not mounted — the census below would be vacuous"
        calls = reachable_calls(routes[key])
        assert deps.verify_jwt in calls, f"{key} no longer verifies a real JWT"
        assert deps.require_token not in calls, (
            f"{key} is back on require_token — the claims it discards are what scopes this read"
        )
    for key in _ESTIMATION_CHILD_READS:
        calls = reachable_calls(routes[key])
        assert tenant_pool.tenant_conn in calls, f"{key} left the tenant connection"

    post = routes[_FEEDBACK_POST]
    calls = reachable_calls(post)
    for held_too_early in (tenant_pool.tenant_conn, deps.get_db_conn, deps.get_llm_client):
        assert held_too_early not in calls, (
            f"{_FEEDBACK_POST} depends on {held_too_early.__name__} again — see the "
            "comment above for why it must open its connections in the handler"
        )
    assert "tenant_pool.tenant_transaction(" in inspect.getsource(post.endpoint), (
        f"{_FEEDBACK_POST} no longer gates on a tenant transaction"
    )


@pytest.mark.parametrize("key", [
    ("POST", "/collections/{collection_id}/shares"),
    ("POST", "/pipeline/shares"),
    # The push device routes (migration 654) have the same shape: the caller's own account at the edge,
    # then the service role, since browser roles hold nothing on either push table.
    ("PUT", "/push/subscription"),
    ("DELETE", "/push/subscription"),
    ("POST", "/push/opened"),
])
def test_the_share_routes_open_the_service_role_only_after_their_gate(key: tuple[str, str]) -> None:
    # tenancy.md's second "tenant gate, then a service-role write": a recipient's or a
    # bystander's refused POST must not have checked out a service-role connection first.
    routes = {(m, p): r for m, p, r in method_routes()}
    share = routes[key]
    calls = reachable_calls(share)
    assert tenant_pool.tenant_conn in calls
    assert deps.get_db_conn not in calls, "the share route took the service role as a dependency again"
    assert "deps.open_background_conn" in inspect.getsource(share.endpoint)
