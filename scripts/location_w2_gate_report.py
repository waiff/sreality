"""The W2 acceptance gate, per portal, as one readable page.

WHAT IT ANSWERS. For each of the seven HTML portals W2-6…W2-12 activate: how the portal's
FROZEN labelled sample scores on the four W2 floors (street / obec / okres / precision
class), for the SHADOWED contract and for the served projection; how the OLD system scores
on the same labels, from the `legacy_*` snapshot frozen onto each member row at draw time;
how much of the archived corpus the sweep lane has actually reached; and the resulting
shadow / un-shadow verdict. Operator action O10 is reading this and then deciding whether to
run `python -m location_data.contracts --unshadow PORTAL@VERSION`.

THE ARITHMETIC IS NOT RESTATED HERE. `toolkit.location_labels.score_sample` and
`score_shadow_claims` are called unchanged, so the shadow numbers and the served numbers are
the same arithmetic on the same floors (both fold through one `_block`, which is why a
second copy of the percentages would eventually disagree about which denominator is which).
This module reads their blocks and renders them; it never re-derives a percentage.

`toolkit.location_quality.w1v_gate` is deliberately NOT called and NOT parameterized. It
spells 'bezrealitky' into four SQL literals and joins `ruian_address_points.kod_adm` out of
an `address_point_id` claim that only bezrealitky publishes — the other eight portals would
come back as structural zeros wearing a percentage sign. It is a W1v artefact; per-portal W2
gating lives entirely in the two label scorers.

IT ONLY EVER READS, AND IT NEVER NAMES A BODY COLUMN. Every statement below is a SELECT, and
not one of them names a page's bytes — not the payload store's inline column, not its R2 key,
not the old archive's own markup column. The archive is ~14 GB and effectively all of it is
TOASTed out of line or in the bucket, so naming any of the three would pull the whole corpus
back over the network to produce a handful of integers.
tests/location_data/test_w2_gate_report.py pins both properties over the whole file, prose
included (the forbidden spellings are the test's business, not this file's), so the guard
needs no judgement call about which occurrence was "only a comment".

NO LEASE, AND OUTSIDE `location-batch`. The report has to be runnable WHILE the lane it
reports on is in flight — the same argument `location_claims_remine_verify.yml` makes — and
that group is measurably oversubscribed by its own crons. It arms its own statement timeouts
instead. Nothing is persisted either: whether these numbers belong in
`location_metrics_rollup` is still an open question, and a report that quietly seeded an
unseeded table would settle it by accident.

Usage:
  python -m scripts.location_w2_gate_report
  python -m scripts.location_w2_gate_report --source remax
  python -m scripts.location_w2_gate_report --json > w2_gate.json
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg

from location_data import claims_remine_archive, loader_db
from scripts import location_archive_denominator as denominator
from scraper import db
from toolkit import location_labels

LOG = logging.getLogger("location_w2_gate_report")

# NOT named LANE / REMINE_VERSION on purpose. tests/location_data/test_lane_identifiers.py
# scans scripts/location_*.py for module-level constants under those names and requires the
# VALUES to be globally unique — re-declaring the lane's own strings here under its own names
# would read as a second lane claiming the same identity. Imported rather than retyped so the
# report can never drift from the lane whose coverage it reads.
ARCHIVE_LANE = claims_remine_archive.LANE                 # "location_claims_remine_archive"
ARCHIVE_EXTRACTOR_VERSION = claims_remine_archive.REMINE_VERSION
ARCHIVE_SURFACE = claims_remine_archive.ARCHIVE_SURFACE   # "archived_html"

# The seven HTML portals W2-6…W2-12 activate. sreality and bezrealitky are JSON / GraphQL and
# hold no archived HTML body at all (denominator.NO_HTML_ARCHIVE), so a W2 gate row for them
# would be a line of structural zeros dressed as a measurement.
W2_PORTALS: tuple[str, ...] = (
    "remax", "ceskereality", "realitymix", "idnes", "bazos", "mmreality", "maxima",
)

# The draw itself refuses n < 100 (06 6.4.0), so a verdict on fewer labels than that is noise
# wearing a percentage sign.
MIN_LABELLED = 100

# Text claim types the shadow scorecard covers; the fourth floor (precision class) is
# resolver-minted and a shadowed contract has no resolution.
SCORED_CLAIM_TYPES = ("street_name", "obec_name", "okres_name")

# The newest batches per source the coverage block prints. Kept in Python rather than in a
# window function: the table is tiny and the fold stays testable without a database.
_BATCHES_PER_SOURCE = 3

STATEMENT_TIMEOUT_ENV = "LOCATION_W2_GATE_TIMEOUT_S"
DEFAULT_STATEMENT_TIMEOUT_S = 60

VERDICT_NO_SAMPLE = "NO SAMPLE"
VERDICT_SAMPLE_UNLABELLED = "SAMPLE UNLABELLED"
VERDICT_NOT_MINED = "NOT MINED"
VERDICT_SHADOW_PASS = "SHADOW PASS"
VERDICT_SHADOW_FAIL = "SHADOW FAIL"
VERDICT_LIVE_PASS = "LIVE PASS"
VERDICT_LIVE_FAIL = "LIVE FAIL"

# The two verdicts `--fail-on-gate` turns into an exit code. Everything else is UNDECIDABLE —
# no sample, too few labels, nothing mined — and an undecidable portal is not a failing one.
FAILING_VERDICTS = frozenset({VERDICT_SHADOW_FAIL, VERDICT_LIVE_FAIL})

SERVED_FIELDS = ("street", "obec", "okres", "precision_class")
SHADOW_FIELDS = location_labels.SHADOW_SCORED_FLOORS
# The fields whose OLD side exists at all: migration 399 snapshots no granularity, so the old
# system's precision class is structurally unscorable rather than merely missing.
OLD_SCORED_FIELDS = ("street", "obec", "okres")


# ------------------------------------------------------------------ SQL (all SELECT)

# THE POPULATION THE LANE CAN ACTUALLY REACH, and it mirrors
# `claims_remine_archive._PAYLOAD_SCAN_FULL_SQL`'s predicates exactly: the same http_status
# filter, the same latest-body anti-join on `(first_observed_at, id)`, and the same INNER
# join to `listings` on `(source, source_id_native)` — never `p.listing_id`, which is nullable
# and populated by nothing. A yield measured over a different population than the one that was
# swept is a fabricated percentage, so this is pinned substring-by-substring against the
# lane's own scan.
_MINABLE_SQL = """
    SELECT p.source, p.page_kind::text AS page_kind, count(*) AS bodies
    FROM portal_raw_payloads p
    JOIN listings l ON l.source = p.source AND l.source_id_native = p.source_id_native
    WHERE p.source = ANY(%(sources)s)
      AND (p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299)
      AND NOT EXISTS (
          SELECT 1 FROM portal_raw_payloads n
          WHERE n.source = p.source
            AND n.source_id_native = p.source_id_native
            AND n.page_kind = p.page_kind
            AND (n.http_status IS NULL OR n.http_status BETWEEN 200 AND 299)
            AND (n.first_observed_at, n.id) > (p.first_observed_at, p.id))
    GROUP BY 1, 2
