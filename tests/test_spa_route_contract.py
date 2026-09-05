"""The SPA route patterns that cross a territory boundary.

Three territories build in-app URLs: the SPA itself (which owns them, in
`frontend/src/lib/routes.ts`), the API's notification deep links (emitted into
email/Telegram, effectively permanent URLs), and the Chrome extension's "Otevřít
v aplikaci" link. Only the SPA's own registry has a rail — its census test lives
inside the frontend, and `frontend-build.yml` (where the lint ban runs) is
path-filtered to `frontend/**`, so neither would fire on an api-only or
extension-only change.

That is the drift this file closes: renaming a route in the SPA would silently
break every already-sent email and every extension install, with no test failing
anywhere. It runs in `test.yml`, which fires unfiltered on every push and PR.

Deliberately narrow. It pins ONLY the patterns actually emitted across a
boundary — verified by reading both emitters, not assumed:
  /listing/:source/:nativeId   api/notification_outbox.py + chrome-extension
  /listing/:sreality_id        api/notification_outbox.py
  /notifications               api/notification_outbox.py (system-health rows)
Widening it past what actually crosses the boundary would make it a second full
route table to maintain, which is the failure mode it exists to prevent.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ROUTES_TS = REPO / "frontend" / "src" / "lib" / "routes.ts"
EXTENSION_TS = REPO / "chrome-extension" / "src" / "content.ts"

# `key: def('/pattern'),` — the one authored form in the registry.
_DEF = re.compile(r"^\s*(\w+):\s*def\('([^']+)'\),", re.MULTILINE)

# The routes that leave the SPA, and who emits them.
CROSS_TERRITORY = {
    "listingCanonical": "/listing/:source/:nativeId",
    "listingLegacy": "/listing/:sreality_id",
    "notifications": "/notifications",
}


def _registry() -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _DEF.finditer(ROUTES_TS.read_text())}


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """`/listing/:source/:nativeId` -> a regex matching one built path."""
    return re.compile("^" + re.sub(r":[A-Za-z_]\w*", r"[^/]+", pattern) + "$")


def test_the_registry_still_declares_every_externally_emitted_route() -> None:
    """A rename or deletion in the SPA registry fails here, not in someone's inbox."""
    registry = _registry()
    assert registry, f"parsed no ROUTES entries from {ROUTES_TS} — did def() change shape?"
    for key, expected in CROSS_TERRITORY.items():
        assert key in registry, (
            f"ROUTES.{key} is gone from the SPA registry, but it is emitted outside "
            f"the SPA. Update the emitters in the same PR."
        )
        assert registry[key] == expected, (
            f"ROUTES.{key} changed from {expected!r} to {registry[key]!r}. Every "
            f"already-sent notification and every installed extension still points "
            f"at the old path. Update api/notification_outbox.py, "
            f"chrome-extension/src/content.ts and this contract together."
        )


@pytest.fixture()
def spa_base(monkeypatch: pytest.MonkeyPatch) -> str:
    base = "https://app.example.invalid"
    monkeypatch.setenv("SPA_BASE_URL", base)
    return base


def _compose(row: dict[str, object]):
    from api.notification_outbox import compose_message

    return compose_message(row)


def test_api_canonical_deep_link_matches_the_registry_pattern(spa_base: str) -> None:
    msg = _compose(
        {
            "change_kind": "new",
            "locality": "Praha 6",
            "source": "bazos",
            "source_id_native": "abc-123",
            "sreality_id": None,
        }
    )
    path = msg.deep_link[len(spa_base) :]
    assert _pattern_to_regex(CROSS_TERRITORY["listingCanonical"]).match(path), (
        f"notification deep link {path!r} no longer matches the SPA's "
        f"{CROSS_TERRITORY['listingCanonical']!r}"
    )


def test_api_legacy_deep_link_matches_the_registry_pattern(spa_base: str) -> None:
    msg = _compose(
        {
            "change_kind": "new",
            "locality": "Praha 6",
            "source": None,
            "source_id_native": None,
            "sreality_id": -284913,
        }
    )
    path = msg.deep_link[len(spa_base) :]
    assert _pattern_to_regex(CROSS_TERRITORY["listingLegacy"]).match(path), (
        f"notification deep link {path!r} no longer matches the SPA's "
        f"{CROSS_TERRITORY['listingLegacy']!r}"
    )


def test_api_system_health_deep_link_matches_the_registry_pattern(spa_base: str) -> None:
    """System-health rows are not property-grain (migration 462) and land on the feed."""
    msg = _compose({"source_kind": "system_health", "message": "disk full", "sreality_id": None})
    path = msg.deep_link[len(spa_base) :]
    assert path == CROSS_TERRITORY["notifications"], (
        f"system-health deep link {path!r} no longer matches the SPA's "
        f"{CROSS_TERRITORY['notifications']!r}"
    )


def test_extension_builds_the_canonical_listing_route() -> None:
    """Vanilla-TS territory, so this reads the source rather than calling it."""
    src = EXTENSION_TS.read_text()
    built = re.search(
        r"\$\{APP_BASE_URL\}(/listing/\$\{encodeURIComponent\([^)]+\)\}"
        r"/\$\{encodeURIComponent\([^)]+\)\})",
        src,
    )
    assert built, (
        "chrome-extension/src/content.ts no longer builds a "
        "${APP_BASE_URL}/listing/{source}/{native} URL — if the shape changed, "
        "check it still matches the SPA registry."
    )
    # Collapse the interpolations to their pattern shape and compare.
    shape = re.sub(r"\$\{encodeURIComponent\([^)]+\)\}", ":param", built.group(1))
    expected = re.sub(r":[A-Za-z_]\w*", ":param", CROSS_TERRITORY["listingCanonical"])
    assert shape == expected, (
        f"extension builds {shape!r} but the SPA registry declares {expected!r}"
    )


def test_the_contract_names_only_what_actually_crosses_a_boundary() -> None:
    """Guards the file against becoming a second full route table.

    Every entry must be emitted by the API or the extension. An entry nothing
    emits is dead weight that still has to be kept in sync.
    """
    emitters = (REPO / "api" / "notification_outbox.py").read_text() + EXTENSION_TS.read_text()
    for key, pattern in CROSS_TERRITORY.items():
        literal_prefix = pattern.split("/:")[0]
        assert literal_prefix in emitters, (
            f"{key} ({pattern}) is pinned by this contract but nothing outside the "
            f"SPA emits it — drop it rather than maintaining a second route table."
        )


def test_registry_file_is_where_this_test_thinks_it_is() -> None:
    assert ROUTES_TS.exists(), ROUTES_TS
    assert EXTENSION_TS.exists(), EXTENSION_TS
    assert os.path.getsize(ROUTES_TS) > 0
