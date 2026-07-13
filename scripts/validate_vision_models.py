"""A/B a candidate (model, max_edge) for the dedup vision tools against ground truth,
across all three forensic lanes, so a model or resolution change can't silently lose
auto-merge recall OR silently weaken a safety gate.

Three lanes, two checks each:

  1. RECALL — replay historical decisive verdicts (compare: 'High'; floor_plan:
     'same_layout'/'different_layout'; site_plan: 'same_unit'/'different_unit') from
     the production model's cache and require the candidate reproduces the SAME
     verdict. A drop here = lost recall (compare) or a weaker gate (floor/site).
  2. PRECISION — replay confirmed-DIFFERENT-property pairs from a frozen
     `dedup_golden_sets` snapshot (scripts/build_dedup_golden_set.py) and require the
     candidate does NOT return the lane's dangerous verdict (compare: 'High' — the
     sole auto-merge trigger, rule 15; floor_plan: 'same_layout' — fails to catch a
     shared-plan false merge; site_plan: 'same_unit' — fails the development guard).
     Skipped (with a loud warning, not silently) when --golden-set-name is omitted.

Also: CLASSIFY agreement — re-label a sample of already-classified listings and
measure per-image room-label agreement. Classify never merges on its own, so this is
a softer gate (mislabels only mis-pair like rooms / the pHash interior gate).

All READ-ONLY re-runs of the LLM — writes no cache / no app_settings, only the
standard llm_calls audit rows. Exits non-zero when a configured gate is missed, so
this can gate a real model flip; for an exploratory multi-candidate bake-off (the
normal use of the new --lanes / --golden-set-name flags) a red run across cheaper
candidates is an expected, informative RESULT, not a script failure — read the
printed per-lane numbers, not just the exit code.

Run via .github/workflows/validate_vision_models.yml (needs ANTHROPIC_API_KEY,
GEMINI_API_KEY, OPENAI_API_KEY, QWEN_API_KEY as available, R2_*, SUPABASE_DB_URL).

  python -m scripts.validate_vision_models \\
      --candidate-model gemini-3.1-flash-lite --max-edge 1568 \\
      --lanes compare,floor_plan,site_plan \\
      --golden-set-name 2026-07-13-session3-baseline
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import Any

from scraper import db, image_storage
from toolkit import image_classification as ic
from toolkit import visual_match as vm
from toolkit.room_taxonomy import FLOOR_PLAN_ROOM_TYPE, FULL_PRIORITY, SITE_PLAN_ROOM_TYPE
from toolkit.vision_images import COMPARISON_MAX_EDGE, DOCUMENT_MAX_EDGE, image_block

LOG = logging.getLogger("validate_vision_models")

_PLAN_LIMIT_PER_SIDE = vm._MAX_PLANS_PER_SIDE  # 20 — mirrors production's N×N cap
_ROOM_LIMIT_PER_SIDE = 4


def _is_infra_error(exc: Exception) -> bool:
    """True for an API/account error (credit, quota, rate, auth) — not a real verdict.

    Such errors mean the run can't measure recall/precision; counting them as a miss
    would masquerade a dead key as a model regression (the 2026-06-17 credit-exhaustion
    incident). The caller aborts and reports INCONCLUSIVE instead.
    """
    s = str(exc).lower()
    return any(
        k in s for k in (
            "credit balance", "quota", "rate_limit", "rate limit", "429",
            "overloaded", "529", "authentication", "x-api-key", "permission",
            # Gemini-side account/infra signatures (google-genai surfaces these
            # inside ProviderError text): quota/billing/auth/unavailable.
            "resource_exhausted", "unauthenticated", "billing", "api key",
            "unavailable", "503",
            # OpenAI/DashScope (OpenAICompatibleProvider surfaces "HTTP <code> <body>"
            # verbatim): 402 payment-required, and OpenAI's literal error type string
            # (redundant with "quota" above, kept explicit for grep-ability in logs).
            "402", "insufficient_quota",
        )
    )


def _provider_for(model: str) -> str:
    """Provider name for a candidate model id — the harness is provider-agnostic;
    LLMClient routes by this name (all four providers are registered in main)."""
    if model.startswith("gemini"):
        return "gemini"
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if model.startswith("qwen"):
        return "qwen"
    return "anthropic"


class _InfraAbort(RuntimeError):
    """Raised to abort an A/B run when the API/account is unusable."""


def _room_images(conn: Any, sreality_id: int, room_type: str, limit: int) -> list[str]:
    """Storage paths of one listing's images in `room_type`, sourced the way the engine
    groups rooms/plans: the free CLIP tag (`image_clip_tags.logical_tag`, ~100% coverage
    and the engine's default grouping) OR any LLM room classification
    (`image_room_classifications`, MODEL-AGNOSTIC — the room label is stored under the
    classify model, never the compare model). Used for compare-lane rooms (kitchen,
    bathroom, ...) AND for the fixed 'floor_plan' / 'site_plan' tags — same selection
    logic the engine uses in both cases (scripts/dedup_engine.py's `_floor_plan_image_ids`
    / the site-plan filter in `_resolve_visual`)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.storage_path FROM images i "
            "WHERE i.sreality_id = %s AND i.storage_path IS NOT NULL AND ("
            "  EXISTS (SELECT 1 FROM image_clip_tags t "
            "          WHERE t.image_id = i.id AND t.logical_tag = %s) "
            "  OR EXISTS (SELECT 1 FROM image_room_classifications c "
            "             WHERE c.image_id = i.id AND c.room_type = %s)) "
            "ORDER BY i.sequence ASC NULLS LAST, i.id ASC LIMIT %s",
            (sreality_id, room_type, room_type, limit),
        )
        return [r[0] for r in cur.fetchall()]


