"""Reconcile `listings.source_url` for sreality from NARROW typed columns (never raw_json).

sreality was the only portal whose page URL was not a stored fact: the parser never emitted
it, and the SPA rebuilt one from display labels that disagree with sreality's own vocabulary
(docs/design/portal-listing-url.md). W0 made `scraper.parser.parse_listing` emit it for every
fresh detail fetch. This job fills the ~232k rows written before that, and doubles as the
standing drift detector (`--report-check`): re-derive every sreality row and count where the
stored URL disagrees with today's derivation.

It reads NO `raw_json`. Every 404-critical URL segment is already a typed column
(`category_type`, `category_main`, `category_sub_cb`, `sreality_id`), and the locality — the
one segment sreality 301-redirects rather than 404s on — is `slugify(locality)` +
`slugify(street)` through the SAME assembler ingest uses (`scraper.sreality_url.from_columns`
is adapter 2 of `canonical_url`; tests pin that the two adapters agree). A `raw_json` pass
would be ~62 KB x 232k = ~14 GB of detoast against a 120-minute job budget, and would need
a permanent reader for the legacy v2 payload shape; the narrow pass is ~3 minutes and
payload-shape-independent.

The keyset query is `id > cursor` and nothing else — no `source` / `source_url` predicate
inside a LIMITed page (the sparse-predicate trap: it turns every resume page worse as the
job succeeds, and there is no `(source, id)` index). Source and NULL-ness are filtered in
Python from the narrow projection. Idempotent without a marker: recompute, write only on a
difference.

Modes:
  * fill (default; `--write` to actually write): stores the derived canonical URL where it
    differs from the stored one. NEVER a placeholder — canonical string or nothing.
  * `--clear`: where derivation DECLINES and the stored value is a sreality URL, write NULL.
    Off by default so the first live pass can never reduce coverage; the dry run reports
    `would_clear` first.
  * `--conformance N`: pre-flight acceptance gate — HEAD-probe N random ACTIVE sreality
    rows' derived URLs at <=2 req/s; 200, or a 301 whose target equals the derived URL,
    passes; any 404 exits 4 BEFORE anything is written. Active rows only: a delisted
    sreality page 404s at any URL.
  * `--report-check`: persist one `pipeline_check_results` row (`outbound_url_parity`) so the
    weekly dry run is the only detector that reaches the inactive archive.

Gates that stop a write pass (exit 3): an UNKNOWN `category_sub_cb` on a row seen in the last
7 days (sreality's own API 422s every code outside the codebook, so a recent unknown code
means the vocabulary moved — the codebook needs the entry before anything else is written);
and a derived URL that does not end in its own listing id (structurally impossible; a
derivation bug, and `listings.source_url` is what the estimation URL-matcher binds a
subject on, with no unique index).

Writes nothing but `listings.source_url`, which is out of every content hash — ZERO
`listing_snapshots` rows, no trigger on `listings` fires (both are `UPDATE OF geom ...`).
DML only, no ACCESS EXCLUSIVE; waits for a `rebuild_%` gap and replays a batch that loses a
lock fight with the hourly MF recompute. sreality's stored URL is DISPLAY-ONLY (its SSR page
is a login-redirect loop; `scraper.db.detail_ref` keeps it off every fetch queue).

Required env: SUPABASE_DB_URL. Defaults to DRY-RUN.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import requests

from scraper import db, sreality_url
from scripts.backfill_support import (
    _STATEMENT_TIMEOUT_SQL,
    execute_with_lock_retry,
    wait_for_rebuild_gap,
)

LOG = logging.getLogger(__name__)

SOURCE = "sreality"
RECENT_DAYS = 7
CHECK_KEY = "outbound_url_parity"
PARITY_FAIL_ROWS = 1000

# Narrow projection: names no wide column, so the page detoasts nothing.
_SELECT_SQL = """
    SELECT l.id, l.source, l.sreality_id, l.category_type, l.category_main,
           l.category_sub_cb, l.locality, l.street, l.street_source,
           l.source_url, l.is_active, l.last_seen_at
    FROM listings l
    WHERE l.id > %(after)s::bigint
    ORDER BY l.id
    LIMIT %(page)s::int
