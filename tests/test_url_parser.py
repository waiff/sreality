"""Tests for scraper.url_parser. Hermetic; no live HTTP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from scraper import url_parser


_FIXTURE = Path(__file__).parent / "fixtures" / "sample_listing.json"


def _load_raw() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


class _StubClient:
    def __init__(
        self,
        raw: dict[str, Any] | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self._raw = raw
        self._exc = exc
        self.calls: list[int] = []

    def get_detail(self, sreality_id: int) -> dict[str, Any]:
        self.calls.append(sreality_id)
        if self._exc is not None:
            raise self._exc
        assert self._raw is not None
        return self._raw


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = ()) -> None:
        self._conn.executions.append((sql, params))

    def fetchone(self) -> Any:
        return self._conn.fetchone_value


class _FakeConn:
    def __init__(self, fetchone_value: Any = None) -> None:
        self.executions: list[tuple[str, Any]] = []
        self.fetchone_value: Any = fetchone_value

    def cursor(self) -> _Cur:
        return _Cur(self)


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://www.sreality.cz/detail/pronajem/byt/2+kk/praha-1-stare-mesto/2836292428",
            2836292428,
        ),
        (
            "https://www.sreality.cz/detail/pronajem/byt/2+kk/x/2836292428?source=foo",
            2836292428,
        ),
        ("https://sreality.cz/detail/x/2836292428", 2836292428),
        ("sreality.cz/detail/byt/2836292428", 2836292428),
        ("www.sreality.cz/detail/byt/2836292428/", 2836292428),
        ("https://www.sreality.cz/detail/x/2836292428#anchor", 2836292428),
    ],
)
def test_extract_id_from_various_shapes(url: str, expected: int) -> None:
    assert url_parser.extract_sreality_id(url) == expected


def test_extract_id_empty_url_raises() -> None:
    with pytest.raises(ValueError):
        url_parser.extract_sreality_id("")


def test_extract_id_non_string_raises() -> None:
    with pytest.raises(ValueError):
        url_parser.extract_sreality_id(None)  # type: ignore[arg-type]


def test_extract_id_no_digits_raises() -> None:
    with pytest.raises(ValueError):
        url_parser.extract_sreality_id(
            "https://www.sreality.cz/hledani/pronajem/byty"
        )


def test_extract_id_short_digits_rejected() -> None:
    """Listing IDs are 9-10 digits; a 5-digit segment should not match."""
    with pytest.raises(ValueError):
        url_parser.extract_sreality_id("https://www.sreality.cz/detail/12345")


def test_parse_sreality_url_returns_spec_and_metadata() -> None:
    raw = _load_raw()
    client = _StubClient(raw=raw)
    conn = _FakeConn(fetchone_value=None)
    res = url_parser.parse_sreality_url(
        "https://www.sreality.cz/detail/prodej/byt/3+kk/praha-smichov-na-cisarce/3292504140",
        client=client,
        conn=conn,
    )
    assert res["sreality_id"] == 3292504140
    assert client.calls == [3292504140]
    assert res["source_url"].endswith("3292504140")
    assert "fetched_at" in res
    assert res["in_database"] is False
    spec = res["spec"]
    assert spec["sreality_id"] == 3292504140
    assert "area_m2" in spec
    assert "disposition" in spec
    assert "lat" in spec
    assert "lng" not in spec  # parser uses 'lon', not 'lng'
    assert "lon" in spec
    assert isinstance(res["images"], list)


def test_parse_sreality_url_marks_in_database_when_row_exists() -> None:
    raw = _load_raw()
    client = _StubClient(raw=raw)
    conn = _FakeConn(fetchone_value=(1,))
    res = url_parser.parse_sreality_url(
        "https://www.sreality.cz/detail/x/2836292428",
        client=client,
        conn=conn,
    )
    assert res["in_database"] is True
    assert any(
        "FROM listings" in sql and "sreality_id" in sql
        for sql, _ in conn.executions
    )


def test_parse_sreality_url_propagates_http_error() -> None:
    resp = requests.Response()
    resp.status_code = 404
    exc = requests.HTTPError("404 Not Found", response=resp)
    client = _StubClient(exc=exc)
    conn = _FakeConn()
    with pytest.raises(requests.HTTPError):
        url_parser.parse_sreality_url(
            "https://www.sreality.cz/detail/x/2836292428",
            client=client,
            conn=conn,
        )


def test_parse_sreality_url_invalid_url_raises_before_fetch() -> None:
    raw = _load_raw()
    client = _StubClient(raw=raw)
    conn = _FakeConn()
    with pytest.raises(ValueError):
        url_parser.parse_sreality_url(
            "https://www.sreality.cz/hledani/byty",
            client=client,
            conn=conn,
        )
    assert client.calls == []