"""

# `location_claims_unretracted`, not `location_claims`: a claim the operator has retracted
# must not count toward yield. And not `location_claims_live` either — under the approved
# activation the contract is SHADOWED, so every archived claim is outside `live` by
# construction and the numerator would read zero.
_ARCHIVED_CLAIMS_SQL = """
    SELECT c.source, count(*) AS claims, count(DISTINCT c.listing_id) AS listings
    FROM location_claims_unretracted c
    WHERE c.source = ANY(%(sources)s) AND c.surface = %(surface)s
    GROUP BY 1
"""

_ARCHIVED_CLAIMS_BY_TYPE_SQL = """
    SELECT c.source, c.claim_type::text AS claim_type,
           count(*) AS claims, count(DISTINCT c.listing_id) AS listings
    FROM location_claims_unretracted c
    WHERE c.source = ANY(%(sources)s) AND c.surface = %(surface)s
    GROUP BY 1, 2
"""

_SHADOW_COUNTS_SQL = """
    SELECT c.source,
           count(*) AS claims,
           count(DISTINCT c.listing_id) AS listings,
           count(*) FILTER (WHERE c.surface = %(surface)s) AS archived_claims
    FROM location_claims_shadow c
    WHERE c.source = ANY(%(sources)s)
    GROUP BY 1