"""

# One statement per batch; IS DISTINCT FROM keeps a replay (lock retry) a no-op.
_UPDATE_SQL = """
    UPDATE listings AS l
    SET source_url = v.url
    FROM (SELECT * FROM unnest(%(ids)s::bigint[], %(urls)s::text[]) AS t(id, url)) AS v
    WHERE l.id = v.id AND l.source_url IS DISTINCT FROM v.url
"""

# The conformance sample: active sreality rows only (a delisted page 404s at any URL).
_SAMPLE_SQL = """
    SELECT l.id, l.source, l.sreality_id, l.category_type, l.category_main,
           l.category_sub_cb, l.locality, l.street, l.street_source,
           l.source_url, l.is_active, l.last_seen_at
    FROM listings l
    WHERE l.source = 'sreality' AND l.is_active
    ORDER BY random()
    LIMIT %(n)s::int
"""

_INSERT_CHECK_SQL = (
    "INSERT INTO pipeline_check_results (run_at, check_key, status, value, details) "
    "VALUES (%s, %s, %s, %s, %s::jsonb)"
)

# Status-code HEAD only, never follow, never read a body, never authenticate: this is the
# one place in the codebase that touches sreality's SSR host, and it must stay a probe.
_PROBE_HEADERS = {
    "Accept-Language": "cs,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}
_PROBE_INTERVAL_S = 0.5

SKIP = "skip"
UNCHANGED = "unchanged"
WRITE = "write"
CLEAR = "clear"
WOULD_CLEAR = "would_clear"
DECLINED = "declined"


@dataclass(frozen=True)
class Row:
    id: int
    source: str | None
    sreality_id: int | None
    category_type: str | None
    category_main: str | None
    category_sub_cb: int | None
    locality: str | None
    street: str | None
    street_source: str | None
    source_url: str | None
    is_active: bool | None
    last_seen_at: _dt.datetime | None

    @classmethod
    def from_tuple(cls, t: tuple[Any, ...]) -> "Row":
        return cls(*t)


@dataclass(frozen=True)
class Decision:
    action: str                       # SKIP | UNCHANGED | WRITE | CLEAR | WOULD_CLEAR | DECLINED
    url: str | None                   # the value to store for WRITE (None for CLEAR)
    reason: sreality_url.Declined | None


def critical_segments(url: str | None) -> tuple[str, str, str, str] | None:
    """(type, main, sub, id) of a sreality URL — the segments sreality 404s on. The
    locality segment is deliberately NOT part of it: sreality 301s any locality to the
    canonical, so two URLs differing only there name the same page."""
    if not url or not url.startswith(sreality_url.BASE_URL + "/"):
        return None
    parts = url[len(sreality_url.BASE_URL) + 1:].split("/")
    if len(parts) != 5:
        return None
    return parts[0], parts[1], parts[2], parts[4]


def decide(row: Row, *, clear: bool) -> Decision:
    """The pure decision for one row. Non-sreality rows are never touched.

    A stored URL that agrees with the derivation on every 404-critical segment is
    UNCHANGED even when its locality differs: ingest wrote it from sreality's own
    seo names (authoritative), the column adapter re-slugifies display text, and
    the reconciler must never downgrade the former to the latter. Only a
    404-critical disagreement is a disagreement.
    """
    if row.source != SOURCE:
        return Decision(SKIP, None, None)
    url, reason = sreality_url.from_columns(
        category_type=row.category_type, category_main=row.category_main,
        category_sub_cb=row.category_sub_cb, locality=row.locality,
        street=row.street, street_source=row.street_source, sreality_id=row.sreality_id,
    )
    if url is not None:
        stored = critical_segments(row.source_url)
        if stored is not None and stored == critical_segments(url):
            return Decision(UNCHANGED, row.source_url, None)
        return Decision(WRITE, url, None)
    stored_is_sreality = bool(row.source_url) and row.source_url.startswith(sreality_url.BASE_URL)
    if stored_is_sreality:
        return Decision(CLEAR if clear else WOULD_CLEAR, None, reason)
    return Decision(DECLINED, None, reason)


def structurally_sound(url: str, sreality_id: int | None) -> bool:
    """The canonical embeds the id; a URL that does not end in its own id is a derivation bug."""
    return sreality_id is not None and url.endswith(f"/{sreality_id}")


def locality_format(locality: str | None) -> str:
    if not locality:
        return "null"
    return "legacy_comma" if ", " in locality else "structured"


class Report:
    """Every counter that carries a decision, logged in the finally block."""

    def __init__(self) -> None:
        self.examined = 0
        self.sreality = 0
        self.actions: Counter[str] = Counter()
        self.declined_by_reason: Counter[str] = Counter()
        self.unknown_cb: Counter[str] = Counter()          # "cb:main" -> rows
        self.unknown_cb_recent = 0
        self.unknown_cb_last_seen: dict[str, str] = {}
        self.locality_format: Counter[str] = Counter()
        self.written_split: Counter[str] = Counter()       # active | inactive
        self.written_street_source: Counter[str] = Counter()
        self.structural_faults = 0
        self.disagreeing = 0                               # stored != derived (write + clear)

    def record(self, row: Row, d: Decision, *, now: _dt.datetime) -> None:
        self.examined += 1
        if d.action == SKIP:
            return
        self.sreality += 1
        self.actions[d.action] += 1
        self.locality_format[locality_format(row.locality)] += 1
        if d.action in (WRITE, CLEAR, WOULD_CLEAR):
            self.disagreeing += 1
        if d.action == WRITE:
            self.written_split["active" if row.is_active else "inactive"] += 1
            self.written_street_source[str(row.street_source)] += 1
        if d.reason is not None:
            self.declined_by_reason[d.reason.value] += 1
            if d.reason is sreality_url.Declined.SUB_UNKNOWN:
                key = f"{row.category_sub_cb}:{row.category_main}"
                self.unknown_cb[key] += 1
                seen = row.last_seen_at
                if seen is not None:
                    if seen.tzinfo is None:
                        seen = seen.replace(tzinfo=_dt.timezone.utc)
                    prev = self.unknown_cb_last_seen.get(key)
                    if prev is None or seen.isoformat() > prev:
                        self.unknown_cb_last_seen[key] = seen.isoformat()
                    if seen > now - _dt.timedelta(days=RECENT_DAYS):
                        self.unknown_cb_recent += 1

    def details(self) -> dict[str, Any]:
        return {
            "examined": self.examined, "sreality": self.sreality,
            "actions": dict(self.actions), "disagreeing": self.disagreeing,
            "declined_by_reason": dict(self.declined_by_reason),
            "unknown_cb": dict(self.unknown_cb),
            "unknown_cb_last_seen": self.unknown_cb_last_seen,
            "unknown_cb_recent": self.unknown_cb_recent,
            "locality_format": dict(self.locality_format),
            "written_split": dict(self.written_split),
            "written_street_source": dict(self.written_street_source),
            "structural_faults": self.structural_faults,
        }

    def check_status(self) -> str:
        if self.unknown_cb_recent > 0 or self.structural_faults > 0:
            return "fail"
        if self.disagreeing > PARITY_FAIL_ROWS:
            return "fail"
        return "warn" if self.disagreeing > 0 else "ok"

    def log(self, *, cursor: int, exhausted: bool, dry_run: bool) -> None:
        LOG.info("RECONCILE examined=%d sreality=%d write=%d unchanged=%d clear=%d "
                 "would_clear=%d declined=%d disagreeing=%d",
                 self.examined, self.sreality, self.actions[WRITE], self.actions[UNCHANGED],
                 self.actions[CLEAR], self.actions[WOULD_CLEAR], self.actions[DECLINED],
                 self.disagreeing)
        for key, n in sorted(self.declined_by_reason.items(), key=lambda kv: -kv[1]):
            LOG.info("RECONCILE declined     %-28s %7d", key, n)
        for key, n in sorted(self.unknown_cb.items(), key=lambda kv: -kv[1]):
            LOG.info("RECONCILE unknown_cb   %-28s %7d last_seen=%s", key, n,
                     self.unknown_cb_last_seen.get(key))
        LOG.info("RECONCILE unknown_cb_recent=%d (>0 BLOCKS the write pass: the codebook "
                 "gained an entry)", self.unknown_cb_recent)
        for key, n in sorted(self.locality_format.items()):
            LOG.info("RECONCILE locality_fmt %-28s %7d", key, n)
        for key, n in sorted(self.written_split.items()):
            LOG.info("RECONCILE written      %-28s %7d", key, n)
        for key, n in sorted(self.written_street_source.items()):
            LOG.info("RECONCILE street_src   %-28s %7d", key, n)
        LOG.info("RECONCILE done cursor=%d exhausted=%s dry_run=%s status=%s",
                 cursor, exhausted, dry_run, self.check_status())


def probe(url: str, session: requests.Session) -> tuple[int, str | None]:
    """HEAD, no redirects followed. Returns (status, Location or None)."""
    resp = session.head(url, headers=_PROBE_HEADERS, allow_redirects=False, timeout=20)
    return resp.status_code, resp.headers.get("Location")


def conforms(status: int, location: str | None, derived: str) -> bool:
    return status == 200 or (status in (301, 302, 308) and location == derived)


def run_conformance(conn: Any, n: int) -> Counter[str]:
    """Pre-flight: derive N random active rows and ask sreality. Returns the code histogram."""
    with conn.cursor() as cur:
        cur.execute(_SAMPLE_SQL, {"n": n})
        rows = [Row.from_tuple(t) for t in cur.fetchall()]
    codes: Counter[str] = Counter()
    session = requests.Session()
    for row in rows:
        d = decide(row, clear=False)
        if d.url is None:
            codes["underivable"] += 1
            continue
        try:
            status, location = probe(d.url, session)
        except requests.RequestException as exc:
            LOG.warning("CONFORMANCE id=%s error=%s", row.sreality_id, exc)
            codes["error"] += 1
            continue
        ok = conforms(status, location, d.url)
        codes["ok" if ok else f"fail:{status}"] += 1
        if not ok:
            LOG.warning("CONFORMANCE id=%s cb=%s status=%s location=%s derived=%s",
                        row.sreality_id, row.category_sub_cb, status, location, d.url)
        time.sleep(_PROBE_INTERVAL_S)
    LOG.info("CONFORMANCE sampled=%d %s", len(rows), dict(codes))
    return codes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max listings examined this run. Default: the whole table.")
    parser.add_argument("--batch-size", type=int, default=5000,
                        help="Rows per keyset page (the read names no wide column).")
    parser.add_argument("--after", type=int, default=0,
                        help="Resume from this listings.id cursor (exclusive).")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Wall-clock budget; stop and exit cleanly.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report what would change; write nothing (the default).")
    parser.add_argument("--write", dest="dry_run", action="store_false",
                        help="Actually write. Without it this script only reports.")
    parser.add_argument("--clear", action="store_true",
                        help="Also NULL a stored sreality URL whose derivation now declines.")
    parser.add_argument("--conformance", type=int, default=0,
                        help="Pre-flight: HEAD-probe N random active rows; any 404 blocks.")
    parser.add_argument("--report-check", action="store_true",
                        help="Persist one pipeline_check_results row (outbound_url_parity).")
    parser.add_argument("--rebuild-wait-seconds", type=float, default=900.0,
                        help="How long to wait for a read-model rebuild gap before starting anyway.")
    parser.add_argument("--ignore-rebuild", action="store_true",
                        help="Start immediately, without waiting for a rebuild gap.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    start = time.monotonic()
    now = _dt.datetime.now(_dt.timezone.utc)
    report = Report()
    cursor = args.after
    exhausted = False
    write_suspended = False
    exit_code = 0

    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_STATEMENT_TIMEOUT_SQL)
            if args.conformance > 0:
                codes = run_conformance(conn, args.conformance)
                if any(k.startswith("fail:") for k in codes):
                    LOG.error("CONFORMANCE FAILED %s — nothing written", dict(codes))
                    if not args.dry_run:
                        return 4
            if not args.ignore_rebuild:
                wait_for_rebuild_gap(conn, args.rebuild_wait_seconds)
            LOG.info("RECONCILE start batch=%d after=%d dry_run=%s clear=%s",
                     args.batch_size, args.after, args.dry_run, args.clear)

            while True:
                page = args.batch_size
                if args.limit is not None:
                    remaining = args.limit - report.examined
                    if remaining <= 0:
                        break
                    page = min(page, remaining)
                with conn.cursor() as cur:
                    cur.execute(_SELECT_SQL, {"after": cursor, "page": page})
                    rows = [Row.from_tuple(t) for t in cur.fetchall()]
                if not rows:
                    exhausted = True
                    break

                ids: list[int] = []
                urls: list[str | None] = []
                for row in rows:
                    cursor = row.id
                    d = decide(row, clear=args.clear)
                    if d.action == WRITE and not structurally_sound(d.url or "", row.sreality_id):
                        report.structural_faults += 1
                        LOG.error("RECONCILE structural fault id=%s url=%s", row.id, d.url)
                        write_suspended = True
                        exit_code = 3
                        continue
                    report.record(row, d, now=now)
                    if d.action in (WRITE, CLEAR):
                        ids.append(row.id)
                        urls.append(d.url)
                        if args.verbose:
                            LOG.debug("RECONCILE id=%d %s -> %s", row.id, row.source_url, d.url)

                if report.unknown_cb_recent > 0 and not write_suspended:
                    LOG.error("RECONCILE unknown category_sub_cb on a row seen in the last %d "
                              "days — the codebook moved; write pass suspended", RECENT_DAYS)
                    write_suspended = True
                    exit_code = 3

                if ids and not args.dry_run and not write_suspended:
                    execute_with_lock_retry(conn, _UPDATE_SQL, {"ids": ids, "urls": urls},
                                            label="RECONCILE batch")

                LOG.info("RECONCILE progress examined=%d write=%d clear=%d would_clear=%d "
                         "declined=%d cursor=%d%s",
                         report.examined, report.actions[WRITE], report.actions[CLEAR],
                         report.actions[WOULD_CLEAR], report.actions[DECLINED], cursor,
                         " (writes suspended)" if write_suspended and not args.dry_run else "")

                if len(rows) < page:
                    exhausted = True
                    break
                if args.max_seconds and time.monotonic() - start > args.max_seconds:
                    LOG.info("RECONCILE stopping: --max-seconds reached after=%d", cursor)
                    break

            if args.report_check and exhausted:
                details = report.details()
                details["dry_run"] = args.dry_run
                with conn.cursor() as cur:
                    cur.execute(_INSERT_CHECK_SQL, (now, CHECK_KEY, report.check_status(),
                                                    report.disagreeing, json.dumps(details)))
                LOG.info("RECONCILE check %s status=%s value=%d", CHECK_KEY,
                         report.check_status(), report.disagreeing)
    finally:
        report.log(cursor=cursor, exhausted=exhausted, dry_run=args.dry_run)
        if not exhausted:
            LOG.warning("RECONCILE INCOMPLETE — rows above id=%d were never examined; "
                        "resume with --after %d", cursor, cursor)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
