"""Convene the vision models on undecided dedup pairs (decision support).

The operator, mid-review on a pair the engine couldn't decide, clicks "compare all models" on
/dedup. This:
  1. snapshots the exact undecided pair(s) into `dedup_model_compare_sets` under a fresh run_label
     (resolving each property to a representative listing that actually has images);
  2. dispatches `dedup_model_compare.yml` once per connected model — they run in parallel and each
     writes `check_type='review'` rows to `dedup_vision_bakeoff_results` (migration 303/304);
  3. returns the run_label so the UI can deep-link to /model-testing, where the jury's votes land.

No production dedup path is touched — this only produces benchmark rows. Dispatch needs a GitHub PAT
(`GH_DISPATCH_TOKEN`, Actions read+write) on the API service, exactly like `price_stats.run_dataset_now`;
503 with a setup hint if unset.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import requests
from fastapi import HTTPException

if TYPE_CHECKING:
    import psycopg

LOG = logging.getLogger(__name__)

# The jury: the trusted Sonnet baseline + the four benchmarked cheap candidates. Each id must be
# routable by scripts.validate_vision_models._provider_for and priced in its provider's PRICES.
MODELS_ALL: tuple[str, ...] = (
    "claude-sonnet-4-5",
    "gpt-5-mini",
    "qwen3-vl-235b-a22b-instruct",
    "qwen3-vl-30b-a3b-instruct",
    "gemini-3.1-flash-lite",
)

_DISPATCH_WORKFLOW = "dedup_model_compare.yml"

# One SQL snapshots the chosen candidates into dedup_model_compare_sets, resolving each property side
# to a representative listing that HAS downloaded images (a pair with no images on a side can't be
# compared, so the LATERAL joins drop it). `%(ids)s` NULL = "the oldest-undecided top-N"; a non-NULL
# array = exactly those candidate rows (the per-card button).
_SNAPSHOT_SQL = """
    INSERT INTO dedup_model_compare_sets
        (run_label, sreality_id_a, sreality_id_b, left_property_id, right_property_id,
         category_main, candidate_id)
    SELECT %(run_label)s, la.sid, lb.sid, c.left_property_id, c.right_property_id,
           pl.category_main, c.id
    FROM (
        SELECT id, left_property_id, right_property_id
        FROM property_identity_candidates
        WHERE status = 'proposed'
          AND (%(ids)s::bigint[] IS NULL OR id = ANY(%(ids)s::bigint[]))
        ORDER BY last_engine_decision_at ASC NULLS FIRST, id ASC
        LIMIT %(limit)s
    ) c
    JOIN properties pl ON pl.id = c.left_property_id
    CROSS JOIN LATERAL (
        SELECT l.sreality_id AS sid FROM listings l
        WHERE l.property_id = c.left_property_id
          AND EXISTS (SELECT 1 FROM images i
                      WHERE i.sreality_id = l.sreality_id AND i.storage_path IS NOT NULL)
        ORDER BY l.sreality_id LIMIT 1
    ) la
    CROSS JOIN LATERAL (
        SELECT l.sreality_id AS sid FROM listings l
        WHERE l.property_id = c.right_property_id
          AND EXISTS (SELECT 1 FROM images i
                      WHERE i.sreality_id = l.sreality_id AND i.storage_path IS NOT NULL)
        ORDER BY l.sreality_id LIMIT 1
    ) lb
    ON CONFLICT (run_label, sreality_id_a, sreality_id_b) DO NOTHING
    RETURNING sreality_id_a
"""


def _now_label() -> str:
    return f"review-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"


def _snapshot_pairs(
    conn: "psycopg.Connection", *, run_label: str, candidate_ids: list[int] | None, limit: int,
) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            _SNAPSHOT_SQL,
            {"run_label": run_label, "ids": candidate_ids, "limit": limit},
        )
        return len(cur.fetchall())


def _dispatch_one(token: str, repo: str, *, candidate_model: str, run_label: str) -> None:
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{_DISPATCH_WORKFLOW}/dispatches"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"ref": "main", "inputs": {"candidate_model": candidate_model, "run_label": run_label}},
        timeout=15,
    )
    if resp.status_code not in (201, 204):
        raise HTTPException(
            502, f"GitHub dispatch rejected for {candidate_model} ({resp.status_code}): {resp.text[:200]}"
        )


def compare_models(
    conn: "psycopg.Connection",
    *,
    candidate_ids: list[int] | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Snapshot undecided pairs + dispatch every model against them. `candidate_ids` = a specific set
    (the per-card button); None = the oldest-undecided top-`limit` (the queue-level button)."""
    limit = max(1, min(limit, 200))
    run_label = _now_label()
    pair_count = _snapshot_pairs(conn, run_label=run_label, candidate_ids=candidate_ids, limit=limit)
    if pair_count == 0:
        raise HTTPException(
            404,
            "No comparable undecided pairs found (none 'proposed', or none with downloaded images "
            "on both sides). Nothing dispatched.",
        )

    token = os.environ.get("GH_DISPATCH_TOKEN")
    if not token:
        raise HTTPException(
            503,
            "GH_DISPATCH_TOKEN is not configured on the API service, so model runs can't be "
            "triggered from the UI. Set a fine-grained GitHub PAT (repo Actions: read+write) as "
            f"GH_DISPATCH_TOKEN. The snapshot '{run_label}' ({pair_count} pairs) is saved and can be "
            "scored by dispatching dedup_model_compare.yml manually.",
        )
    repo = os.environ.get("GH_REPO", "waiff/sreality")
    dispatched: list[str] = []
    for model in MODELS_ALL:
        try:
            _dispatch_one(token, repo, candidate_model=model, run_label=run_label)
            dispatched.append(model)
        except HTTPException:
            raise
        except requests.RequestException as exc:
            raise HTTPException(502, f"GitHub dispatch request failed for {model}: {exc}") from exc

    return {
        "dispatched": True,
        "run_label": run_label,
        "pair_count": pair_count,
        "models": dispatched,
        "model_testing_url": f"/model-testing?run={run_label}",
        "run_url": f"https://github.com/{repo}/actions/workflows/{_DISPATCH_WORKFLOW}",
    }
