"""Every location lane's coordination identifiers are globally unique.

W2 and W3 both designed a "re-mine" lane, independently, against different substrates —
`listing_snapshots.raw_json` and archived `portal_raw_payloads` bodies — and both design
documents assigned them the SAME module name, the same `LANE = "location_claims_remine"`
and the same `REMINE_VERSION = "claims_remine@1"`. It was caught by two sessions happening
to compare notes, which is not a mechanism.

The collision is not cosmetic. These strings are PRIMARY KEYS in the coordination tables:

  * `LANE` keys `location_claim_batches`' resume/watermark queries — `(lane, source,
    scan_mode)` and nothing else — so two lanes sharing one would resume from each other's
    cursor over a different table, silently, and each would claim coverage the other
    achieved. No error, no duplicate row, just a hole where the unscanned rows were.
  * `JOB_NAME` keys `location_jobs`, whose lease is a CAS on that row: two lanes sharing one
    would lock each other out at random and each would log "another run holds the lease".
  * the extractor-version constants ride into `location_claim_batches.extractor_version`,
    which is what `location_claim_retractions` scopes a rollback by — two lanes sharing one
    makes "retract everything that version wrote" un-decidable.

`CONCURRENCY_GROUP` is deliberately NOT in that list. It is the serialisation group, so
sharing one is the mechanism working: `payload_prune` and `payload_backfill` both declare
`location-payload` precisely because both move bodies in `portal_raw_payloads` and 01 §9.1's
lease is what keeps them off each other. What IS checked about it is that a lane declaring
one half of a lease declares the other.

So the fix is a gate, not a rename. This file is that gate.

KNOWN LIMIT, stated rather than hidden: the scan is name-based, so a lane that spells its
lane string into a constant called something else entirely escapes it. That is why
`test_a_lane_also_declares_the_version_it_stamps_on_its_batches` exists — it forces any
module declaring `LANE` to declare a version constant under a name this file knows, so the
escape hatch fails loudly instead of silently.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

# `location_data/**` is the lane fleet; `scripts/location_*.py` is the rest of it (the
# payload refetch probe holds a `location_jobs` lease from there). Both write to the same
# coordination tables, so scanning one and not the other would be a gate with a door in it.
_SCAN_ROOTS: tuple[tuple[Path, str], ...] = (
    (_ROOT / "location_data", "**/*.py"),
    (_ROOT / "scripts", "location_*.py"),
)

# constant name -> the namespace whose values must be globally unique. Several names can
# share a namespace when they land in the SAME column: every version constant below is
# `location_claim_batches.extractor_version`, so `claims_intake@3`, `payload_prune@1` and
# `claims_remine_archive@1` compete for one identity space despite the different spellings.
# A lane that invents a sixth spelling is caught by
# `test_a_lane_also_declares_the_version_it_stamps_on_its_batches`, not by silence.
_NAMESPACE_OF: dict[str, str] = {
    "LANE": "lane",
    "JOB_NAME": "job_name",
    "CONCURRENCY_GROUP": "concurrency_group",
    "INTAKE_VERSION": "extractor_version",
    "REMINE_VERSION": "extractor_version",
    "PRUNER_VERSION": "extractor_version",
    "BACKFILL_VERSION": "extractor_version",
}

# `concurrency_group` is scanned (the lease-halves test reads it) but exempt from
# uniqueness: it is the serialisation group, so two lanes sharing one is the mechanism.
_UNIQUE_NAMESPACES = frozenset({"lane", "job_name", "extractor_version"})

_VERSION_NAMES = frozenset(
    name for name, namespace in _NAMESPACE_OF.items() if namespace == "extractor_version")

# A soft floor, not a pin: adding a lane must not break this file, but a parser that stops
# finding anything must. Every value here is load-bearing in production today.
_KNOWN_LANES = frozenset({
    "location_claims_intake", "location_claims_remine_archive",
    "location_payload_prune", "location_payload_backfill",
})
_KNOWN_JOB_NAMES = frozenset({
    "location_claims_remine_archive", "location_resolve_incremental",
    "pin_collision_recompute", "location_payload_refetch_probe",
    "payload_archive_prune", "location_payload_backfill",
})
_KNOWN_VERSIONS = frozenset({
    "claims_intake@3", "claims_remine_archive@1", "payload_prune@1", "payload_backfill@1",
})


def _string_constants(path: Path) -> Iterator[tuple[str, str]]:
    """(constant name, value) for every MODULE-LEVEL string assignment.

    Module level only, and deliberately: a lane string built inside a function or a class is
    not the declarative constant this gate is about, and reaching into one would mean
    guessing at values the AST cannot evaluate. Both `NAME = "x"` and `NAME: str = "x"`
    count — the annotation is a style choice, not a different declaration.
    """
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                yield target.id, value.value


def _declarations() -> list[tuple[str, str, str, str]]:
    """(namespace, constant name, value, module) across the whole location fleet."""
    found: list[tuple[str, str, str, str]] = []
    for root, pattern in _SCAN_ROOTS:
        for path in sorted(root.glob(pattern)):
            if "__pycache__" in path.parts:
                continue
            module = path.relative_to(_ROOT).as_posix()
            for name, value in _string_constants(path):
                namespace = _NAMESPACE_OF.get(name)
                if namespace is not None:
                    found.append((namespace, name, value, module))
    return found


def _collisions(
    declarations: list[tuple[str, str, str, str]],
) -> dict[tuple[str, str], list[str]]:
    """{(namespace, value): [modules]} for every value claimed by more than one module.

    Keyed on the MODULE, not the declaration: one module reusing a value across namespaces
    is fine and intended (`claims_remine_archive` is both `LANE` and `JOB_NAME`
    `location_claims_remine_archive` — one lane, one lease, one name)."""
    by_value: dict[tuple[str, str], set[str]] = {}
    for namespace, _name, value, module in declarations:
        if namespace not in _UNIQUE_NAMESPACES:
            continue
        by_value.setdefault((namespace, value), set()).add(module)
    return {key: sorted(modules) for key, modules in by_value.items() if len(modules) > 1}


def _values(namespace: str) -> set[str]:
    return {value for space, _name, value, _module in _declarations() if space == namespace}


def _modules_declaring(name: str) -> set[str]:
    return {module for _space, constant, _value, module in _declarations() if constant == name}


# ------------------------------------------------------------------ the gate

def test_no_two_location_lanes_share_a_coordination_identifier():
    found = _collisions(_declarations())
    assert not found, (
        "two location lanes claim the same coordination identifier:\n"
        + "\n".join(f"  {namespace} = {value!r} in {', '.join(modules)}"
                    for (namespace, value), modules in sorted(found.items()))
        + "\n\nThese strings are primary keys in `location_claim_batches` / `location_jobs`."
          " Sharing one is not a duplicate row, it is two lanes reading and overwriting each"
          " other's resume cursor with no error anywhere. Rename the NEWER lane (the one"
          " that has not run in production yet) and give it its own workflow + concurrency"
          " group at the same time.")


@pytest.mark.parametrize("namespace,known", [
    ("lane", _KNOWN_LANES),
    ("job_name", _KNOWN_JOB_NAMES),
    ("extractor_version", _KNOWN_VERSIONS),
])
def test_the_scan_still_finds_the_lanes_that_exist(namespace, known):
    """Non-vacuity. A uniqueness assertion over an empty set passes forever, so a parser
    that quietly stops matching (a moved directory, a renamed constant, an `ast` API change)
    would take this whole file down to a no-op without failing once."""
    missing = known - _values(namespace)
    assert not missing, f"the {namespace} scan no longer finds {sorted(missing)}"


def test_a_lane_also_declares_the_version_it_stamps_on_its_batches():
    """The escape hatch, closed. A new lane that spells its extractor version into a
    constant this file does not know would drop out of the `extractor_version` namespace
    silently — so a module declaring `LANE` must declare one of the known version names
    too, and adding a third spelling means adding it to `_NAMESPACE_OF`."""
    versioned = set().union(*(_modules_declaring(name) for name in _VERSION_NAMES))
    unversioned = _modules_declaring("LANE") - versioned
    assert not unversioned, (
        f"{sorted(unversioned)} declare a LANE but no extractor version under a name this "
        f"gate knows ({sorted(_VERSION_NAMES)}); add the constant, or add its spelling to "
        f"_NAMESPACE_OF so its uniqueness is checked too")


def test_a_leased_lane_declares_both_halves_of_its_lease():
    """`lease.held()` takes a job name AND a concurrency group; a lane that declares one
    without the other has half a lease and the missing half becomes a literal at the call
    site, where nothing checks it against anyone else's."""
    assert _modules_declaring("JOB_NAME") == _modules_declaring("CONCURRENCY_GROUP")


