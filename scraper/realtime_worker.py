"""Always-on realtime supervisor (realtime-scrapers Wave C-3).

A SECOND Railway service from the same Docker image (start command
`python -m scraper.realtime_worker`) that replaces cron quantization for the
latency-critical path. Five settings-paced asyncio lanes (the proven
matcher/outbox pattern from api/notifications + api/notification_outbox):

- probe:     every `realtime_probe_interval_seconds` (default 180), run the
             newest-first delta probe (portal_runner.run_index_probe, Wave C-2)
             sequentially over the probe-capable portals — diff + enqueue only,
             never mark_inactive.
- drain:     every `realtime_drain_interval_seconds` (default 30), claim a
             bounded slice of the shared listing_detail_queue per source that
             has claimable rows. SKIP LOCKED makes this safe beside the GitHub
             Actions drains by construction. Sources listed in
             `realtime_drain_disabled_sources` are skipped — a per-source
             kill-switch (e.g. to freeze a portal's queue at low attempts
             during a proxy outage instead of burning them to given_up).
- images:    every `realtime_images_interval_seconds` (default 60), drain a
             `realtime_images_slice`-capped slice (default 500) of pending
             image downloads via the one existing machinery
             (scraper.main._run_image_downloads: per-host semaphore + breaker,
             active-only newest-first) — the latency lever that feeds the
             images-first publication gate in api/notifications. Coexists with
             images_fresh.yml (idempotent storage_path-IS-NULL selection);
             without R2 env vars the lane logs once and idles (the proxy-skip
             posture).
- count_probe: every `realtime_sreality_count_interval_seconds` (default 600),
             poll pagination.total per sreality (cm, ct) pair (one cheap request
             each — SrealityClient.probe_result_size). sreality's v1 API ignores
             sort params, so a newest-first probe is impossible; a count change
             beyond +-1 jitter is the cheap "something appeared/left" signal, and
             (when dispatch is opted in — a token env AND the
             realtime_sreality_count_dispatch_enabled setting) it triggers a
             targeted index_walk sooner than the */15 cron. Always records
             per-pair totals to sreality_count_probe_state (migration 270).
- heartbeat: every 30s, upsert this worker's beat + per-lane counters into
             worker_heartbeats (migration 269) — the Health-page liveness hook
             and where future lanes (dedup wake) will report.

SHIPS DARK: the process exits immediately unless env REALTIME_WORKER_ENABLED=1,
so merging changes nothing until the operator creates the Railway service.
Setting any lane's interval <= 0 idles that lane (kill-switch without redeploy).

sreality and mmreality are deliberately OUT of the registry: sreality's v1 API
ignores every sort param (no probe capability; its own */15 Actions split
already covers it) and mmreality is proxied low-frequency by design. Adding a
portal later is one registry line.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from scraper import db, image_storage, portal_runner
from scraper.portal import PortalConfig, default_config, load_portal_config

LOG = logging.getLogger("scraper.realtime_worker")

ENABLE_ENV = "REALTIME_WORKER_ENABLED"
WORKER_NAME = "realtime-worker"

PROBE_PAGES = 1
PROBE_INTERVAL_DEFAULT = 180
DRAIN_INTERVAL_DEFAULT = 30
DRAIN_SLICE_DEFAULT = 200
DRAIN_MAX_SECONDS = 120.0
IMAGES_INTERVAL_DEFAULT = 60
IMAGES_SLICE_DEFAULT = 500
# Modest beside the Actions lanes' 32: the worker slice is small and the lane
# shares the CDNs with images_fresh.yml.
IMAGES_WORKERS = 8
HEARTBEAT_INTERVAL_SECONDS = 30.0
IDLE_WAIT_SECONDS = 60.0
LANE_RESTART_SECONDS = 30.0

# sreality count-probe lane (W3): sreality's v1 search API ignores every sort
# param, so the newest-first delta probe the other portals use is impossible for
# it. Instead this lane polls pagination.total per (cm, ct) every
# COUNT_PROBE_INTERVAL_DEFAULT seconds; a change beyond +-COUNT_PROBE_JITTER can
# trigger a targeted index_walk sooner than the */15 cron. Interval <= 0 idles it.
COUNT_PROBE_INTERVAL_DEFAULT = 600
COUNT_PROBE_JITTER = 1  # |new-old| within this band is API noise, not a real change
COUNT_PROBE_RATE_PER_S = 2.0  # ~20 total-only requests in ~10s, politely paced

# The probe-capable portals (design doc: newest-first index order, or a bespoke
# probe_category). Stable order = polite, predictable per-pass sequencing. Also
# the set the drain lane serves — the worker only drains portals it knows how
# to build.
REALTIME_SOURCES: tuple[str, ...] = (
    "bazos", "bezrealitky", "ceskereality", "idnes",
    "maxima", "realitymix", "remax",
)

_PORTAL_CLASSES: dict[str, tuple[str, str]] = {
    "bezrealitky": ("scraper.bezrealitky_main", "BezrealitkyPortal"),
    "ceskereality": ("scraper.ceskereality_main", "CeskerealityPortal"),
    "idnes": ("scraper.idnes_main", "IdnesPortal"),
    "maxima": ("scraper.maxima_main", "MaximaPortal"),
    "realitymix": ("scraper.realitymix_main", "RealitymixPortal"),
    "remax": ("scraper.remax_main", "RemaxPortal"),
}

_CLIENT_CLASSES: dict[str, tuple[str, str]] = {
    "bazos": ("scraper.bazos_client", "BazosClient"),
    "bezrealitky": ("scraper.bezrealitky_client", "BezrealitkyClient"),
    "ceskereality": ("scraper.ceskereality_client", "CeskerealityClient"),
    "idnes": ("scraper.idnes_client", "IdnesClient"),
    "maxima": ("scraper.maxima_client", "MaximaClient"),
    "realitymix": ("scraper.realitymix_client", "RealitymixClient"),
    "remax": ("scraper.remax_client", "RemaxClient"),
}

# log-once-per-process guard for proxied portals skipped without SCRAPER_PROXY_URL.
_PROXY_WARNED: set[str] = set()

# log-once-per-process guard when R2 env vars are absent on the worker.
_R2_WARNED: set[str] = set()

# Count-probe walk DISPATCH is double-gated — a token env var AND the
# realtime_sreality_count_dispatch_enabled setting (default off) — so the lane
# ships dark for triggering (it still RECORDS per-pair totals for observability).
# index_walk.yml has no per-category inputs, so a trigger is a FULL walk.
DISPATCH_TOKEN_ENVS = ("WORKER_DISPATCH_TOKEN", "SCRAPE_CHAIN_TOKEN")
DISPATCH_REPO = os.environ.get("WORKER_GH_REPO", "waiff/sreality")
DISPATCH_WORKFLOW = "index_walk.yml"
DISPATCH_REF = os.environ.get("WORKER_GH_REF", "main")
# log-once-per-process guard when dispatch is enabled but no token is configured.
_DISPATCH_WARNED: set[str] = set()


# --- settings (read per pass so operator edits take effect without restart) ---


def _read_setting(key: str) -> Any:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
            row = cur.fetchone()
        return None if row is None else row[0]
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _read_int(key: str, default: int) -> int:
    value = _read_setting(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_probe_interval() -> int:
    return _read_int("realtime_probe_interval_seconds", PROBE_INTERVAL_DEFAULT)


def _read_drain_interval() -> int:
    return _read_int("realtime_drain_interval_seconds", DRAIN_INTERVAL_DEFAULT)


def _read_drain_slice() -> int:
    return _read_int("realtime_drain_slice", DRAIN_SLICE_DEFAULT)


def _read_images_interval() -> int:
    return _read_int("realtime_images_interval_seconds", IMAGES_INTERVAL_DEFAULT)


def _read_images_slice() -> int:
    return _read_int("realtime_images_slice", IMAGES_SLICE_DEFAULT)


def _read_source_set(key: str) -> set[str]:
    value = _read_setting(key)
    if not isinstance(value, list):
        return set()
    return {str(v) for v in value}


def _read_disabled_sources() -> set[str]:
    return _read_source_set("realtime_probe_disabled_sources")


def _read_drain_disabled_sources() -> set[str]:
    return _read_source_set("realtime_drain_disabled_sources")


def _read_count_probe_interval() -> int:
    return _read_int(
        "realtime_sreality_count_interval_seconds", COUNT_PROBE_INTERVAL_DEFAULT)


def _read_flag(key: str) -> bool:
    value = _read_setting(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _read_count_dispatch_enabled() -> bool:
    return _read_flag("realtime_sreality_count_dispatch_enabled")


# --- portal construction ----------------------------------------------------


def _load_config(source: str) -> PortalConfig:
    try:
        with db.connect() as conn:
            return load_portal_config(conn, source)
    except Exception as exc:  # noqa: BLE001 - registry hiccup must not break a pass
        LOG.warning(
            "load_portal_config failed source=%s: %s; using baked-in default",
            source, exc,
        )
        return default_config(source)


def _build_portal(source: str, config: PortalConfig) -> Any:
    if source == "bazos":
        # Bazos predates the config-taking constructor: it takes scopes +
        # geocoder and reads its limits off attributes (the bazos_main.main
        # wiring, reproduced here).
        from scraper import bazos_main

        scopes = [
            c for c in config.categories
            if bazos_main.SALE_TYPE.get(c.get("sale_type"))
            and bazos_main.CATEGORY_MAIN.get(c.get("category"))
        ]
        portal = bazos_main.BazosPortal(
            categories=scopes, geocoder=bazos_main._build_geocoder(),
        )
        portal.index_rate = config.limits.index_rate
        portal.shared_rate_limiter = config.limits.shared_rate_limiter
        portal.supports_complete_walk = config.supports_complete_walk
        return portal
    mod_name, cls_name = _PORTAL_CLASSES[source]
    cls = getattr(importlib.import_module(mod_name), cls_name)
    return cls(config)


def _skip_for_proxy(source: str) -> bool:
    """True when the portal's client rides the residential proxy (USE_PROXY)
    and the proxy env is unset — a direct request would only burn a WAF 403."""
    mod_name, cls_name = _CLIENT_CLASSES[source]
    cls = getattr(importlib.import_module(mod_name), cls_name)
    if not getattr(cls, "USE_PROXY", False):
        return False
    env = getattr(cls, "PROXY_ENV", "SCRAPER_PROXY_URL")
    if os.environ.get(env):
        return False
    if source not in _PROXY_WARNED:
        _PROXY_WARNED.add(source)
        LOG.warning("%s unset; skipping proxied portal %s", env, source)
    return True


# --- lane passes (sync halves run in a thread) --------------------------------


def _run_probe_sync(source: str) -> dict[str, Any]:
    config = _load_config(source)
    portal = _build_portal(source, config)
    rc, agg = portal_runner.run_index_probe(
        portal, dry_run=False, probe_pages=PROBE_PAGES,
    )
    agg["rc"] = rc
    return agg


def _run_drain_sync(source: str, max_claims: int) -> dict[str, Any]:
    config = _load_config(source)
    portal = _build_portal(source, config)
    # run_id=None: no scrape_runs row per pass — a 30s cadence would write
    # thousands of bookkeeping rows/day (the images-only precedent: liveness
    # noise). Worker observability lives in worker_heartbeats; the listing
    # writes themselves feed Health identically (first_seen_at, queue age).
    rc, agg = portal_runner.run_detail_drain(
        portal,
        max_claims=max_claims,
        dry_run=False,
        detail_workers=config.limits.detail_workers,
        detail_rate=config.limits.detail_rate,
        max_seconds=DRAIN_MAX_SECONDS,
    )
    agg["rc"] = rc
    return agg


def _claimable_by_source() -> dict[str, int]:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source, count(*) FROM listing_detail_queue "
                "WHERE claimed_at IS NULL AND given_up = false "
                "GROUP BY source"
            )
            return {source: int(n) for source, n in cur.fetchall()}
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _record_pass(state: dict[str, Any], lane: str, last: dict[str, Any]) -> None:
    prev = state["lanes"].get(lane, {})
    state["lanes"][lane] = {
        "last_pass_at": datetime.now(timezone.utc).isoformat(),
        "passes": int(prev.get("passes", 0)) + 1,
        "last": last,
    }


async def _probe_pass(stop_event: asyncio.Event, state: dict[str, Any]) -> None:
    disabled: set[str] = set()
    try:
        disabled = await asyncio.to_thread(_read_disabled_sources)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("probe lane: failed to read disabled sources: %s", exc)
    totals = {"portals": 0, "new": 0, "enqueued": 0, "errors": 0, "skipped": 0}
    for source in REALTIME_SOURCES:
        if stop_event.is_set():
            break
        if source in disabled or _skip_for_proxy(source):
            totals["skipped"] += 1
            continue
        try:
            agg = await asyncio.to_thread(_run_probe_sync, source)
        except Exception:  # noqa: BLE001 - one portal must not end the pass
            LOG.exception("PROBE lane source=%s failed", source)
            totals["errors"] += 1
            continue
        totals["portals"] += 1
        totals["new"] += agg.get("listings_found_new", 0)
        totals["enqueued"] += agg.get("listings_enqueued", 0)
        totals["errors"] += agg.get("errors", 0)
        LOG.info(
            "PROBE lane source=%s pages=%d new=%d enqueued=%d "
            "early_stopped=%d errors=%d",
            source, agg.get("index_pages", 0), agg.get("listings_found_new", 0),
            agg.get("listings_enqueued", 0), agg.get("early_stopped", 0),
            agg.get("errors", 0),
        )
    _record_pass(state, "probe", totals)


async def _drain_pass(stop_event: asyncio.Event, state: dict[str, Any]) -> None:
    slice_ = DRAIN_SLICE_DEFAULT
    try:
        slice_ = await asyncio.to_thread(_read_drain_slice)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("drain lane: failed to read slice: %s", exc)
    if slice_ <= 0:
        LOG.debug("drain lane: slice<=0; skipping pass")
        return
    disabled: set[str] = set()
    try:
        disabled = await asyncio.to_thread(_read_drain_disabled_sources)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("drain lane: failed to read disabled sources: %s", exc)
    counts = await asyncio.to_thread(_claimable_by_source)
    totals = {
        "sources": 0, "new": 0, "updated": 0, "gone": 0, "errors": 0, "skipped": 0,
    }
    for source in REALTIME_SOURCES:
        if stop_event.is_set():
            break
        claimable = counts.get(source, 0)
        if claimable <= 0:
            continue
        if source in disabled or _skip_for_proxy(source):
            totals["skipped"] += 1
            continue
        try:
            agg = await asyncio.to_thread(_run_drain_sync, source, slice_)
        except Exception:  # noqa: BLE001 - one portal must not end the pass
            LOG.exception("DRAIN lane source=%s failed", source)
            totals["errors"] += 1
            continue
        totals["sources"] += 1
        totals["new"] += agg.get("listings_scraped_new", 0)
        totals["updated"] += agg.get("listings_updated", 0)
        totals["gone"] += agg.get("listings_inactive", 0)
        totals["errors"] += agg.get("errors", 0)
        LOG.info(
            "DRAIN lane source=%s claimable=%d new=%d updated=%d gone=%d errors=%d",
            source, claimable, agg.get("listings_scraped_new", 0),
            agg.get("listings_updated", 0), agg.get("listings_inactive", 0),
            agg.get("errors", 0),
        )
    _record_pass(state, "drain", totals)


def _run_images_sync(max_downloads: int) -> dict[str, Any]:
    # Reuse THE image machinery (per-host semaphore, breaker, active-only +
    # newest-first via db.pending_image_downloads) — never fork it. Lazy import
    # keeps scraper.main off the worker's startup path.
    from scraper.main import _run_image_downloads

    return _run_image_downloads(max_downloads, IMAGES_WORKERS, active_only=True)


async def _images_pass(stop_event: asyncio.Event, state: dict[str, Any]) -> None:
    slice_ = IMAGES_SLICE_DEFAULT
    try:
        slice_ = await asyncio.to_thread(_read_images_slice)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("images lane: failed to read slice: %s", exc)
    if slice_ <= 0:
        LOG.debug("images lane: slice<=0; skipping pass")
        return
    if not image_storage.is_configured():
        # Same posture as the proxy skip: log once and idle. The Actions image
        # lanes keep draining; setting the R2 vars enables this lane live.
        if "r2" not in _R2_WARNED:
            _R2_WARNED.add("r2")
            LOG.warning("R2 env vars unset; images lane idling")
        _record_pass(state, "images", {"downloaded": 0, "skipped_no_r2": True})
        return
    if stop_event.is_set():
        return
    agg = await asyncio.to_thread(_run_images_sync, slice_)
    totals = {
        "downloaded": agg.get("images_stored", 0),
        "stopped_suspicious": bool(agg.get("stopped_suspicious", False)),
        "cap": slice_,
    }
    LOG.info(
        "IMAGES lane downloaded=%d cap=%d stopped_suspicious=%s",
        totals["downloaded"], slice_, totals["stopped_suspicious"],
    )
    _record_pass(state, "images", totals)


# --- count-probe lane (sreality) ---------------------------------------------


def _dispatch_token() -> str | None:
    for env in DISPATCH_TOKEN_ENVS:
        tok = os.environ.get(env)
        if tok:
            return tok
    return None


def _count_probe_sync() -> dict[str, Any]:
    """One cheap pagination.total request per sreality (cm, ct) pair; diff against
    sreality_count_probe_state and upsert. Returns the pairs whose total moved beyond
    +-COUNT_PROBE_JITTER. A FIRST sighting (no prior total) is recorded but never flagged,
    so first-populating the table can't trigger a walk. No detail/enqueue: the count is the
    only cheap signal sreality's sort-blind v1 API gives."""
    from scraper.main import CATEGORIES, _build_client
    from scraper.rate_limit import RateLimiter

    limiter = RateLimiter(COUNT_PROBE_RATE_PER_S)
    prior: dict[tuple[int, int], int | None] = {}
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT category_main_cb, category_type_cb, last_total "
                "FROM sreality_count_probe_state")
            for cm, ct, total in cur.fetchall():
                prior[(int(cm), int(ct))] = None if total is None else int(total)
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    changed: list[dict[str, Any]] = []
    errors = 0
    rows: list[tuple[int, int, int, bool]] = []  # cm, ct, new_total, is_changed
    for cm, ct in CATEGORIES:
        try:
            total = _build_client(cm, ct, limiter=limiter).probe_result_size()
        except Exception as exc:  # noqa: BLE001 - one category must not end the pass
            LOG.warning("COUNT-PROBE cm=%s ct=%s failed: %s", cm, ct, exc)
            errors += 1
            continue
        if total is None:
            errors += 1
            continue
        old = prior.get((cm, ct))
        is_changed = old is not None and abs(total - old) > COUNT_PROBE_JITTER
        rows.append((cm, ct, total, is_changed))
        if is_changed:
            changed.append({"cm": cm, "ct": ct, "old": old, "new": total})
    _upsert_count_state(rows)
    return {"pairs": len(rows), "changed": changed, "errors": errors}