def _listing_images(conn: Any, sreality_id: int, limit: int) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, storage_path FROM images "
            "WHERE sreality_id = %s AND storage_path IS NOT NULL "
            "ORDER BY sequence ASC NULLS LAST, id ASC LIMIT %s",
            (sreality_id, limit),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _prod_labels(conn: Any, image_ids: list[int], prod_model: str) -> dict[int, str]:
    if not image_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT image_id, room_type FROM image_room_classifications "
            "WHERE model = %s AND image_id = ANY(%s)",
            (prod_model, image_ids),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


# --- lane registry ----------------------------------------------------------
# One config per forensic lane. `danger_verdict` is what a CONFIRMED-DIFFERENT
# (golden negative) pair must NOT produce: compare's High is the sole auto-merge
# gate (rule 15); floor_plan's same_layout lets a would-merge pair proceed unvetoed;
# site_plan's same_unit fails the development guard that would otherwise queue it.

@dataclass(frozen=True)
class _LaneConfig:
    tool: dict[str, Any]
    prompt_key: str
    called_for: str
    unit: str                    # e.g. "image(s)", "floor plan(s)"
    instruction: str
    extract: Any                 # tool_calls -> verdict str
    danger_verdict: str
    fixed_room_type: str | None  # None => compare lane resolves per-case
    limit_per_side: int


_LANES: dict[str, _LaneConfig] = {
    "compare": _LaneConfig(
        tool=vm.RECORD_VISUAL_MATCH_TOOL,
        prompt_key=vm._PROMPT_KEY,
        called_for="compare_listings_visually",
        unit="image(s)",
        instruction=(
            "Both sets show the same room type. Decide whether they depict the "
            "same physical property, then call record_visual_match once."
        ),
        extract=lambda tcs: vm._extract(tcs)[0],
        danger_verdict="High",
        fixed_room_type=None,
        limit_per_side=_ROOM_LIMIT_PER_SIDE,
    ),
    "floor_plan": _LaneConfig(
        tool=vm.RECORD_FLOOR_PLAN_MATCH_TOOL,
        prompt_key=vm._FLOOR_PLAN_PROMPT_KEY,
        called_for="compare_listing_floor_plans",
        unit="floor plan(s)",
        instruction=(
            "Compare EVERY plan of A against EVERY plan of B (N×N). same_layout if ANY "
            "pair matches; different_layout only if NO pair matches. Call "
            "record_floor_plan_match once."
        ),
        extract=lambda tcs: vm._extract_floor_plan(tcs)[0],
        danger_verdict="same_layout",
        fixed_room_type=FLOOR_PLAN_ROOM_TYPE,
        limit_per_side=_PLAN_LIMIT_PER_SIDE,
    ),
    "site_plan": _LaneConfig(
        tool=vm.RECORD_SITE_PLAN_MATCH_TOOL,
        prompt_key=vm._SITE_PLAN_PROMPT_KEY,
        called_for="compare_listing_site_plans",
        unit="site/situation plan(s)",
        instruction=(
            "Identify the unit each listing highlights across its plans, then compare "
            "A vs B. same_unit if ANY pair shares a unit; different_unit only if NO pair "
            "does. Call record_site_plan_match once."
        ),
        extract=lambda tcs: vm._extract_site_plan(tcs)[0],
        danger_verdict="same_unit",
        fixed_room_type=SITE_PLAN_ROOM_TYPE,
        limit_per_side=_PLAN_LIMIT_PER_SIDE,
    ),
}


def _call_lane(
    conn: Any, llm: Any, r2: Any, *, lane: str, candidate_model: str, max_edge: int,
    paths_a: list[str], paths_b: list[str],
) -> tuple[str, float]:
    """Build the lane's payload, call the candidate, return (verdict, cost_usd)."""
    del conn  # kept for signature symmetry with the DB-backed callers above
    cfg = _LANES[lane]
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"Listing A — {len(paths_a)} {cfg.unit}:"}
    ]
    content.extend(image_block(r2, p, max_edge) for p in paths_a)
    content.append({"type": "text", "text": f"Listing B — {len(paths_b)} {cfg.unit}:"})
    content.extend(image_block(r2, p, max_edge) for p in paths_b)
    content.append({"type": "text", "text": cfg.instruction})
    resp = llm.call(
        called_for=cfg.called_for,
        messages=[{"role": "user", "content": content}],
        system=llm.resolve_system_prompt(cfg.prompt_key),
        tools=[cfg.tool],
        model=candidate_model,
        provider=_provider_for(candidate_model),
    )
    return cfg.extract(resp.tool_calls), float(resp.cost_usd or 0.0)


_MAX_ROOM_ATTEMPTS = 4  # mirrors scripts.dedup_engine's max_room_attempts default


