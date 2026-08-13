"""The fixture-diff gate — permanent CI, not a wave gate (02 §2.1.8.3, §2.7 item 0(b)).

`location_claims` is append-only: a mis-pointed locator writes tens of thousands of wrong
claims that can never be deleted, only retracted. Two mechanisms guard that, and 02 §2.7
item 0(b) requires BOTH standing before the first contract writes a production claim.
`location_claim_retractions` is the first (migration 382 + `contracts.retract()`); this
module is the second.

What it does: for every portal contract, take the listing ids its `regressions:` block
names, run each one that has a frozen fixture body through the real extractor
(`claims_intake.extract_listing`, dispatching whatever readers that portal's YAML declares
today), and compare the resulting claims / absences / enrichment tasks against a committed
golden. A change fails the build with a claim-level diff — which extractor, which field,
old value vs new — and is accepted by committing the re-blessed golden:

    python -m tests.location_data.test_contract_fixture_diff --bless

It rides `.github/workflows/test.yml` rather than a workflow of its own, so it runs on
every push including the ones that only touch `contracts/`.

Two things it deliberately does NOT gate. Contract prose: a `regressions:` line can be
reworded freely — only the listing ids it names are golden. And the contract bytes: 02
§2.1.8.3 makes the failure condition "a contract change that alters claims", so a comment
or a `volatile_paths` edit stays green. A `contract_version` bump does red the build,
because the golden is per version and a bump is exactly when a human should look; the
superseded golden is kept rather than deleted, both so that failure can show the diff
across the bump and because a retraction (02 §2.1.8 mechanism 2) needs to know what the
retracted version claimed.

Coverage is a subset today by design: most W2 entries carry no `reader` yet (they land in
W2-6…W2-12), and most pinned listing ids have no captured body in this repo. Both gaps are
written into the golden — `listings_without_a_fixture_body` — so coverage arriving later
is itself a reviewed diff rather than a silent change.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from location_data import contracts
from location_data.claims_intake import (
    DEFAULT_MAX_CLAIM_VALUE_BYTES,
    Absence,
    Claim,
    EnrichmentTask,
    IntakeResult,
    ListingRow,
    extract_listing,
    value_norm_mirror,
)
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parents[2]
_W2 = _ROOT / "tests" / "fixtures" / "location_w2"
_GOLDEN_DIR = _W2 / "golden"
_BODY_DIR = _W2 / "regressions"

BLESS_COMMAND = "python -m tests.location_data.test_contract_fixture_diff --bless"


# --------------------------------------------------------- the pinned regression register

# 02 §2.7 item 0(b), quoted by BUILD-PLAN-w2a-w2 § W2-5. These ids may not silently leave a
# contract's `regressions:` block; the fixture body behind each one may still be missing.
PINNED_REGRESSIONS: dict[str, tuple[str, ...]] = {
    "sreality": ("520268", "1588965452", "3067969612"),
    "realitymix": ("8375963", "8375983", "8595551"),
    "maxima": ("f60012522", "d40026367", "f60012682"),
}

# A regression line is prose: "<id>[ / <id>…] — what went wrong". Only the head before the
# em-dash is scanned, so the ids inside the explanation (realitymix's "48.37498/17.00147",
# idnes' "obec/okres/region/ku_id") are never mistaken for listings.
_LISTING_ID = re.compile(r"^[A-Za-z]?\d{4,}$")


def regression_listing_ids(line: str) -> list[str]:
    ids: list[str] = []
    for token in line.split("—", 1)[0].split("/"):
        token = token.strip()
        if _LISTING_ID.match(token) and token not in ids:
            ids.append(token)
    return ids


def contract_regression_ids(contract: contracts.PortalContract) -> list[str]:
    ids: list[str] = []
    for line in contract.fetch_config.get("regressions") or []:
        for listing_id in regression_listing_ids(str(line)):
            if listing_id not in ids:
                ids.append(listing_id)
    return sorted(ids)


# ------------------------------------------------------------------- the frozen bodies

@dataclass(frozen=True, slots=True)
class Body:
    """One frozen input row for one regression listing.

    `name` is what the golden records as the body's provenance, so a reviewer reading a
    diff can find the bytes that produced it.
    """
    name: str
    raw_json: dict[str, Any]
    lat: float | None = None
    lon: float | None = None
    in_mapy_inventory: bool = False
    locality: str | None = None
    street: str | None = None
    street_source: str | None = None

    def row(self, source: str, listing_id: str) -> ListingRow:
        return fx.listing(
            source, self.raw_json, native=listing_id, lat=self.lat, lon=self.lon,
            in_mapy_inventory=self.in_mapy_inventory, locality=self.locality,
            street=self.street, street_source=self.street_source)

    def as_json(self) -> dict[str, Any]:
        return {
            "lat": self.lat, "lon": self.lon,
            "in_mapy_inventory": self.in_mapy_inventory,
            "listings.locality": self.locality,
            "listings.street": self.street,
            "listings.street_source": self.street_source,
        }


# Bodies already committed to this repo, bound to a regression listing only where the repo
# itself already ties the two together — the payload's own `id` key, or the comment above
# it. Nothing here is synthesised for this gate: a pinned id with no such body is reported
# as uncovered rather than given invented bytes. Coordinates and legacy-column values are
# the ones the W1 portal/licence suites already commit for the same payload.
_COMMITTED_BODIES: dict[str, dict[str, tuple[Body, ...]]] = {
    "sreality": {
        # claim_intake_fixtures.py:155 names this listing: the 80 KB geometry blob that
        # truncated raw_json and destroyed the locality object.
        "1588965452": (Body("claim_intake_fixtures.SREALITY_TRUNCATED",
                            fx.SREALITY_TRUNCATED, lat=50.0, lon=14.0),),
    },
    "bezrealitky": {
        "1037096": (Body("claim_intake_fixtures.BEZREALITKY", fx.BEZREALITKY,
                         lat=50.1092, lon=14.4749),),
    },
    "bazos": {
        "220059906": (Body("claim_intake_fixtures.BAZOS_LINK", fx.BAZOS_LINK,
                           lat=48.8489, lon=17.1325),),
        "220870847": (Body("claim_intake_fixtures.BAZOS_STREET_GEOCODE",
                           fx.BAZOS_STREET_GEOCODE, lat=49.5, lon=15.5),),
    },
    "remax": {
        # Two committed payloads carry this id: the v1 shape and the mixed row where the
        # banned `address` key sits beside the subject's own line. Both are scored.
        "445781": (Body("claim_intake_fixtures.REMAX", fx.REMAX,
                        lat=50.0810, lon=14.4508),
                   Body("claim_intake_fixtures.REMAX_BOTH_ADDRESS_KEYS",
                        fx.REMAX_BOTH_ADDRESS_KEYS, lat=50.0810, lon=14.4508)),
    },
    "ceskereality": {
        "3180041": (Body("claim_intake_fixtures.CESKEREALITY_NULL_LOCALITY",
                         fx.CESKEREALITY_NULL_LOCALITY,
                         locality="České Budějovice 4, U Smaltovny",
                         street="U Smaltovny", street_source="parser"),),
        "3849899": (Body("claim_intake_fixtures.CESKEREALITY_PAGE", fx.CESKEREALITY_PAGE,
                         lat=50.0446, lon=14.3204, locality="Praha Stodůlky"),),
    },
    "realitymix": {
        "8375963": (Body("claim_intake_fixtures.REALITYMIX_GEOCODE",
                         fx.REALITYMIX_GEOCODE, lat=48.37498, lon=17.00147),),
    },
    "maxima": {
        "d40031686": (Body("claim_intake_fixtures.MAXIMA_PAGE", fx.MAXIMA_PAGE,
                           lat=50.7663, lon=15.0562),),
    },
}


def disk_bodies(source: str, root: Path = _BODY_DIR) -> dict[str, tuple[Body, ...]]:
    """Captured bodies dropped on disk as `<root>/<portal>/<listing-id>[.variant].json`.

    The extension point W2-6…W2-12 use: a portal PR adds a captured payload here and
    re-blesses, and the gate scores it from then on with no change to this module.
    """
    directory = root / source
    found: dict[str, list[Body]] = {}
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        listing_id = path.stem.split(".", 1)[0]
        doc = json.loads(path.read_text(encoding="utf-8"))
        found.setdefault(listing_id, []).append(Body(
            name=str(path.relative_to(_ROOT) if path.is_relative_to(_ROOT) else path),
            raw_json=doc["raw_json"],
            lat=doc.get("lat"), lon=doc.get("lon"),
            in_mapy_inventory=bool(doc.get("in_mapy_inventory", False)),
            locality=doc.get("listings.locality"),
            street=doc.get("listings.street"),
            street_source=doc.get("listings.street_source")))
    return {k: tuple(v) for k, v in found.items()}


def bodies_for(source: str) -> dict[str, tuple[Body, ...]]:
    merged = {k: tuple(v) for k, v in _COMMITTED_BODIES.get(source, {}).items()}
    for listing_id, bodies in disk_bodies(source).items():
        merged[listing_id] = merged.get(listing_id, ()) + bodies
    return merged


# ------------------------------------------------------------------ the golden artefact

# Everything a reader of `location_claims` would see, minus the four fields that are row
# identity or wall-clock rather than extraction behaviour: `listing_id` / `source` /
# `source_id_native` (the golden's own key), `contract_entry_id` (a bigserial the DB
# assigns), `extractor_version` (the golden header's `contract_version`) and
# `first_observed_at` (the fixture's clock). `value_norm` is the diagnostic mirror of the
# generated column, included because a normalisation change is exactly the kind of silent
# drift this gate exists to surface.
_CLAIM_FIELDS = (
    "claim_type", "surface", "page_kind", "extraction_method", "snapshot_anchor",
    "value_text", "value_norm", "value_num", "value_geom_wkt", "value_shape_wkt",
    "value_jsonb", "distance_m", "travel_mode", "target_text",
    "declared_precision_label", "declared_confidence", "declared_radius_m",
    "claim_confidence", "blur_evidence", "licence_class", "history_completeness",
    "subject_scoped", "legacy_source_column", "legacy_write_path_unknown",
)


def project_claim(claim: Claim) -> dict[str, Any]:
    projected: dict[str, Any] = {"extractor_id": claim.extractor_id}
    for name in _CLAIM_FIELDS:
        projected[name] = (value_norm_mirror(claim.value_text) if name == "value_norm"
                           else getattr(claim, name))
    return projected


def project_absence(absence: Absence) -> dict[str, Any]:
    return {"surface": absence.surface, "field": absence.field_, "reason": absence.reason,
            "extraction_method": absence.extraction_method, "detail": absence.detail}


def project_enrichment(task: EnrichmentTask) -> dict[str, Any]:
    return {"method": task.method, "lane": task.lane, "outcome": task.outcome,
            "input_hash": task.input_hash, "error": task.error}


def project_result(result: IntakeResult) -> dict[str, Any]:
    return {
        "claims": [project_claim(c) for c in result.claims],
        "absences": [project_absence(a) for a in result.absences],
        "enrichment": [project_enrichment(e) for e in result.enrichment],
        "oversized": result.oversized,
    }


def score(source: str, listing_id: str, body: Body) -> dict[str, Any]:
    """One fixture through the real extractor. `max_value_bytes` is pinned rather than
    read from the environment so a CI env var can never move the golden."""
    result = extract_listing(body.row(source, listing_id), fx.entries_for(source),
                             max_value_bytes=DEFAULT_MAX_CLAIM_VALUE_BYTES)
    return {"listing": listing_id, "body": body.name, "row": body.as_json(),
            **project_result(result)}


def build_golden(contract: contracts.PortalContract) -> dict[str, Any]:
    declared = contract_regression_ids(contract)
    bodies = bodies_for(contract.source)
    scored = [score(contract.source, listing_id, body)
              for listing_id in sorted(bodies)
              for body in bodies[listing_id]]
    return {
        "portal": contract.source,
        "contract_version": contract.version,
        "regression_listing_ids": declared,
        "listings_without_a_fixture_body": [i for i in declared if i not in bodies],
        "fixtures": scored,
    }


def golden_path(source: str, version: int) -> Path:
    return _GOLDEN_DIR / f"{source}@{version}.json"


def previous_golden(source: str, version: int) -> Path | None:
    older = [(int(p.stem.split("@")[1]), p) for p in _GOLDEN_DIR.glob(f"{source}@*.json")
             if int(p.stem.split("@")[1]) < version]
    return max(older)[1] if older else None


def write_golden(contract: contracts.PortalContract) -> Path:
    path = golden_path(contract.source, contract.version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_golden(contract), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return path


# ------------------------------------------------------------------ the readable diff

def _fixture_key(fixture: dict[str, Any]) -> tuple[str, str]:
    return str(fixture["listing"]), str(fixture["body"])


def _keyed(items: list[dict[str, Any]],
           *fields: str) -> dict[tuple[Any, ...], dict[str, Any]]:
    """Key each item on its identity fields plus an ordinal, so a repeated (cardinality:
    many) emission stays distinguishable and a VALUE edit reads as a change rather than as
    a removal beside an unrelated addition."""
    seen: dict[tuple[Any, ...], int] = {}
    keyed: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        identity = tuple(item.get(f) for f in fields)
        ordinal = seen.get(identity, 0)
        seen[identity] = ordinal + 1
        keyed[(*identity, ordinal)] = item
    return keyed


def _label(kind: str, key: tuple[Any, ...]) -> str:
    parts = [str(p) for p in key[:-1]]
    suffix = f" #{key[-1] + 1}" if key[-1] else ""
    return f"{kind} {' / '.join(parts)}{suffix}"


def _short(value: Any) -> str:
    text = (repr(value) if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False))
    return text if len(text) <= 88 else text[:85] + "…"


def _diff_section(kind: str, golden: list[dict[str, Any]], actual: list[dict[str, Any]],
                  *fields: str) -> list[str]:
    old, new = _keyed(golden, *fields), _keyed(actual, *fields)
    lines: list[str] = []
    for key in old:
        if key not in new:
            lines.append(f"    - removed  {_label(kind, key)}")
            lines.extend(f"                  {k} = {_short(v)}"
                         for k, v in old[key].items() if k not in fields and v is not None)
    for key in new:
        if key not in old:
            lines.append(f"    + added    {_label(kind, key)}")
            lines.extend(f"                  {k} = {_short(v)}"
                         for k, v in new[key].items() if k not in fields and v is not None)
    for key in new:
        if key not in old:
            continue
        changed = [(k, old[key].get(k), new[key][k]) for k in new[key]
                   if old[key].get(k) != new[key][k]]
        changed += [(k, v, None) for k, v in old[key].items() if k not in new[key]]
        if changed:
            lines.append(f"    ~ changed  {_label(kind, key)}")
            lines.extend(f"                  {k}: {_short(was)} -> {_short(now)}"
                         for k, was, now in changed)
    # Emission order is contract entry order, and 02 §2.1.8 forbids reordering entries
    # (ids are never reused). A pure reorder changes no key, so it is checked separately.
    was_order = [i.get(fields[0]) for i in golden]
    now_order = [i.get(fields[0]) for i in actual]
    if not lines and was_order != now_order:
        lines.append(f"    ~ reordered {kind}s: {was_order} -> {now_order}")
    return lines


def diff_golden(golden: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for field in ("contract_version", "regression_listing_ids",
                  "listings_without_a_fixture_body"):
        if golden.get(field) != actual.get(field):
            lines.append(f"  {field}: {_short(golden.get(field))} -> "
                         f"{_short(actual.get(field))}")

    old = {_fixture_key(f): f for f in golden.get("fixtures", [])}
    new = {_fixture_key(f): f for f in actual.get("fixtures", [])}
    for key in sorted(old.keys() - new.keys()):
        lines.append(f"  - fixture dropped: listing {key[0]} ({key[1]})")
    for key in sorted(new.keys() - old.keys()):
        lines.append(f"  + fixture added:   listing {key[0]} ({key[1]})")
    for key in sorted(old.keys() & new.keys()):
        body = _diff_section("claim", old[key]["claims"], new[key]["claims"],
                             "extractor_id", "claim_type")
        body += _diff_section("absence", old[key]["absences"], new[key]["absences"],
                              "field", "reason", "surface")
        body += _diff_section("enrichment", old[key]["enrichment"],
                              new[key]["enrichment"], "method", "lane")
        if old[key]["oversized"] != new[key]["oversized"]:
            body.append(f"    ~ oversized: {old[key]['oversized']} -> "
                        f"{new[key]['oversized']}")
        if old[key]["row"] != new[key]["row"]:
            body.append(f"    ~ input row: {_short(old[key]['row'])} -> "
                        f"{_short(new[key]['row'])}")
        if body:
            lines.append(f"  listing {key[0]}  ({key[1]})")
            lines.extend(body)
    return lines


# ------------------------------------------------------------------------------ tests

_CONTRACTS = {c.source: c for c in contracts.load_all()}
_SOURCES = sorted(_CONTRACTS)


@pytest.mark.parametrize("source", sorted(PINNED_REGRESSIONS))
def test_the_design_pinned_regressions_stay_declared_in_their_contract(source: str) -> None:
    """02 §2.7 item 0(b)'s named listings. Dropping one from a `regressions:` block would
    quietly delete the gate's reason to score that listing at all."""
    declared = contract_regression_ids(_CONTRACTS[source])
    missing = [i for i in PINNED_REGRESSIONS[source] if i not in declared]
    assert not missing, (
        f"contracts/portals/{source}.yaml no longer declares pinned regression(s) "
        f"{missing}; 02 §2.7 item 0(b) requires {list(PINNED_REGRESSIONS[source])}. "
        f"Declared today: {declared}")


