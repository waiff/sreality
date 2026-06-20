"""Hermetic tests for the delivery outbox (Sprint N PR 3).

No DB / network: a fake conn scripts the NEW + RETRY queries and the recipient
lookup; a fake ChannelClient records send/retry calls. Covers the composer, the
configured-channel + recipient gating, the new-pair send path, and the retry pass.
"""

from __future__ import annotations

from typing import Any

from api import notification_outbox as ob
from api.transports.base import RenderedMessage


# --- compose --------------------------------------------------------------


def test_compose_new_subject_and_deep_link(monkeypatch: Any) -> None:
    monkeypatch.setenv("SPA_BASE_URL", "https://app.example/")
    msg = ob.compose_message({
        "change_kind": "new", "sreality_id": 123,
        "locality": "Praha 2", "disposition": "2+kk",
        "price_czk": 6_900_000, "price_unit": None,
    })
    assert isinstance(msg, RenderedMessage)
    assert msg.subject == "Nový inzerát: 2+kk Praha 2"
    assert msg.deep_link == "https://app.example/listing/123"  # trailing slash trimmed
    assert "6 900 000 Kč" in msg.body_text


def test_compose_price_drop_shows_prev_to_new(monkeypatch: Any) -> None:
    monkeypatch.setenv("SPA_BASE_URL", "https://app.example")
    msg = ob.compose_message({
        "change_kind": "price_drop", "sreality_id": 9, "locality": "Brno",
        "disposition": "3+1", "price_czk": 4_500_000, "price_unit": None,
        "prev_price_czk": 5_000_000, "trigger_price_czk": 4_500_000,
    })
    assert msg.subject.startswith("Zlevněno")
    assert "5 000 000 Kč → 4 500 000 Kč" in msg.body_text


# --- fakes for drain_once -------------------------------------------------


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        norm = " ".join(sql.split())
        if "FROM app_settings" in norm:
            self._rows = [(self._conn.recipient,)] if self._conn.recipient else [(None,)]
        elif "CROSS JOIN LATERAL" in norm:
            self._rows = self._conn.new_rows
        elif "FROM channel_sends cs JOIN" in norm:
            self._rows = self._conn.retry_rows
        else:
            self._rows = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _Conn:
    def __init__(self, *, recipient: str | None, new_rows: list, retry_rows: list) -> None:
        self.recipient = recipient
        self.new_rows = new_rows
        self.retry_rows = retry_rows

    def cursor(self) -> _Cur:
        return _Cur(self)


class _FakeClient:
    def __init__(self, configured: set[str]) -> None:
        self._configured = configured
        self.sends: list[dict[str, Any]] = []
        self.retries: list[dict[str, Any]] = []

    def configured_channels(self) -> set[str]:
        return self._configured

    def send(self, **kw: Any) -> dict[str, Any]:
        self.sends.append(kw)
        return {"status": "sent", "id": len(self.sends)}

    def retry(self, **kw: Any) -> dict[str, Any]:
        self.retries.append(kw)
        return {"status": "sent", "id": kw["send_id"]}


def _new_row(ch: str = "email") -> tuple:
    # (dispatch_id, source_kind, change_kind, sreality_id, subscription_id,
    #  collection_id, trigger_price, prev_price, locality, disposition,
    #  price_czk, price_unit, category_main, ch)
    return ("dab-1", "watchdog", "new", 123, "sub-1", None,
            None, None, "Praha 2", "2+kk", 6_900_000, None, "byt", ch)


def test_drain_noop_without_configured_channels() -> None:
    client = _FakeClient(configured=set())
    conn = _Conn(recipient="op@example.cz", new_rows=[_new_row()], retry_rows=[])
    stats = ob.drain_once(conn, client)  # type: ignore[arg-type]
    assert stats == {"sent": 0, "failed": 0, "skipped": 0, "retried": 0}
    assert client.sends == []


def test_drain_sends_new_pair_with_correct_routing() -> None:
    client = _FakeClient(configured={"email"})
    conn = _Conn(recipient="op@example.cz", new_rows=[_new_row()], retry_rows=[])
    stats = ob.drain_once(conn, client)  # type: ignore[arg-type]
    assert stats["sent"] == 1
    assert len(client.sends) == 1
    call = client.sends[0]
    assert call["channel"] == "email"
    assert call["recipient"] == "op@example.cz"
    assert call["consumer"] == "watchdog"
    assert call["dedupe_key"] == "notif:dab-1:email"
    assert call["notification_id"] == "dab-1"
    assert call["source_id"] == "sub-1"


def test_drain_skips_when_recipient_unset() -> None:
    client = _FakeClient(configured={"email"})
    conn = _Conn(recipient=None, new_rows=[_new_row()], retry_rows=[])
    stats = ob.drain_once(conn, client)  # type: ignore[arg-type]
    assert stats["sent"] == 0
    assert stats["skipped"] == 1
    assert client.sends == []


def test_drain_collection_monitor_routes_collection_id_as_source() -> None:
    client = _FakeClient(configured={"email"})
    row = ("dab-2", "collection_monitor", "price_drop", 55, None, 7,
           4_000_000, 4_500_000, "Plzeň", "1+kk", 4_000_000, None, "byt", "email")
    conn = _Conn(recipient="op@example.cz", new_rows=[row], retry_rows=[])
    ob.drain_once(conn, client)  # type: ignore[arg-type]
    call = client.sends[0]
    assert call["consumer"] == "collection_monitor"
    assert call["source_id"] == "7"  # collection_id stringified


def test_drain_retries_failed_due_rows() -> None:
    client = _FakeClient(configured={"email"})
    # (send_id, channel, recipient, consumer, source_kind, change_kind, sreality_id,
    #  trigger_price, prev_price, locality, disposition, price_czk, price_unit, category_main)
    retry_row = (42, "email", "op@example.cz", "watchdog", "watchdog", "new", 5,
                 None, None, "Ostrava", "2+1", 3_000_000, None, "byt")
    conn = _Conn(recipient="op@example.cz", new_rows=[], retry_rows=[retry_row])
    stats = ob.drain_once(conn, client)  # type: ignore[arg-type]
    assert stats["retried"] == 1
    assert client.retries[0]["send_id"] == 42
    assert client.retries[0]["channel"] == "email"
