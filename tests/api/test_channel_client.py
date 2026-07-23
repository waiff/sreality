"""Hermetic tests for the channel-delivery orchestrator (Sprint N PR 1).

ChannelClient drives a transport and writes the channel_sends ledger. These
assert the claim/send/finalize SQL + control flow against a fake psycopg conn;
the real transports (Resend, Telegram) + the outbox loop land in later PRs.
"""

from __future__ import annotations

from typing import Any

import pytest

from api.channel_client import ChannelClient
from api.transports.base import RenderedMessage, SendResult, TransportError


# --- fakes ----------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._last: tuple[Any, ...] | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        norm = " ".join(sql.split())
        self._conn.executed.append((norm, params))
        # The claim INSERT ... RETURNING id yields a row; the suppression probe
        # yields one only when this conn is flagged suppressed.
        if "INSERT INTO channel_sends" in norm:
            self._last = self._conn.claim_result
        elif "notification_suppression" in norm:
            self._last = (1,) if self._conn.suppressed else None
        else:
            self._last = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._last

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _FakeTxn:
    def __enter__(self) -> "_FakeTxn":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _FakeConn:
    def __init__(
        self,
        claim_result: tuple[Any, ...] | None = (1,),
        suppressed: bool = False,
    ) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.claim_result = claim_result
        self.suppressed = suppressed

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()


class _FakeTransport:
    name = "email"
    transport = "resend"

    def __init__(self, result: SendResult | None = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, RenderedMessage]] = []

    def is_configured(self) -> bool:
        return True

    def send(self, *, recipient: str, message: RenderedMessage) -> SendResult:
        self.calls.append((recipient, message))
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


_MSG = RenderedMessage(body_text="Nový 2+kk Praha 2", deep_link="https://app/listing/1")


def _send(client: ChannelClient, **over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = dict(
        channel="email",
        recipient="op@example.cz",
        message=_MSG,
        consumer="watchdog",
        dedupe_key="notif:abc:email",
        notification_id="abc",
        source_kind="watchdog",
        source_id="sub-1",
    )
    kw.update(over)
    return client.send(**kw)


# --- tests ----------------------------------------------------------------


def test_send_success_claims_then_finalizes_sent() -> None:
    conn = _FakeConn(claim_result=(7,))
    transport = _FakeTransport(SendResult(status="sent", provider_message_id="pm1"))
    client = ChannelClient(conn, transports={"email": transport})  # type: ignore[arg-type]

    out = _send(client)

    assert out == {"status": "sent", "id": 7, "provider_message_id": "pm1"}
    assert len(transport.calls) == 1
    insert_sql, insert_params = conn.executed[0]
    assert "INSERT INTO channel_sends" in insert_sql
    assert "ON CONFLICT (dedupe_key) DO NOTHING" in insert_sql
    # dedupe_key is the last positional param.
    assert insert_params[-1] == "notif:abc:email"
    update_sql, update_params = next(
        (sql, p)
        for sql, p in conn.executed
        if sql.startswith("UPDATE channel_sends SET")
    )
    assert update_sql.startswith("UPDATE channel_sends SET")
    assert update_params[0] == "sent"  # status
    assert update_params[2] == "pm1"   # provider_message_id


def test_send_skips_suppressed_recipient() -> None:
    """A globally-suppressed (channel, address) records a terminal 'suppressed'
    channel_sends row and never hits the transport (migration 367 pre-send gate)."""
    conn = _FakeConn(claim_result=(9,), suppressed=True)
    transport = _FakeTransport(SendResult(status="sent"))
    client = ChannelClient(conn, transports={"email": transport})  # type: ignore[arg-type]

    out = _send(client)

    assert out == {"status": "suppressed", "id": 9}
    assert transport.calls == []  # transport never fired
    assert any("notification_suppression" in sql for sql, _ in conn.executed)
    _, update_params = next(
        (sql, p)
        for sql, p in conn.executed
        if sql.startswith("UPDATE channel_sends SET")
    )
    assert update_params[0] == "suppressed"


def test_send_already_claimed_is_idempotent_noop() -> None:
    """No row back from the claim INSERT => the (event, channel) is already
    claimed/sent. The transport must NOT fire and no UPDATE is issued."""
    conn = _FakeConn(claim_result=None)
    transport = _FakeTransport(SendResult(status="sent"))
    client = ChannelClient(conn, transports={"email": transport})  # type: ignore[arg-type]

    out = _send(client)

    assert out == {"status": "already_claimed", "id": None}
    assert transport.calls == []
    assert not any(sql.startswith("UPDATE channel_sends") for sql, _ in conn.executed)


def test_unconfigured_channel_records_failed_then_raises() -> None:
    conn = _FakeConn(claim_result=(3,))
    client = ChannelClient(conn, transports={})  # ships dark — nothing registered

    with pytest.raises(TransportError):
        _send(client)

    # The claim row was marked failed before the raise (audit trail intact).
    update_sql, update_params = next(
        (s, p) for s, p in conn.executed if s.startswith("UPDATE channel_sends")
    )
    assert update_params[0] == "failed"


def test_transport_send_exception_is_caught_and_recorded() -> None:
    conn = _FakeConn(claim_result=(5,))
    transport = _FakeTransport(raises=RuntimeError("smtp down"))
    client = ChannelClient(conn, transports={"email": transport})  # type: ignore[arg-type]

    out = _send(client)  # must NOT raise — the failed row is the audit trail

    assert out["status"] == "failed"
    assert out["id"] == 5
    assert "smtp down" in out["error"]
    update_params = next(
        p for s, p in conn.executed if s.startswith("UPDATE channel_sends")
    )
    assert update_params[0] == "failed"


def test_transport_lookup_lists_available_channels() -> None:
    conn = _FakeConn()
    client = ChannelClient(conn, transports={"telegram": _FakeTransport()})  # type: ignore[arg-type]
    with pytest.raises(TransportError) as exc:
        client.transport("email")
    assert "available: ['telegram']" in str(exc.value)
