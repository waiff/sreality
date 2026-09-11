"""THE ACCOUNT-SCOPE CENSUS — a tenant scope cannot go missing quietly.

Companion to `tests/api/test_admin_route_coverage.py`'s route-scope census. That one
walks the live FastAPI app and asks whether a route is SCOPED. This one walks `api/`
on disk and asks the question one layer down, where #917 actually did its damage:
can an account be absent without anyone noticing?

`POST /listings/lookup` lost its account because `lookup_portal_listings` declared
`account_id: uuid.UUID | None = None`. A three-argument call to a four-parameter
function is not an error in Python; the default absorbed it, `IS NOT DISTINCT FROM
NULL` matched nothing, and the extension answered "not in pipeline" for seven weeks.
W4 removed every such default it could. This keeps them removed.

TWO ARMS, each with a reasoned allowlist:
  * `default` — a function declaring `account_id` / `account_ids` with a `None`
    default. A dropped argument then binds None instead of raising TypeError.
  * `resolver` — a direct call to `tenant_pool.resolve_account_id`, which returns
    `uuid | None`. `tenant_pool.require_account_id` is the sanctioned wrapper (it
    400s on None); anything else resolving by hand is carrying a nullable account
    past the route edge and has to say why.

WHY A CENSUS AND NOT A BAN, and why the allowlist reasons are the product: both
shapes are legitimate somewhere. `create_estimation_run` runs on a service-role
connection for child runs spawned by `building_runs`, where `estimation_runs` has a
SYSTEM RLS arm (migration 291) that a tenant table does not; `GET /billing/me` must
answer "default plan" for a caller with no membership rather than 400. A ban would
be false. A census allows exactly the population enumerated below and reds on
anything else it can see.

SCOPE, STATED HONESTLY. This scans EVERY function under `api/`, not only those
reachable from a tenant route. "Reachable" is not resolvable from the AST alone in
this codebase — routes call module-level names rebound by monkeypatch in tests, and
`api/main.py` imports its helpers as bare globals — so the census is deliberately
WIDER than the question, and the allowlist absorbs the difference. That is the
trade the precedent (`tests/test_measure_registry_census.py`) makes too.

THE NAMED BLIND SPOTS — real, and listed so nobody has to rediscover them:
  * NAME-BASED. A tenant scope spelled `acct`, `owner_id`, `tenant`, or passed
    inside a dict/dataclass/Pydantic body is invisible to both arms.
  * HELPER INDIRECTION. `f(*args)`, `f(**kwargs)`, a partial, or a dispatch table
    hides both the missing argument and the resolver call.
  * ALIASED IMPORTS. The resolver arm matches `<something>.resolve_account_id(...)`
    and a bare `resolve_account_id(...)`; `from api.tenant_pool import
    resolve_account_id as r` then `r(...)` walks past.
  * A DEFAULT THAT IS NOT LITERALLY `None` — `= SYSTEM_ACCOUNT_ID`, `= ""` — is not
    flagged here. It is worse, not better, and the route-scope census is what sees
    it (a route that never declares `require_account_id`).
  * FILES ON DISK, not the running app: a route registered by dynamic code, or a
    helper living outside `api/`, is out of scope.
A rail that documents its own edges cannot manufacture confidence.
"""

from __future__ import annotations

import ast
import pathlib

_API = pathlib.Path(__file__).resolve().parents[2] / "api"

_SCOPE_PARAMS = frozenset({"account_id", "account_ids"})

# `api/tenant_pool.py` is the DEFINITION site: `require_account_id` calls
# `resolve_account_id` and raises 400 on None — that call IS the gate, not a bypass
# of it. Excluded by rule rather than by an entry, so the entries below stay a list
# of exceptions rather than a list of exceptions plus one tautology.
_RESOLVER_HOME = "api/tenant_pool.py"

# Every entry is a deliberate, reviewable decision — adding one should take an
# argument, not a reflex. The reason must say what makes a NULLABLE account correct
# HERE, when it is wrong everywhere else.
_NULLABLE_DEFAULT_ALLOWLIST: dict[str, str] = {
    "api/estimation_runs.py::create_estimation_run": (
        "service-role child-run path: `building_runs` spawns children with no caller "
        "JWT, and `estimation_runs.account_id` is NULLABLE with a SYSTEM default and a "
        "SYSTEM arm in its RLS policy (migration 291) — the one table where an absent "
        "account is a real, writable state rather than a dropped argument"
    ),
    "api/estimation_runs.py::_persist_failed_run": (
        "the failure twin of create_estimation_run — rule #12 says a failed run still "
        "persists a row, so it must be writable on exactly the same service-role path"
    ),
}

_HANDROLLED_RESOLVER_ALLOWLIST: dict[str, str] = {
    "api/main.py::post_estimations": (
        "POST /estimations runs on the SERVICE-ROLE connection on purpose (an agent run "
        "can take minutes; tenant_conn would hold one pooler transaction open for all of "
        "it), and falls back to SYSTEM — legal only because migration 291 gives "
        "`estimation_runs` a SYSTEM arm. Never copy this onto a migration-290 table."
    ),
    "api/routes/billing.py::get_billing_me": (
        "no resolvable account must yield the DEFAULT PLAN, unmetered — a 400 here would "
        "break the account menu for a signed-in user before their membership exists"
    ),
    "api/routes/billing.py::require_entitlement::_gate": (
        "same nullable-account-means-default-plan contract as get_billing_me, evaluated "
        "as a dependency instead of in a handler"
    ),
    "api/dependencies.py::account_scope": (
        "the FOURTH tenancy shape, and the one worth naming so nobody rediscovers it as a "
        "divergence: it returns a READ SCOPE `[account_id, SYSTEM]` mirroring migration "
        "291's three-arm policy, so an unresolvable account narrows to `[SYSTEM]` rather "
        "than failing — it never widens, and it never returns empty"
    ),
}