# ------------------------------------------------------------------ the detector itself

def test_the_detector_reports_two_modules_claiming_one_value():
    assert _collisions([
        ("lane", "LANE", "location_claims_remine", "location_data/claims_remine.py"),
        ("lane", "LANE", "location_claims_remine", "location_data/claims_remine_archive.py"),
    ]) == {("lane", "location_claims_remine"): [
        "location_data/claims_remine.py", "location_data/claims_remine_archive.py"]}


def test_the_detector_exempts_the_serialisation_group():
    """Two lanes sharing a concurrency group is the group doing its job — `payload_prune`
    and `payload_backfill` share `location-payload` on purpose."""
    assert _collisions([
        ("concurrency_group", "CONCURRENCY_GROUP", "location-payload",
         "location_data/payload_prune.py"),
        ("concurrency_group", "CONCURRENCY_GROUP", "location-payload",
         "location_data/payload_backfill.py"),
    ]) == {}


def test_the_detector_allows_one_module_to_reuse_a_value_across_namespaces():
    assert _collisions([
        ("lane", "LANE", "x", "location_data/a.py"),
        ("job_name", "JOB_NAME", "x", "location_data/a.py"),
    ]) == {}


def test_the_detector_sees_an_annotated_assignment(tmp_path):
    module = tmp_path / "annotated.py"
    module.write_text('LANE: str = "location_annotated"\nOTHER = 3\n', encoding="utf-8")
    assert ("LANE", "location_annotated") in set(_string_constants(module))


def test_the_detector_ignores_a_lane_string_built_inside_a_function(tmp_path):
    module = tmp_path / "nested.py"
    module.write_text('def f():\n    LANE = "location_nested"\n    return LANE\n',
                      encoding="utf-8")
    assert not [n for n, _ in _string_constants(module) if n == "LANE"]