"""

_CONTRACTS_SQL = """
    SELECT source, version, is_active, shadow, loaded_at
    FROM portal_contracts
    WHERE source = ANY(%(sources)s) AND (is_active OR shadow)
    ORDER BY source, version
"""

_BATCHES_SQL = """
    SELECT source, scan_mode, outcome, started_at, finished_at,
           row_count, cursor_after_id, coverage_since, note
    FROM location_claim_batches
    WHERE lane = %(lane)s AND source = ANY(%(sources)s)
    ORDER BY source, started_at DESC
"""


# ------------------------------------------------------------------ shapes


@dataclass(frozen=True)
class FloorLine:
    """One field's scorecard row, read straight off `location_labels._block`."""

    field: str
    floor_pct: float
    determinable: int
    new_asserted: int
    new_precision_pct: float | None
    new_yield_pct: float | None
    old_asserted: int | None        # None where the old system is structurally unscorable
    old_precision_pct: float | None
    passes: bool | None             # None when nothing was asserted (undecidable, not a fail)


@dataclass(frozen=True)
class PortalGate:
    source: str
    active_version: int | None
    shadowed_versions: tuple[int, ...]
    sample: dict[str, Any] | None            # id / drawn_at / n / members / labelled
    served: tuple[FloorLine, ...]            # 4 lines, from score_sample
    shadow: tuple[FloorLine, ...]            # 3 lines, from score_shadow_claims
    shadow_gate_pass: bool | None
    minable_bodies: dict[str, int]           # page_kind -> latest OK bodies with a listing
    w2_0_archived_listings: int | None       # None when --skip-denominator
    w2_0_floor_verdict: str | None
    archived_claims: int
    archived_claim_listings: int
    archived_by_claim_type: dict[str, tuple[int, int]]   # type -> (claims, listings)
    shadow_claims: int
    shadow_claim_listings: int
    batches: tuple[dict[str, Any], ...]
    verdict: str
    reasons: tuple[str, ...]


# ------------------------------------------------------------------ the pure fold


def floor_line(block: dict[str, Any], field: str, *, has_old: bool) -> FloorLine:
    """One `_block()` output as a `FloorLine`, re-deriving no percentage.

    `passes` is the block's own `floor_pass`, EXCEPT when nothing was asserted: a field with
    no assertion has no precision, and calling that a failure would read as "the extractor
    got it wrong" when the honest reading is "there is nothing to judge".
    """
    new = block["new"]
    old = block.get("old") if has_old else None
    passes: bool | None = None
    if new["precision_pct"] is not None:
        passes = bool(new["floor_pass"])
    return FloorLine(
        field=field,
        floor_pct=float(block["floor_pct"]),
        determinable=int(block["determinable"]),
        new_asserted=int(new["asserted"]),
        new_precision_pct=new["precision_pct"],
        new_yield_pct=new["yield_pct"],
        old_asserted=None if old is None else int(old["asserted"]),
        old_precision_pct=None if old is None else old["precision_pct"],
        passes=passes,
    )


