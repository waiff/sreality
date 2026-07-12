"""FastAPI routes for the broker outreach CRM (Phase 4).

Mounted under `/outreach/*`, admin-gated via `require_admin` — these are
operator write actions over PII (broker contacts + drafted messages).
Thin HTTP layer over `api.outreach`.
Sending is human-in-the-loop: the UI marks a message 'sent' after the operator
sends it manually (mailto/copy); there is no automated email send in v1.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import dependencies as deps
from api import outreach

router = APIRouter(prefix="/outreach", tags=["outreach"])


class CampaignCreate(BaseModel):
    name: str
    goal: str | None = None
    guidance: str | None = None
    target: dict[str, Any] | None = None


class CampaignUpdate(BaseModel):
    name: str | None = None
    goal: str | None = None
    guidance: str | None = None
    status: str | None = None
    target: dict[str, Any] | None = None


class MessageUpdate(BaseModel):
    status: str | None = None
    subject: str | None = None
    body: str | None = None
    notes: str | None = None


class SuppressionCreate(BaseModel):
    broker_id: int
    reason: str | None = None


@router.post("/campaigns")
def create_campaign(
    body: CampaignCreate,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return outreach.create_campaign(
        conn, name=body.name, goal=body.goal, guidance=body.guidance, target=body.target)


@router.get("/campaigns")
def list_campaigns(
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return {"campaigns": outreach.list_campaigns(conn)}


@router.get("/campaigns/{campaign_id}")
def get_campaign(
    campaign_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = outreach.get_campaign(conn, campaign_id)
    if result is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return result


@router.patch("/campaigns/{campaign_id}")
def update_campaign(
    campaign_id: int,
    body: CampaignUpdate,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = outreach.update_campaign(
        conn, campaign_id, name=body.name, goal=body.goal, guidance=body.guidance,
        status=body.status, target=body.target)
    if result is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return result


@router.get("/campaigns/{campaign_id}/targets")
def preview_targets(
    campaign_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    campaign = outreach.get_campaign(conn, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    targets = outreach.select_targets(
        conn, campaign.get("target") or {}, campaign_id=campaign_id, limit=limit)
    return {"targets": targets, "count": len(targets)}


@router.post("/campaigns/{campaign_id}/generate")
def generate_drafts(
    campaign_id: int,
    limit: int = Query(default=25, ge=1, le=200),
    conn: Any = Depends(deps.get_db_conn),
    llm_client: Any = Depends(deps.get_llm_client),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = outreach.generate_drafts(conn, llm_client, campaign_id, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return result


@router.get("/campaigns/{campaign_id}/messages")
def list_messages(
    campaign_id: int,
    status: str | None = None,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return {"messages": outreach.list_messages(conn, campaign_id, status=status)}


@router.patch("/messages/{message_id}")
def update_message(
    message_id: int,
    body: MessageUpdate,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = outreach.update_message(
        conn, message_id, status=body.status, subject=body.subject,
        body=body.body, notes=body.notes)
    if result is None:
        raise HTTPException(status_code=404, detail="message not found or no changes")
    return result


@router.post("/messages/{message_id}/regenerate")
def regenerate_message(
    message_id: int,
    conn: Any = Depends(deps.get_db_conn),
    llm_client: Any = Depends(deps.get_llm_client),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = outreach.regenerate_message(conn, llm_client, message_id)
    if result is None:
        raise HTTPException(status_code=404, detail="message not found")
    return result


@router.get("/suppressions")
def list_suppressions(
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return {"suppressions": outreach.list_suppressions(conn)}


@router.post("/suppressions")
def add_suppression(
    body: SuppressionCreate,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return outreach.suppress_broker(conn, body.broker_id, reason=body.reason)


@router.delete("/suppressions/{broker_id}")
def remove_suppression(
    broker_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    if not outreach.unsuppress_broker(conn, broker_id):
        raise HTTPException(status_code=404, detail="suppression not found")
    return {"removed": broker_id}