def _upsert_count_state(rows: list[tuple[int, int, int, bool]]) -> None:
    if not rows:
        return
    conn = db.connect()  # autocommit
    try:
        with conn.cursor() as cur:
            for cm, ct, total, is_changed in rows:
                cur.execute(
                    """
                    INSERT INTO sreality_count_probe_state
                        (category_main_cb, category_type_cb, last_total,
                         last_checked_at, last_changed_at)
                    VALUES (%s, %s, %s, now(), CASE WHEN %s THEN now() ELSE NULL END)
                    ON CONFLICT (category_main_cb, category_type_cb) DO UPDATE SET
                        last_total      = EXCLUDED.last_total,
                        last_checked_at = now(),
                        last_changed_at = CASE WHEN %s THEN now()
                            ELSE sreality_count_probe_state.last_changed_at END
                    """,
                    (cm, ct, total, is_changed, is_changed),
                )
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _seconds_since_last_sreality_index_walk() -> float | None:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT extract(epoch FROM (now() - max(started_at))) "
                "FROM scrape_runs WHERE source = 'sreality' AND index_pages > 0")
            row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _post_workflow_dispatch(token: str) -> bool:
    import requests

    url = (f"https://api.github.com/repos/{DISPATCH_REPO}"
           f"/actions/workflows/{DISPATCH_WORKFLOW}/dispatches")
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": DISPATCH_REF},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - a dispatch hiccup must not kill the lane
        LOG.warning("count-probe: dispatch POST failed: %s", exc)
        return False
    if resp.status_code == 204:
        LOG.info("count-probe: dispatched %s (ref=%s)", DISPATCH_WORKFLOW, DISPATCH_REF)
        return True
    LOG.warning("count-probe: dispatch returned %s: %s",
                resp.status_code, resp.text[:200])
    return False


