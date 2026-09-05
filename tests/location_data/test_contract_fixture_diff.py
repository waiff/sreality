"""The fixture-diff gate: a contract change that alters claims fails the build.

- 02 §2.7 item 0(b): this and `location_claim_retractions` (mig 382 + `contracts.retract()`)
  must BOTH stand before the first contract writes a production claim, because
  `location_claims` is append-only and a mis-pointed locator can only be retracted.
- Each contract's `regressions:` listings run through `claims_intake.extract_listing` and
  are diffed against `tests/fixtures/location_w2/golden/<portal>@<version>.json`.
- Re-bless a reviewed change:
  `python -m tests.location_data.test_contract_fixture_diff --bless`.
- pytest, not a workflow, so it rides `test.yml` on every push — 02 §2.1.8.3 makes it
  permanent CI that does not expire when W2 closes.
- It gates claims, not bytes: prose and comments are free, a `contract_version` bump is not
  (the golden is per version; the superseded one is kept so a retraction can read it).
- Coverage is a subset today — a listing with no frozen body is recorded in the golden as
  `listings_without_a_fixture_body`, so coverage arriving later is itself a reviewed diff.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
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
from location_data.claims_remine_archive import (
    ARCHIVE_READERS,
    ArchivedPayload,
    _licensed_coordinate,
    stamp_archive_claim,
)
from location_data.html_scope import ScopeRegister, scope_html
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parents[2]
_W2 = _ROOT / "tests" / "fixtures" / "location_w2"
_GOLDEN_DIR = _W2 / "golden"
_BODY_DIR = _W2 / "regressions"

BLESS_COMMAND = "python -m tests.location_data.test_contract_fixture_diff --bless"

# The archived arm's fixed clock. `stamp_archive_claim` copies the payload's
# `first_observed_at` onto every claim (06 §6.6 rule 1), so a real timestamp here would put
# wall-clock into the golden and make it re-bless itself on every run.
_ARCHIVE_CLOCK = datetime(2026, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------- the pinned regression register

# 02 §2.7 item 0(b), quoted by BUILD-PLAN-w2a-w2 § W2-5. These ids may not silently leave a
# contract's `regressions:` block; the fixture body behind each one may still be missing.
PINNED_REGRESSIONS: dict[str, tuple[str, ...]] = {
    "sreality": ("520268", "1588965452", "3067969612"),
    "realitymix": ("8375963", "8375983", "8595551"),
    "maxima": ("f60012522", "d40026367", "f60012682"),
    # W2-11 pinned 951845 (the committed archived body, whose neighbour blob is the LARGER
    # one) beside 943671, so the two listings that name mmreality's subject-selection defect
    # cannot leave the contract silently.
    "mmreality": ("943671", "951845"),
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
    """One frozen input row for a regression listing; `name` is its provenance."""
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
    """Captured bodies at `<root>/<portal>/<id>[.variant].json`; W2-6…W2-12's hook."""
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


# ------------------------------------------------- the ARCHIVED arm of the same gate
#
# W1's `extract_listing` reads `listings.raw_json` and SKIPS every DOM reader
# (`ARCHIVE_ONLY_READERS`), so without this the golden could not see a single entry a W2
# portal PR activates: remax@3 turned two entries on and the claim-level diff came out
# EMPTY. A gate that shows "nothing changed" for the one change a PR makes is worse than
# no gate, because it reads as a positive result.
#
# So the archived lane runs here too, over the portal's pinned HTML body, and its claims
# land in the golden under `archived_claims`. The evidence span is projected as a LENGTH,
# not an offset pair: offsets move whenever a fixture is re-captured, which would make
# every re-capture look like an extraction change, while a length that stops matching its
# quote is a real defect.
def archived_html_for(source: str) -> Path | None:
    path = _W2 / f"{source}_detail.html"
    return path if path.exists() else None


