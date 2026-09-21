"""The three contract gates, over the checked-in per-portal key census. Pure, offline.

They read `data/field_capture/census/*.json` — what each portal's stored payload actually
carries — and never a hand-authored fixture, because a hand-authored fixture can only
assert back what the test planted. That is how remax's parser read `balkon` and `lodzie`
for the whole time the portal emitted neither, with green tests and `has_balcony` 0/0 on
every active row.

  A1  no dead read      — every source key a `structured` cell names is a key that portal
                          emits.
  A2  no unread emission — every census key above the floor is read by some cell or is on
                          the explicit `IGNORED` list WITH a reason.
  A3  no unmapped value  — every value the census records for an enum cell resolves in
                          `scraper.vocabulary`.

The census is a sample of the newest 1,000 rows, so a key below the floor is a known blind
spot (PROGRAM.md §7); staleness is a `verify_pipeline` warning, never a CI failure keyed
on the calendar.
"""

from __future__ import annotations

import pytest

from scraper import attribute_contract as contract
from scraper import field_census, vocabulary

# The share of sampled rows above which an unread key is a decision someone has to make.
UNREAD_FLOOR_PCT = 5.0

# Which cells carry a closed vocabulary, by the contract's own source keys. The value is
# the vocabulary field; `None` means the cell is read by a grammar (a disposition, a PENB
# letter, a boolean) whose input space is not a label list.
_ENUM_FIELDS: tuple[str, ...] = (
    "condition", "building_type", "ownership", "furnished",
)


@pytest.fixture(scope="module")
def censuses() -> dict[str, dict]:
    return {c["portal"]: c for c in field_census.load_censuses()}


def _emitted_keys(census: dict) -> set[str]:
    return set(census.get("keys") or {}) | set((census.get("rare") or {}).get("keys") or [])


def _is_flattened_name(entry: dict) -> bool:
    """A census row the instrument FLATTENED out of a list, not a payload key.

    The census lifts mmreality's accessory names and the presence of the description
    column into the key space so they can be counted; both are stamped with the single
    value `present`. They are read through the key that HOLDS them (`accessoryGroups`),
    so A2 must not demand a cell of its own for "Kuchyňská linka"."""
    return list((entry.get("values") or {})) == ["present"]


def test_a1_no_dead_read(censuses: dict[str, dict]) -> None:
    """Every key a structured cell names appears in that portal's census."""
    assert set(contract.CONTRACT) <= set(censuses), "a portal has no checked-in census"
    dead: list[str] = []
    for portal, cells in sorted(contract.CONTRACT.items()):
        emitted = _emitted_keys(censuses[portal])
        for field, declared in sorted(cells.items()):
            if declared.producer != "structured":
                continue
            assert declared.keys, f"{portal}/{field}: a structured cell names no key"
            dead += [f"{portal}/{field} reads {key!r}"
                     for key in declared.keys if key not in emitted]
    assert not dead, (
        f"{len(dead)} dead read(s) — the portal has never emitted the key, so the cell "
        f"cannot be anything but empty: {dead}"
    )


def test_a2_no_unread_emission(censuses: dict[str, dict]) -> None:
    """Every census key above the floor is read by a cell or explicitly ignored."""
    unread: list[str] = []
    for portal, census in sorted(censuses.items()):
        read = {key for declared in contract.CONTRACT[portal].values()
                for key in declared.keys}
        ignored = contract.IGNORED[portal]
        for key, entry in sorted((census.get("keys") or {}).items()):
            if (key in read or key in ignored
                    or _is_flattened_name(entry)
                    or float(entry.get("pct", 0.0)) < UNREAD_FLOOR_PCT):
                continue
            unread.append(f"{portal} emits {key!r} on {entry['pct']}% of rows")
    assert not unread, (
        f"{len(unread)} key(s) the portal publishes and nothing reads. Either wire the "
        f"key to a cell or add it to attribute_contract.IGNORED with a one-line reason "
        f"— the list is the deliverable: it turns 'nobody noticed' into 'someone "
        f"decided'. {unread}"
    )


def test_a2_ignored_entries_are_real_and_reasoned() -> None:
    """An ignored key must be a key, must carry a reason, and must not also be read."""
    for portal, ignored in sorted(contract.IGNORED.items()):
        read = {key for declared in contract.CONTRACT[portal].values()
                for key in declared.keys}
        for key, reason in sorted(ignored.items()):
            assert reason.strip(), f"{portal}: {key!r} is ignored with no reason"
            assert key not in read, f"{portal}: {key!r} is both read and ignored"


def test_a3_no_unmapped_value(censuses: dict[str, dict]) -> None:
    """Every enum value the census records resolves in the vocabulary."""
    vocabulary.UNMAPPED.clear()
    for portal, census in sorted(censuses.items()):
        keys = census.get("keys") or {}
        for field in _ENUM_FIELDS:
            declared = contract.CONTRACT[portal][field]
            if declared.producer != "structured":
                continue
            for key in declared.keys:
                for value in sorted((keys.get(key) or {}).get("values") or {}):
                    if (value == field_census.JSON_NULL
                            or len(value) >= field_census.VALUE_TRUNCATE_CHARS):
                        continue
                    label = _label(value)
                    if contract.source_value(portal, field, {key: label}) is None:
                        continue  # a declared sentinel: absence, not a value
                    vocabulary.canonical(field, portal, label)
    # A refusal ("neuvedeno", "jiné") is a decision and stays silent; only a label
    # NOTHING names is counted, and every one of those is a column cell that would go
    # NULL on the next scrape.
    unmapped = vocabulary.unmapped_events()
    assert not unmapped, (
        f"{len(unmapped)} live value(s) no vocabulary entry names — map each one or "
        f"declare it a refusal: {unmapped}"
    )


def _label(value: str) -> str:
    """The census records a JSON portal's value as its JSON text; the label is inside."""
    import json

    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    if isinstance(parsed, dict):
        for key in ("name", "code"):
            if isinstance(parsed.get(key), str):
                return parsed[key]
    return value


def test_known_gaps_cover_every_declared_zero() -> None:
    """A cell with no producer declares whether a census key would fill it."""
    gaps = contract.known_gaps()
    assert gaps, "the contract declares no gaps at all — that cannot be right"
    for name, key in sorted(gaps.items()):
        portal = name.split("/", 1)[0]
        if key is None:
            continue
        census = {c["portal"]: c for c in field_census.load_censuses()}[portal]
        assert key in _emitted_keys(census), (
            f"{name}: the gap names {key!r}, which that portal does not emit"
        )
