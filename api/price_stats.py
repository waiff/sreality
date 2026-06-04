"""Write + read handlers for /price-stats (ceny-nemovitosti datasets).

Dataset CRUD goes through the bearer-gated API (no write path from the
browser). Ingestion itself runs in GitHub Actions — creating a dataset here
makes it `is_active`, and the next scheduled `scrape_price_stats` run (or a
manual dispatch) populates it; the UI shows "queued until next run" until
metrics land. The read handlers wrap `toolkit.price_stats` for API/ClickUp
consumers; the SPA reads the `*_public` views directly.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import psycopg
import requests
from fastapi import HTTPException
from psycopg.rows import dict_row

from api import schemas as s
from toolkit import price_stats as analysis

if TYPE_CHECKING:
    pass

# Workflow we dispatch for an on-demand / expand scrape of one dataset.
_DISPATCH_WORKFLOW = "scrape_price_stats.yml"

_COLS = (
    "id, slug, name, description, category_main_cb, building_condition, "
    "building_type, ownership, usable_area_from, usable_area_to, distance, "
    "is_active, created_at, updated_at, start_ym, end_ym, obec_ids, "
    "min_population, max_population"
)


def create_dataset(conn: "psycopg.Connection", body: s.PriceStatDatasetIn) -> dict[str, Any]:
    try:
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                INSERT INTO price_stat_datasets (
                    slug, name, description, category_main_cb, building_condition,
                    building_type, ownership, usable_area_from, usable_area_to,
                    distance, start_ym, end_ym, obec_ids, min_population,
                    max_population, created_by
                ) VALUES (
                    %(slug)s, %(name)s, %(description)s, %(category_main_cb)s,
                    %(building_condition)s, %(building_type)s, %(ownership)s,
                    %(usable_area_from)s, %(usable_area_to)s, %(distance)s,
                    %(start_ym)s, %(end_ym)s, %(obec_ids)s, %(min_population)s,
                    %(max_population)s, 'api'
                ) RETURNING {_COLS}
                """,
                body.model_dump(),
            )
            return cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"dataset slug '{body.slug}' already exists")


def list_datasets(conn: "psycopg.Connection", *, include_inactive: bool = False) -> dict[str, Any]:
    where = "" if include_inactive else "WHERE is_active"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT {_COLS} FROM price_stat_datasets {where} ORDER BY name")
        rows = cur.fetchall()
    return {"datasets": rows}


def update_dataset(
    conn: "psycopg.Connection", dataset_id: int, body: s.PriceStatDatasetUpdateIn
) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "no fields to update")
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"UPDATE price_stat_datasets SET {sets}, updated_at = now() "
            f"WHERE id = %(id)s RETURNING {_COLS}",
            {**fields, "id": dataset_id},
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"dataset {dataset_id} not found")
    return row


def deactivate_dataset(conn: "psycopg.Connection", dataset_id: int) -> dict[str, Any]:
    """Soft-delete: flip is_active=false (no hard delete, per the data model)."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE price_stat_datasets SET is_active = false, updated_at = now() "
            "WHERE id = %s RETURNING id",
            (dataset_id,),
        )
        if cur.fetchone() is None:
            raise HTTPException(404, f"dataset {dataset_id} not found")
    return {"id": dataset_id, "is_active": False}


def dataset_summary(conn: "psycopg.Connection", dataset_id: int, window_years: int) -> dict[str, Any]:
    return analysis.dataset_summary(conn, dataset_id, window_years=window_years)


def dataset_city_metrics(conn: "psycopg.Connection", dataset_id: int) -> dict[str, Any]:
    return analysis.dataset_city_metrics(conn, dataset_id)


def dataset_city_series(
    conn: "psycopg.Connection", dataset_id: int, entity_type: str, entity_id: int
) -> dict[str, Any]:
    return analysis.dataset_city_series(conn, dataset_id, entity_type, entity_id)


def run_dataset_now(conn: "psycopg.Connection", dataset_id: int) -> dict[str, Any]:
    """Dispatch the scrape_price_stats workflow for one dataset (on-demand /
    expand). Needs a GitHub PAT (GH_DISPATCH_TOKEN, Actions: read+write) on the
    API service; 503 if unset so the operator gets a clear setup hint."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM price_stat_datasets WHERE id = %s", (dataset_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, f"dataset {dataset_id} not found")

    token = os.environ.get("GH_DISPATCH_TOKEN")
    if not token:
        raise HTTPException(
            503,
            "GH_DISPATCH_TOKEN is not configured on the API service, so runs "
            "can't be triggered from the UI. Set a fine-grained GitHub PAT "
            "(repo Actions: read+write) as GH_DISPATCH_TOKEN; the dataset will "
            "otherwise scrape on the next scheduled run.",
        )
    repo = os.environ.get("GH_REPO", "waiff/sreality")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{_DISPATCH_WORKFLOW}/dispatches"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": "main", "inputs": {"mode": "full", "dataset_id": str(dataset_id)}},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"GitHub dispatch request failed: {exc}")
    if resp.status_code not in (201, 204):
        raise HTTPException(
            502, f"GitHub dispatch rejected ({resp.status_code}): {resp.text[:300]}"
        )
    return {
        "dispatched": True,
        "run_url": f"https://github.com/{repo}/actions/workflows/{_DISPATCH_WORKFLOW}",
    }