def project_archived(read: Any) -> dict[str, Any]:
    claim = read.claim
    return {
        "extractor_id": claim.extractor_id,
        "claim_type": claim.claim_type,
        "value_text": claim.value_text,
        "value_geom_wkt": claim.value_geom_wkt,
        "surface": claim.surface,
        "page_kind": claim.page_kind,
        "licence_class": claim.licence_class,
        "blur_evidence": claim.blur_evidence,
        "subject_scoped": claim.subject_scoped,
        "position_branch": read.position_branch,
        "evidence_quote": claim.evidence_quote,
        "evidence_span_len": (
            None if claim.span_start is None or claim.span_end is None
            else claim.span_end - claim.span_start),
    }


def score_archived(contract: contracts.PortalContract) -> list[dict[str, Any]]:
    """Every executable DOM entry of this contract, run over the pinned detail body."""
    path = archived_html_for(contract.source)
    entries = [e for e in fx.entries_for(contract.source)
               if e.reader in ARCHIVE_READERS and e.page_kind == "detail"]
    if path is None or not entries:
        return []
    register = ScopeRegister.from_zones(contract.source, contract.exclusion_zones)
    document = scope_html(path.read_bytes(), register=register)
    payload = ArchivedPayload(
        id=1, source=contract.source, source_id_native="fixture", page_kind="detail",
        payload_sha256="0" * 64, first_observed_at=_ARCHIVE_CLOCK,
        body=path.read_bytes())
    row = fx.listing(contract.source, {}, native="fixture")
    out: list[dict[str, Any]] = []
    for entry in sorted(entries, key=lambda e: e.entry_id):
        for read in ARCHIVE_READERS[entry.reader](entry, row, payload, document):
            stamped = stamp_archive_claim(read.claim, payload,
                                          scope_version=document.scope_version)
            # The C6 licence ladder, applied exactly as the real lane applies it. Without
            # this the gate would show a green claim for a coordinate the lane REFUSES —
            # an entry id absent from ARCHIVED_COORDINATE_RULES, or one vetoed by the Mapy
            # inventory — which is a false safety signal on the licence rail specifically,
            # the one place a wrong answer is a legal problem rather than a data problem.
            if stamped.claim_type == "coordinate":
                stamped, reason = _licensed_coordinate(
                    stamped, row, entry, read.position_branch)
                if stamped is None:
                    out.append({"extractor_id": entry.entry_id, "refused": reason})
                    continue
            out.append(project_archived(replace(read, claim=stamped)))
    return out


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
        "archived_claims": score_archived(contract),
    }


def golden_path(source: str, version: int) -> Path:
    return _GOLDEN_DIR / f"{source}@{version}.json"


def previous_golden(source: str, version: int) -> Path | None:
    found = ((int(p.stem.split("@")[1]), p)
             for p in _GOLDEN_DIR.glob(f"{source}@*.json"))
    older = [pair for pair in found if pair[0] < version]
    return max(older, key=lambda pair: pair[0])[1] if older else None


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


def _grouped(items: list[dict[str, Any]],
             fields: tuple[str, ...]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(tuple(item.get(f) for f in fields), []).append(item)
    return grouped


def _label(kind: str, identity: tuple[Any, ...], ordinal: int, group_size: int) -> str:
    suffix = f" #{ordinal + 1}" if group_size > 1 else ""
    return f"{kind} {' / '.join(str(p) for p in identity)}{suffix}"


def _short(value: Any) -> str:
    text = (repr(value) if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False))
    return text if len(text) <= 88 else text[:85] + "…"


