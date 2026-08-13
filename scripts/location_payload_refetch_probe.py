"""The 200 x 3 confirmation probe — 02 §2.3.2's gate protocol, run on demand.

*"The gate: fetch 200 listings × 3 fetches per portal, compute raw-vs-normalised change
rates, and only then set each contract's `volatile_paths` and enable P2."*

The passive instrument (migration 402) measures how often a body changes between the
scrape's own refetches — hours apart, so a change there is mostly a listing that
actually changed. This probe measures the other half, and it is the half `volatile_paths`
exists for: three fetches of the SAME listing minutes apart. Nothing about the listing
can have changed, so

  * the RAW change rate is per-request noise — ad slots, CSRF tokens, build hashes,
    re-signed CDN URLs — the churn that would make a content-addressed archive grow
    without carrying one bit of new information;
  * the NORMALISED change rate is the residue the volatile profile FAILED to strip, and
    it is the number that says whether the profile is finished. Zero is the target.

Three rails, none of them optional:

  * **Every fetch goes through the portal's own client**, so the probe inherits the
    429/403 penalisation, the retry/backoff, `ListingGoneError` and the
    `SCRAPER_PROXY_URL` egress the live scrape uses. A bespoke `requests.get` here would
    be a second, impolite front door to nine portals.
  * **It writes churn rows and nothing else.** No `portal_raw_pages` row, no payload row,
    no listing write. The only statement it runs besides its own sample read is
    `db.record_payload_churn`, under `payload_norm.probe_normalizer_version()` — its own
    cohort, so a cadence of minutes can never contaminate the passive readout the storage
    gate is signed from.
  * **Round-major, sequential, paced.** All keys in round 1, then all keys in round 2:
    one listing is never hammered back-to-back, the spacing between a key's own fetches
    is the length of a round, and a run that hits its wall-clock budget mid-round still
    leaves every key with the same whole number of rounds behind it.

Usage:
  python -m scripts.location_payload_refetch_probe --source bazos
  python -m scripts.location_payload_refetch_probe --dry-run          # no network
Required: SUPABASE_DB_URL (and SCRAPER_PROXY_URL for ceskereality / mmreality).
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from location_data import loader_db
from location_data.payload_norm import probe_normalizer_version, sniff_content_type
from location_data.resolver import lease
from scraper import db
from scraper.portal import PortalConfig, default_config, load_portal_config
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import PROBE_LEASE_N
from scraper.rate_ledger import build_rate_limiter
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger("location_payload_refetch_probe")

JOB_NAME = "location_payload_refetch_probe"
CONCURRENCY_GROUP = "location-payload-churn"
# `location_jobs.cadence` is NOT NULL and is the ops-calendar column, so a dispatch-only
# lane still declares one: this is "no more often than", not a schedule. Nothing reads it
# to trigger a run — the workflow has no `schedule` block at all.
CADENCE = "7 days"
LEASE_TTL_S = 3600

# 02 §2.3.2's protocol, verbatim.
DEFAULT_LISTINGS = 200
DEFAULT_ROUNDS = 3

# One request per second per portal — half the live drain's configured 2/s, and the
# probe is single-threaded where the drain runs four workers. `--rate-per-s` widens it;
# the portal's own configured detail rate wins whenever it is politer still.
PROBE_RATE_PER_S = 1.0

# 200 x 3 at 1 req/s is ~10 min per portal, so the nine-portal sweep does not fit one
# job. The budget stops the run cleanly between fetches (never mid-round for a key that
# has already been counted) and the workflow's 55-minute ceiling stays a backstop, not
# the mechanism.
DEFAULT_MAX_SECONDS = 2_700

PAGE_KIND = "detail"

STATEMENT_TIMEOUT_ENV = "LOCATION_PROBE_TIMEOUT_S"
DEFAULT_STATEMENT_TIMEOUT_S = 120

# The nine portals, each with the client class the live scrape uses. Mirrors
# realtime_worker._CLIENT_CLASSES and adds mmreality (cron-only, proxied), so the probe
# covers the whole fleet the instrument does. Every client takes `limiter=` and either
# `fetch_detail(ref) -> (text, status)` or `get_detail(id) -> dict`.
PROBE_CLIENTS: dict[str, tuple[str, str]] = {
    "bazos": ("scraper.bazos_client", "BazosClient"),
    "bezrealitky": ("scraper.bezrealitky_client", "BezrealitkyClient"),
    "ceskereality": ("scraper.ceskereality_client", "CeskerealityClient"),
    "idnes": ("scraper.idnes_client", "IdnesClient"),
    "maxima": ("scraper.maxima_client", "MaximaClient"),
    "mmreality": ("scraper.mmreality_client", "MmRealityClient"),
    "realitymix": ("scraper.realitymix_client", "RealitymixClient"),
    "remax": ("scraper.remax_client", "RemaxClient"),
    "sreality": ("scraper.sreality_client", "SrealityClient"),
}

# The two portals whose detail body is JSON, not a page. Their passive call sites
# (main._record_detail_churn, bezrealitky_main.write_details) declare
# 'application/json' explicitly; the seven HTML portals reach the instrument through
# db.upsert_portal_raw_page, which sniffs — so the probe sniffs for them too, and the
# two paths hash the same projection of the same bytes.
JSON_CONTENT_TYPE = "application/json"

KIND_OK = "ok"
KIND_GONE = "gone"
KIND_ERROR = "error"

# Active listings only: a delisted one 404s on the first round and measures nothing.
# `source_url` is the fetch reference for the seven HTML portals (their `detail_url`
# helpers pass a full URL straight through); the two JSON portals key on the native id.
_SAMPLE_LISTINGS_SQL = """
    SELECT l.source_id_native, l.source_url
      FROM listings l
     WHERE l.source = %(source)s
       AND l.is_active
       AND l.source_id_native IS NOT NULL
     ORDER BY random()
     LIMIT %(limit)s
