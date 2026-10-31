"""The live app's route table, walked once for every route census: its routes, each route's dependencies, its auth bucket."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from api import dependencies as deps
from api import main as api_main
from api import tenant_pool


def _walk(routes: Iterable[Any]) -> Iterator[APIRoute]:
    # Older FastAPI splices an included router's routes into app.routes; since 0.13x `include_router` appends one
    # `_IncludedRouter` wrapper holding the original router. Without recursing through either shape this returns
    # almost nothing and a census over it passes vacuously (each census keeps a mount sentinel for that).
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
            continue
        nested = getattr(getattr(r, "original_router", None), "routes", None)
        if nested is None:
            nested = getattr(r, "routes", None)
        if nested:
            yield from _walk(nested)


def flat_routes() -> list[APIRoute]:
    """Every `APIRoute` the live app serves, recursing through each included router."""
    return list(_walk(api_main.app.routes))


def method_routes() -> list[tuple[str, str, APIRoute]]:
    """Every (method, path, route) the live app serves."""
    return [(m, r.path, r) for r in flat_routes() for m in sorted(r.methods)]


def dependants(route: APIRoute) -> Iterator[Dependant]:
    """Every dependency reachable from `route`, nested ones included; each keeps its own `call` and `scope`."""
    stack = list(route.dependant.dependencies)
    while stack:
        d = stack.pop()
        yield d
        stack.extend(d.dependencies)


def reachable_calls(route: APIRoute) -> set[Callable[..., Any]]:
    """Every dependency callable reachable from `route`: router-level, per-parameter and nested (`require_admin`'s `verify_jwt`)."""
    return {d.call for d in dependants(route) if d.call is not None}


def auth_bucket(route: APIRoute) -> str:
    """The strongest auth gate `route` reaches: admin, tenant, token or public."""
    calls = reachable_calls(route)
    if deps.require_admin in calls:
        return "admin"
    if tenant_pool.tenant_conn in calls or deps.verify_jwt in calls:
        return "tenant"
    # account_scope is require_token's reader (`_bearer`: a verified JWT) plus a read scope, so it buckets with it.
    if deps.require_token in calls or deps.token_claims in calls or deps.account_scope in calls:
        return "token"
    return "public"