@pytest.mark.parametrize("source", _SOURCES)
def test_the_regression_fixtures_still_produce_the_golden_claims(source: str) -> None:
    contract = _CONTRACTS[source]
    path = golden_path(source, contract.version)
    actual = build_golden(contract)

    if not path.exists():
        stale = previous_golden(source, contract.version)
        detail = ""
        if stale is not None:
            drift = diff_golden(json.loads(stale.read_text(encoding="utf-8")), actual)
            detail = ("\n  vs the last blessed golden " + stale.name + ":\n"
                      + ("\n".join(drift) if drift
                         else "    (no claim changed — the version bump alone)"))
        pytest.fail(
            f"contracts/portals/{source}.yaml is at contract_version "
            f"{contract.version} and has no golden claim set.\n"
            f"A version bump is a reviewed change: inspect the diff, then re-bless with\n"
            f"    {BLESS_COMMAND}{detail}", pytrace=False)

    golden = json.loads(path.read_text(encoding="utf-8"))
    drift = diff_golden(golden, actual)
    if drift:
        # `pytrace=False`: the diff IS the failure. A traceback and a truncated repr of
        # the drift list on top of it is exactly the "assertion failed" unreadability this
        # gate exists to avoid (02 §2.1.8.3 — "prints a claim-level diff").
        pytest.fail(
            f"contracts/portals/{source}.yaml (or a fixture body it names) changed what "
            f"the extractor claims.\nGolden: {path.relative_to(_ROOT)}\n"
            + "\n".join(drift)
            + f"\n\nIf every line above is intended, re-bless the golden:\n"
              f"    {BLESS_COMMAND}", pytrace=False)


