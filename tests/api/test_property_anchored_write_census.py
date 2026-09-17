"""THE PROPERTY-ANCHORED WRITE CENSUS — a merged-away id cannot be written blind.

Sibling to `tests/api/test_account_scope_census.py`. That one asks "can an account
go missing quietly?"; this one asks the same question of the OTHER id every operator
write is keyed on, where a stale value is just as silent and twice as reachable.

A merged-away property (status <> 'active', `merged_into` set) still satisfies a
`references properties(id)` FK, and merge re-points operator state onto the survivor
inside the merge transaction (rule #18, `toolkit/operator_state.py`). So a write
keyed on a stale property_id addresses a row that is no longer there:

  * an INSERT lands on the retired row and never appears on the survivor;
  * an UPDATE/DELETE matches nothing and reports success-shaped failure —
    HTTP 200 `{"removed": false}`, or a 404 for a row that is alive and well.

Commit 426fa575 ("redirect property-anchored writes off merged-away properties")
closed the first shape and said it applied the resolver "at every property-anchored
write entry" — but it enumerated only the four INSERT sites. The five UPDATE/DELETE
twins kept the bug for fourteen months: remove_property_from_collection, detach_tag,
update_note, delete_note (curation) and remove_card / move_card (pipeline). An
enumeration in a commit message is not a rail. This is the rail.

THE RULE: a function under `api/` that takes a `property_id` / `property_ids` and
writes one of the merge-carried tables below must first call
`toolkit.property_identity.resolve_active_property_id(s)` — or carry an allowlist
entry saying what makes a raw id correct there.

WHY A CENSUS AND NOT A BAN. Resolution is genuinely wrong in two places, and the
reasons are the product: the merge route itself CREATES survivors (resolving would
ask it to merge a property into itself), and `properties.asset_id` is a column on
the property row rather than carried state, so following the pointer would mutate a
different row than the caller named.

TWO HALVES, ONE MEANING: the add and remove halves of one affordance must resolve
alike. `<PipelineMark>` (rule #22) and the collection/tag toggles are single
controls; hardening only the add half produced the exact asymmetry where the same
cached id could CREATE a membership it could then never remove.

THE NAMED BLIND SPOTS — real, and listed so nobody has to rediscover them:
  * NAME-BASED. An id spelled `pid`, `prop`, or arriving inside a dict / Pydantic
    body under another name is invisible here (`AddPipelineCardIn.property_id` is
    seen only because the handler also names the parameter, or the SQL names the
    table — a body field alone is not enough).
  * SQL BUILT ACROSS EXPRESSIONS. The verb and the table name must appear in one
    string literal (or f-string) in the function body. SQL assembled from a list of
    fragments, a module constant, or a helper walks past.
  * HELPER INDIRECTION. A handler that delegates its write to a function taking a
    cursor is judged at the helper, not the caller — see `lift_dismissals` below.
  * TOOLKIT AND SCRAPER WRITES. Only `api/` is scanned. `toolkit/` writes these
    tables too (the merge reconcilers themselves), and must not resolve.
  * READS. A read keyed on a stale id shows an empty list rather than corrupting
    state; out of scope here, and deliberately so.
A rail that documents its own edges cannot manufacture confidence.
"""

from __future__ import annotations

import ast
import pathlib
import re

_API = pathlib.Path(__file__).resolve().parents[2] / "api"

_ID_PARAMS = frozenset({"property_id", "property_ids"})

# Tables whose rows are keyed on `property_id` AND carried onto the survivor at
# merge — `toolkit/operator_state.py::OPERATOR_STATE_TABLES` plus the two with
# bespoke reconcilers (`property_pipeline`, rule #22; `property_dismissals`,
# migration 536). A row in any of them is reachable only under the survivor's id
# once a merge has happened.
_CARRIED_TABLES = (
    "collection_properties",
    "property_tags",
    "property_notes",
    "property_pipeline",
    "property_pipeline_events",
    "property_dismissals",
    "property_status_events",
    "notification_dispatches",
)

_WRITE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:public\.)?(" + "|".join(_CARRIED_TABLES) + r")\b",
    re.IGNORECASE,
)

_RESOLVERS = frozenset({"resolve_active_property_id", "resolve_active_property_ids"})