def _maybe_dispatch_index_walk(
    changed: list[dict[str, Any]], cooldown_seconds: float,
) -> dict[str, Any]:
    """Trigger ONE targeted index_walk when a count changed, IF dispatch is enabled
    (the setting AND a token env) and no sreality index walk is fresher than
    cooldown_seconds — a debounce against the */15 cron AND prior triggers (both land in
    scrape_runs as index rows). index_walk.yml has no per-category inputs, so this is a
    full walk. Returns {dispatched, reason}."""
    if not changed:
        return {"dispatched": False, "reason": "no_change"}
    if not _read_count_dispatch_enabled():
        return {"dispatched": False, "reason": "disabled"}
    token = _dispatch_token()
    if not token:
        if "token" not in _DISPATCH_WARNED:
            _DISPATCH_WARNED.add("token")
            LOG.warning(
                "count-probe: dispatch enabled but no token env (%s) set; recording only",
                "/".join(DISPATCH_TOKEN_ENVS))
        return {"dispatched": False, "reason": "no_token"}
    age = _seconds_since_last_sreality_index_walk()
    if age is not None and age < cooldown_seconds:
        return {"dispatched": False, "reason": "fresh_walk", "age": int(age)}
    ok = _post_workflow_dispatch(token)
    return {"dispatched": ok, "reason": "triggered" if ok else "dispatch_failed"}