def _candidate_rooms(
    conn: Any, lane: str, a: int, b: int, max_attempts: int = _MAX_ROOM_ATTEMPTS,
) -> list[tuple[str, list[str], list[str]]]:
    """Rooms to try for one pair in this lane, in the SAME priority order and up to the
    SAME cap the engine uses: the fixed plan tag for floor/site_plan (at most one entry —
    there's no per-room OR-gate there, the N×N compare already happens inside one call),
    or up to `max_attempts` FULL_PRIORITY rooms both sides have images for, for compare.

    The compare lane's merge decision is an OR-gate across rooms (ANY room High merges —
    stop-at-first-High, `scripts.dedup_engine._resolve_visual`), so a precision check that
    only tried the FIRST shared room would understate real risk: a room later in priority
    order could still fire the dangerous verdict in production. Empty list if no
    comparable images exist on either side for any candidate room."""
    cfg = _LANES[lane]
    if cfg.fixed_room_type is not None:
        pa = _room_images(conn, a, cfg.fixed_room_type, cfg.limit_per_side)
        pb = _room_images(conn, b, cfg.fixed_room_type, cfg.limit_per_side)
        return [(cfg.fixed_room_type, pa, pb)] if pa and pb else []
    out: list[tuple[str, list[str], list[str]]] = []
    for room in FULL_PRIORITY:
        if len(out) >= max_attempts:
            break
        pa = _room_images(conn, a, room, cfg.limit_per_side)
        pb = _room_images(conn, b, room, cfg.limit_per_side)
        if pa and pb:
            out.append((room, pa, pb))
    return out


# --- historical (recall) case sourcing --------------------------------------
# One query per lane (shapes differ: compare is per-room, floor/site are per-pair) but
# all return the SAME (a, b, room_type, expected_verdict) shape so run_lane_recall_ab
# can stay lane-agnostic.

def _compare_recall_cases(conn: Any, *, prod_model: str, limit: int) -> list[tuple[int, int, str, str]]:
    # Sample the MOST RECENT High verdicts whose images still exist on BOTH sides. The
    # naive ORDER BY sreality_id puts negative (foreign-portal) ids first — mostly
    # long-delisted listings whose R2 images are purged — so recency + an
    # images-present guard makes the corpus evaluable (2026-06 harness fix).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sreality_id_a, sreality_id_b, room_type "
            "FROM listing_visual_matches v "
            "WHERE v.verdict = 'High' AND v.model = %s "
            "  AND EXISTS (SELECT 1 FROM images i "
            "              WHERE i.sreality_id = v.sreality_id_a AND i.storage_path IS NOT NULL) "
            "  AND EXISTS (SELECT 1 FROM images i "
            "              WHERE i.sreality_id = v.sreality_id_b AND i.storage_path IS NOT NULL) "
            "GROUP BY sreality_id_a, sreality_id_b, room_type "
            "ORDER BY max(v.created_at) DESC LIMIT %s",
            (prod_model, limit),
        )
        return [(a, b, room, "High") for a, b, room in cur.fetchall()]


