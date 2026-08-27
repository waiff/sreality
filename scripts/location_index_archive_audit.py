"""Is index-page archiving actually working? — the W2a gate (c) readout (02 §2.3.2 P2).

Three axes per portal, combined into one verdict, because "is it wired" is not a
yes/no once a call site can exist and still not fire:

  * **CONTRACT** — `page_kind: index` claim entries, plus whether `fetch.surfaces`
    declares an index surface and with `archive: true` or `false`. A declaration
    with no code behind it is a gap; `archive: false` is a decision, not a gap.
  * **CODE** — `wired` / `gated` / `absent`, three states and not two, decided by
    the archive call's REACHABILITY past the client-side freshness skip
    (`db.fresh_index_page_keys`) rather than by the token appearing in the file.
    `gated` = the skip wraps the call or returns before it, so it also suppresses
    the W2a-2 dual-write for a page that genuinely changed inside the 22 h window.
    `gated` is owed a code fix and `absent` a build; collapsing them into "not
    wired" hides which. Reachability is what lets the eventual P2 fix be seen:
    hoisting the append above the guard reads `wired` with the guard still there.
  * **DATA** — `portal_raw_pages` index rows AND the `portal_raw_payloads` rows
    they should be becoming. Judged on FRESHNESS, never `count(*) > 0`: index
    archiving was switched off in early June 2026, so a portal can hold thousands
    of index rows and archive nothing today. Staging fresh while payloads went
    stale is a STALLED archive — `append_payload_if_enabled` swallows its own
    failures, so this is the only place that failure is visible.

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

# BOTH archive call shapes. `upsert_portal_raw_page` stages a body and appends via
# the chokepoint; `append_payload_if_enabled` is the direct form the two portals
# that stage no body already use for their detail bodies (sreality's estate JSON,
# bezrealitky's advert), and the form a bezrealitky index archiver would have to
# take — its GraphQL index response has no HTML to stage. Looking for only the
# first would call a genuinely wired portal `absent`.
ARCHIVE_CALLS = frozenset({"upsert_portal_raw_page", "append_payload_if_enabled"})

# The helper that builds the skip set, and the substring that names a variable
# carrying one (`fresh`, `fresh_keys`, `fresh_index_page_keys`) — ceskereality
# loads the set in `walk_category` and passes it into `_walk_slice`, so the name
# is what travels, not the call.
FRESHNESS_SKIP_TOKEN = "fresh_index_page_keys"
FRESHNESS_NAME_HINT = "fresh"

# Statements that abandon the current artefact. An `if <freshness>: <one of these>`
# BEFORE the archive call suppresses it exactly as wrapping the call in the
# inverse test does, so both shapes have to count as the same gate.
_SKIP_STATEMENTS = (ast.Return, ast.Continue, ast.Break)

# An index row older than this says the archiver is not running, whatever count(*)
# says. Two refresh windows: one missed walk is jitter, two is a dead archiver.
STALE_AFTER_HOURS = 2.0 * db.INDEX_ARCHIVE_REFRESH_HOURS

VERDICT_YES = "YES — index bodies would accumulate"
VERDICT_PARTIAL = "PARTIAL — the freshness skip drops bodies that changed inside the window"
VERDICT_NO_CALL_SITE = "NO — contract wants index claims, no call site exists"
VERDICT_DECLARED_UNBUILT = "NO — fetch config declares archive: true, no call site exists"
VERDICT_NOT_ASKED = "n/a — no index contract entry and no declared index surface"
VERDICT_DECLARED_OFF = "n/a — fetch config declares archive: false (intentional)"
VERDICT_STALLED = "NO — the archive has STALLED: staging is fresh, payload rows are not"

DRIFT = "DRIFT: registry says {declared}, the module reads {observed}"
STALE = "STALE: newest index row is {hours:.0f} h old (> {limit:.0f} h)"
NO_ROWS = "NO ROWS: portal_raw_pages holds no index page for this portal"
UNEXPECTED_ROWS = (
    "UNEXPECTED: index rows are still landing with no call site found — "
    "a writer this audit does not know about"
)
PAYLOAD_STALLED = (
    "STALLED: portal_raw_payloads gained no index row in {hours:.0f} h (> {limit:.0f} h) "
    "while portal_raw_pages kept landing them — the append is failing silently"
)
PAYLOAD_UNVERIFIED = (
    "UNVERIFIED: no index payload row yet — expected while payload_index_archive "
    "is off, and the reason this row's verdict is a projection"
)


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
        call_site="scraper/main.py:1365 (_index_page_archiver.archive)",
        gate="main.py:1362 `if key in fresh:` returning on the next line — the set is "
             "db.fresh_index_page_keys(hours=INDEX_ARCHIVE_REFRESH_HOURS), read before the upsert",
        intentional_skips=(
            "main.py:912 `def probe_category(` fetches index pages via fetch_index_page and "
            "never attaches the on_page archiver — discovery only, by design",
        ),
    ),
    "remax": CallSite(
        module="remax_main.py",
        state=STATE_GATED,
        call_site="scraper/remax_main.py:183 (_walk_agenda)",
        gate="remax_main.py:181 `if archive_ok and key not in fresh:` — the set is "
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
        intentional_skips=(
            "ceskereality_main.py:177 `if conn is not None and archive_week is not None:` — "
            "a dry run has no connection to stage into and no week to key by",
            "ceskereality_main.py:375 `client.fetch_search(url)` in probe_category fetches "
            "index pages off the /nejnovejsi/ sort slug and never archives — discovery only, "
            "by design, exactly as sreality's probe is",
        ),
    ),
    "bazos": CallSite(module="bazos_main.py", state=STATE_ABSENT),
    "bezrealitky": CallSite(module="bezrealitky_main.py", state=STATE_ABSENT),
    "idnes": CallSite(module="idnes_main.py", state=STATE_ABSENT),
    "maxima": CallSite(module="maxima_main.py", state=STATE_ABSENT),
    "mmreality": CallSite(module="mmreality_main.py", state=STATE_ABSENT),
    "realitymix": CallSite(module="realitymix_main.py", state=STATE_ABSENT),
}


@dataclass(frozen=True)
class ArchiveCall:
    """One `page_kind='index'` archive call found in a module."""

    call: str
    lineno: int
    freshness_guarded: bool


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


def _is_index_archive_call(node: ast.AST) -> bool:
    """An archive call whose `page_kind` keyword is the literal 'index'.

    * AST, not a regex: each call site spans five lines, so a proximity match on
      the tokens would also fire on a detail call above an unrelated 'index'.
    """
    if not isinstance(node, ast.Call) or _called_name(node) not in ARCHIVE_CALLS:
        return False
    return any(
        kw.arg == "page_kind"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value == INDEX
        for kw in node.keywords
    )


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _references_freshness(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and FRESHNESS_NAME_HINT in sub.id.lower():
            return True
        if isinstance(sub, ast.Attribute) and FRESHNESS_NAME_HINT in sub.attr.lower():
            return True
    return False


def _skips(node: ast.If) -> bool:
    return any(isinstance(sub, _SKIP_STATEMENTS) for sub in ast.walk(node))


def _enclosing_blocks(
    node: ast.AST, parents: dict[ast.AST, ast.AST],
) -> list[tuple[list[ast.stmt], int]]:
    """Each statement list this node sits in, innermost first, with its index."""
    blocks: list[tuple[list[ast.stmt], int]] = []
    current = node
    while current in parents:
        parent = parents[current]
        for field in ("body", "orelse", "finalbody"):
            block = getattr(parent, field, None)
            if isinstance(block, list) and any(stmt is current for stmt in block):
                blocks.append((block, [id(s) for s in block].index(id(current))))
                break
        current = parent
    return blocks


def _freshness_guarded(call: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Can the freshness skip stop control reaching this call?

    * WRAPPED — the call sits in the body of an `if` testing the skip set
      (remax's `if archive_ok and key not in fresh`, ceskereality's `_walk_slice`);
    * PRECEDED — an `if <freshness>: return` runs before it in a block the call is
      nested in (sreality's `_index_page_archiver`).

    Position, not presence: hoisting the append ABOVE the guard — the shape the P2
    fix is expected to take — leaves the token in the file and satisfies neither
    clause, so this reads `wired` and the fix is observable. A whole-module
    substring test could never report anything but `gated` again.
    """
    current = call
    while current in parents:
        parent = parents[current]
        if isinstance(parent, ast.If) and _references_freshness(parent.test):
            in_branch = any(stmt is current for stmt in (*parent.body, *parent.orelse))
            if in_branch:
                return True
        current = parent
    for block, index in _enclosing_blocks(call, parents):
        for prior in block[:index]:
            if isinstance(prior, ast.If) and _references_freshness(prior.test) and _skips(prior):
                return True
    return False