"""


@dataclass(frozen=True)
class SampleKey:
    source: str
    native_id: str
    detail_ref: str | None


@dataclass(frozen=True)
class Fetched:
    kind: str
    body: bytes | None = None
    content_type: str | None = None
    error: str | None = None


@dataclass
class Pacer:
    """Minimum wall-clock spacing between two requests, with an injectable clock.

    The portal client's limiter already paces (and is what carries the 429/403 penalty
    across the fleet), so this is deliberately the SAME interval rather than an extra
    one: the limiter schedules each slot from its own previous slot, which this sleep
    has already passed, so the two compose to one interval and not to two. What the
    probe gains is a spacing guarantee that does not depend on which limiter it was
    handed — including the ledger limiter's permanent fall back to local pacing after a
    DB error — and one that a test can drive on a fake clock with no network.
    """

    min_interval_s: float
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    last_at: float | None = None

    def next_at(self) -> float:
        """The earliest instant the next request may go out."""
        now = self.monotonic()
        if self.last_at is None:
            return now
        return max(now, self.last_at + self.min_interval_s)

    def wait(self) -> None:
        target = self.next_at()
        now = self.monotonic()
        if target > now:
            self.sleep(target - now)
            now = self.monotonic()
        self.last_at = now


@dataclass
class ProbeCounts:
    keys: int = 0
    rounds_completed: int = 0
    fetches: int = 0
    ok: int = 0
    gone: int = 0
    errors: int = 0
    recorded: int = 0
    write_errors: int = 0
    stopped_early: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "keys": self.keys,
            "rounds_completed": self.rounds_completed,
            "fetches": self.fetches,
            "ok": self.ok,
            "gone": self.gone,
            "errors": self.errors,
            "recorded": self.recorded,
            "write_errors": self.write_errors,
            "stopped_early": self.stopped_early,
        }


def probe_rounds(
    keys: Sequence[SampleKey],
    *,
    rounds: int,
    fetch: Callable[[SampleKey], Fetched],
    record: Callable[[SampleKey, Fetched], bool],
    pacer: Pacer,
    deadline: float | None = None,
) -> ProbeCounts:
    """Round-major sequential passes over `keys`. The whole protocol lives here.

    A key that comes back `gone` leaves the sample: it will 404 for the rest of the run,
    and re-asking a portal for a listing it has already said is gone is neither polite
    nor informative. A key that errors stays — a transient 502 is not an answer.

    `record` returns whether the row was written, so a write failure is counted without
    being able to abort the pass that is already paid for in requests.
    """
    counts = ProbeCounts(keys=len(keys))
    live = list(keys)
    for round_index in range(rounds):
        if not live:
            break
        gone: set[str] = set()
        for key in live:
            # `next_at`, not `monotonic`: a request whose pacing would carry it past
            # the budget is not started, so the run never overshoots by an interval.
            if deadline is not None and pacer.next_at() >= deadline:
                counts.stopped_early = True
                LOG.info(
                    "PROBE budget reached after %d fetches (round %d/%d)",
                    counts.fetches, round_index + 1, rounds,
                )
                return counts
            pacer.wait()
            result = fetch(key)
            counts.fetches += 1
            if result.kind == KIND_GONE:
                counts.gone += 1
                gone.add(key.native_id)
                continue
            if result.kind != KIND_OK:
                counts.errors += 1
                LOG.warning(
                    "PROBE fetch failed source=%s id=%s: %s",
                    key.source, key.native_id, result.error,
                )
                continue
            counts.ok += 1
            if record(key, result):
                counts.recorded += 1
            else:
                counts.write_errors += 1
        counts.rounds_completed = round_index + 1
        if gone:
            live = [k for k in live if k.native_id not in gone]
    return counts


def sample_keys(
    conn: psycopg.Connection, source: str, *, limit: int, statement_timeout_s: int,
) -> list[SampleKey]:
    with loader_db.bounded(conn, statement_timeout_s) as cur:
        cur.execute(_SAMPLE_LISTINGS_SQL, {"source": source, "limit": limit})
        rows = cur.fetchall()
    return [
        SampleKey(source=source, native_id=str(native_id), detail_ref=source_url)
        for native_id, source_url in rows
    ]


def build_client(source: str, limiter: RateLimiter) -> Any:
    module_name, class_name = PROBE_CLIENTS[source]
    client_class = getattr(importlib.import_module(module_name), class_name)
    return client_class(limiter=limiter)


def proxy_unavailable(source: str) -> bool:
    """True when this portal's client rides the residential proxy and it is not set.

    Without it, ceskereality and mmreality answer a datacenter IP with a WAF 403 — 600
    requests of pure noise, and 600 penalisation events on a shared rate ledger.
    """
    module_name, class_name = PROBE_CLIENTS[source]
    client_class = getattr(importlib.import_module(module_name), class_name)
    if not getattr(client_class, "USE_PROXY", False):
        return False
    return not os.environ.get(getattr(client_class, "PROXY_ENV", "SCRAPER_PROXY_URL"))


def fetch_body(source: str, client: Any, key: SampleKey) -> Fetched:
    """One detail fetch, projected exactly as that portal's passive call site projects it.

    Three shapes, because the instrument already has three call sites: the seven HTML
    portals stage a page body through `db.upsert_portal_raw_page`; sreality hashes the
    unwrapped estate JSON in `main._record_detail_churn`; bezrealitky hashes the parsed
    listing's `raw` (the GraphQL advert plus the parser's derived `image_urls`) in
    `bezrealitky_main.write_details`. A probe that hashed a different projection would
    measure a different thing from the passive cohort it exists to confirm.
    """
    try:
        if source == "sreality":
            estate = client.get_detail(int(key.native_id))
            body = json.dumps(estate, ensure_ascii=False).encode("utf-8")
            return Fetched(kind=KIND_OK, body=body, content_type=JSON_CONTENT_TYPE)
        if source == "bezrealitky":
            advert = client.get_detail(key.native_id)
            body = json.dumps(_bezrealitky_raw(advert), ensure_ascii=False).encode("utf-8")
            return Fetched(kind=KIND_OK, body=body, content_type=JSON_CONTENT_TYPE)
        html, _status = client.fetch_detail(key.detail_ref or key.native_id)
        body = html.encode("utf-8")
        return Fetched(kind=KIND_OK, body=body, content_type=sniff_content_type(body))
    except ListingGoneError:
        return Fetched(kind=KIND_GONE)
    except Exception as exc:  # noqa: BLE001 - one listing must not end the probe
        return Fetched(kind=KIND_ERROR, error=str(exc))


def _bezrealitky_raw(advert: dict[str, Any]) -> dict[str, Any]:
    """The advert as the passive path hashes it, or the bare advert if the parse fails.

    `image_urls` is derived from the advert, so its presence cannot change WHEN the hash
    moves — only the body size the projection reports. Falling back is therefore a small
    size under-count rather than a different measurement, and it is logged.
    """
    from scraper.bezrealitky_parser import parse_advert

    try:
        return parse_advert(advert).raw
    except Exception as exc:  # noqa: BLE001 - a parser fault must not lose the fetch
        LOG.warning("PROBE bezrealitky parse failed (hashing the bare advert): %s", exc)
        return advert


def record_fetch(
    conn: psycopg.Connection, key: SampleKey, result: Fetched, *, cohort: str,
) -> bool:
    """Count this fetch in the probe cohort. Returns whether the row was written.

    Deliberately NOT `record_payload_churn_if_enabled`: that wrapper is gated on
    `location_payload_shadow_hash`, which is the passive instrument's switch. A probe the
    operator has explicitly dispatched must record whether or not the passive lane is on.
    """
    if result.body is None or result.content_type is None:
        return False
    try:
        db.record_payload_churn(
            conn,
            source=key.source,
            source_id_native=key.native_id,
            page_kind=PAGE_KIND,
            body=result.body,
            content_type=result.content_type,
            observation=uuid.uuid4().hex,
            normalizer_version=cohort,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - the fetch is spent; keep probing
        LOG.warning("PROBE churn write failed source=%s id=%s: %s",
                    key.source, key.native_id, exc)
        return False


def portal_config(conn: psycopg.Connection | None, source: str) -> PortalConfig:
    if conn is None:
        return default_config(source)
    try:
        return load_portal_config(conn, source)
    except Exception as exc:  # noqa: BLE001 - a registry hiccup must not stop the probe
        LOG.warning("PROBE load_portal_config failed source=%s: %s", source, exc)
        return default_config(source)


def probe_source(
    conn: psycopg.Connection | None,
    source: str,
    *,
    listings: int,
    rounds: int,
    rate_per_s: float,
    dry_run: bool,
    deadline: float | None,
    statement_timeout_s: int,
) -> ProbeCounts:
    """One portal: sample, build the paced client, run the rounds, count the fetches."""
    config = portal_config(conn, source)
    # The politer of the two rates wins: an operator who has throttled a fragile portal
    # in `portals.operational_limits` must not be overridden by this lane's default.
    configured = config.limits.detail_rate
    rate = min(rate_per_s, configured) if configured and configured > 0 else rate_per_s
    keys = (
        [SampleKey(source=source, native_id=f"dry-run-{i + 1}", detail_ref=None)
         for i in range(min(listings, 3))]
        if conn is None
        else sample_keys(conn, source, limit=listings, statement_timeout_s=statement_timeout_s)
    )
    if not keys:
        LOG.warning("PROBE no active listings sampled for source=%s", source)
        return ProbeCounts()

    limiter = build_rate_limiter(
        source, rate, getattr(config.limits, "shared_rate_limiter", False),
        lease_n=PROBE_LEASE_N,
    )
    # A dry run issues no request, so there is nothing to be polite to: pacing a
    # 200-key sample at 1/s would make "show me what this would fetch" a ten-minute
    # answer. The live path is the only one that paces.
    pacer = Pacer(min_interval_s=0.0 if dry_run else 1.0 / rate)
    LOG.info(
        "PROBE start source=%s keys=%d rounds=%d rate=%.2f/s dry_run=%s",
        source, len(keys), rounds, rate, dry_run,
    )

    if dry_run:
        def fetch(key: SampleKey) -> Fetched:
            LOG.info("DRY-RUN would fetch source=%s id=%s ref=%s",
                     key.source, key.native_id, key.detail_ref)
            return Fetched(kind=KIND_OK, body=b"", content_type="text/html")

        def record(key: SampleKey, result: Fetched) -> bool:
            return True
    else:
        client = build_client(source, limiter)
        cohort = probe_normalizer_version()

        def fetch(key: SampleKey) -> Fetched:
            return fetch_body(source, client, key)

        def record(key: SampleKey, result: Fetched) -> bool:
            assert conn is not None
            return record_fetch(conn, key, result, cohort=cohort)

    counts = probe_rounds(
        keys, rounds=rounds, fetch=fetch, record=record, pacer=pacer, deadline=deadline,
    )
    LOG.info("PROBE done source=%s %s", source, counts.as_dict())
    return counts


def run(
    conn: psycopg.Connection | None,
    sources: Sequence[str],
    *,
    listings: int,
    rounds: int,
    rate_per_s: float,
    max_seconds: float | None,
    dry_run: bool,
    statement_timeout_s: int,
) -> dict[str, ProbeCounts]:
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    results: dict[str, ProbeCounts] = {}
    for source in sources:
        if deadline is not None and time.monotonic() >= deadline:
            LOG.info("PROBE budget spent; %s not started", source)
            break
        if not dry_run and proxy_unavailable(source):
            LOG.warning("PROBE skipping proxied portal %s: SCRAPER_PROXY_URL is unset", source)
            continue
        results[source] = probe_source(
            conn, source,
            listings=listings, rounds=rounds, rate_per_s=rate_per_s, dry_run=dry_run,
            deadline=deadline, statement_timeout_s=statement_timeout_s,
        )
    return results


def parse_sources(raw: str) -> list[str]:
    """Comma-separated portals, or every portal the instrument covers."""
    names = [name.strip() for name in raw.split(",") if name.strip()]
    if not names:
        return list(PROBE_CLIENTS)
    unknown = [name for name in names if name not in PROBE_CLIENTS]
    if unknown:
        raise ValueError(f"unknown source(s): {', '.join(unknown)}")
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="200 x 3 payload refetch probe")
    parser.add_argument("--source", default="",
                        help="comma-separated portals; blank = all nine.")
    parser.add_argument("--listings", type=int, default=DEFAULT_LISTINGS)
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--rate-per-s", type=float, default=PROBE_RATE_PER_S)
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    parser.add_argument("--dry-run", action="store_true",
                        help="No network and no writes: sample (when a DB is reachable), "
                             "log what would be fetched, exercise the pacing.")
    parser.add_argument(
        "--statement-timeout", type=int,
        default=loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S),
        help=f"Per-statement timeout in seconds (${STATEMENT_TIMEOUT_ENV}).")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    try:
        sources = parse_sources(args.source)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.rate_per_s <= 0:
        print("ERROR: --rate-per-s must be positive.", file=sys.stderr)
        return 2

    kwargs: dict[str, Any] = {
        "listings": args.listings,
        "rounds": args.rounds,
        "rate_per_s": args.rate_per_s,
        "max_seconds": args.max_seconds if args.max_seconds > 0 else None,
        "dry_run": args.dry_run,
        "statement_timeout_s": args.statement_timeout,
    }

    if not os.environ.get("SUPABASE_DB_URL"):
        if not args.dry_run:
            print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
            return 2
        LOG.warning("PROBE no SUPABASE_DB_URL: dry-running on synthetic keys")
        results = run(None, sources, **kwargs)
        return _report(results, as_json=args.json)

    with db.connect() as conn:
        with lease.held(
            conn, JOB_NAME, cadence=CADENCE, concurrency_group=CONCURRENCY_GROUP,
            ttl_seconds=LEASE_TTL_S,
        ) as acquired:
            if not acquired:
                LOG.info("PROBE skipped: another run holds the %s lease", JOB_NAME)
                return 0
            results = run(conn, sources, **kwargs)
    return _report(results, as_json=args.json)


def _report(results: dict[str, ProbeCounts], *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(
            {source: counts.as_dict() for source, counts in results.items()}, indent=2,
        ))
        return 0
    print(f"{'source':<14}{'keys':>7}{'rounds':>8}{'fetches':>9}{'ok':>7}{'gone':>7}"
          f"{'errors':>8}{'recorded':>10}{'write_err':>11}")
    for source, counts in results.items():
        print(f"{source:<14}{counts.keys:>7}{counts.rounds_completed:>8}"
              f"{counts.fetches:>9}{counts.ok:>7}{counts.gone:>7}{counts.errors:>8}"
              f"{counts.recorded:>10}{counts.write_errors:>11}")
    print("\nRead the result with: python -m scripts.location_payload_churn_report")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