def _floor_plan_recall_cases(conn: Any, *, prod_model: str, limit: int) -> list[tuple[int, int, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sreality_id_a, sreality_id_b, verdict "
            "FROM listing_floor_plan_matches v "
            "WHERE v.verdict IN ('same_layout', 'different_layout') AND v.model = %s "
            "  AND EXISTS (SELECT 1 FROM images i "
            "              WHERE i.sreality_id = v.sreality_id_a AND i.storage_path IS NOT NULL) "
            "  AND EXISTS (SELECT 1 FROM images i "
            "              WHERE i.sreality_id = v.sreality_id_b AND i.storage_path IS NOT NULL) "
            "ORDER BY v.created_at DESC LIMIT %s",
            (prod_model, limit),
        )
        return [(a, b, FLOOR_PLAN_ROOM_TYPE, verdict) for a, b, verdict in cur.fetchall()]


def _site_plan_recall_cases(conn: Any, *, prod_model: str, limit: int) -> list[tuple[int, int, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sreality_id_a, sreality_id_b, verdict "
            "FROM listing_site_plan_matches v "
            "WHERE v.verdict IN ('same_unit', 'different_unit') AND v.model = %s "
            "  AND EXISTS (SELECT 1 FROM images i "
            "              WHERE i.sreality_id = v.sreality_id_a AND i.storage_path IS NOT NULL) "
            "  AND EXISTS (SELECT 1 FROM images i "
            "              WHERE i.sreality_id = v.sreality_id_b AND i.storage_path IS NOT NULL) "
            "ORDER BY v.created_at DESC LIMIT %s",
            (prod_model, limit),
        )
        return [(a, b, SITE_PLAN_ROOM_TYPE, verdict) for a, b, verdict in cur.fetchall()]


_RECALL_CASE_FNS = {
    "compare": _compare_recall_cases,
    "floor_plan": _floor_plan_recall_cases,
    "site_plan": _site_plan_recall_cases,
}


# --- golden-set (precision) case sourcing -----------------------------------
# Reads a FROZEN snapshot only (scripts/build_dedup_golden_set.py), never the live
# dedup_label_events view — see that script's docstring for why (the view recomputes
# and grows every day; two benchmark runs against it aren't comparable). Golden
# POSITIVES are deliberately NOT separately queried here: the historical recall
# sampling above already exercises both directions for floor_plan/site_plan
# (same_layout AND different_layout / same_unit AND different_unit come from the
# SAME query), so a second positive source would mostly duplicate that coverage for
# more $ spent, not add a new dimension.

def _golden_negative_cases(
    conn: Any, *, set_name: str, limit: int,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT label_id, left_listing_id, right_listing_id, category_main, label_source "
            "FROM dedup_golden_sets "
            "WHERE set_name = %(set_name)s AND is_same = false "
            "  AND left_listing_id IS NOT NULL AND right_listing_id IS NOT NULL "
            "ORDER BY labeled_at DESC NULLS LAST LIMIT %(limit)s",
            {"set_name": set_name, "limit": limit},
        )
        return [
            {
                "label_id": r[0], "a": int(r[1]), "b": int(r[2]),
                "category_main": r[3], "label_source": r[4],
            }
            for r in cur.fetchall()
        ]


# --- lane runners ------------------------------------------------------------

def _recall_is_same(lane: str, expected: str) -> bool | None:
    """Ground-truth PROXY for a recall pair, derived from the PROD-MODEL cached verdict (NOT
    operator-confirmed): compare High => same; plan same_* => same, different_* => different.
    Used only to colour the explorer page; recall pairs are labeled by check_type='recall' so
    the page never presents this as gold."""
    del lane
    if expected in ("High", "same_layout", "same_unit"):
        return True
    if expected in ("different_layout", "different_unit"):
        return False
    return None


def _make_persister(conn: Any, *, run_label: str, set_name: str, model: str) -> Any:
    """Best-effort writer of one dedup_vision_bakeoff_results row per (pair, lane) evaluation
    (migration 303) — the per-pair matrix behind /model-testing. conn is autocommit, so each
    row commits immediately (crash-resilient); a persist failure logs and is swallowed so it
    never fails the benchmark (mirrors the llm_calls best-effort recording pattern)."""
    def _persist(**row: Any) -> None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dedup_vision_bakeoff_results "
                    "(run_label, set_name, check_type, lane, model, sreality_id_a, sreality_id_b, "
                    " room_type, is_same, label_source, category_main, expected_verdict, "
                    " danger_verdict, candidate_verdict, is_correct, is_dangerous, cost_usd) "
                    "VALUES (%(run_label)s, %(set_name)s, %(check_type)s, %(lane)s, %(model)s, "
                    " %(a)s, %(b)s, %(room_type)s, %(is_same)s, %(label_source)s, %(category_main)s, "
                    " %(expected_verdict)s, %(danger_verdict)s, %(candidate_verdict)s, "
                    " %(is_correct)s, %(is_dangerous)s, %(cost_usd)s)",
                    {"run_label": run_label, "set_name": set_name, "model": model, **row},
                )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            LOG.warning("persist failed (%s pair %s/%s): %s",
                        row.get("lane"), row.get("a"), row.get("b"), exc)
    return _persist


def run_lane_recall_ab(
    conn: Any, llm: Any, r2: Any, *, lane: str, candidate_model: str, max_edge: int,
    prod_model: str, limit: int, persist: Any = None,
) -> tuple[int, int, float, list[str]]:
    """Replay historical decisive verdicts; return (still_matching, evaluated, cost_usd, misses)."""
    cfg = _LANES[lane]
    cases = _RECALL_CASE_FNS[lane](conn, prod_model=prod_model, limit=limit)
    ok = evaluated = 0
    cost_total = 0.0
    misses: list[str] = []
    for a, b, room_type, expected in cases:
        paths_a = _room_images(conn, a, room_type, cfg.limit_per_side)
        paths_b = _room_images(conn, b, room_type, cfg.limit_per_side)
        if not paths_a or not paths_b:
            LOG.info("%s recall skip a=%s b=%s room=%s (images gone)", lane, a, b, room_type)
            continue
        try:
            verdict, cost = _call_lane(
                conn, llm, r2, lane=lane, candidate_model=candidate_model,
                max_edge=max_edge, paths_a=paths_a, paths_b=paths_b,
            )
        except Exception as exc:  # noqa: BLE001
            if _is_infra_error(exc):
                raise _InfraAbort(f"{lane} recall a={a} b={b} room={room_type}: {exc}") from exc
            LOG.warning("%s recall a=%s b=%s room=%s failed (counted neither): %s", lane, a, b, room_type, exc)
            continue
        evaluated += 1
        cost_total += cost
        if verdict == expected:
            ok += 1
        else:
            misses.append(f"{a}/{b} room={room_type}: {expected} -> {verdict}")
        if persist is not None:
            persist(
                check_type="recall", lane=lane, a=a, b=b, room_type=room_type,
                is_same=_recall_is_same(lane, expected), label_source=None, category_main=None,
                expected_verdict=expected, danger_verdict=cfg.danger_verdict,
                candidate_verdict=verdict, is_correct=(verdict == expected),
                is_dangerous=(verdict == cfg.danger_verdict), cost_usd=round(cost, 6),
            )
    return ok, evaluated, cost_total, misses


def run_lane_precision_ab(
    conn: Any, llm: Any, r2: Any, *, lane: str, candidate_model: str, max_edge: int,
    golden_set_name: str, limit: int, persist: Any = None,
) -> tuple[int, int, float, list[str]]:
    """Replay confirmed-DIFFERENT golden pairs; return (safe, evaluated, cost_usd, unsafe).

    Walks up to `_MAX_ROOM_ATTEMPTS` candidate rooms per pair (compare lane only — floor/
    site_plan have exactly one candidate), stopping at the FIRST dangerous verdict, exactly
    like the engine's real stop-at-first-High OR-gate: if room 1 is safe but room 3 would
    fire High, production reaches room 3 and merges, so a precision check that quit after
    room 1 would UNDERSTATE risk. `evaluated`/`safe` are PAIR-level (one outcome per golden
    pair, not per room attempt); cost accumulates every room call actually made. Persists ONE
    decisive row per pair (the danger room if any fired, else the last room evaluated).
    """
    cfg = _LANES[lane]
    cases = _golden_negative_cases(conn, set_name=golden_set_name, limit=limit)
    safe = evaluated = 0
    cost_total = 0.0
    unsafe: list[str] = []
    for case in cases:
        a, b = case["a"], case["b"]
        candidates = _candidate_rooms(conn, lane, a, b)
        if not candidates:
            LOG.info(
                "%s precision skip a=%s b=%s src=%s (no %s images on both sides)",
                lane, a, b, case["label_source"], cfg.unit,
            )
            continue
        pair_evaluated = False
        pair_unsafe_detail: str | None = None
        pair_cost = 0.0
        decisive_room: str | None = None
        decisive_verdict: str | None = None
        for room_type, paths_a, paths_b in candidates:
            try:
                verdict, cost = _call_lane(
                    conn, llm, r2, lane=lane, candidate_model=candidate_model,
                    max_edge=max_edge, paths_a=paths_a, paths_b=paths_b,
                )
            except Exception as exc:  # noqa: BLE001
                if _is_infra_error(exc):
                    raise _InfraAbort(f"{lane} precision a={a} b={b} room={room_type}: {exc}") from exc
                LOG.warning(
                    "%s precision a=%s b=%s room=%s failed (trying next candidate room): %s",
                    lane, a, b, room_type, exc,
                )
                continue
            pair_evaluated = True
            cost_total += cost
            pair_cost += cost
            decisive_room, decisive_verdict = room_type, verdict
            if verdict == cfg.danger_verdict:
                pair_unsafe_detail = (
                    f"{a}/{b} room={room_type} src={case['label_source']}: "
                    f"{verdict} (DANGEROUS on a confirmed-different pair, reason={case['label_source']})"
                )
                break  # stop-at-first-danger, mirrors the engine's stop-at-first-High
        if not pair_evaluated:
            continue
        evaluated += 1
        is_dangerous = pair_unsafe_detail is not None
        if is_dangerous:
            unsafe.append(pair_unsafe_detail)  # type: ignore[arg-type]
        else:
            safe += 1
        if persist is not None:
            persist(
                check_type="precision", lane=lane, a=a, b=b, room_type=decisive_room,
                is_same=False, label_source=case["label_source"], category_main=case["category_main"],
                expected_verdict=None, danger_verdict=cfg.danger_verdict,
                candidate_verdict=decisive_verdict, is_correct=(not is_dangerous),
                is_dangerous=is_dangerous, cost_usd=round(pair_cost, 6),
            )
    return safe, evaluated, cost_total, unsafe


def _review_cases(conn: Any, set_name: str, limit: int) -> list[dict[str, Any]]:
    """Pairs for one /dedup 'compare all models' snapshot (dedup_model_compare_sets, migration 304)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sreality_id_a, sreality_id_b, category_main FROM dedup_model_compare_sets "
            "WHERE run_label = %s ORDER BY id LIMIT %s",
            (set_name, limit),
        )
        return [{"a": int(r[0]), "b": int(r[1]), "category_main": r[2]} for r in cur.fetchall()]


def run_review_set(
    conn: Any, llm: Any, r2: Any, *, lanes: list[str], candidate_model: str,
    max_edge_for: dict[str, int], set_name: str, persist: Any, limit: int,
) -> tuple[int, float]:
    """Score one candidate model on every snapshot pair × lane and persist check_type='review' rows —
    the operator's decision-support 'jury poll'. UNLIKE recall/precision there is NO ground truth:
    each row just records the model's verdict + whether it VOTED TO MERGE (is_dangerous). Walks rooms
    stop-at-first-merge exactly like the engine's OR-gate (compare); one plan set for floor/site.
    Returns (pairs_with_any_lane_evaluated, cost_usd)."""
    cases = _review_cases(conn, set_name, limit)
    total_cost = 0.0
    evaluated_pairs = 0
    for case in cases:
        a, b = case["a"], case["b"]
        any_lane = False
        for lane in lanes:
            cfg = _LANES[lane]
            candidates = _candidate_rooms(conn, lane, a, b)
            if not candidates:
                continue
            decisive_room: str | None = None
            decisive_verdict: str | None = None
            merged = False
            lane_cost = 0.0
            for room_type, paths_a, paths_b in candidates:
                try:
                    verdict, cost = _call_lane(
                        conn, llm, r2, lane=lane, candidate_model=candidate_model,
                        max_edge=max_edge_for[lane], paths_a=paths_a, paths_b=paths_b,
                    )
                except Exception as exc:  # noqa: BLE001
                    if _is_infra_error(exc):
                        raise _InfraAbort(f"review {lane} a={a} b={b} room={room_type}: {exc}") from exc
                    LOG.warning("review %s a=%s b=%s room=%s failed (next room): %s", lane, a, b, room_type, exc)
                    continue
                total_cost += cost
                lane_cost += cost
                decisive_room, decisive_verdict = room_type, verdict
                if verdict == cfg.danger_verdict:
                    merged = True
                    break
            if decisive_verdict is None:
                continue
            any_lane = True
            if persist is not None:
                persist(
                    check_type="review", lane=lane, a=a, b=b, room_type=decisive_room,
                    is_same=None, label_source="review", category_main=case["category_main"],
                    expected_verdict=None, danger_verdict=cfg.danger_verdict,
                    candidate_verdict=decisive_verdict, is_correct=None,
                    is_dangerous=merged, cost_usd=round(lane_cost, 6),
                )
        if any_lane:
            evaluated_pairs += 1
    return evaluated_pairs, total_cost


def run_classify_ab(
    conn: Any, llm: Any, r2: Any, *,
    candidate_model: str, max_edge: int, prod_model: str, sample: int,
) -> tuple[int, int]:
    """Re-classify a sample; return (agreeing_labels, total_labels)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT i.sreality_id FROM images i "
            "JOIN image_room_classifications c ON c.image_id = i.id "
            "WHERE c.model = %s ORDER BY i.sreality_id LIMIT %s",
            (prod_model, sample),
        )
        sids = [r[0] for r in cur.fetchall()]

    agree = 0
    total = 0
    for sid in sids:
        imgs = _listing_images(conn, sid, 12)  # mirrors classify_listing_images n_images default
        if not imgs:
            continue
        prod = _prod_labels(conn, [i for i, _ in imgs], prod_model)
        if not prod:
            continue
        content: list[dict[str, Any]] = [
            {"type": "text", "text": f"{len(imgs)} listing images, in order (index 0..N):"}
        ]
        for idx, (_, path) in enumerate(imgs):
            content.append({"type": "text", "text": f"Image index {idx}:"})
            content.append(image_block(r2, path, max_edge))
        try:
            resp = llm.call(
                called_for="classify_listing_images",
                messages=[{"role": "user", "content": content}],
                system=llm.resolve_system_prompt(ic._PROMPT_KEY),
                tools=[ic.RECORD_ROOM_TYPES_TOOL],
                model=candidate_model,
                provider=_provider_for(candidate_model),
            )
            rooms = ic._extract_rooms(resp.tool_calls)
        except Exception as exc:  # noqa: BLE001
            if _is_infra_error(exc):
                raise _InfraAbort(f"classify sid={sid}: {exc}") from exc
            LOG.warning("classify sid=%s failed (skipped): %s", sid, exc)
            continue
        cand = {}
        for entry in rooms:
            i = entry.get("index")
            if isinstance(i, int) and 0 <= i < len(imgs):
                cand[imgs[i][0]] = entry.get("room_type")
        for image_id, prod_rt in prod.items():
            if image_id in cand:
                total += 1
                if cand[image_id] == prod_rt:
                    agree += 1
    return agree, total


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate-model", default="claude-haiku-4-5")
    ap.add_argument("--max-edge", type=int, default=COMPARISON_MAX_EDGE, help="Compare-lane image edge (px).")
    ap.add_argument("--plan-max-edge", type=int, default=DOCUMENT_MAX_EDGE, help="Floor/site-plan image edge (px).")
    ap.add_argument(
        "--lanes", default="compare,floor_plan,site_plan",
        help="Comma-separated subset of compare,floor_plan,site_plan (or 'all').",
    )
    ap.add_argument("--compare-limit", type=int, default=200)
    ap.add_argument("--floor-plan-limit", type=int, default=60)
    ap.add_argument("--site-plan-limit", type=int, default=60)
    ap.add_argument(
        "--golden-set-name", default=None,
        help="Frozen dedup_golden_sets snapshot to replay negatives from (scripts/build_dedup_golden_set.py). "
             "Precision checks are SKIPPED (loudly) if omitted.",
    )
    ap.add_argument("--precision-limit", type=int, default=50, help="Golden-negative sample size, PER LANE.")
    ap.add_argument("--classify-sample", type=int, default=40)
    ap.add_argument("--min-compare-recall", type=float, default=1.0)
    ap.add_argument("--min-floor-plan-recall", type=float, default=1.0)
    ap.add_argument("--min-site-plan-recall", type=float, default=1.0)
    ap.add_argument("--min-precision", type=float, default=1.0, help="Applies to every lane that ran a precision check.")
    ap.add_argument("--min-classify-agreement", type=float, default=0.85)
    ap.add_argument("--skip-classify", action="store_true")
    ap.add_argument(
        "--persist-results", action="store_true",
        help="Write one dedup_vision_bakeoff_results row per (pair, lane) evaluation (migration 303), "
             "for the /model-testing explorer. Requires --run-label.",
    )
    ap.add_argument(
        "--run-label", default=None,
        help="Groups this run's persisted rows on the explorer (e.g. '2026-07-13-session3'). "
             "Required with --persist-results; re-using a label appends (delete old rows first to replace).",
    )
    ap.add_argument(
        "--review-set-name", default=None,
        help="REVIEW MODE (decision support): score the candidate model on the pair snapshot in "
             "dedup_model_compare_sets under this label and persist check_type='review' rows (no "
             "ground truth — just each model's would-merge vote). Implies --persist-results with "
             "--run-label defaulting to this name. Skips the golden-set recall/precision flow.",
    )
    ap.add_argument("--review-limit", type=int, default=200, help="Max snapshot pairs to score in review mode.")
    args = ap.parse_args()

    if args.review_set_name:
        # Review mode always persists (that IS its output) under the set name unless a run_label overrides.
        args.persist_results = True
        args.run_label = args.run_label or args.review_set_name
    if args.persist_results and not args.run_label:
        LOG.error("--persist-results requires --run-label.")
        return 2

    lanes = list(_LANES) if args.lanes == "all" else [s.strip() for s in args.lanes.split(",") if s.strip()]
    unknown = [lane for lane in lanes if lane not in _LANES]
    if unknown:
        LOG.error("unknown lane(s) %s; choose from %s", unknown, list(_LANES))
        return 2

    # Build the LLMClient without api.dependencies (which pulls in FastAPI) — mirrors
    # scripts.backfill_condition_scores so the harness runs under the minimal scoring deps.
    from api.llm_client import LLMClient
    from api.providers.anthropic import AnthropicProvider
    from api.providers.gemini import GeminiProvider
    from api.providers.openai import OpenAIProvider
    from api.providers.qwen import QwenProvider

    if not image_storage.is_configured():
        LOG.error("R2 is not configured; cannot fetch image bytes. Aborting.")
        return 2

    if args.golden_set_name is None:
        LOG.warning(
            "--golden-set-name not set: PRECISION checks (does the candidate avoid the "
            "dangerous verdict on a CONFIRMED-DIFFERENT pair) are SKIPPED for every lane. "
            "Recall-only numbers are NOT sufficient to recommend a model — see "
            "scripts/build_dedup_golden_set.py to freeze a snapshot first."
        )

    conn = db.connect()
    try:
        r2 = image_storage.R2Client.from_env()
        llm = LLMClient(conn, providers={
            "anthropic": AnthropicProvider(),
            "gemini": GeminiProvider(),
            "openai": OpenAIProvider(),
            "qwen": QwenProvider(),
        })
        prod_compare_model = llm.resolve_model(vm._MODEL_KEY)
        prod_floor_plan_model = llm.resolve_model(vm._FLOOR_PLAN_MODEL_KEY)
        prod_site_plan_model = llm.resolve_model(vm._SITE_PLAN_MODEL_KEY)
        prod_classify_model = llm.resolve_model(ic._MODEL_KEY)
        prod_model_for = {
            "compare": prod_compare_model,
            "floor_plan": prod_floor_plan_model,
            "site_plan": prod_site_plan_model,
        }
        recall_limit_for = {
            "compare": args.compare_limit,
            "floor_plan": args.floor_plan_limit,
            "site_plan": args.site_plan_limit,
        }
        min_recall_for = {
            "compare": args.min_compare_recall,
            "floor_plan": args.min_floor_plan_recall,
            "site_plan": args.min_site_plan_recall,
        }
        max_edge_for = {
            "compare": args.max_edge,
            "floor_plan": args.plan_max_edge,
            "site_plan": args.plan_max_edge,
        }

        # --- REVIEW MODE (decision support) — a distinct, early-return path ---
        # Score this one model on a /dedup 'compare all models' snapshot; no golden set, no
        # recall/precision, no classify. The verdict grid on /model-testing is the deliverable.
        if args.review_set_name:
            review_persist = _make_persister(
                conn, run_label=args.run_label, set_name=args.review_set_name,
                model=args.candidate_model,
            )
            LOG.info(
                "REVIEW candidate=%s lanes=%s set=%s (persist run_label=%s)",
                args.candidate_model, lanes, args.review_set_name, args.run_label,
            )
            try:
                pairs_n, cost = run_review_set(
                    conn, llm, r2, lanes=lanes, candidate_model=args.candidate_model,
                    max_edge_for=max_edge_for, set_name=args.review_set_name,
                    persist=review_persist, limit=args.review_limit,
                )
            except _InfraAbort as exc:
                print(
                    f"\n=== MODEL REVIEW RESULT ===\ncandidate: {args.candidate_model}\n"
                    f"verdict: INCONCLUSIVE — API/account error: {exc}\n"
                )
                return 3
            print(
                f"\n=== MODEL REVIEW RESULT ===\n"
                f"candidate: {args.candidate_model}  set: {args.review_set_name}\n"
                f"pairs scored: {pairs_n}   cost: ${cost:.4f}\n"
                f"Persisted as check_type='review' — see /model-testing run '{args.run_label}'.\n"
            )
            return 0

        LOG.info(
            "A/B candidate model=%s lanes=%s vs prod compare=%s floor_plan=%s site_plan=%s classify=%s",
            args.candidate_model, lanes, prod_compare_model, prod_floor_plan_model,
            prod_site_plan_model, prod_classify_model,
        )

        persist = None
        if args.persist_results:
            persist = _make_persister(
                conn, run_label=args.run_label,
                set_name=args.golden_set_name or "recall_only",
                model=args.candidate_model,
            )
            LOG.info("persisting per-pair results to dedup_vision_bakeoff_results run_label=%s", args.run_label)

        total_cost = 0.0
        lane_results: dict[str, dict[str, Any]] = {}
        try:
            for lane in lanes:
                recall_ok, recall_n, recall_cost, misses = run_lane_recall_ab(
                    conn, llm, r2, lane=lane, candidate_model=args.candidate_model,
                    max_edge=max_edge_for[lane], prod_model=prod_model_for[lane],
                    limit=recall_limit_for[lane], persist=persist,
                )
                total_cost += recall_cost
                recall = (recall_ok / recall_n) if recall_n else None
                LOG.info(
                    "%s RECALL %d/%d = %s (cost $%.4f)", lane, recall_ok, recall_n,
                    f"{100 * recall:.1f}%" if recall is not None else "n/a (nothing evaluable)",
                    recall_cost,
                )
                for m in misses:
                    LOG.warning("%s RECALL miss: %s", lane, m)

                precision = None
                precision_ok = precision_n = 0
                unsafe: list[str] = []
                if args.golden_set_name is not None:
                    precision_ok, precision_n, prec_cost, unsafe = run_lane_precision_ab(
                        conn, llm, r2, lane=lane, candidate_model=args.candidate_model,
                        max_edge=max_edge_for[lane], golden_set_name=args.golden_set_name,
                        limit=args.precision_limit, persist=persist,
                    )
                    total_cost += prec_cost
                    precision = (precision_ok / precision_n) if precision_n else None
                    LOG.info(
                        "%s PRECISION %d/%d = %s (cost $%.4f)", lane, precision_ok, precision_n,
                        f"{100 * precision:.1f}%" if precision is not None else "n/a (nothing evaluable)",
                        prec_cost,
                    )
                    for u in unsafe:
                        LOG.warning("%s PRECISION unsafe: %s", lane, u)

                lane_results[lane] = {
                    "recall": recall, "recall_n": recall_n,
                    "precision": precision, "precision_n": precision_n,
                }

            agreement = 1.0
            classify_total = 1
            if not args.skip_classify:
                agree, classify_total = run_classify_ab(
                    conn, llm, r2,
                    candidate_model=args.candidate_model, max_edge=args.max_edge,
                    prod_model=prod_classify_model, sample=args.classify_sample,
                )
                agreement = (agree / classify_total) if classify_total else 1.0
                LOG.info("CLASSIFY agreement %d/%d = %.1f%%", agree, classify_total, 100 * agreement)
        except _InfraAbort as exc:
            print(
                f"\n=== VISION A/B RESULT ===\n"
                f"candidate model: {args.candidate_model}\n"
                f"verdict: INCONCLUSIVE — API/account error (credit, quota, rate or auth):\n"
                f"  {exc}\n"
                f"spent so far: ${total_cost:.4f}\n"
                f"Top up / fix the key and re-run. No conclusion can be drawn from a partial "
                f"run (API errors are NOT recall/precision misses).\n"
            )
            return 3

        if not any(r["recall_n"] or r["precision_n"] for r in lane_results.values()):
            print(
                "\n=== VISION A/B RESULT ===\n"
                "verdict: INCONCLUSIVE — nothing was evaluable on any lane "
                "(none reproducible / no images / empty golden set). Nothing to conclude.\n"
            )
            return 3

        lines = [
            "\n=== VISION A/B RESULT ===",
            f"candidate model: {args.candidate_model}  "
            f"(compare@{args.max_edge}px, plans@{args.plan_max_edge}px)",
        ]
        overall_ok = True
        for lane in lanes:
            r = lane_results[lane]
            recall_gate = min_recall_for[lane]
            ok_recall = r["recall"] is not None and r["recall"] >= recall_gate
            recall_txt = (
                f"{100 * r['recall']:.1f}% ({r['recall_n']} eval, gate >= {100 * recall_gate:.0f}%) "
                f"{'PASS' if ok_recall else 'FAIL'}"
                if r["recall"] is not None else f"n/a ({r['recall_n']} eval)"
            )
            lines.append(f"{lane:11s} recall:    {recall_txt}")
            if r["recall"] is not None:
                overall_ok = overall_ok and ok_recall

            if r["precision"] is not None:
                ok_prec = r["precision"] >= args.min_precision
                lines.append(
                    f"{lane:11s} precision: {100 * r['precision']:.1f}% "
                    f"({r['precision_n']} eval, gate >= {100 * args.min_precision:.0f}%) "
                    f"{'PASS' if ok_prec else 'FAIL'}"
                )
                overall_ok = overall_ok and ok_prec
            else:
                lines.append(f"{lane:11s} precision: SKIPPED (no --golden-set-name / nothing evaluable)")

        ok_classify = args.skip_classify or agreement >= args.min_classify_agreement
        lines.append(
            f"classify    agree:     {100 * agreement:.1f}% (gate >= {100 * args.min_classify_agreement:.0f}%) "
            f"{'PASS' if ok_classify else 'FAIL'}"
        )
        overall_ok = overall_ok and ok_classify
        lines.append(f"total cost this run: ${total_cost:.4f}")
        lines.append(f"verdict: {'ADOPT — every configured gate cleared' if overall_ok else 'DO NOT ADOPT AS-IS'}")
        print("\n".join(lines) + "\n")
        return 0 if overall_ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