def _diff_group(kind: str, identity: tuple[Any, ...], olds: list[dict[str, Any]],
                news: list[dict[str, Any]], fields: tuple[str, ...]) -> list[str]:
    """One identity's items. Byte-identical items are matched by CONTENT before anything is
    paired positionally: an entry with `cardinality: many` emits several claims under one
    extractor_id and claim_type (sreality's 13 `sr.idx.poi_distance` values, told apart only
    by target_text/distance_m — inert until a W2 reader wires it up), and pairing those by
    position alone would report a harmless reorder as N field changes and force a needless
    re-bless."""
    rest_old, rest_new = list(olds), list(news)
    for item in list(rest_new):
        if item in rest_old:
            rest_old.remove(item)
            rest_new.remove(item)

    if not rest_old and not rest_new:
        return ([] if olds == news else
                [f"    ~ reordered {_label(kind, identity, 0, 1)}: "
                 f"{len(olds)} identical items in a different order"])

    lines: list[str] = []
    for ordinal, (was, now) in enumerate(zip(rest_old, rest_new)):
        changed = [(k, was.get(k), now[k]) for k in now if was.get(k) != now[k]]
        changed += [(k, v, None) for k, v in was.items() if k not in now]
        if changed:
            lines.append(f"    ~ changed  {_label(kind, identity, ordinal, len(rest_new))}")
            lines.extend(f"                  {k}: {_short(o)} -> {_short(n)}"
                         for k, o, n in changed)
    for ordinal, item in enumerate(rest_old[len(rest_new):], start=len(rest_new)):
        lines.append(f"    - removed  {_label(kind, identity, ordinal, len(rest_old))}")
        lines.extend(f"                  {k} = {_short(v)}"
                     for k, v in item.items() if k not in fields and v is not None)
    for ordinal, item in enumerate(rest_new[len(rest_old):], start=len(rest_old)):
        lines.append(f"    + added    {_label(kind, identity, ordinal, len(rest_new))}")
        lines.extend(f"                  {k} = {_short(v)}"
                     for k, v in item.items() if k not in fields and v is not None)
    return lines


def _diff_section(kind: str, golden: list[dict[str, Any]], actual: list[dict[str, Any]],
                  *fields: str) -> list[str]:
    old, new = _grouped(golden, fields), _grouped(actual, fields)
    lines: list[str] = []
    for identity in list(old) + [i for i in new if i not in old]:
        lines += _diff_group(kind, identity, old.get(identity, []),
                             new.get(identity, []), fields)
    # Emission order is contract entry order, and 02 §2.1.8 forbids reordering entries (ids
    # are never reused). Reordering whole groups changes no group, so it is checked here.
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

    # The ARCHIVED arm. Compared explicitly and by the SAME `_diff_section` machinery, so
    # a DOM entry that changes what it claims prints a claim-level diff exactly like a
    # payload entry does. Without this the key would sit in the golden and never be read —
    # which is what it did when first added, and a gate nobody compares is decoration.
    lines += _diff_section("archived claim",
                           golden.get("archived_claims", []),
                           actual.get("archived_claims", []),
                           "extractor_id", "claim_type")

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


def _poi_golden(pois: list[tuple[str, int]]) -> dict[str, Any]:
    return {
        "portal": "sreality", "contract_version": 1, "regression_listing_ids": [],
        "listings_without_a_fixture_body": [],
        "fixtures": [{
            "listing": "520268", "body": "synthetic", "row": {}, "oversized": 0,
            "absences": [], "enrichment": [],
            "claims": [{"extractor_id": "sr.idx.poi_distance", "claim_type": "poi_distance",
                        "target_text": name, "distance_m": metres}
                       for name, metres in pois],
        }],
    }


def test_a_cardinality_many_reorder_is_not_read_as_a_field_change() -> None:
    """`sr.idx.poi_distance` emits 13 claims under ONE extractor_id and claim_type, told
    apart only by target_text/distance_m. Positional pairing would render a reorder as N
    changed fields and force a re-bless that reviews nothing. Inert until W2 wires the
    reader — the shape is asserted now so it is not rediscovered the hard way."""
    pois = [("Park Riegrovy sady", 220), ("Náměstí Míru", 450), ("Vinohradská", 90)]

    assert diff_golden(_poi_golden(pois), _poi_golden(pois)) == []

    rendered = "\n".join(diff_golden(_poi_golden(pois), _poi_golden(pois[::-1])))
    assert "changed" not in rendered and "removed" not in rendered
    assert "~ reordered claim sr.idx.poi_distance / poi_distance" in rendered

    # …and a real edit inside a reordered group is still caught, per item.
    moved_and_edited = [pois[2], pois[1], ("Park Riegrovy sady", 999)]
    rendered = "\n".join(diff_golden(_poi_golden(pois), _poi_golden(moved_and_edited)))
    assert "~ changed  claim sr.idx.poi_distance / poi_distance" in rendered
    assert "distance_m: 220 -> 999" in rendered


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