def _qualnames(tree: ast.Module) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    out: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}::{child.name}" if prefix else child.name
                if not isinstance(child, ast.ClassDef):
                    out.append((name, child))
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def _api_modules() -> list[tuple[str, ast.Module]]:
    mods = []
    for path in sorted(_API.rglob("*.py")):
        rel = path.relative_to(_API.parent).as_posix()
        mods.append((rel, ast.parse(path.read_text(encoding="utf-8"))))
    return mods


def _defaulted_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    positional = fn.args.posonlyargs + fn.args.args
    defaults = list(fn.args.defaults)
    pairs: list[tuple[ast.arg, ast.expr | None]] = [
        (arg, defaults[i - (len(positional) - len(defaults))]
         if i >= len(positional) - len(defaults) else None)
        for i, arg in enumerate(positional)
    ]
    pairs += list(zip(fn.args.kwonlyargs, fn.args.kw_defaults))
    return [
        arg.arg
        for arg, default in pairs
        if arg.arg in _SCOPE_PARAMS
        and isinstance(default, ast.Constant)
        and default.value is None
    ]


_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _calls_resolver(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if THIS function's own body calls the resolver — nested defs are their
    own census rows (a factory like `require_entitlement` must not inherit the blame
    for its inner `_gate`)."""
    stack: list[ast.AST] = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED):
            continue
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "resolve_account_id":
                return True
            if isinstance(func, ast.Name) and func.id == "resolve_account_id":
                return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def test_no_nullable_account_scope_default() -> None:
    """A tenant-scope parameter may not default to None.

    This is the line that let #917 through: `lookup_portal_listings(market_conn,
    conn, body.items)` was three arguments to a four-parameter function, and
    `account_id: uuid.UUID | None = None` swallowed the fourth. With no default the
    same revert is a TypeError at import-time-adjacent call sites and a red suite,
    not a silently empty 200.
    """
    offenders = []
    for rel, tree in _api_modules():
        for qual, fn in _qualnames(tree):
            key = f"{rel}::{qual}"
            for param in _defaulted_params(fn):
                if key in _NULLABLE_DEFAULT_ALLOWLIST:
                    continue
                offenders.append(f"  {key}({param}=None)  line {fn.lineno}")
    assert not offenders, (
        "tenant-scope parameter(s) default to None — a dropped argument binds NULL "
        "instead of raising. Make it required (keyword-only, no default) so the call "
        "site must name it, or add a _NULLABLE_DEFAULT_ALLOWLIST entry whose reason "
        "says what makes an absent account correct there:\n" + "\n".join(offenders)
    )


def test_hand_rolled_account_resolution_is_enumerated() -> None:
    """`resolve_account_id` returns `uuid | None`; `require_account_id` is the gate.

    The route-scope census deliberately does not judge the service-role routes that
    reach `verify_jwt` without a tenant connection (`/brokers/*`, `POST /estimations`)
    — this is where they are accounted for. Every place in `api/` that resolves an
    account outside `require_account_id` is carrying a nullable one on purpose, and
    says so here.
    """
    offenders = []
    for rel, tree in _api_modules():
        if rel == _RESOLVER_HOME:
            continue
        for qual, fn in _qualnames(tree):
            key = f"{rel}::{qual}"
            if _calls_resolver(fn) and key not in _HANDROLLED_RESOLVER_ALLOWLIST:
                offenders.append(f"  {key}  line {fn.lineno}")
    assert not offenders, (
        "call(s) to tenant_pool.resolve_account_id outside require_account_id. On a "
        "tenant connection declare Depends(tenant_pool.require_account_id) instead — "
        "one account, resolved once, 400 on none. If a nullable account is genuinely "
        "correct, add a _HANDROLLED_RESOLVER_ALLOWLIST entry saying why:\n"
        + "\n".join(offenders)
    )


def test_census_population_is_not_vacuous() -> None:
    """A census over an empty file set passes every assertion. Pin the population,
    and keep both allowlists honest: an entry matching nothing is a stale exemption
    that would silently cover whatever next takes that name."""
    mods = _api_modules()
    assert len(mods) > 20, f"only {len(mods)} modules found under api/ — check _API"
    keys = {f"{rel}::{qual}" for rel, tree in mods for qual, _fn in _qualnames(tree)}
    stale = sorted(
        (set(_NULLABLE_DEFAULT_ALLOWLIST) | set(_HANDROLLED_RESOLVER_ALLOWLIST)) - keys
    )
    assert not stale, (
        "allowlist entries name no function in api/ — delete them:\n  " + "\n  ".join(stale)
    )
    assert f"{_RESOLVER_HOME}::require_account_id" in keys, (
        "require_account_id is gone from api/tenant_pool.py — the resolver arm's "
        "exclusion-by-rule no longer describes anything"
    )