def served_lines(scorecard: dict[str, Any]) -> tuple[FloorLine, ...]:
    return tuple(
        floor_line(scorecard["data"][field], field,
                   has_old=field in OLD_SCORED_FIELDS)
        for field in SERVED_FIELDS
    )


def shadow_lines(scorecard: dict[str, Any]) -> tuple[FloorLine, ...]:
    return tuple(
        floor_line(scorecard["data"][field], field, has_old=False)
        for field in SHADOW_FIELDS
    )


def regressions(lines: tuple[FloorLine, ...]) -> list[str]:
    """Fields where the NEW system is less precise than the one it replaces.

    Computed independently of the verdict and appended in EVERY branch, because a portal can
    clear all four floors and still be worse than what it replaced — which is exactly the
    reading a gate stated only as pass/fail would hide.
    """
    out: list[str] = []
    for line in lines:
        if line.field not in OLD_SCORED_FIELDS:
            continue
        new, old = line.new_precision_pct, line.old_precision_pct
        if new is None or old is None or new >= old:
            continue
        out.append(
            f"REGRESSION {line.field}: new {new:.2f} % < old {old:.2f} % "
            f"(old = the legacy_* snapshot frozen at draw time)")
    return out


def _failing_reasons(lines: tuple[FloorLine, ...]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if line.passes is None:
            out.append(f"{line.field}: nothing asserted — undecidable, not a pass")
        elif not line.passes:
            out.append(
                f"{line.field} {line.new_precision_pct:.2f} % < {line.floor_pct:.1f} %")
    return out


_DEFERRED_NOTE = (
    "precision_class is DEFERRED under shadow — a granularity is resolver-minted and a "
    "shadowed contract has no resolution; re-run this report after un-shadowing to score "
    "the fourth floor."
)


def decide(
    *,
    source: str,
    sample: dict[str, Any] | None,
    shadowed_versions: tuple[int, ...],
    archived_claims: int,
    served: tuple[FloorLine, ...],
    shadow: tuple[FloorLine, ...],
    shadow_gate_pass: bool | None,
) -> tuple[str, tuple[str, ...]]:
    """The verdict and its reasons. Pure — no connection, first match wins."""
    regression_reasons = regressions(served)

    if sample is None:
        return VERDICT_NO_SAMPLE, tuple([
            "no current `location_labelled_samples` row for this portal. Dispatch "
            "`location_labelled_sample.yml` (operator action O8) BEFORE the sweep — a sample "
            "drawn after a sweep voids the gate, because the sweep moves "
            "`listing_location_current` and the member row snapshots `legacy_*` at draw time.",
            *regression_reasons,
        ])

    labelled = int(sample.get("labelled") or 0)
    members = int(sample.get("members") or 0)
    if labelled < MIN_LABELLED:
        return VERDICT_SAMPLE_UNLABELLED, tuple([
            f"{labelled} of {members} labelled; the gate needs at least {MIN_LABELLED}.",
            *regression_reasons,
        ])

    if archived_claims == 0:
        return VERDICT_NOT_MINED, tuple([
            "no `location_claims` row with surface='archived_html' for this portal — either "
            "the sweep has not run, or its active contract names no reader in "
            "ARCHIVE_READERS.",
            *regression_reasons,
        ])

    if shadowed_versions:
        blend: list[str] = []
        if len(shadowed_versions) > 1:
            blend.append(
                f"`score_shadow_claims` keys on SOURCE, not on contract version — these "
                f"numbers are a blend of {len(shadowed_versions)} shadowed versions.")
        if shadow_gate_pass is True:
            return VERDICT_SHADOW_PASS, tuple([
                f"python -m location_data.contracts --unshadow "
                f"{source}@{max(shadowed_versions)}",
                _DEFERRED_NOTE, *blend, *regression_reasons,
            ])
        return VERDICT_SHADOW_FAIL, tuple([
            *_failing_reasons(shadow), _DEFERRED_NOTE, *blend, *regression_reasons,
        ])

    if all(line.passes is True for line in served):
        return VERDICT_LIVE_PASS, tuple(regression_reasons)
    return VERDICT_LIVE_FAIL, tuple([*_failing_reasons(served), *regression_reasons])


def newest_batches(rows: list[dict[str, Any]], source: str,
                   *, keep: int = _BATCHES_PER_SOURCE) -> tuple[dict[str, Any], ...]:
    """The newest `keep` batch rows for one source, in Python.

    `_BATCHES_SQL` already orders by `started_at DESC`; slicing here rather than with a
    window function keeps the fold testable without a database.
    """
    return tuple(row for row in rows if row["source"] == source)[:keep]


# ------------------------------------------------------------------ database reads


def _rows(conn: psycopg.Connection, sql: str, params: dict[str, Any],
          columns: tuple[str, ...], *, statement_timeout_s: int) -> list[dict[str, Any]]:
    with loader_db.bounded(conn, statement_timeout_s) as cur:
        cur.execute(sql, params)
        fetched = cur.fetchall()
    return [dict(zip(columns, row)) for row in fetched]


def gather(
    conn: psycopg.Connection,
    *,
    sources: tuple[str, ...],
    statement_timeout_s: int,
    skip_denominator: bool,
) -> list[PortalGate]:
    """Every portal's gate row. One pass of the corpus-wide statements, then per portal."""
    params: dict[str, Any] = {"sources": list(sources)}
    surface_params: dict[str, Any] = {"sources": list(sources), "surface": ARCHIVE_SURFACE}

    minable = _rows(conn, _MINABLE_SQL, params, ("source", "page_kind", "bodies"),
                    statement_timeout_s=statement_timeout_s)
    archived = _rows(conn, _ARCHIVED_CLAIMS_SQL, surface_params,
                     ("source", "claims", "listings"),
                     statement_timeout_s=statement_timeout_s)
    by_type = _rows(conn, _ARCHIVED_CLAIMS_BY_TYPE_SQL, surface_params,
                    ("source", "claim_type", "claims", "listings"),
                    statement_timeout_s=statement_timeout_s)
    shadow_counts = _rows(conn, _SHADOW_COUNTS_SQL, surface_params,
                          ("source", "claims", "listings", "archived_claims"),
                          statement_timeout_s=statement_timeout_s)
    contracts = _rows(conn, _CONTRACTS_SQL, params,
                      ("source", "version", "is_active", "shadow", "loaded_at"),
                      statement_timeout_s=statement_timeout_s)
    batches = _rows(conn, _BATCHES_SQL,
                    {"lane": ARCHIVE_LANE, "sources": list(sources)},
                    ("source", "scan_mode", "outcome", "started_at", "finished_at",
                     "row_count", "cursor_after_id", "coverage_since", "note"),
                    statement_timeout_s=statement_timeout_s)

    archived_listings: dict[str, int] = {}
    floor_verdicts: dict[str, str] = {}
    if not skip_denominator:
        measurement = denominator.measure(
            conn, timezone_name=denominator.FLOOR_TIMEZONE,
            statement_timeout_s=statement_timeout_s)
        for report in measurement.reports:
            if report.page_kind == denominator.DETAIL and report.source in sources:
                archived_listings[report.source] = denominator.archived_listings(report)
        for check in denominator.floor_checks(measurement.reports):
            floor_verdicts[check.source] = check.verdict

    gates: list[PortalGate] = []
    for source in sources:
        versions = [row for row in contracts if row["source"] == source]
        active = next((int(r["version"]) for r in versions if r["is_active"]), None)
        shadowed = tuple(sorted(int(r["version"]) for r in versions if r["shadow"]))

        sample = location_labels.current_sample(conn, source)
        served: tuple[FloorLine, ...] = ()
        shadow: tuple[FloorLine, ...] = ()
        shadow_gate_pass: bool | None = None
        if sample is not None:
            served = served_lines(location_labels.score_sample(conn, source))
            shadow_card = location_labels.score_shadow_claims(conn, source)
            shadow = shadow_lines(shadow_card)
            shadow_gate_pass = shadow_card["data"]["gate_pass"]

        claim_row = next((r for r in archived if r["source"] == source), None)
        shadow_row = next((r for r in shadow_counts if r["source"] == source), None)
        verdict, reasons = decide(
            source=source, sample=sample, shadowed_versions=shadowed,
            archived_claims=int((claim_row or {}).get("claims") or 0),
            served=served, shadow=shadow, shadow_gate_pass=shadow_gate_pass)

        gates.append(PortalGate(
            source=source,
            active_version=active,
            shadowed_versions=shadowed,
            sample=sample,
            served=served,
            shadow=shadow,
            shadow_gate_pass=shadow_gate_pass,
            minable_bodies={r["page_kind"]: int(r["bodies"])
                            for r in minable if r["source"] == source},
            w2_0_archived_listings=archived_listings.get(source),
            w2_0_floor_verdict=floor_verdicts.get(source),
            archived_claims=int((claim_row or {}).get("claims") or 0),
            archived_claim_listings=int((claim_row or {}).get("listings") or 0),
            archived_by_claim_type={
                r["claim_type"]: (int(r["claims"]), int(r["listings"]))
                for r in by_type if r["source"] == source
            },
            shadow_claims=int((shadow_row or {}).get("claims") or 0),
            shadow_claim_listings=int((shadow_row or {}).get("listings") or 0),
            batches=newest_batches(batches, source),
            verdict=verdict,
            reasons=reasons,
        ))
    return gates


# ------------------------------------------------------------------ rendering


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f} %"