# Every entry is a deliberate, reviewable decision. The reason must say what makes a
# RAW property id correct HERE, when it is wrong everywhere else.
_RAW_ID_ALLOWLIST: dict[str, str] = {
    "api/dismissals.py::lift_dismissals": (
        "takes a CURSOR, not a connection, and is called only from paths that have "
        "already resolved (`dismiss`/`undismiss` here, `add_card` in api/pipeline.py) "
        "— resolving again would be a second round trip for the same answer, and it "
        "has no connection to do it with"
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
    return [
        (path.relative_to(_API.parent).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(_API.rglob("*.py"))
    ]


def _takes_property_id(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    args = fn.args
    names = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    return bool(names & _ID_PARAMS)


def _own_nodes(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """This function's own body — a nested def is its own census row."""
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _written_tables(nodes: list[ast.AST]) -> set[str]:
    hits: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            hits |= {m.lower() for m in _WRITE.findall(node.value)}
    return hits


def _calls_resolver(nodes: list[ast.AST]) -> bool:
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _RESOLVERS:
            return True
        if isinstance(func, ast.Name) and func.id in _RESOLVERS:
            return True
    return False


def test_property_anchored_writes_resolve_the_merge_survivor() -> None:
    """A write keyed on a caller-supplied property_id resolves it first.

    Both halves of every affordance, not just the INSERT half — the DELETE half is
    where the failure is silent (`rowcount == 0` reads as "nothing to remove").
    """
    offenders = []
    for rel, tree in _api_modules():
        for qual, fn in _qualnames(tree):
            key = f"{rel}::{qual}"
            if key in _RAW_ID_ALLOWLIST or not _takes_property_id(fn):
                continue
            nodes = _own_nodes(fn)
            tables = _written_tables(nodes)
            if tables and not _calls_resolver(nodes):
                offenders.append(
                    f"  {key}  line {fn.lineno}  writes {', '.join(sorted(tables))}",
                )
    assert not offenders, (
        "property-anchored write(s) key on a raw, caller-supplied property_id. A "
        "merged-away id addresses a row that merge already moved onto the survivor: "
        "the INSERT orphans, the UPDATE/DELETE matches nothing and returns a "
        "success-shaped 200. Call resolve_active_property_id(s) first (4xx when the "
        "write needs a real target, `or property_id` when a remove should stay "
        "idempotent), or add a _RAW_ID_ALLOWLIST entry whose reason says what makes "
        "a raw id correct there:\n" + "\n".join(offenders)
    )


def test_the_census_can_still_see_a_regression() -> None:
    """The census is only worth its line count if it fails on the real thing.

    Replays the pre-fix body of `remove_property_from_collection` — the exact shape
    that shipped for fourteen months — through the same detectors. A refactor that
    quietly stops matching SQL (a module-level constant, a fragment list) makes the
    test above vacuously green; this one goes red instead.
    """
    regressed = ast.parse(
        "def remove_property_from_collection(conn, collection_id, property_id):\n"
        "    sql = ('DELETE FROM collection_properties '\n"
        "           'WHERE collection_id = %s AND property_id = %s')\n"
        "    with conn.transaction(), conn.cursor() as cur:\n"
        "        cur.execute(sql, (collection_id, property_id))\n"
        "    return {'removed': cur.rowcount > 0}\n",
    )
    fn = next(f for _, f in _qualnames(regressed))
    nodes = _own_nodes(fn)
    assert _takes_property_id(fn)
    assert _written_tables(nodes) == {"collection_properties"}
    assert not _calls_resolver(nodes)


def test_allowlist_entries_are_real_and_reasoned() -> None:
    """No stale entry: every allowlisted key must still name a live function, and
    say why — an allowlist nobody can audit is an exemption, not a decision."""
    known = {
        f"{rel}::{qual}" for rel, tree in _api_modules() for qual, _ in _qualnames(tree)
    }
    for key, reason in _RAW_ID_ALLOWLIST.items():
        assert key in known, f"_RAW_ID_ALLOWLIST names a function that no longer exists: {key}"
        assert len(reason) > 60, f"_RAW_ID_ALLOWLIST[{key}] needs a reason, not a label"
