"""The operator's per-portal politeness constraint, as CI.

Hoisting the sighting diff into portal_runner must not move a single request: each
portal's page delay, 429/403 back-off and concurrency still come from THAT portal's
config + client. These rails pin the one seam politeness enters through (the runner
builds a limiter from the portal's own limits and hands it to the portal's client),
and pin that the shared diff itself never touches the network.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from scraper import portal_factory, portal_runner
from scraper.portal import default_config
from scraper.rate_ledger import DEFAULT_LEASE_N

SOURCES = sorted(portal_factory.PORTAL_CLASSES)
_RATE, _PCT, _SHARED = 0.123, 0.0456, True


def _config(source: str) -> Any:
    base = default_config(source)
    limits = dataclasses.replace(
        base.limits, index_rate=_RATE, price_change_min_pct=_PCT, shared_rate_limiter=_SHARED,
    )
    return dataclasses.replace(base, limits=limits)


@pytest.mark.parametrize("source", SOURCES)
def test_every_portal_is_built_with_its_own_limits(source: str) -> None:
    portal = portal_factory.build_portal(source, _config(source))
    assert portal.source == source
    assert (portal.index_rate, portal.price_change_min_pct, portal.shared_rate_limiter) == (
        _RATE, _PCT, _SHARED,
    )


class _Sentinel(Exception):
    pass


@pytest.mark.parametrize("source", SOURCES)
def test_every_phase_paces_with_this_portals_own_limits(
    source: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float, bool, int]] = []

    def spy(src: str, rate: float, shared: bool, *, lease_n: int = DEFAULT_LEASE_N) -> Any:
        calls.append((src, rate, shared, lease_n))
        raise _Sentinel

    monkeypatch.setattr(portal_runner, "build_rate_limiter", spy)
    portal = portal_factory.build_portal(source, _config(source))
    with pytest.raises(_Sentinel):
        portal_runner.run_index_walk(portal, dry_run=True)
    with pytest.raises(_Sentinel):
        portal_runner.run_index_probe(portal, dry_run=True)
    with pytest.raises(_Sentinel):
        portal_runner.run_detail_drain(portal, None, True, 3, 0.77)
    assert calls == [
        (source, _RATE, _SHARED, DEFAULT_LEASE_N),
        (source, _RATE, _SHARED, portal_runner.PROBE_LEASE_N),
        (source, 0.77, _SHARED, DEFAULT_LEASE_N),
    ]


def test_the_sighting_diff_makes_no_http_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_http(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the sighting diff / nomination made an HTTP request")

    monkeypatch.setattr(requests.Session, "request", no_http)
    db = portal_runner.db
    monkeypatch.setattr(
        db, "index_summary_native",
        lambda _c, _s, ids: {"a": {"id": 1, "price_czk": 100}} if "a" in ids else {})
    monkeypatch.setattr(db, "touch_listings_by_id", lambda _c, ids: len(ids))
    monkeypatch.setattr(db, "enqueue_detail", lambda _c, _s, entries: len(entries))
    monkeypatch.setattr(
        db, "presence_candidates", lambda *_a, **_k: ([("z", None, None)], 3))
    monkeypatch.setattr(
        db, "enqueue_presence_checks", lambda *_a, **_k: (1, 0))

    out = portal_runner.reconcile_sightings(
        object(), "remax", {"a": ("r", 200), "b": ("r", 5)}, min_change_pct=0.0)
    assert out == {"found_new": 1, "enqueued": 2}
    fake = SimpleNamespace(source="remax")
    assert portal_runner._queue_presence_checks(
        fake, object(), {"category_main": "byt"}, {"a"}, "byt", "prodej") == (1, 0)


def _client_calls_without_limiter(tree: ast.AST) -> list[str]:
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
        if not (name.endswith("Client") or name == "_build_client"):
            continue
        if not any(kw.arg == "limiter" for kw in node.keywords):
            bad.append(f"{name}() at line {node.lineno}")
    return bad


def _class_node(module_name: str, class_name: str) -> ast.ClassDef:
    path = Path(importlib.import_module(module_name).__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)


@pytest.mark.parametrize("source", SOURCES)
def test_every_walk_client_is_built_with_the_runner_limiter(source: str) -> None:
    module_name, class_name = portal_factory.PORTAL_CLASSES[source]
    trees: list[ast.AST] = [_class_node(module_name, class_name)]
    if source == "sreality":
        from scraper import main as sreality_main

        trees.append(ast.parse(inspect.getsource(sreality_main._walk_category_split)))
    bad = [b for t in trees for b in _client_calls_without_limiter(t)]
    assert bad == [], (
        f"{source}: {bad} build a client without the runner's limiter -- that client "
        "would pace itself outside this portal's configured rate"
    )


def test_the_default_client_seam_passes_the_runner_limiter() -> None:
    assert "limiter=limiter" in inspect.getsource(portal_runner.PortalDefaults.make_client)
