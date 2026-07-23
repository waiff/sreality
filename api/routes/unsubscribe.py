"""Unauthenticated one-click unsubscribe endpoint (RFC 8058, Wave 3).

- `GET  /u/{token}` — renders a confirmation page for a logged-out recipient (a
  human clicking the link in the email body). The page POSTs to the same token.
- `POST /u/{token}` — the RFC 8058 one-click target (mail clients auto-POST here
  because of the `List-Unsubscribe-Post` header): verify the HMAC token, insert a
  GLOBAL `notification_suppression` row (source='unsubscribe'), render a done page.

The HMAC token IS the authentication — no session, works for logged-out users.
Both routes render even for a bad/expired token (400 + a friendly message).
"""

from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, Depends, Response

from api import dependencies as deps
from api.unsubscribe import verify_unsub_token

router = APIRouter(tags=["unsubscribe"])


def _page(title: str, body_html: str, *, status: int = 200) -> Response:
    doc = (
        "<!doctype html><html lang=cs><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;"
        "padding:0 1rem;color:#1a1a1a;line-height:1.5}button{font:inherit;padding:.6rem 1.2rem;"
        "border:1px solid #1a1a1a;background:#1a1a1a;color:#fff;border-radius:.4rem;cursor:pointer}"
        "</style></head><body>"
        f"<h1>{html.escape(title)}</h1>{body_html}</body></html>"
    )
    return Response(content=doc, media_type="text/html; charset=utf-8", status_code=status)


def _bad() -> Response:
    return _page(
        "Neplatný odkaz",
        "<p>Tento odhlašovací odkaz je neplatný nebo vypršel.</p>",
        status=400,
    )


@router.get("/u/{token}")
def unsubscribe_page(token: str) -> Response:
    parsed = verify_unsub_token(token)
    if parsed is None:
        return _bad()
    _channel, address = parsed
    return _page(
        "Odhlásit odběr upozornění",
        f"<p>Odhlásit <strong>{html.escape(address)}</strong> ze všech e-mailových "
        "upozornění?</p>"
        f"<form method=post action='/u/{html.escape(token)}'>"
        "<button type=submit>Odhlásit</button></form>",
    )


@router.post("/u/{token}")
def unsubscribe_confirm(
    token: str,
    conn: Any = Depends(deps.get_db_conn),
) -> Response:
    parsed = verify_unsub_token(token)
    if parsed is None:
        return _bad()
    channel, address = parsed
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notification_suppression (channel, address, reason, source) "
            "VALUES (%s, %s, 'one-click unsubscribe', 'unsubscribe') "
            "ON CONFLICT (channel, address) DO NOTHING",
            (channel, address),
        )
    return _page(
        "Odhlášeno",
        f"<p><strong>{html.escape(address)}</strong> už nebude dostávat e-mailová "
        "upozornění.</p>",
    )
