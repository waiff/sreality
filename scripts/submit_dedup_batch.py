"""Submit dedup VISION work to the Anthropic Message Batches API (50% off).

PRE-WARMS the dedup engine's vision caches. Runs the engine's FREE funnel
(rules A/B/C + the pHash fast-path — reusing the very same pure rules and SQL
helpers the synchronous engine uses) to find the pairs that would reach the paid
visual stage (Wave 3 removed the cross-source gate, so same-source pairs warm too),
then enqueues their classify / compare / site_plan requests as one or more
size-bounded batches into dedup_batches / dedup_batch_requests.

It NEVER merges and NEVER calls the LLM synchronously: a request is enqueued
only when its result isn't already cached and isn't already in flight. The daily
dedup_engine.yml run later REPLAYS unchanged over the warm caches and produces
the identical merges for free (a cache miss falls back to a synchronous call —
still correct, just not discounted).

Two waves fall out naturally from re-running on a schedule: a pair whose listings
aren't classified yet enqueues classify (wave 1); once those ingest, a later run
finds them classified and enqueues compare / site_plan (wave 2). A both-site-plan
pair defers compare behind its development-guard verdict, exactly mirroring the
engine's _resolve_visual control flow — so the rooms submit enqueues are a
SUPERSET of the rooms the synchronous engine would walk (recall-identical replay).

Results are picked up by scripts.ingest_dedup_batch.

Usage (typically via .github/workflows/dedup_batches.yml):

    python -m scripts.submit_dedup_batch --max-pairs 4000 --max-requests 1500

Required env: SUPABASE_DB_URL, ANTHROPIC_API_KEY (the latter only when not
--dry-run), R2_* (to fetch image bytes for the requests; --dry-run needs neither).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable

# Pure rules + SQL helpers shared with the synchronous engine — no rule logic is
# re-expressed here, only the traversal that collects (rather than resolves) the
# visual candidates. (The submit/ingest plumbing duplicated from the condition
# lane is consolidated in a later shared-primitive PR — see the PR description.)
from scripts.dedup_engine import (
    MAX_GROUP_SIZE,
    _both_have_site_plan,
    _floor_plan_image_ids,
    _group_by_street,
    _high_render_image_ids,
    _load_eligible,
    _phash_distinctive_match,
    _phash_identical_pairs,
)
from scripts.submit_condition_batch import should_flush
from toolkit.dedup_engine import (
    PHASH_MIN_IDENTICAL_PAIRS,
    classify_pair,
    decide_phash_fastpath,
    phash_excluded_tags_for,
    phash_render_exclude_for,
    rooms_in_priority,
)
from toolkit.image_classification import (
    DEFAULT_CLASSIFY_N_IMAGES,
    SITE_PLAN_ROOM_TYPE,
    build_classify_request,
    cached_classification,
)
from toolkit.visual_match import (
    build_compare_request,
    build_floor_plan_request,
    build_site_plan_request,
    cached_floor_plan_verdict,
    cached_site_plan_verdict,
    cached_visual_verdict,
)

LOG = logging.getLogger("submit_dedup_batch")

_CLASSIFY_MODEL_KEY = "llm_room_classify_model"
_COMPARE_MODEL_KEY = "llm_visual_match_model"
_SITE_PLAN_MODEL_KEY = "llm_site_plan_match_model"
_FLOOR_PLAN_MODEL_KEY = "llm_floor_plan_match_model"


@dataclass
class _Req:
    custom_id: str
    kind: str
    model: str
    sreality_id_a: int
    sreality_id_b: int | None
    room_type: str | None
    image_ids: list[int] | None
    params: dict[str, Any]


class _Submitter:
    """Accumulates vision requests into size-bounded batches and submits them.

    Dedups by custom_id within a run and against in-flight batches, caps total
    requests, and streams chunks (one chunk held in memory at a time — vision
    payloads are large) so a big run never balloons memory.
    """

    def __init__(self, conn: Any, provider: Any, *, max_requests: int, dry_run: bool) -> None:
        self._conn = conn
        self._provider = provider
        self._dry_run = dry_run
        self.requests_left = max_requests
        self._in_flight = _in_flight_custom_ids(conn)
        self._collected: set[str] = set()
        self._chunk: list[_Req] = []
        self._chunk_bytes = 0
        self.stats: dict[str, int] = {
            "want_classify": 0, "want_compare": 0, "want_site_plan": 0,
            "want_floor_plan": 0, "skipped_in_flight": 0, "batches": 0,
        }

    @property
    def exhausted(self) -> bool:
        return self.requests_left <= 0

    def add(
        self,
        *,
        custom_id: str,
        kind: str,
        model: str,
        a: int,
        b: int | None,
        room_type: str | None,
        build_fn: Callable[[], dict[str, Any]],
    ) -> None:
        """Enqueue one request if it's not already collected / in flight / capped.

        build_fn is a thunk so the expensive R2 download only happens when the
        request actually needs building (skipped on dedup / in-flight / dry-run)."""
        if self.requests_left <= 0 or custom_id in self._collected:
            return
        if custom_id in self._in_flight:
            self.stats["skipped_in_flight"] += 1
            self._collected.add(custom_id)  # don't re-count on re-encounter
            return
        if self._dry_run:
            self._collected.add(custom_id)
            self.requests_left -= 1
            self.stats[f"want_{kind}"] += 1
            return
        try:
            built = build_fn()
        except Exception as exc:  # noqa: BLE001 - one bad listing must not kill the run
            LOG.warning("BATCH build %s failed: %s", custom_id, exc)
            return
        params = self._provider.build_batch_request_params(
            system=built["system"], messages=built["messages"],
            tools=built["tools"], model=built["model"],
        )
        item_bytes = len(json.dumps(params, separators=(",", ":")))
        if should_flush(
            n_items=len(self._chunk), chunk_bytes=self._chunk_bytes,
            next_item_bytes=item_bytes,
        ):
            self.flush()
        self._chunk.append(_Req(
            custom_id=custom_id, kind=kind, model=model,
            sreality_id_a=a, sreality_id_b=b, room_type=room_type,
            image_ids=built.get("image_ids"), params=params,
        ))
        self._chunk_bytes += item_bytes
        self._collected.add(custom_id)
        self.requests_left -= 1
        self.stats[f"want_{kind}"] += 1

    def flush(self) -> None:
        if not self._chunk:
            self._chunk_bytes = 0
            return
        items = [(r.custom_id, r.params) for r in self._chunk]
        mb = self._chunk_bytes / (1024 * 1024)
        provider_batch_id = self._provider.submit_batch(items)
        batch_id = _insert_batch(self._conn, provider_batch_id, self._chunk)
        LOG.info(
            "BATCH submitted provider_batch_id=%s requests=%d serialized=%.1fMB "
            "batch_id=%d kinds=%s",
            provider_batch_id, len(self._chunk), mb, batch_id, _kind_counts(self._chunk),
        )
        self.stats["batches"] += 1
        self._chunk = []
        self._chunk_bytes = 0


def _kind_counts(chunk: list[_Req]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in chunk:
        out[r.kind] = out.get(r.kind, 0) + 1
    return out


def collect(
    conn: Any,
    llm_client: Any,
    submitter: _Submitter,
    *,
    max_pairs: int,
    max_room_attempts: int,
    n_images: int,
) -> dict[str, int]:
    """Walk the engine's free funnel and enqueue the visual requests it implies.

    Returns funnel stats merged with the submitter's request counters.
    """
    classify_model = llm_client.resolve_model(_CLASSIFY_MODEL_KEY)
    compare_model = llm_client.resolve_model(_COMPARE_MODEL_KEY)
    site_plan_model = llm_client.resolve_model(_SITE_PLAN_MODEL_KEY)
    floor_plan_model = llm_client.resolve_model(_FLOOR_PLAN_MODEL_KEY)
    # Same operator-reordered tag priorities the engine warms against — so this lane warms
    # the SAME priority-ordered room prefix the replay will stop at (recall-safe superset).
    from toolkit.dedup_priorities import load_tag_priority_overrides
    tag_overrides = load_tag_priority_overrides(conn)

    keys = _load_eligible(conn)
    groups = _group_by_street(keys)

    funnel = {"visual_candidates": 0, "pairs_deferred_classify": 0, "floor_plan_warmed": 0}
    seen_listing_pairs: set[tuple[int, int]] = set()
    seen_property_pairs: set[tuple[int, int]] = set()
    pairs_left = max_pairs

    for members in groups.values():
        if submitter.exhausted or pairs_left <= 0:
            break
        if len(members) > MAX_GROUP_SIZE:
            continue
        for i in range(len(members)):
            if submitter.exhausted or pairs_left <= 0:
                break
            for j in range(i + 1, len(members)):
                if submitter.exhausted or pairs_left <= 0:
                    break
                a, b = members[i], members[j]
                lpair = (min(a.sreality_id, b.sreality_id), max(a.sreality_id, b.sreality_id))
                if lpair in seen_listing_pairs:
                    continue
                seen_listing_pairs.add(lpair)

                decision = classify_pair(a, b)
                if decision.action == "reject":
                    continue
                if a.property_id is None or b.property_id is None:
                    continue
                ppair = tuple(sorted((a.property_id, b.property_id)))
                if ppair in seen_property_pairs:
                    continue
                seen_property_pairs.add(ppair)

                # Warm the floor-plan verdict for any both-floor-plan pair (migration 234):
                # the engine's floor-plan gate runs on a pHash, a visual, OR a rule-B
                # exact-address merge (Wave 3), so warm it FIRST — before any skip — or the
                # cache-only run would queue/defer every floor-plan pair.
                _warm_floor_plan(
                    conn, llm_client, submitter, a, b, floor_plan_model, funnel)

                # rule B exact address — the engine merges it via the floor-plan gate (now
                # warmed above), no all-rooms compare needed.
                if decision.action == "auto_merge":
                    continue

                # pHash fast-path — replay merges for free (unless both site_plan, which
                # defers to the development guard below). Byt excludes known-exterior images
                # (mirrors run_engine), so the warm-up funnel and the daily engine agree on
                # which byt pairs the fast-path resolves.
                _rmin = phash_render_exclude_for(a.category_main)
                phash_pairs = _phash_identical_pairs(
                    conn, a.sreality_id, b.sreality_id,
                    phash_excluded_tags_for(a.category_main), render_exclude_min=_rmin)
                distinctive = (
                    phash_pairs < PHASH_MIN_IDENTICAL_PAIRS
                    and _phash_distinctive_match(
                        conn, a.sreality_id, b.sreality_id, render_exclude_min=_rmin))
                if decide_phash_fastpath(phash_pairs, distinctive) and not _both_have_site_plan(
                    conn, a.sreality_id, b.sreality_id
                ):
                    continue

                # Wave 3 removed the cross-source gate: same-source non-exact pairs now reach
                # the visual stage in the engine, so the warmer must warm them too.

                pairs_left -= 1
                funnel["visual_candidates"] += 1
                _collect_visual(
                    conn, llm_client, submitter, a, b,
                    classify_model=classify_model, compare_model=compare_model,
                    site_plan_model=site_plan_model, n_images=n_images,
                    max_room_attempts=max_room_attempts, funnel=funnel,
                    tag_overrides=tag_overrides,
                )

    return {**funnel, **submitter.stats}


def _warm_floor_plan(
    conn: Any, llm_client: Any, submitter: "_Submitter",
    a: Any, b: Any, floor_plan_model: str, funnel: dict[str, int],
) -> None:
    """Enqueue a floor-plan compare for a both-floor-plan pair if not already cached.
    The engine's floor-plan gate (migration 234) reads this verdict on any pHash/visual
    merge, so warming it keeps the cache-only daily run from queueing every such pair."""
    if submitter.exhausted:
        return
    ids_a = _floor_plan_image_ids(conn, a.sreality_id)
    ids_b = _floor_plan_image_ids(conn, b.sreality_id)
    if not (ids_a and ids_b):
        return
    if cached_floor_plan_verdict(
        conn, sreality_id_a=a.sreality_id, sreality_id_b=b.sreality_id,
        model=floor_plan_model,
    ) is not None:
        return
    ca, cb = sorted((a.sreality_id, b.sreality_id))
    submitter.add(
        custom_id=f"fpl-{ca}-{cb}", kind="floor_plan", model=floor_plan_model,
        a=ca, b=cb, room_type=None,
        build_fn=lambda: build_floor_plan_request(
            conn, llm_client, sreality_id_a=a.sreality_id, sreality_id_b=b.sreality_id,
            image_ids_a=ids_a, image_ids_b=ids_b),
    )
    funnel["floor_plan_warmed"] += 1


def _collect_visual(
    conn: Any,
    llm_client: Any,
    s: _Submitter,
    a: Any,
    b: Any,
    *,
    classify_model: str,
    compare_model: str,
    site_plan_model: str,
    n_images: int,
    max_room_attempts: int,
    funnel: dict[str, int],
    tag_overrides: dict[str, list[str]] | None = None,
) -> None:
    """Mirror of run_engine._resolve_visual, but it ENQUEUES batch requests for
    the LLM calls the synchronous resolver would make, instead of making them."""
    state_a, rooms_a = cached_classification(
        conn, sreality_id=a.sreality_id, model=classify_model, n_images=n_images)
    state_b, rooms_b = cached_classification(
        conn, sreality_id=b.sreality_id, model=classify_model, n_images=n_images)

    # Wave 1: any side not yet classified -> enqueue its classify; compare/site_plan
    # need BOTH classified, so defer them (next run, after these ingest).
    if state_a == "need_classify":
        s.add(custom_id=f"cls-{a.sreality_id}", kind="classify", model=classify_model,
              a=a.sreality_id, b=None, room_type=None,
              build_fn=lambda: build_classify_request(
                  conn, llm_client, sreality_id=a.sreality_id, n_images=n_images))
    if state_b == "need_classify":
        s.add(custom_id=f"cls-{b.sreality_id}", kind="classify", model=classify_model,
              a=b.sreality_id, b=None, room_type=None,
              build_fn=lambda: build_classify_request(
                  conn, llm_client, sreality_id=b.sreality_id, n_images=n_images))
    if state_a != "classified" or state_b != "classified":
        if state_a == "need_classify" or state_b == "need_classify":
            funnel["pairs_deferred_classify"] += 1
        return

    # Wave 2: both classified.
    ca, cb = sorted((a.sreality_id, b.sreality_id))
    assert rooms_a is not None and rooms_b is not None  # state == 'classified'
    site_a = rooms_a.get(SITE_PLAN_ROOM_TYPE) or []
    site_b = rooms_b.get(SITE_PLAN_ROOM_TYPE) or []

    # Development guard first (matches _resolve_visual): if both carry a site plan,
    # warm that verdict and DEFER compare until it's known (different_unit -> the
    # replay queues, no compare needed).
    if site_a and site_b:
        verdict = cached_site_plan_verdict(
            conn, sreality_id_a=a.sreality_id, sreality_id_b=b.sreality_id, model=site_plan_model)
        if verdict is None:
            s.add(custom_id=f"spl-{ca}-{cb}", kind="site_plan", model=site_plan_model,
                  a=ca, b=cb, room_type=None,
                  build_fn=lambda: build_site_plan_request(
                      conn, llm_client, sreality_id_a=a.sreality_id, sreality_id_b=b.sreality_id,
                      image_ids_a=site_a, image_ids_b=site_b))
            return
        if verdict == "different_unit":
            return

    # Render exclusion (migration 239): MUST mirror _resolve_visual — drop shared
    # development RENDER images from the room compare for byt. The visual-verdict cache is
    # keyed (a, b, room, model) and ignores the image_ids on a hit, so if the warm-up
    # compared the render-INCLUDED set the engine would replay that render-inflated High;
    # both lanes have to compare the SAME filtered set. Empty rooms drop (like the engine).
    rmin = phash_render_exclude_for(a.category_main)
    if rmin is not None:
        render_ids = _high_render_image_ids(conn, a.sreality_id, b.sreality_id, rmin)
        if render_ids:
            rooms_a = {r: f for r, ids in rooms_a.items()
                       if (f := [i for i in ids if i not in render_ids])}
            rooms_b = {r: f for r, ids in rooms_b.items()
                       if (f := [i for i in ids if i not in render_ids])}

    # Forensic compare: common rooms in priority order, capped — the replay stops
    # at the first High, so warming this priority-ordered prefix is the recall-safe
    # superset (whatever room replay stops at is among these and is warm).
    common = set(rooms_a) & set(rooms_b)
    for room in rooms_in_priority(common, a.category_main, tag_overrides)[:max_room_attempts]:
        if s.exhausted:
            break
        if cached_visual_verdict(
            conn, sreality_id_a=a.sreality_id, sreality_id_b=b.sreality_id,
            room_type=room, model=compare_model,
        ) is not None:
            continue
        ids_a, ids_b = rooms_a[room], rooms_b[room]
        s.add(custom_id=f"cmp-{ca}-{cb}-{room}", kind="compare", model=compare_model,
              a=ca, b=cb, room_type=room,
              build_fn=lambda r=room, ia=ids_a, ib=ids_b: build_compare_request(
                  conn, llm_client, sreality_id_a=a.sreality_id, sreality_id_b=b.sreality_id,
                  room_type=r, image_ids_a=ia, image_ids_b=ib))


def _in_flight_custom_ids(conn: Any) -> set[str]:
    sql = (
        "SELECT r.custom_id FROM dedup_batch_requests r "
        "JOIN dedup_batches b ON b.id = r.batch_id "
        "WHERE b.status IN ('submitted', 'ended') AND r.status = 'pending'"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        return {row[0] for row in cur.fetchall()}


def _insert_batch(conn: Any, provider_batch_id: str, chunk: list[_Req]) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dedup_batches (provider, provider_batch_id, request_count, status) "
            "VALUES ('anthropic', %s, %s, 'submitted') RETURNING id",
            (provider_batch_id, len(chunk)),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT into dedup_batches returned no id")
        batch_id = int(row[0])
        cur.executemany(
            "INSERT INTO dedup_batch_requests "
            "(batch_id, custom_id, kind, model, sreality_id_a, sreality_id_b, room_type, image_ids) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [(batch_id, r.custom_id, r.kind, r.model, r.sreality_id_a,
              r.sreality_id_b, r.room_type, r.image_ids) for r in chunk],
        )
    return batch_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-pairs", type=int, default=4000,
                        help="Max visual candidate pairs examined per run.")
    parser.add_argument("--max-requests", type=int, default=1500,
                        help="Cap total vision requests enqueued per run.")
    parser.add_argument("--max-room-attempts", type=int, default=4,
                        help="Max like-room compare requests enqueued per pair "
                             "(matches the engine's per-pair room cap).")
    parser.add_argument("--n-images", type=int, default=DEFAULT_CLASSIFY_N_IMAGES)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be enqueued without building or submitting.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 2

    import psycopg

    from api.llm_client import LLMClient
    from api.providers.anthropic import AnthropicProvider
    from scraper import image_storage

    if not args.dry_run and not image_storage.is_configured():
        LOG.warning("R2 is not configured; no vision requests can be built this run.")

    provider = AnthropicProvider()
    LOG.info(
        "BATCH submit config max_pairs=%d max_requests=%d max_room_attempts=%d "
        "n_images=%d dry_run=%s",
        args.max_pairs, args.max_requests, args.max_room_attempts,
        args.n_images, args.dry_run,
    )

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        from toolkit.dedup_settings import read_setting
        if not read_setting(conn, "dedup_batch_warmer_enabled"):
            LOG.info("WARMER disabled (dedup_batch_warmer_enabled=false); nothing to submit")
            return 0
        llm_client = LLMClient(conn, providers={"anthropic": provider})
        submitter = _Submitter(conn, provider, max_requests=args.max_requests, dry_run=args.dry_run)
        stats = collect(
            conn, llm_client, submitter,
            max_pairs=args.max_pairs, max_room_attempts=args.max_room_attempts,
            n_images=args.n_images,
        )
        if not args.dry_run:
            submitter.flush()
            stats["batches"] = submitter.stats["batches"]

    LOG.info(
        "BATCH done visual_candidates=%d want_classify=%d want_compare=%d "
        "want_site_plan=%d want_floor_plan=%d floor_plan_warmed=%d skipped_in_flight=%d "
        "deferred_classify=%d batches=%d dry_run=%s",
        stats["visual_candidates"], stats["want_classify"], stats["want_compare"],
        stats["want_site_plan"], stats.get("want_floor_plan", 0),
        stats.get("floor_plan_warmed", 0), stats["skipped_in_flight"],
        stats["pairs_deferred_classify"], stats["batches"], args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
