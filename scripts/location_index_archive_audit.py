"""Is index-page archiving actually working? — the W2a gate (c) readout (02 §2.3.2 P2).

Three axes per portal, combined into one verdict, because "is it wired" is not a
yes/no once a call site can exist and still not fire:

  * **CONTRACT** — `page_kind: index` claim entries, plus whether `fetch.surfaces`
    declares an index surface and with `archive: true` or `false`. A declaration
    with no code behind it is a gap; `archive: false` is a decision, not a gap.
  * **CODE** — `wired` / `gated` / `absent`, three states and not two. `gated` =
    the call site EXISTS but sits behind the client-side freshness skip
    (`db.fresh_index_page_keys`), which `return`s before `upsert_portal_raw_page`
    and so also suppresses the W2a-2 payload dual-write for a page that genuinely
    changed inside the 22 h window. `gated` is owed a code fix and `absent` is
    owed a build; collapsing them into "not wired" hides which.
  * **DATA** — `portal_raw_pages` index rows and the `portal_raw_payloads` rows
    they would become. Judged on FRESHNESS, never `count(*) > 0`: index archiving
    was switched off in early June 2026, so a portal can hold thousands of index
    rows and archive nothing today.

Scope and safety:

  * measures the freshness gap, does NOT fix it — reworking the skip is the open
    P2 design question the three `KNOWN GAP` comments (PR #1060) point here for;
  * read-only, and neither `portal_raw_pages.html` nor `portal_raw_payloads.body`
    is ever projected — tests/location_data/test_index_archive_audit.py pins both.

Usage:
  python -m scripts.location_index_archive_audit
  python -m scripts.location_index_archive_audit --skip-db     # contract + code only
  python -m scripts.location_index_archive_audit --json
Required (unless --skip-db): SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from location_data import contracts, loader_db
from scraper import db

LOG = logging.getLogger("location_index_archive_audit")

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRAPER_DIR = REPO_ROOT / "scraper"

INDEX = db.INDEX_PAGE_KIND

# The three CODE states. Two of them would let "a call site exists" stand in for
# "a changed body is archived", which is exactly the conflation PR #1060 found.
STATE_WIRED = "wired"
STATE_GATED = "gated"
STATE_ABSENT = "absent"

# The call the classifier looks for, and the token that says a found call site is
# gated. Module-scoped on purpose: ceskereality loads the skip set in
# `walk_category` and passes it down into `_walk_slice`, so an enclosing-function
# check would call that call site `wired`. Module scope errs toward `gated`, which
# is the direction that reports a gap that isn't there rather than hiding one.
ARCHIVE_CALL = "upsert_portal_raw_page"
FRESHNESS_SKIP_TOKEN = "fresh_index_page_keys"

# An index row older than this says the archiver is not running, whatever count(*)
# says. Two refresh windows: one missed walk is jitter, two is a dead archiver.
STALE_AFTER_HOURS = 2.0 * db.INDEX_ARCHIVE_REFRESH_HOURS

VERDICT_YES = "YES — index bodies would accumulate"
VERDICT_PARTIAL = "PARTIAL — the freshness skip drops bodies that changed inside the window"
VERDICT_NO_CALL_SITE = "NO — contract wants index claims, no call site exists"
VERDICT_DECLARED_UNBUILT = "NO — fetch config declares archive: true, no call site exists"
VERDICT_NOT_ASKED = "n/a — no index contract entry and no declared index surface"
VERDICT_DECLARED_OFF = "n/a — fetch config declares archive: false (intentional)"

DRIFT = "DRIFT: registry says {declared}, the module reads {observed}"
STALE = "STALE: newest index row is {hours:.0f} h old (> {limit:.0f} h)"
NO_ROWS = "NO ROWS: portal_raw_pages holds no index page for this portal"


@dataclass(frozen=True)
class CallSite:
    """One portal's index-archiving call site, as read out of the module.

    * `state` is the human-verified classification; `classify_module` re-derives it
      at runtime and the readout marks any disagreement as DRIFT, so the registry
      cannot rot silently against the code it describes.
    * `gate` is the guard that can suppress the archive on a FULL walk — the gap.
    * `intentional_skips` are paths that archive nothing BY DESIGN and are not gaps;
      naming them is what stops the audit reading a deliberate skip as a bug.
    """

    module: str
    state: str
    call_site: str | None = None
    gate: str | None = None
    intentional_skips: tuple[str, ...] = ()

    @property
    def module_path(self) -> Path:
        return SCRAPER_DIR / self.module


# Built by reading each module (PR #1060's three `KNOWN GAP` comments mark the
# gated ones). Every portal is listed, including the six with nothing to say, so a
# new portal has to be classified rather than silently omitted.
INDEX_ARCHIVERS: dict[str, CallSite] = {
    "sreality": CallSite(
        module="main.py",
        state=STATE_GATED,
        call_site="scraper/main.py:1359 (_index_page_archiver.archive)",
        gate="main.py:1356 `if key in fresh: return` — db.fresh_index_page_keys("
             "hours=INDEX_ARCHIVE_REFRESH_HOURS), before the upsert",
        intentional_skips=(
            "probe_category (main.py:907) fetches index pages via fetch_index_page and "
            "never attaches the on_page archiver — discovery only, by design",
        ),
    ),
    "remax": CallSite(
        module="remax_main.py",
        state=STATE_GATED,
        call_site="scraper/remax_main.py:183 (_walk_agenda)",
        gate="remax_main.py:181 `if archive_ok and key not in fresh` — "
             "db.fresh_index_page_keys(hours=INDEX_ARCHIVE_REFRESH_HOURS)",
        intentional_skips=(
            "remax_main.py:144 `archive_ok = conn is not None and not self._max_pages` — "
            "a page-capped walk (the ~3 min delta probe) never archives, so a probe "
            "fetch cannot claim a page's daily slot ahead of the full 6 h walk",
        ),
    ),
    "ceskereality": CallSite(
        module="ceskereality_main.py",
        state=STATE_GATED,
        call_site="scraper/ceskereality_main.py:196 (_walk_slice)",
        gate="ceskereality_main.py:194 `if fresh_keys is None or key not in fresh_keys` — "
             "the skip set is loaded in walk_category and passed down",
    ),
    "bazos": CallSite(module="bazos_main.py", state=STATE_ABSENT),
    "bezrealitky": CallSite(module="bezrealitky_main.py", state=STATE_ABSENT),
    "idnes": CallSite(module="idnes_main.py", state=STATE_ABSENT),
    "maxima": CallSite(module="maxima_main.py", state=STATE_ABSENT),
    "mmreality": CallSite(module="mmreality_main.py", state=STATE_ABSENT),
    "realitymix": CallSite(module="realitymix_main.py", state=STATE_ABSENT),
}


def has_index_archive_call(tree: ast.AST) -> bool:
    """Does this module call `upsert_portal_raw_page` with `page_kind='index'`?

    * AST, not a regex: each call site spans five lines, so a proximity match on
      the two tokens would also fire on a detail call above an unrelated 'index'.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != ARCHIVE_CALL:
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "page_kind"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == INDEX
            ):
                return True
    return False


