"""The resolver is a pure function — enforced by AST scan, not by review (03 §3.0).

"Same inputs ⇒ byte-identical output" is only true if nothing in the core reads a wall
clock, a socket or an RNG. The core's ONLY notion of "now" is
`as_of = max(observed_at)` over the consumed claims, which is why
`location_field_policy.max_age_days` is interpreted relative to `as_of` and never to
`now()`.

The three JOB modules are excluded by name and by nothing else: they are allowed a clock
(a drain budget), a connection and a lease id, and they are the only modules that are.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).resolve().parents[2] / "location_data" / "resolver"

# Jobs, not the resolver: they own the DB connection, the run budget and the lease.
_JOB_MODULES = {"drain.py", "epoch_job.py", "resolve_db.py", "lease.py"}

_FORBIDDEN_ATTRS = {
    ("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"),
    ("date", "today"), ("time", "time"), ("time", "monotonic"), ("time", "perf_counter"),
}
_FORBIDDEN_MODULES = {"random", "secrets", "socket", "requests", "urllib", "uuid", "psycopg"}
_FORBIDDEN_CALLS = {"input", "id"}


def _pure_modules() -> list[Path]:
    return sorted(p for p in _PACKAGE.glob("*.py") if p.name not in _JOB_MODULES)


def test_the_scan_actually_covers_the_core():
    names = {p.name for p in _pure_modules()}
    assert {"core.py", "candidates.py", "position.py", "precision.py", "survivorship.py"} <= names


@pytest.mark.parametrize("path", _pure_modules(), ids=lambda p: p.name)
def test_no_wall_clock_no_network_no_randomness(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offences: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _FORBIDDEN_MODULES:
                    offences.append(f"import {alias.name} (line {node.lineno})")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _FORBIDDEN_MODULES:
                offences.append(f"from {node.module} import … (line {node.lineno})")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if (func.value.id, func.attr) in _FORBIDDEN_ATTRS:
                    offences.append(f"{func.value.id}.{func.attr}() (line {node.lineno})")
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALLS:
                offences.append(f"{func.id}() (line {node.lineno})")
    assert not offences, (
        f"{path.name} breaks resolver purity (03 §3.0): " + ", ".join(offences)
    )


def test_jobs_are_the_only_modules_naming_psycopg_in_code():
    """Prose may discuss psycopg; the CODE of a pure module may not name it. Scanned over
    the AST so a docstring cannot fail the test and a lazy in-function import cannot pass
    it."""
    offenders: list[str] = []
    for path in _pure_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "psycopg":
                offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == "psycopg":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "a pure resolver module names psycopg in code: " + ", ".join(offenders)
    )
