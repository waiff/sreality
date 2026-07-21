"""Standing gate: admin-class API routes carry `require_admin`, and no route ships
unauthenticated by accident.

The DB side now has a generalizing gate (tests/test_tenant_isolation_live.py), but
nothing checked the API side, so a new /admin or /dedup route that forgot
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
})

# Every route under these must resolve to require_admin.
_ADMIN_PREFIXES: tuple[str, ...] = (
    "/admin", "/dedup", "/outreach", "/broker-review", "/location-audit",
    "/skill-refinements",
)

# Sentinels proving those routers actually mounted. Without this, a mis-mounted router
# would leave an empty route table and every assertion below would pass vacuously.
_MOUNT_SENTINELS: tuple[str, ...] = ("/admin", "/dedup", "/notifications")


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