def _num(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def _share(part: int, whole: int) -> str:
    return "-" if not whole else f"{100.0 * part / whole:.1f} % of {whole:,}"


_LABEL_WIDTH = 54


def _row(label: str, value: str, suffix: str = "") -> str:
    """One label/number line, so the two denominators land in the same column."""
    return f"    {label:<{_LABEL_WIDTH}}{value:>10}{'   ' + suffix if suffix else ''}"


def header_lines() -> list[str]:
    """The three omissions, printed so they cannot be mistaken for measurements."""
    floors = "  ".join(
        f"{field} >= {location_labels.FLOORS[field]:.1f} %" for field in SERVED_FIELDS)
    return [
        f"  floors: {floors}",
        "          (toolkit.location_labels.FLOORS, uniform — no per-source override ships "
        "in W2-13)",
        "  house number: NOT SCORED — score_sample computes no house-number counters; the "
        "NEW side is",
        "          three columns (cp / co / evidencni) against one free-text label",
        "  old precision class: n/a — migration 399 snapshots no granularity and no "
        "coordinate",
    ]


def _sample_line(gate: PortalGate) -> str:
    version = f"contract v{gate.active_version}" if gate.active_version else "contract none"
    shadowed = (f"   (SHADOWED: {', '.join(str(v) for v in gate.shadowed_versions)})"
                if gate.shadowed_versions else "")
    if gate.sample is None:
        return f"{gate.source}   {version}{shadowed}   no frozen sample"
    drawn = str(gate.sample.get("drawn_at") or "?")[:10]
    return (f"{gate.source}   {version}{shadowed}   sample #{gate.sample.get('id')} "
            f"drawn {drawn}  {gate.sample.get('members')} members  "
            f"{gate.sample.get('labelled')} labelled")


def _denominator_lines(gate: PortalGate) -> list[str]:
    detail = gate.minable_bodies.get(denominator.DETAIL, 0)
    kinds = "  ".join(f"{kind} {count:,}"
                      for kind, count in sorted(gate.minable_bodies.items())) or "none"
    if gate.w2_0_archived_listings is None:
        cited = "    W2-0 archived detail listings: not measured (--skip-denominator)"
    else:
        cited = _row("W2-0 archived detail listings (portal_raw_pages)",
                     _num(gate.w2_0_archived_listings),
                     f"floor: {gate.w2_0_floor_verdict or 'n/a'}")
    return [
        "  DENOMINATOR",
        cited,
        _row("payload store, latest OK body with a listing", _num(detail), kinds),
        "    (two tables: the payload store was backfilled from portal_raw_pages and "
        "dual-written since)",
    ]


def _coverage_lines(gate: PortalGate) -> list[str]:
    detail = gate.minable_bodies.get(denominator.DETAIL, 0)
    lines = [
        "  ARCHIVE LANE COVERAGE",
        _row("listings with >= 1 archived_html claim",
             _num(gate.archived_claim_listings),
             _share(gate.archived_claim_listings, detail)),
        _row("archived claims (unretracted)", _num(gate.archived_claims)),
    ]
    for claim_type, (claims, listings) in sorted(gate.archived_by_claim_type.items()):
        lines.append(f"      {claim_type:<24}{claims:>12,} claims / {listings:,} listings")
    lines.append(_row("location_claims_shadow", _num(gate.shadow_claims),
                      f"claims / {_num(gate.shadow_claim_listings)} listings"))
    if not gate.batches:
        lines.append("    batches   none for this lane and source")
    for batch in gate.batches:
        started = str(batch.get("started_at") or "?")[:10]
        lines.append(
            f"    batch     {started} {batch.get('scan_mode')} {batch.get('outcome')}"
            f"  rows={_num(batch.get('row_count'))}  note={batch.get('note')}")
    return lines


def _scorecard_lines(gate: PortalGate) -> list[str]:
    lines: list[str] = []
    shadowed = ", ".join(str(v) for v in gate.shadowed_versions) or "none"
    lines.append(f"  SHADOW SCORECARD (location_claims_shadow; shadowed versions: "
                 f"{shadowed})")
    if not gate.shadow:
        lines.append("    (not scored — the portal has no frozen labelled sample)")
    else:
        lines.append("    field             determinable  asserted  precision    yield"
                     "    floor   verdict")
        for line in gate.shadow:
            verdict = "PASS" if line.passes else ("UNDECIDABLE" if line.passes is None
                                                  else "FAIL")
            lines.append(
                f"    {line.field:<18}{line.determinable:>12}{line.new_asserted:>10}"
                f"{_pct(line.new_precision_pct):>11}{_pct(line.new_yield_pct):>9}"
                f"{line.floor_pct:>9.1f}   {verdict}")
        lines.append(
            f"    {'precision_class':<18}{'-':>12}{'-':>10}{'-':>11}{'-':>9}"
            f"{location_labels.FLOORS['precision_class']:>9.1f}   DEFERRED")

    lines.append("  SERVED SCORECARD (listing_location_current) vs OLD (legacy_* frozen at "
                 "draw time)")
    if not gate.served:
        lines.append("    (not scored — the portal has no frozen labelled sample)")
    else:
        lines.append("    field             determinable  new asserted  new precision  "
                     "old asserted  old precision")
        for line in gate.served:
            lines.append(
                f"    {line.field:<18}{line.determinable:>12}{line.new_asserted:>14}"
                f"{_pct(line.new_precision_pct):>15}{_num(line.old_asserted):>14}"
                f"{_pct(line.old_precision_pct):>15}")
    return lines


def render(gates: list[PortalGate], *, generated_at: str) -> list[str]:
    lines = [f"W2 PER-PORTAL GATE REPORT — {generated_at}", *header_lines(), ""]
    for gate in gates:
        lines.append(_sample_line(gate))
        lines.extend(_denominator_lines(gate))
        lines.extend(_coverage_lines(gate))
        lines.extend(_scorecard_lines(gate))
        lines.append(f"  VERDICT  {gate.verdict}")
        for reason in gate.reasons:
            lines.append(f"    -> {reason}")
        lines.append("")
    return lines


def _line_json(line: FloorLine) -> dict[str, Any]:
    return {
        "field": line.field, "floor_pct": line.floor_pct,
        "determinable": line.determinable, "new_asserted": line.new_asserted,
        "new_precision_pct": line.new_precision_pct,
        "new_yield_pct": line.new_yield_pct,
        "old_asserted": line.old_asserted,
        "old_precision_pct": line.old_precision_pct,
        "passes": line.passes,
    }


def to_json(gates: list[PortalGate], *, generated_at: str) -> dict[str, Any]:
    return {
        "data": {
            "generated_at": generated_at,
            "floors": dict(location_labels.FLOORS),
            "house_number_scored": False,
            "old_precision_class_scored": False,
            "portals": [
                {
                    "source": gate.source,
                    "active_version": gate.active_version,
                    "shadowed_versions": list(gate.shadowed_versions),
                    "sample": gate.sample,
                    "served": [_line_json(line) for line in gate.served],
                    "shadow": [_line_json(line) for line in gate.shadow],
                    "shadow_gate_pass": gate.shadow_gate_pass,
                    "minable_bodies": gate.minable_bodies,
                    "w2_0_archived_listings": gate.w2_0_archived_listings,
                    "w2_0_floor_verdict": gate.w2_0_floor_verdict,
                    "archived_claims": gate.archived_claims,
                    "archived_claim_listings": gate.archived_claim_listings,
                    "archived_by_claim_type": {
                        name: {"claims": claims, "listings": listings}
                        for name, (claims, listings) in gate.archived_by_claim_type.items()
                    },
                    "shadow_claims": gate.shadow_claims,
                    "shadow_claim_listings": gate.shadow_claim_listings,
                    "batches": [dict(batch) for batch in gate.batches],
                    "verdict": gate.verdict,
                    "reasons": list(gate.reasons),
                }
                for gate in gates
            ],
        },
        "metadata": {
            "tool": "location_w2_gate_report",
            "reads": [
                "location_labelled_samples", "location_labelled_sample_members",
                "listing_location_current", "location_claims_unretracted",
                "location_claims_shadow", "portal_contracts", "portal_raw_payloads",
                "location_claim_batches",
            ],
            "writes": [],
            "lane": ARCHIVE_LANE,
            "extractor_version": ARCHIVE_EXTRACTOR_VERSION,
        },
    }


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The W2 acceptance gate, per portal.")
    parser.add_argument("--source", choices=W2_PORTALS, default=None,
                        help="One portal (default: all seven W2 HTML portals).")
    parser.add_argument("--json", action="store_true",
                        help="Emit the report as JSON on stdout instead of a table.")
    parser.add_argument("--skip-denominator", action="store_true",
                        help="Skip the W2-0 archived denominator scan; the report then says "
                             "the citation is missing rather than omitting it.")
    parser.add_argument("--fail-on-gate", action="store_true",
                        help="Exit 1 when any scored portal fails its floors.")
    parser.add_argument(
        "--statement-timeout", type=int,
        default=loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S),
        help=f"Per-statement timeout in seconds (${STATEMENT_TIMEOUT_ENV}).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    sources = (args.source,) if args.source else W2_PORTALS
    generated_at = datetime.now(timezone.utc).isoformat()

    with db.connect() as conn:
        gates = gather(conn, sources=sources,
                       statement_timeout_s=args.statement_timeout,
                       skip_denominator=args.skip_denominator)

    if args.json:
        print(json.dumps(to_json(gates, generated_at=generated_at), default=str, indent=2))
    else:
        print("\n".join(render(gates, generated_at=generated_at)))

    failing = [gate.source for gate in gates if gate.verdict in FAILING_VERDICTS]
    if failing and args.fail_on_gate:
        LOG.error("W2 GATE failing floors for %s", ", ".join(failing))
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