async def _count_probe_pass(stop_event: asyncio.Event, state: dict[str, Any]) -> None:
    if stop_event.is_set():
        return
    agg = await asyncio.to_thread(_count_probe_sync)
    dispatched = False
    if agg["changed"]:
        cooldown = float(await asyncio.to_thread(_read_count_probe_interval))
        try:
            disp = await asyncio.to_thread(
                _maybe_dispatch_index_walk, agg["changed"], cooldown)
            dispatched = bool(disp.get("dispatched"))
        except Exception:  # noqa: BLE001 - a dispatch decision must not end the pass
            LOG.exception("COUNT-PROBE dispatch decision failed")
    LOG.info(
        "COUNT-PROBE pairs=%d changed=%d errors=%d dispatched=%s",
        agg["pairs"], len(agg["changed"]), agg["errors"], dispatched,
    )
    _record_pass(state, "count_probe", {
        "pairs": agg["pairs"], "changed": len(agg["changed"]),
        "errors": agg["errors"], "dispatched": dispatched,
    })


_HEARTBEAT_SQL = """
    INSERT INTO worker_heartbeats (worker, beat_at, started_at, details)
    VALUES (%(worker)s, now(), %(started_at)s, %(details)s)
    ON CONFLICT (worker) DO UPDATE SET
        beat_at    = now(),
        started_at = EXCLUDED.started_at,
        details    = EXCLUDED.details
"""


