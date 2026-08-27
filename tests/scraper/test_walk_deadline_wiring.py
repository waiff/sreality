"""The walk deadline must be WIRED, not merely implemented.

A deadline check that no caller ever supplies a deadline to is a no-op that
reads like a fix. That is not hypothetical: when this change was first written,
the per-page check landed in all nine portals and `--max-seconds` was forwarded
to `run_index_walk` by only TWO of them, so seven portals kept exactly the
behaviour the change existed to remove — while every unit test passed.

This module is a census, in the same spirit as the per-m² registry census: it
walks the real portal modules and asserts the whole chain exists, because the
per-portal unit tests each verify their own link and none of them can see a
missing one.
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

_SCRAPER = Path(__file__).resolve().parents[2] / "scraper"

# Every portal that the shared runner drives. sreality's module is `main`; the
# rest follow `<source>_main`.
PORTAL_MODULES = [
    "main", "bazos", "bezrealitky", "ceskereality", "idnes",
    "maxima", "mmreality", "realitymix", "remax",
]


def _module_path(name: str) -> Path:
    return _SCRAPER / f"{name}.py" if name == "main" else _SCRAPER / f"{name}_main.py"


def _portal_class(name: str):
    mod = importlib.import_module(f"scraper.{name}" if name == "main" else f"scraper.{name}_main")
    for obj in vars(mod).values():
        if inspect.isclass(obj) and hasattr(obj, "walk_category") and obj.__module__ == mod.__name__:
            return obj
    raise AssertionError(f"no Portal class with walk_category in {name}")


@pytest.mark.parametrize("portal", PORTAL_MODULES)
def test_walk_category_accepts_a_deadline(portal: str) -> None:
    """The runner passes the deadline POSITIONALLY, so a portal that omits the
    parameter raises TypeError on every walk — a total outage, not a degradation."""
    sig = inspect.signature(_portal_class(portal).walk_category)
    assert "deadline" in sig.parameters, f"{portal}.walk_category has no deadline parameter"


@pytest.mark.parametrize("portal", PORTAL_MODULES)
def test_the_page_loop_actually_checks_the_deadline(portal: str) -> None:
    """Checking only between categories is what let idnes be SIGKILLed at page
    599 of ~1,050: one category is bigger than the whole budget, so the outer
    check never got a turn."""
    src = _module_path(portal).read_text(encoding="utf-8")
    assert "deadline_reached(deadline)" in src, (
        f"{portal} never calls deadline_reached(deadline) — its walk cannot stop early"
    )


@pytest.mark.parametrize("portal", PORTAL_MODULES)
def test_max_seconds_is_forwarded_to_the_index_walk(portal: str) -> None:
    """The link that was missing on seven of nine portals. Without it the
    deadline is always None and every per-page check below is dead code."""
    src = _module_path(portal).read_text(encoding="utf-8")
    call = re.search(
        r"run_index_walk[^)]*?max_seconds\s*=", src, re.DOTALL,
    )
    assert call, (
        f"{portal} calls run_index_walk without max_seconds — the deadline is "
        f"None in production and the walk is unbounded"
    )


@pytest.mark.parametrize("portal", PORTAL_MODULES)
def test_the_cli_exposes_max_seconds(portal: str) -> None:
    """A budget nothing can set is a budget that is never set. sreality had no
    --max-seconds flag at all, so its walk was structurally unbounded and
    survived only because it happens to finish inside the CI ceiling today."""
    src = _module_path(portal).read_text(encoding="utf-8")
    assert '"--max-seconds"' in src, f"{portal} has no --max-seconds flag"


def test_nobody_hand_rolls_the_deadline_comparison() -> None:
    """One definition, because `>=` inverted to `<=` yields a walk that runs to
    the job timeout while looking correct. The helper cannot be got backwards
    at a call site; an inline comparison can."""
    offenders: list[str] = []
    for path in sorted(_SCRAPER.glob("*.py")):
        if path.name == "portal.py":
            continue  # the definition site itself
        src = path.read_text(encoding="utf-8")
        if re.search(r"time\.monotonic\(\)\s*[<>]=?\s*deadline", src) or re.search(
            r"deadline\s*[<>]=?\s*time\.monotonic\(\)", src
        ):
            offenders.append(path.name)
    assert offenders == [], f"{offenders} compare against deadline inline; use deadline_reached()"
