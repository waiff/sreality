"""Hermetic tests for one-click unsubscribe (RFC 8058) — HMAC token + endpoint.

The token HMAC is the security boundary: a valid token lets an unauthenticated
POST suppress an address. These assert round-trip, tamper/wrong-secret rejection,
the dark (no-secret) posture, and that the POST handler writes a suppression.
"""

from __future__ import annotations

from typing import Any

from api.unsubscribe import make_unsub_token, verify_unsub_token

_SECRET = "unsub-test-secret"


def test_token_roundtrip(monkeypatch: Any) -> None:
    monkeypatch.setenv("NOTIFICATION_UNSUB_SECRET", _SECRET)
    tok = make_unsub_token("email", "op@example.cz")
    assert tok is not None
    assert verify_unsub_token(tok) == ("email", "op@example.cz")


def test_token_tamper_rejected(monkeypatch: Any) -> None:
    monkeypatch.setenv("NOTIFICATION_UNSUB_SECRET", _SECRET)
    tok = make_unsub_token("email", "op@example.cz")
    assert tok is not None
    payload, _, sig = tok.partition(".")
    assert verify_unsub_token(f"{payload}x.{sig}") is None  # tampered payload
    assert verify_unsub_token(f"{payload}.{sig[:-2]}AA") is None  # tampered sig


def test_token_dark_without_secret(monkeypatch: Any) -> None:
    monkeypatch.delenv("NOTIFICATION_UNSUB_SECRET", raising=False)
    assert make_unsub_token("email", "a@b.cz") is None
    assert verify_unsub_token("anything.here") is None


def test_token_wrong_secret_rejected(monkeypatch: Any) -> None:
    monkeypatch.setenv("NOTIFICATION_UNSUB_SECRET", "secret-A")
    tok = make_unsub_token("email", "a@b.cz")
    assert tok is not None
    monkeypatch.setenv("NOTIFICATION_UNSUB_SECRET", "secret-B")
    assert verify_unsub_token(tok) is None


def test_email_emits_list_unsubscribe_when_configured(monkeypatch: Any) -> None:
    monkeypatch.setenv("NOTIFICATION_UNSUB_SECRET", _SECRET)
    monkeypatch.setenv("API_PUBLIC_URL", "https://api.example/")
    from api.transports.email_resend import _list_unsubscribe_headers

    h = _list_unsubscribe_headers("op@example.cz")
    assert h["List-Unsubscribe"].startswith("<https://api.example/u/")
    assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_email_no_list_unsubscribe_when_dark(monkeypatch: Any) -> None:
    monkeypatch.delenv("NOTIFICATION_UNSUB_SECRET", raising=False)
    monkeypatch.setenv("API_PUBLIC_URL", "https://api.example")
    from api.transports.email_resend import _list_unsubscribe_headers

    assert _list_unsubscribe_headers("op@example.cz") == {}


class _Cur:
    def __init__(self, sink: list[tuple[str, Any]]) -> None:
        self._sink = sink

    def execute(self, sql: str, params: Any = None) -> None:
        self._sink.append((" ".join(sql.split()), params))

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _Txn:
    def __enter__(self) -> "_Txn":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _Conn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self.executed)

    def transaction(self) -> _Txn:
        return _Txn()


def test_post_unsubscribe_inserts_suppression(monkeypatch: Any) -> None:
    monkeypatch.setenv("NOTIFICATION_UNSUB_SECRET", _SECRET)
    from api.routes.unsubscribe import unsubscribe_confirm

    tok = make_unsub_token("email", "op@example.cz")
    assert tok is not None
    conn = _Conn()
    resp = unsubscribe_confirm(tok, conn=conn)  # type: ignore[arg-type]
    assert resp.status_code == 200
    sql, params = next(
        (s, p) for s, p in conn.executed if "notification_suppression" in s
    )
    assert "INSERT INTO notification_suppression" in sql
    assert params == ("email", "op@example.cz")


def test_post_unsubscribe_bad_token_400_no_db(monkeypatch: Any) -> None:
    monkeypatch.setenv("NOTIFICATION_UNSUB_SECRET", _SECRET)
    from api.routes.unsubscribe import unsubscribe_confirm

    class _Boom:
        def cursor(self) -> Any:
            raise AssertionError("must not touch the DB on a bad token")

        def transaction(self) -> Any:
            raise AssertionError("must not touch the DB on a bad token")

    resp = unsubscribe_confirm("bad.token", conn=_Boom())  # type: ignore[arg-type]
    assert resp.status_code == 400