def _beat_sync(state: dict[str, Any]) -> None:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_HEARTBEAT_SQL, {
                "worker": WORKER_NAME,
                "started_at": state["started_at"],
                "details": Jsonb(state["lanes"]),
            })
    finally:
        with contextlib.suppress(Exception):
            conn.close()


async def _heartbeat_pass(state: dict[str, Any]) -> None:
    await asyncio.to_thread(_beat_sync, state)


# --- the supervisor -----------------------------------------------------------


async def _lane_loop(
    name: str,
    stop_event: asyncio.Event,
    read_interval: Callable[[], float],
    run_pass: Callable[[], Awaitable[None]],
    *,
    default_interval: float,
    idle_seconds: float = IDLE_WAIT_SECONDS,
) -> None:
    """One forever-lane: re-read the interval each pass (live app_settings
    edits apply on the next wake), interval<=0 = idle-not-dead, per-pass
    try/except, clean stop_event exit. Mirrors notifications.matcher_loop."""
    LOG.info("%s lane starting", name)
    while not stop_event.is_set():
        interval = default_interval
        try:
            interval = float(await asyncio.to_thread(read_interval))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("%s lane: failed to read interval: %s", name, exc)

        if interval <= 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=idle_seconds)
            except asyncio.TimeoutError:
                continue
            else:
                break

        try:
            await run_pass()
        except Exception:  # noqa: BLE001 - a pass failure never kills the lane
            LOG.exception("%s lane pass failed", name)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
        else:
            break
    LOG.info("%s lane stopped", name)


