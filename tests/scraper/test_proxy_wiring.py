"""A portal that declares `USE_PROXY` must actually GET the proxy.

This is the same census shape as `test_walk_deadline_wiring.py`, and it exists
for the same reason: `USE_PROXY = True` in a client is only half of the change.
The other half is `SCRAPER_PROXY_URL` reaching the job's environment, and the
failure when it doesn't is INVISIBLE — `BasePortalClient` logs one warning and
falls back to the direct IP, so the scrape stays green, the tests stay green,
and the portal quietly keeps the throttled egress the flag existed to escape.

idnes is the case that makes this worth pinning. It is not 403-blocked like
ceskereality and mmreality; it is soft-throttled, so "the proxy silently did not
apply" and "the proxy applied" produce the same status codes and the same parsed
rows — only 13x apart in wall-clock, which nothing asserts on.

Each portal's own unit tests can see its own client. None of them can see a
missing line in a YAML file.
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRAPER = _ROOT / "scraper"
_WORKFLOWS = _ROOT / ".github" / "workflows"

PROXY_ENV = "SCRAPER_PROXY_URL"


def _proxied_clients() -> list[tuple[str, str]]:
    """(module stem, source slug) for every client class with USE_PROXY = True."""
    found: list[tuple[str, str]] = []
    for path in sorted(_SCRAPER.glob("*_client.py")):
        mod = importlib.import_module(f"scraper.{path.stem}")
        for obj in vars(mod).values():
            if (
                inspect.isclass(obj)
                and obj.__module__ == mod.__name__
                and getattr(obj, "USE_PROXY", False)
            ):
                found.append((path.stem, path.stem.removesuffix("_client")))
    return found


def test_at_least_one_portal_is_proxied() -> None:
    """Guards the census itself: if the discovery ever silently matches nothing,
    every parametrised test below would vacuously pass."""
    assert _proxied_clients(), "no client declares USE_PROXY — has the attribute moved?"


@pytest.mark.parametrize("stem,source", _proxied_clients())
def test_a_proxied_portals_workflows_pass_the_secret(stem: str, source: str) -> None:
    """Every workflow that runs this portal's scraper must hand it the secret."""
    runners = [
        p for p in sorted(_WORKFLOWS.glob("*.yml"))
        if re.search(rf"scraper\.{source}(_main)?\b", p.read_text(encoding="utf-8"))
    ]
    assert runners, f"no workflow runs scraper.{source}* — did a workflow get renamed?"
    missing = [
        p.name for p in runners
        if PROXY_ENV not in p.read_text(encoding="utf-8")
    ]
    assert missing == [], (
        f"{stem} sets USE_PROXY but {missing} never pass {PROXY_ENV}: those jobs "
        f"fall back to the direct IP with only a log warning, so the portal keeps "
        f"the throttled egress while every check stays green"
    )


def test_the_fallback_warns_rather_than_failing_silently() -> None:
    """The direct-IP fallback is deliberate — a missing secret must degrade, not
    break — but it has to be LOUD, because a silent fallback is how a proxied
    portal reverts to unproxied without anyone noticing."""
    src = (_SCRAPER / "portal_base.py").read_text(encoding="utf-8")
    block = src[src.index("USE_PROXY"):]
    assert "LOG.warning" in block[: block.index("def ") if "def " in block else len(block)] or \
        "LOG.warning" in src, "portal_base no longer warns when USE_PROXY has no URL"


# --- required vs merely preferred --------------------------------------------
#
# There are two ways an edge punishes our datacenter IP, and conflating them
# costs data in opposite directions. ceskereality and mmreality HARD-403: without
# the proxy they return nothing, so running them unproxied only burns requests.
# idnes SOFT-throttles: every page still arrives, ~13x slower. Skipping idnes
# when the proxy is absent would trade degraded data for NO data.


def test_every_proxied_client_states_whether_the_proxy_is_required() -> None:
    for stem, _ in _proxied_clients():
        mod = importlib.import_module(f"scraper.{stem}")
        for obj in vars(mod).values():
            if (
                inspect.isclass(obj)
                and obj.__module__ == mod.__name__
                and getattr(obj, "USE_PROXY", False)
            ):
                assert isinstance(getattr(obj, "PROXY_REQUIRED", None), bool), (
                    f"{stem}.{obj.__name__} sets USE_PROXY without saying whether "
                    f"the direct IP still works"
                )


def test_the_worker_skips_only_portals_that_REQUIRE_the_proxy(monkeypatch) -> None:
    """The regression this guards: marking a soft-throttled portal proxied would
    silently remove it from the realtime worker's probe rotation, because the
    skip used to key on USE_PROXY alone."""
    from scraper import realtime_worker as rw

    monkeypatch.delenv(PROXY_ENV, raising=False)
    monkeypatch.setattr(rw, "_PROXY_WARNED", set())
    # Only sources the worker actually rotates — mmreality is proxied but has no
    # worker lane, so it never reaches this function.
    assert rw._skip_for_proxy("ceskereality") is True   # hard 403 without it
    assert rw._skip_for_proxy("idnes") is False         # slow, but it works
    assert rw._skip_for_proxy("sreality") is False      # not proxied at all


def test_the_proxy_is_used_when_present_even_if_not_required(monkeypatch) -> None:
    """`PROXY_REQUIRED = False` must not read as `USE_PROXY = False`: the whole
    point is that idnes still RIDES the proxy wherever one is configured."""
    from scraper.idnes_client import IdnesClient

    monkeypatch.setenv(PROXY_ENV, "http://user:pass@residential.example:8080")
    client = IdnesClient()
    assert client._session.proxies.get("https") == "http://user:pass@residential.example:8080"
