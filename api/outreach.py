"""Broker outreach CRM (Phase 4) — campaign + message write layer.

Human-in-the-loop: the operator creates a campaign, the LLM drafts a personalised
message per targeted broker, the operator reviews / edits / approves and sends
MANUALLY (the UI offers mailto + copy). There is no automated email send in v1 —
`sent_via='manual'` records that the operator sent it themselves; the schema is
ready for an 'email' channel when a provider is wired.

GDPR (legitimate-interest B2B basis): suppressed brokers are never targeted, the
draft prompt requires an opt-out sentence, and every message records the contact
used + timestamps for auditability. Reads stay in toolkit.brokers; this module is
the WRITE path and lives in api/ (toolkit is read-only, rule #5).
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

_DRAFT_MODEL_KEY = "outreach_draft_model"
_DRAFT_PROMPT_KEY = "outreach_draft_system_prompt"
_CALLED_FOR = "outreach_draft"

RECORD_OUTREACH_DRAFT_TOOL: dict[str, Any] = {
    "name": "record_outreach_draft",
    "description": "Record the drafted outreach email. Call exactly once with both fields.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subject": {"type": "string", "description": "Email subject, Czech, max 80 chars."},
            "body": {"type": "string", "description": "Email body, Czech, max ~140 words, includes an opt-out sentence."},
        },
        "required": ["subject", "body"],
    },
}


# --- Campaigns -------------------------------------------------------------

def create_campaign(conn: Any, *, name: str, goal: str | None = None,
                    guidance: str | None = None, target: dict[str, Any] | None = None,
                    created_by: str | None = None) -> dict[str, Any]:
    from psycopg.types.json import Jsonb
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO outreach_campaigns (name, goal, guidance, target, created_by) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING *",
            (name, goal, guidance, Jsonb(target or {}), created_by))
        conn.commit()
        return _iso_campaign(cur.fetchone())


def list_campaigns(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT c.*, "
            "  coalesce(m.total, 0) AS message_count, "
            "  coalesce(m.sent, 0) AS sent_count, "
            "  coalesce(m.approved, 0) AS approved_count, "
            "  coalesce(m.draft, 0) AS draft_count "
            "FROM outreach_campaigns c "
            "LEFT JOIN ("
            "  SELECT campaign_id, count(*) AS total, "
            "    count(*) FILTER (WHERE status='sent') AS sent, "
            "    count(*) FILTER (WHERE status='approved') AS approved, "
            "    count(*) FILTER (WHERE status='draft') AS draft "
            "  FROM outreach_messages GROUP BY campaign_id"
            ") m ON m.campaign_id = c.id "
            "ORDER BY c.created_at DESC")
        return [_iso_campaign(r) for r in cur.fetchall()]


def get_campaign(conn: Any, campaign_id: int) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM outreach_campaigns WHERE id = %s", (campaign_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "SELECT status, count(*) AS n FROM outreach_messages "
            "WHERE campaign_id = %s GROUP BY status", (campaign_id,))
        stats = {r["status"]: r["n"] for r in cur.fetchall()}
    out = _iso_campaign(row)
    out["message_stats"] = stats
    return out


def update_campaign(conn: Any, campaign_id: int, *, name: str | None = None,
                    goal: str | None = None, guidance: str | None = None,
                    status: str | None = None, target: dict[str, Any] | None = None
                    ) -> dict[str, Any] | None:
    from psycopg.types.json import Jsonb
    sets, params = ["updated_at = now()"], {"id": campaign_id}
    for col, val in (("name", name), ("goal", goal), ("guidance", guidance), ("status", status)):
        if val is not None:
            sets.append(f"{col} = %({col})s")
            params[col] = val
    if target is not None:
        sets.append("target = %(target)s")
        params["target"] = Jsonb(target)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"UPDATE outreach_campaigns SET {', '.join(sets)} WHERE id = %(id)s RETURNING *", params)
        row = cur.fetchone()
        conn.commit()
    return _iso_campaign(row) if row else None


# --- Targeting + drafting --------------------------------------------------

def select_targets(conn: Any, target: dict[str, Any], *, campaign_id: int | None = None,
                   limit: int = 25) -> list[dict[str, Any]]:
    """Brokers matching the campaign criteria that are reachable (have an email),
    not suppressed, and not already drafted for this campaign."""
    region_ids = target.get("region_ids") or None
    okres_ids = target.get("okres_ids") or None
    obec_ids = target.get("obec_ids") or None
    metric = target.get("metric") or "active_property_count"
    limit = max(1, min(int(limit), 500))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT lb.* FROM broker_leaderboard(%s, %s, %s, %s, %s, %s, %s) lb "
            "WHERE lb.primary_email IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM broker_outreach_suppression s WHERE s.broker_id = lb.broker_id) "
            "  AND (%s::bigint IS NULL OR NOT EXISTS ("
            "        SELECT 1 FROM outreach_messages m "
            "        WHERE m.campaign_id = %s::bigint AND m.broker_id = lb.broker_id)) "
            "LIMIT %s",
            (region_ids, okres_ids, obec_ids, target.get("category_main"),
             target.get("category_type"), metric, 2000,
             campaign_id, campaign_id, limit))
        return cur.fetchall()


def generate_drafts(conn: Any, llm_client: Any, campaign_id: int, *, limit: int = 25) -> dict[str, Any] | None:
    campaign = get_campaign(conn, campaign_id)
    if campaign is None:
        return None
    targets = select_targets(conn, campaign.get("target") or {}, campaign_id=campaign_id, limit=limit)
    if not targets:
        return {"generated": 0, "targets": 0}
    system = llm_client.resolve_system_prompt(_DRAFT_PROMPT_KEY)
    model = llm_client.resolve_model(_DRAFT_MODEL_KEY)
    generated = 0
    for t in targets:
        ctx = _broker_context(conn, t)
        resp = llm_client.call(
            called_for=_CALLED_FOR,
            messages=[{"role": "user", "content": _build_payload(campaign, ctx)}],
            system=system, tools=[RECORD_OUTREACH_DRAFT_TOOL], model=model)
        draft = _extract_tool(resp.tool_calls, "record_outreach_draft")
        if not draft:
            continue
        _upsert_message(conn, campaign_id, t, draft, resp)
        generated += 1
    conn.commit()
    return {"generated": generated, "targets": len(targets)}


def regenerate_message(conn: Any, llm_client: Any, message_id: int) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT m.*, c.goal, c.guidance FROM outreach_messages m "
            "JOIN outreach_campaigns c ON c.id = m.campaign_id WHERE m.id = %s", (message_id,))
        msg = cur.fetchone()
    if msg is None:
        return None
    if msg["status"] not in ("draft", "approved"):
        return _iso_message(msg)  # don't clobber sent/replied
    leader = _leader_row(conn, msg["broker_id"])
    if leader is None:
        return None
    ctx = _broker_context(conn, leader)
    system = llm_client.resolve_system_prompt(_DRAFT_PROMPT_KEY)
    model = llm_client.resolve_model(_DRAFT_MODEL_KEY)
    resp = llm_client.call(
        called_for=_CALLED_FOR,
        messages=[{"role": "user", "content": _build_payload({"goal": msg["goal"], "guidance": msg["guidance"]}, ctx)}],
        system=system, tools=[RECORD_OUTREACH_DRAFT_TOOL], model=model)
    draft = _extract_tool(resp.tool_calls, "record_outreach_draft")
    if not draft:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "UPDATE outreach_messages SET subject=%s, body=%s, status='draft', "
            "llm_call_id=%s, model=%s, cost_usd=%s, generated_at=now(), approved_at=NULL "
            "WHERE id=%s RETURNING *",
            (draft["subject"], draft["body"], resp.llm_call_id, resp.model, resp.cost_usd, message_id))
        row = cur.fetchone()
        conn.commit()
    return _iso_message(row)


# --- Messages --------------------------------------------------------------

def list_messages(conn: Any, campaign_id: int, *, status: str | None = None) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT m.*, b.display_name AS broker_name, f.display_name AS firm_name "
            "FROM outreach_messages m "
            "JOIN brokers b ON b.id = m.broker_id "
            "LEFT JOIN firms f ON f.id = b.primary_firm_id "
            "WHERE m.campaign_id = %s AND (%s::text IS NULL OR m.status = %s::text) "
            "ORDER BY m.status, m.generated_at DESC",
            (campaign_id, status, status))
        return [_iso_message(r) for r in cur.fetchall()]


def update_message(conn: Any, message_id: int, *, status: str | None = None,
                   subject: str | None = None, body: str | None = None,
                   notes: str | None = None) -> dict[str, Any] | None:
    sets, params = [], {"id": message_id}
    if subject is not None:
        sets.append("subject = %(subject)s"); params["subject"] = subject
    if body is not None:
        sets.append("body = %(body)s"); params["body"] = body
    if notes is not None:
        sets.append("notes = %(notes)s"); params["notes"] = notes
    if status is not None:
        sets.append("status = %(status)s"); params["status"] = status
        if status == "approved":
            sets.append("approved_at = now()")
        elif status == "sent":
            sets.append("sent_at = now()")
            sets.append("sent_via = coalesce(sent_via, 'manual')")
    if not sets:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"UPDATE outreach_messages SET {', '.join(sets)} WHERE id = %(id)s RETURNING *", params)
        row = cur.fetchone()
        conn.commit()
    return _iso_message(row) if row else None


# --- Suppression -----------------------------------------------------------

def suppress_broker(conn: Any, broker_id: int, *, reason: str | None = None,
                    created_by: str | None = None) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO broker_outreach_suppression (broker_id, reason, created_by) "
            "VALUES (%s, %s, %s) ON CONFLICT (broker_id) DO UPDATE SET "
            "reason = EXCLUDED.reason, suppressed_at = now(), created_by = EXCLUDED.created_by "
            "RETURNING *",
            (broker_id, reason, created_by))
        row = cur.fetchone()
        conn.commit()
    row["suppressed_at"] = _iso(row.get("suppressed_at"))
    return row


def unsuppress_broker(conn: Any, broker_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM broker_outreach_suppression WHERE broker_id = %s", (broker_id,))
        deleted = cur.rowcount
        conn.commit()
    return bool(deleted)


def list_suppressions(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT s.broker_id, s.reason, s.suppressed_at, b.display_name AS broker_name "
            "FROM broker_outreach_suppression s "
            "JOIN brokers b ON b.id = s.broker_id ORDER BY s.suppressed_at DESC")
        rows = cur.fetchall()
    for r in rows:
        r["suppressed_at"] = _iso(r.get("suppressed_at"))
    return rows


# --- Internals -------------------------------------------------------------

def _broker_context(conn: Any, leader: dict[str, Any]) -> dict[str, Any]:
    broker_id = leader["broker_id"]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT o.name FROM broker_region_type_stats s "
            "LEFT JOIN broker_geo_options o ON o.geo_level='region' AND o.geo_id=s.geo_id "
            "WHERE s.broker_id=%s AND s.geo_level='region' "
            "GROUP BY o.name ORDER BY sum(s.active_property_count) DESC NULLS LAST LIMIT 3",
            (broker_id,))
        regions = [r["name"] for r in cur.fetchall() if r["name"]]
        cur.execute(
            "SELECT s.category_main, s.category_type FROM broker_region_type_stats s "
            "WHERE s.broker_id=%s AND s.geo_level='region' "
            "GROUP BY s.category_main, s.category_type "
            "ORDER BY sum(s.active_property_count) DESC NULLS LAST LIMIT 3",
            (broker_id,))
        cats = [f"{r['category_main']}/{r['category_type']}" for r in cur.fetchall() if r["category_main"]]
    return {
        "broker_id": broker_id,
        "name": leader.get("display_name"),
        "firm": leader.get("firm_name") or leader.get("firm_domain"),
        "email": leader.get("primary_email"),
        "phone": leader.get("primary_phone"),
        "active_listings": leader.get("active_property_count"),
        "total_listings": leader.get("property_count"),
        "regions": regions,
        "categories": cats,
    }


def _build_payload(campaign: dict[str, Any], ctx: dict[str, Any]) -> str:
    lines = [
        "OUTREACH GOAL:",
        (campaign.get("goal") or "Navázat kontakt a zjistit, zda makléř nabízí off-market nemovitosti.").strip(),
    ]
    if campaign.get("guidance"):
        lines += ["", "OPERATOR GUIDANCE:", campaign["guidance"].strip()]
    lines += [
        "", "BROKER FACTS (use only these; do not invent):",
        f"- Name: {ctx.get('name') or 'neznámé'}",
        f"- Firm: {ctx.get('firm') or 'neuvedeno'}",
    ]
    if ctx.get("active_listings") is not None:
        lines.append(f"- Active listings tracked: {ctx['active_listings']}")
    if ctx.get("regions"):
        lines.append(f"- Most active regions: {', '.join(ctx['regions'])}")
    if ctx.get("categories"):
        lines.append(f"- Typical inventory (category_main/type): {', '.join(ctx['categories'])}")
    return "\n".join(lines)


def _upsert_message(conn: Any, campaign_id: int, leader: dict[str, Any],
                    draft: dict[str, Any], resp: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO outreach_messages (campaign_id, broker_id, to_email, to_phone, "
            "  subject, body, llm_call_id, model, cost_usd) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (campaign_id, broker_id) DO UPDATE SET "
            "  subject=EXCLUDED.subject, body=EXCLUDED.body, llm_call_id=EXCLUDED.llm_call_id, "
            "  model=EXCLUDED.model, cost_usd=EXCLUDED.cost_usd, generated_at=now() "
            "  WHERE outreach_messages.status = 'draft'",
            (campaign_id, leader["broker_id"], leader.get("primary_email"),
             leader.get("primary_phone"), draft["subject"], draft["body"],
             resp.llm_call_id, resp.model, resp.cost_usd))


def _leader_row(conn: Any, broker_id: int) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT broker_id, display_name, primary_email, primary_phone, firm_name, "
            "  firm_domain, listing_count AS property_count, active_listing_count AS active_property_count "
            "FROM brokers_public WHERE broker_id = %s", (broker_id,))
        return cur.fetchone()


def _extract_tool(tool_calls: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for tc in tool_calls or []:
        if tc.get("name") == name:
            return tc.get("input")
    return None


def _iso(v: Any) -> Any:
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


def _iso_campaign(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    for k in ("created_at", "updated_at"):
        if k in row:
            row[k] = _iso(row[k])
    return row


def _iso_message(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    for k in ("generated_at", "approved_at", "sent_at"):
        if k in row:
            row[k] = _iso(row[k])
    if "cost_usd" in row and row["cost_usd"] is not None:
        row["cost_usd"] = float(row["cost_usd"])
    return row