def find_index_archive_calls(source: str) -> list[ArchiveCall]:
    """Every index-archiving call in a module, each with its reachability."""
    tree = ast.parse(source)
    parents = _parent_map(tree)
    return [
        ArchiveCall(
            call=_called_name(node) or "?",
            lineno=node.lineno,
            freshness_guarded=_freshness_guarded(node, parents),
        )
        for node in ast.walk(tree)
        if _is_index_archive_call(node)
    ]


def classify_module_source(source: str) -> str:
    """Derive a module's CODE state from its own syntax tree.

    * no index call site at all → `absent`;
    * at least one call the freshness skip cannot stop → `wired`;
    * calls, but every one of them behind the skip → `gated`.
    """
    calls = find_index_archive_calls(source)
    if not calls:
        return STATE_ABSENT
    return STATE_GATED if all(c.freshness_guarded for c in calls) else STATE_WIRED


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
    def payload_age_hours(self) -> float | None:
        if self.payloads is None or self.payloads.newest is None:
            return None
        return (self.now - self.payloads.newest).total_seconds() / 3600.0

    @property
    def payload_stalled(self) -> bool:
        """Staging is landing rows and the ARCHIVE has stopped — the silent failure.

        * `append_payload_if_enabled` swallows every exception by design (warn and
          return), so a broken archive looks exactly like a healthy scrape from
          `portal_raw_pages` alone. This is the only signal that separates them,
          and it needs rows to have landed once: never having archived is the
          expected state while the gate is off, not a stall.
        """
        if self.payloads is None or not self.payloads.payloads:
            return False
        if not self.accumulating:
            return False
        age = self.payload_age_hours
        return age is None or age > STALE_AFTER_HOURS

    @property
    def payload_note(self) -> str | None:
        """What the payload archive says that the staging table cannot."""
        if self.payloads is None or self.state == STATE_ABSENT:
            return None
        if self.payload_stalled:
            return PAYLOAD_STALLED.format(
                hours=self.payload_age_hours or 0.0, limit=STALE_AFTER_HOURS,
            )
        if not self.payloads.payloads:
            return PAYLOAD_UNVERIFIED
        return None

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
        if self.staging is None:
            return None
        if self.state == STATE_ABSENT:
            # Fresh rows with no call site in sight means the static classification
            # missed a writer — the ONE direction this audit cannot self-check, so
            # the data axis is where it has to surface.
            return UNEXPECTED_ROWS if self.accumulating else None
        if not self.staging.pages:
            return NO_ROWS
        age = self.staging_age_hours
        if age is not None and age > STALE_AFTER_HOURS:
            return STALE.format(hours=age, limit=STALE_AFTER_HOURS)
        return None

    @property
    def verdict(self) -> str:
        """Would `portal_raw_payloads` index rows accumulate once the flag is on?

        * a stall outranks the code axis: whatever the call site looks like, rows
          that stopped landing while pages kept staging is the answer to the
          question, and reading PARTIAL over the top of it would be a healthy
          verdict on a broken archive.
        """
        if self.payload_stalled:
            return VERDICT_STALLED
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
# denominator's own rule forbids touching, so nothing here detoasts. It is NOT an
# index-only scan though: `fetched_at` appears in no index on the table
# (portal_raw_pages_key is (source, source_id_native, page_kind)), so the min/max
# force a heap scan and the statement timeout is the only bound on it. That is
# acceptable for an audit run by hand; it would not be for anything routine, and a
# partial index on (source, fetched_at) where page_kind = 'index' is the fix if
# this ever gets a schedule.
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
      database — the half that answers "is this even built";
    * a DB read that fails or times out degrades to that same half rather than
      taking the run down. `_STAGING_INDEX_SQL` is a heap scan bounded only by the
      statement timeout, and the two axes that need no database must not be
      collateral when it trips.
    """
    stamp = now or datetime.datetime.now(datetime.UTC)
    staging: dict[str, StagingRows] = {}
    payloads: dict[str, PayloadRows] = {}
    have_data = False
    if conn is not None:
        try:
            staging = read_staging(conn, statement_timeout_s=statement_timeout_s)
            payloads = read_payloads(conn, statement_timeout_s=statement_timeout_s)
            have_data = True
            LOG.info("INDEX-AUDIT staging sources=%d payload sources=%d",
                     len(staging), len(payloads))
        except Exception as exc:  # noqa: BLE001 - the offline axes must survive it
            LOG.warning("INDEX-AUDIT data axis unavailable, reporting without it: %s", exc)

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
            staging=staging.get(contract.source, StagingRows(0, None, None))
            if have_data else None,
            payloads=payloads.get(contract.source, PayloadRows(0, 0, None))
            if have_data else None,
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
        f"  CODE states: {STATE_WIRED} = every call site is reachable past the freshness skip;",
        f"    {STATE_GATED} = a call site exists but {FRESHNESS_SKIP_TOKEN} either wraps it or",
        "    returns before it, dropping a body that changed inside the window (#1060's KNOWN GAP);",
        f"    {STATE_ABSENT} = no call to {' / '.join(sorted(ARCHIVE_CALLS))}"
        f"(page_kind='{INDEX}')",
        "  the state is REACHABILITY, not the presence of a token: hoisting the append above",
        "    the guard reads wired with the guard still in the file, so the P2 fix is observable",
        f"  a staging row older than {STALE_AFTER_HOURS:.0f} h "
        f"(2 x INDEX_ARCHIVE_REFRESH_HOURS = {db.INDEX_ARCHIVE_REFRESH_HOURS:g} h) is not",
        "    accumulation — index archiving was switched off in early June 2026, so old rows persist",
        "  payload rows are read too: staging fresh while portal_raw_payloads went stale is a",
        "    STALLED archive (the append swallows its own failures), and outranks the code axis",
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
        notes = [
            note for note in (a.drift, a.contract_note, a.data_note, a.payload_note) if note
        ]
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
            "payload_age_hours": a.payload_age_hours,
            "payload_stalled": a.payload_stalled,
            "payload_note": a.payload_note,
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