async def _supervised(
    name: str,
    lane: Callable[[], Awaitable[None]],
    stop_event: asyncio.Event,
) -> None:
    """Belt on top of the per-pass try/except: a lane-loop bug restarts the
    lane after a pause instead of leaving it silently dead until redeploy."""
    while True:
        try:
            await lane()
            return
        except Exception:  # noqa: BLE001 - a lane crash never kills the process
            LOG.exception(
                "%s lane crashed; restarting in %ss", name, LANE_RESTART_SECONDS,
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=LANE_RESTART_SECONDS)
            return
        except asyncio.TimeoutError:
            continue


def _new_state() -> dict[str, Any]:
    return {"started_at": datetime.now(timezone.utc), "lanes": {}}


async def _amain() -> int:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Railway sends SIGTERM on redeploy; finish the current pass, then exit.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    state = _new_state()
    LOG.info(
        "realtime worker starting sources=%s probe_pages=%d",
        ",".join(REALTIME_SOURCES), PROBE_PAGES,
    )
    lanes: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        ("probe", lambda: _lane_loop(
            "probe", stop_event, _read_probe_interval,
            lambda: _probe_pass(stop_event, state),
            default_interval=PROBE_INTERVAL_DEFAULT)),
        ("drain", lambda: _lane_loop(
            "drain", stop_event, _read_drain_interval,
            lambda: _drain_pass(stop_event, state),
            default_interval=DRAIN_INTERVAL_DEFAULT)),
        ("images", lambda: _lane_loop(
            "images", stop_event, _read_images_interval,
            lambda: _images_pass(stop_event, state),
            default_interval=IMAGES_INTERVAL_DEFAULT)),
        ("count_probe", lambda: _lane_loop(
            "count_probe", stop_event, _read_count_probe_interval,
            lambda: _count_probe_pass(stop_event, state),
            default_interval=COUNT_PROBE_INTERVAL_DEFAULT)),
        ("heartbeat", lambda: _lane_loop(
            "heartbeat", stop_event, lambda: HEARTBEAT_INTERVAL_SECONDS,
            lambda: _heartbeat_pass(state),
            default_interval=HEARTBEAT_INTERVAL_SECONDS)),
    ]
    tasks = [
        asyncio.create_task(_supervised(name, fn, stop_event), name=f"realtime-{name}")
        for name, fn in lanes
    ]
    await asyncio.gather(*tasks)
    LOG.info("realtime worker stopped")
    return 0


def _enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip().lower() in {"1", "true", "yes"}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not _enabled():
        LOG.info(
            "realtime worker disabled (%s != 1); exiting — set the env var on "
            "the Railway service to enable", ENABLE_ENV,
        )
        return 0
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