def classify_module_source(source: str) -> str:
    """Derive a module's CODE state from its own text — the drift check's other half.

    * no index call site at all → `absent` (nothing to gate);
    * a call site in a module that consults the freshness skip set → `gated`;
    * a call site with no skip set in sight → `wired`.
    """
    if not has_index_archive_call(ast.parse(source)):
        return STATE_ABSENT
    return STATE_GATED if FRESHNESS_SKIP_TOKEN in source else STATE_WIRED


def classify_module(path: Path) -> str:
    return classify_module_source(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class ContractFacts:
    """What the portal's contract ASKS for on the index surface."""

    index_entries: int
    declares_index_surface: bool
    declared_archive: bool | None

    @property
    def wants_index(self) -> bool:
        """Does anything in the contract expect an archived index body?"""
        return self.index_entries > 0 or bool(self.declared_archive)


def contract_facts(contract: contracts.PortalContract) -> ContractFacts:
    surfaces = (contract.fetch_config.get("fetch") or {}).get("surfaces") or []
    index_surfaces = [s for s in surfaces if s.get("page_kind") == INDEX]
    declared = index_surfaces[0].get("archive") if index_surfaces else None
    return ContractFacts(
        index_entries=sum(1 for e in contract.entries if e.page_kind == INDEX),
        declares_index_surface=bool(index_surfaces),
        declared_archive=None if declared is None else bool(declared),
    )


@dataclass(frozen=True)
class StagingRows:
    """`portal_raw_pages` index rows for one portal — keys only, never the html."""

    pages: int
    oldest: datetime.datetime | None
    newest: datetime.datetime | None


@dataclass(frozen=True)
class PayloadRows:
    """`portal_raw_payloads` index rows for one portal — keys only, never the body."""

    payloads: int
    artefacts: int
    newest: datetime.datetime | None


@dataclass(frozen=True)
class PortalAudit:
    """One portal across all three axes, plus the verdict they combine into."""

    source: str
    contract: ContractFacts
    site: CallSite
    observed_state: str | None
    staging: StagingRows | None
    payloads: PayloadRows | None
    now: datetime.datetime

    @property
    def state(self) -> str:
        """What the CODE does — the module's own text wins over the registry.

        * the registry carries what a parser cannot (line numbers, the guard in
          prose, which skips are deliberate); the module carries whether the call
          site is there at all, so a drifted registry costs a marker, not a wrong
          verdict.
        """
        return self.observed_state or self.site.state

    @property
    def drift(self) -> str | None:
        if self.observed_state is None or self.observed_state == self.site.state:
            return None
        return DRIFT.format(declared=self.site.state, observed=self.observed_state)

    @property
    def staging_age_hours(self) -> float | None:
        if self.staging is None or self.staging.newest is None:
            return None
        return (self.now - self.staging.newest).total_seconds() / 3600.0

    @property
    def accumulating(self) -> bool | None:
        """Is the staging table gaining index rows NOW, rather than holding old ones?"""
        if self.staging is None:
            return None
        age = self.staging_age_hours
        return age is not None and age <= STALE_AFTER_HOURS

    @property
    def contract_note(self) -> str | None:
        """The contract disagreeing with ITSELF about the index surface.

        * entries with no declared surface = claims mined off a body the fetch
          config never says is archived; a declared surface with no entries = a
          body archived for nothing. Neither is visible from one column.
        """
        if self.contract.index_entries and not self.contract.declares_index_surface:
            return (f"{self.contract.index_entries} index claim entr"
                    f"{'y' if self.contract.index_entries == 1 else 'ies'} but fetch config "
                    "declares no index surface")
        if self.contract.declared_archive and not self.contract.index_entries:
            return "fetch config declares archive: true but no index claim entry reads it"
        return None

    @property
    def data_note(self) -> str | None:
        """Why the DATA axis disagrees with the CODE axis, when it does."""
        if self.staging is None or self.state == STATE_ABSENT:
            return None
        if not self.staging.pages:
            return NO_ROWS
        age = self.staging_age_hours
        if age is not None and age > STALE_AFTER_HOURS:
            return STALE.format(hours=age, limit=STALE_AFTER_HOURS)
        return None

    @property
    def verdict(self) -> str:
        """Would `portal_raw_payloads` index rows accumulate once the flag is on?"""
        if self.state == STATE_WIRED:
            return VERDICT_YES
        if self.state == STATE_GATED:
            return VERDICT_PARTIAL
        if self.contract.index_entries:
            return VERDICT_NO_CALL_SITE
        if self.contract.declared_archive:
            return VERDICT_DECLARED_UNBUILT
        if self.contract.declares_index_surface:
            return VERDICT_DECLARED_OFF
        return VERDICT_NOT_ASKED


# Key columns only. `portal_raw_pages.html` is the 14 GB TOASTed column the W2-0
# denominator's own rule forbids touching — count(*) and the two timestamps are
# served from portal_raw_pages_key and detoast nothing.
_STAGING_INDEX_SQL = """
    SELECT source,
           count(*)        AS pages,
           min(fetched_at) AS oldest,
           max(fetched_at) AS newest
      FROM portal_raw_pages
     WHERE page_kind = 'index'
     GROUP BY 1
     ORDER BY 1
"""

# Same discipline on the archive: never project `body`. `artefacts` strips the
# index archivers' week suffix (…/{offset}/{week}, db.index_archive_week) so one
# page POSITION counts once however many ISO weeks it has been archived over.
_PAYLOAD_INDEX_SQL = """
    SELECT source,
           count(*)                 AS payloads,
           count(DISTINCT regexp_replace(source_id_native, '/[0-9]{4}w[0-9]{2}$', ''))
                                    AS artefacts,
           max(first_observed_at)   AS newest
      FROM portal_raw_payloads
     WHERE page_kind = 'index'
     GROUP BY 1
     ORDER BY 1
"""

STATEMENT_TIMEOUT_ENV = "LOCATION_INDEX_AUDIT_TIMEOUT_S"
DEFAULT_STATEMENT_TIMEOUT_S = 120


def read_staging(
    conn: psycopg.Connection, *, statement_timeout_s: int,
) -> dict[str, StagingRows]:
    with loader_db.bounded(conn, statement_timeout_s) as cur:
        cur.execute(_STAGING_INDEX_SQL)
        rows = cur.fetchall()
    return {
        str(source): StagingRows(pages=int(pages), oldest=oldest, newest=newest)
        for source, pages, oldest, newest in rows
    }


def read_payloads(
    conn: psycopg.Connection, *, statement_timeout_s: int,
) -> dict[str, PayloadRows]:
    with loader_db.bounded(conn, statement_timeout_s) as cur:
        cur.execute(_PAYLOAD_INDEX_SQL)
        rows = cur.fetchall()
    return {
        str(source): PayloadRows(
            payloads=int(payloads), artefacts=int(artefacts), newest=newest,
        )
        for source, payloads, artefacts, newest in rows
    }


def audit(
    conn: psycopg.Connection | None,
    *,
    statement_timeout_s: int = DEFAULT_STATEMENT_TIMEOUT_S,
    now: datetime.datetime | None = None,
) -> list[PortalAudit]:
    """Every portal with a contract, across all three axes.

    * `conn=None` skips the DATA axis so the contract/code half runs with no
      database — the half that answers "is this even built".
    """
    stamp = now or datetime.datetime.now(datetime.UTC)
    staging = read_staging(conn, statement_timeout_s=statement_timeout_s) if conn else {}
    payloads = read_payloads(conn, statement_timeout_s=statement_timeout_s) if conn else {}
    if conn is not None:
        LOG.info("INDEX-AUDIT staging sources=%d payload sources=%d",
                 len(staging), len(payloads))

    audits: list[PortalAudit] = []
    for contract in sorted(contracts.load_all(), key=lambda c: c.source):
        site = INDEX_ARCHIVERS.get(contract.source)
        if site is None:
            LOG.warning("INDEX-AUDIT %s has a contract but no registry entry", contract.source)
            site = CallSite(module=f"{contract.source}_main.py", state=STATE_ABSENT)
        observed: str | None = None
        try:
            observed = classify_module(site.module_path)
        except (OSError, SyntaxError) as exc:
            LOG.warning("INDEX-AUDIT cannot classify %s: %s", site.module, exc)
        audits.append(PortalAudit(
            source=contract.source,
            contract=contract_facts(contract),
            site=site,
            observed_state=observed,
            staging=staging.get(contract.source, StagingRows(0, None, None)) if conn else None,
            payloads=payloads.get(contract.source, PayloadRows(0, 0, None)) if conn else None,
            now=stamp,
        ))
    return audits


# ------------------------------------------------------------------ rendering


def _num(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _stamp(value: datetime.datetime | None) -> str:
    return "—" if value is None else value.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M")


def _yesno(value: bool | None) -> str:
    return "—" if value is None else ("yes" if value else "no")


def _header() -> list[str]:
    return [
        "INDEX ARCHIVE COVERAGE — W2a gate (c), 02 §2.3.2 P2",
        f"  CODE states: {STATE_WIRED} = the call site always runs on a full walk;",
        f"    {STATE_GATED} = the call site exists but the freshness skip "
        f"({FRESHNESS_SKIP_TOKEN}) can return",
        "    before it, dropping a body that changed inside the window (PR #1060's KNOWN GAP);",
        f"    {STATE_ABSENT} = no {ARCHIVE_CALL}(page_kind='{INDEX}') call site in the module",
        f"  a staging row older than {STALE_AFTER_HOURS:.0f} h "
        f"(2 x INDEX_ARCHIVE_REFRESH_HOURS = {db.INDEX_ARCHIVE_REFRESH_HOURS:g} h) is not",
        "    accumulation — index archiving was switched off in early June 2026, so old rows persist",
        "  the CODE state is re-derived from each module at run time; a registry that",
        "    disagrees with its module is marked DRIFT rather than believed",
    ]


def _render_matrix(audits: Sequence[PortalAudit]) -> list[str]:
    lines = [
        "",
        "PER PORTAL — contract (what is asked) x code (what runs) x data (what landed)",
        f"{'source':<14}{'entries':>8}{'declared':>15}{'code':>8}{'staging':>10}"
        f"{'newest':>18}{'accum':>7}{'payloads':>10}  verdict",
    ]
    for a in audits:
        declared = (
            "—" if a.contract.declared_archive is None
            else ("archive:true" if a.contract.declared_archive else "archive:false")
        )
        lines.append(
            f"{a.source:<14}{a.contract.index_entries:>8}{declared:>15}{a.state:>8}"
            f"{_num(a.staging.pages if a.staging else None):>10}"
            f"{_stamp(a.staging.newest if a.staging else None):>18}"
            f"{_yesno(a.accumulating):>7}"
            f"{_num(a.payloads.payloads if a.payloads else None):>10}  {a.verdict}"
        )
    return lines


def _render_markers(audits: Sequence[PortalAudit]) -> list[str]:
    """Everything that needs a sentence rather than a column."""
    lines: list[str] = []
    for a in audits:
        notes = [note for note in (a.drift, a.contract_note, a.data_note) if note]
        if not notes:
            continue
        lines.append(f"  {a.source}: " + "; ".join(notes))
    return ["", "MARKERS", *lines] if lines else []


def _render_call_sites(audits: Sequence[PortalAudit]) -> list[str]:
    """The gated call sites in full, and the paths that skip archiving BY DESIGN.

    * an intentional skip printed beside the gap is what stops the next reader
      filing sreality's `probe_category` or remax's `_max_pages` guard as a defect.
    """
    lines: list[str] = ["", "CALL SITES"]
    for a in audits:
        if a.state == STATE_ABSENT:
            continue
        lines.append(f"  {a.source} [{a.state}] {a.site.call_site or '—'}")
        if a.site.gate:
            lines.append(f"    gate: {a.site.gate}")
        for skip in a.site.intentional_skips:
            lines.append(f"    intentional (NOT a gap): {skip}")
    absent = [a for a in audits if a.state == STATE_ABSENT]
    if absent:
        lines.append("  no call site at all: " + ", ".join(a.source for a in absent))
    return lines


def _render_summary(audits: Sequence[PortalAudit]) -> list[str]:
    gated = [a for a in audits if a.state == STATE_GATED]
    wired = [a for a in audits if a.state == STATE_WIRED]
    owed = [a for a in audits if a.verdict in (VERDICT_NO_CALL_SITE, VERDICT_DECLARED_UNBUILT)]
    lines = [
        "",
        "SUMMARY",
        f"  {len(wired)} portal(s) unconditionally archive the index page",
        f"  {len(gated)} portal(s) have a call site the freshness skip can suppress: "
        + (", ".join(a.source for a in gated) or "—"),
        f"  {len(owed)} portal(s) are asked for an archived index page and have no call "
        "site: " + (", ".join(a.source for a in owed) or "—"),
    ]
    if gated and not wired:
        lines.append(
            "  => flipping payload_index_archive today archives index bodies at MOST once per "
            f"{db.INDEX_ARCHIVE_REFRESH_HOURS:g} h per page position — every intra-window change "
            "is dropped and unrecoverable (an append-on-change archive cannot backfill a body "
            "it never saw). Reworking the skip is the open P2 design question."
        )
    return lines


def render(audits: Sequence[PortalAudit]) -> list[str]:
    lines = list(_header())
    lines.extend(_render_matrix(audits))
    lines.extend(_render_markers(audits))
    lines.extend(_render_call_sites(audits))
    lines.extend(_render_summary(audits))
    return lines


def audit_json(a: PortalAudit) -> dict[str, Any]:
    return {
        "source": a.source,
        "contract": {
            "index_entries": a.contract.index_entries,
            "declares_index_surface": a.contract.declares_index_surface,
            "declared_archive": a.contract.declared_archive,
            "wants_index": a.contract.wants_index,
            "note": a.contract_note,
        },
        "code": {
            "state": a.state,
            "registry_state": a.site.state,
            "observed_state": a.observed_state,
            "drift": a.drift,
            "module": a.site.module,
            "call_site": a.site.call_site,
            "gate": a.site.gate,
            "intentional_skips": list(a.site.intentional_skips),
        },
        "data": {
            "staging_pages": a.staging.pages if a.staging else None,
            "staging_oldest": _iso(a.staging.oldest) if a.staging else None,
            "staging_newest": _iso(a.staging.newest) if a.staging else None,
            "staging_age_hours": a.staging_age_hours,
            "accumulating": a.accumulating,
            "payload_rows": a.payloads.payloads if a.payloads else None,
            "payload_artefacts": a.payloads.artefacts if a.payloads else None,
            "payload_newest": _iso(a.payloads.newest) if a.payloads else None,
            "note": a.data_note,
        },
        "verdict": a.verdict,
    }


def _iso(value: datetime.datetime | None) -> str | None:
    return None if value is None else value.astimezone(datetime.UTC).isoformat()


def to_json(audits: Sequence[PortalAudit]) -> dict[str, Any]:
    return {
        "audited_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "states": [STATE_WIRED, STATE_GATED, STATE_ABSENT],
        "stale_after_hours": STALE_AFTER_HOURS,
        "index_archive_refresh_hours": db.INDEX_ARCHIVE_REFRESH_HOURS,
        "portals": [audit_json(a) for a in audits],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="audit index-page archive coverage")
    parser.add_argument("--json", action="store_true",
                        help="Emit the audit as JSON on stdout instead of a table.")
    parser.add_argument("--skip-db", action="store_true",
                        help="Contract + code axes only; no database needed.")
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

    if args.skip_db:
        audits = audit(None)
    else:
        if not os.environ.get("SUPABASE_DB_URL"):
            print("ERROR: SUPABASE_DB_URL is not set (or pass --skip-db).", file=sys.stderr)
            return 2
        with db.connect() as conn:
            audits = audit(conn, statement_timeout_s=args.statement_timeout)

    if args.json:
        print(json.dumps(to_json(audits), indent=2, default=str))
    else:
        for line in render(audits):
            print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