def test_every_portal_carries_a_golden_so_a_new_contract_cannot_arrive_ungated() -> None:
    """A portal with no golden is a portal whose claims nobody reviews. Nine today; a
    tenth contract file must bring its own."""
    blessed = {p.stem.split("@")[0] for p in _GOLDEN_DIR.glob("*.json")}
    assert set(_SOURCES) <= blessed, (
        f"no golden claim set for {sorted(set(_SOURCES) - blessed)}; run {BLESS_COMMAND}")


def test_only_the_head_of_a_regression_line_is_read_as_listing_ids() -> None:
    """The prose after the em-dash carries slashed numbers that are not listings —
    realitymix's own pin, idnes' column list — and reading them would invent coverage."""
    assert regression_listing_ids(
        "8375963 / 8375983 — the Bílovec pair, both stored at 48.37498/17.00147"
    ) == ["8375963", "8375983"]
    assert regression_listing_ids(
        "16,833 active rows outside the CZ bbox with obec/okres/region/ku_id all NULL"
    ) == []
    assert regression_listing_ids("d40026367 / f60012682 — byte-identical") == [
        "d40026367", "f60012682"]


def test_a_captured_body_on_disk_is_scored_like_a_committed_one(tmp_path: Path) -> None:
    """W2-6…W2-12's extension point: a portal PR drops a captured payload under
    `tests/fixtures/location_w2/regressions/<portal>/` and re-blesses."""
    (tmp_path / "sreality").mkdir()
    (tmp_path / "sreality" / "999999.json").write_text(json.dumps(
        {"raw_json": fx.SREALITY_POST_CUTOVER, "lat": 50.0784977, "lon": 14.4501973}),
        encoding="utf-8")

    loaded = disk_bodies("sreality", root=tmp_path)
    assert list(loaded) == ["999999"]
    scored = score("sreality", "999999", loaded["999999"][0])
    assert scored["body"].endswith("999999.json")
    assert any(c["extractor_id"] == "sr.det.street" and c["value_text"] ==
               "náměstí Jiřího z Poděbrad" for c in scored["claims"])


def test_the_diff_names_the_field_and_both_values() -> None:
    """The gate's whole value proposition: a reviewer must be able to read what changed
    without opening the golden (02 §2.1.8.3 — "prints a claim-level diff")."""
    golden, index = next(
        (g, i) for g in (build_golden(_CONTRACTS[s]) for s in _SOURCES)
        for i, fixture in enumerate(g["fixtures"]) if fixture["claims"])
    mutated = json.loads(json.dumps(golden))
    mutated["fixtures"][index]["claims"][0]["value_text"] = "MUTATED"

    rendered = "\n".join(diff_golden(golden, mutated))
    assert "~ changed  claim" in rendered
    assert "value_text:" in rendered and "'MUTATED'" in rendered
    assert golden["fixtures"][index]["claims"][0]["extractor_id"] in rendered


if __name__ == "__main__":
    if "--bless" not in sys.argv[1:]:
        raise SystemExit(f"usage: {BLESS_COMMAND}")
    for contract in contracts.load_all():
        print(f"blessed {write_golden(contract).relative_to(_ROOT)}")
