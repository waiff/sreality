"""The W2a-0 churn instrument must be invisible until it is switched on, and
must never be able to break the scrape it measures.

The instrument rides inside the live ingest path on all nine portals, so the two
properties that matter are behavioural, not arithmetic: with
`location_payload_shadow_hash` unset the scrape writes exactly what it wrote
before, and with it set no failure inside the hook — flag read, normaliser or
upsert — can reach the caller. Hermetic fake conn records the executed SQL +
bound params, same pattern as test_db_mark_inactive_null_safety.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

import pytest

from scraper import db


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))

    def fetchone(self) -> tuple[Any, ...] | None:
        last = self._conn.executed[-1][0]
        if "FROM app_settings" in last:
            return None if self._conn.flag is None else (self._conn.flag,)
        return (1,)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _FakeConn:
    def __init__(self, flag: Any = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.flag = flag

    def cursor(self) -> _Cur:
        return _Cur(self)


def _churn_statements(conn: _FakeConn) -> list[tuple[str, Any]]:
    return [e for e in conn.executed if "portal_payload_churn" in e[0]]


_PAGE = (
    "<html>\n"
    '  <head>\n    <script src="//x.gemius.pl/a.js"></script>\n'
    "    <style>p { color: red }</style>\n  </head>\n"
    "  <body>\n"
    '    <div class="advertisement">stehuju.cz</div>\n'
    '    <h1 nonce="abc">Byt 3+1</h1>\n'
    "  </body>\n</html>\n"
)


def _archive(conn: _FakeConn, **kwargs: Any) -> int | None:
    return db.upsert_portal_raw_page(
        conn,
        source="idnes",
        source_id_native="123",
        source_url="https://reality.idnes.cz/x",
        page_kind="detail",
        html=_PAGE,
        http_status=200,
        **kwargs,
    )


def test_flag_off_writes_no_churn_row() -> None:
    conn = _FakeConn(flag=None)

    _archive(conn)

    assert _churn_statements(conn) == []
    writes = [e for e in conn.executed if "INSERT INTO" in e[0]]
    assert len(writes) == 1
    assert "portal_raw_pages" in writes[0][0]


def test_flag_off_costs_one_app_settings_lookup_and_nothing_else() -> None:
    # The Gate-2 precedent (_gate2_null_sreality_id_enabled) reads app_settings
    # live rather than caching, so a flip reaches the always-on worker on its
    # next batch instead of after a restart; the instrument pays the same PK
    # lookup. Pin the shape so the flag-off path can't grow anything more.
    conn = _FakeConn(flag=None)

    _archive(conn)

    assert len(conn.executed) == 2
    assert "FROM app_settings" in conn.executed[0][0]
    assert conn.executed[0][1] == ("location_payload_shadow_hash",)


@pytest.mark.parametrize("falsey", [None, False, "false", "off", "0", ""])
def test_unset_or_false_flag_values_read_as_off(falsey: Any) -> None:
    conn = _FakeConn(flag=falsey)

    _archive(conn)

    assert _churn_statements(conn) == []


@pytest.mark.parametrize("truthy", [True, "true", "1", "yes", "on", " TRUE "])
def test_flag_on_writes_exactly_one_churn_row_per_call(truthy: Any) -> None:
    conn = _FakeConn(flag=truthy)

    _archive(conn)

    churn = _churn_statements(conn)
    assert len(churn) == 1
    sql, params = churn[0]
    assert sql.startswith("INSERT INTO portal_payload_churn")
    source, native, page_kind = params[0], params[1], params[2]
    raw_sha, norm_sha = params[5], params[6]
    byte_size, norm_size, version = params[7], params[8], params[9]
    assert (source, native, page_kind) == ("idnes", "123", "detail")
    assert len(raw_sha) == 32 and len(norm_sha) == 32
    assert raw_sha != norm_sha
    assert byte_size == len(_PAGE.encode("utf-8"))
    # The idnes profile drops the ad slot, the analytics script and the chrome,
    # so the normalised projection is strictly smaller than the fetched page.
    assert 0 < norm_size < byte_size
    assert version.startswith("payload_norm@")


def test_flag_on_still_writes_the_raw_page() -> None:
    conn = _FakeConn(flag=True)

    assert _archive(conn) == 1
    assert any("portal_raw_pages" in e[0] for e in conn.executed)


def test_record_churn_false_suppresses_the_hook_entirely() -> None:
    conn = _FakeConn(flag=True)

    _archive(conn, record_churn=False)

    assert conn.executed == [e for e in conn.executed if "portal_raw_pages" in e[0]]
    assert len(conn.executed) == 1


def test_hook_failure_never_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn(flag=True)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("normaliser exploded")

    monkeypatch.setattr(db, "record_payload_churn", boom)

    assert _archive(conn) == 1
    assert _churn_statements(conn) == []


def test_flag_read_failure_never_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn(flag=True)

    def boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("app_settings unreachable")

    monkeypatch.setattr(db, "_app_settings_flag", boom)

    assert _archive(conn) == 1
    assert _churn_statements(conn) == []


def test_json_and_html_bodies_take_different_normalisation_paths() -> None:
    # The archive path is handed both through one `html` parameter, so the
    # instrument sniffs; a JSON body that only reorders keys must not register
    # as a normalised change.
    a, b = _FakeConn(flag=True), _FakeConn(flag=True)
    for conn, body in ((a, '{"x":1,"y":2}'), (b, '{"y":2,\n "x":1}')):
        db.upsert_portal_raw_page(
            conn, source="sreality", source_id_native="k", source_url="u",
            page_kind="index", html=body, http_status=200,
        )

    pa, pb = _churn_statements(a)[0][1], _churn_statements(b)[0][1]
    assert pa[5] != pb[5]
    assert pa[6] == pb[6]


def test_sreality_detail_drain_records_only_successful_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # sreality stages no detail body in portal_raw_pages (the estate JSON goes
    # to listings.raw_json), so the heaviest-cadence portal is measured from
    # write_details or not at all.
    from scraper import main as scraper_main
    from scraper.portal_runner import DrainItem

    monkeypatch.setattr(scraper_main.db, "write_detail_batch", lambda *a, **k: {})
    conn = _FakeConn(flag=True)
    items = [
        DrainItem("1", "ok", scraper_main.FetchResult(1, "ok", raw={"price_czk": 1})),
        DrainItem("2", "gone", scraper_main.FetchResult(2, "gone")),
        DrainItem("3", "error", scraper_main.FetchResult(3, "error", source="fetch")),
    ]

    scraper_main.SrealityPortal().write_details(conn, items)

    churn = _churn_statements(conn)
    assert len(churn) == 1
    assert churn[0][1][:3] == ("sreality", "1", "detail")


def test_sreality_index_archiver_counts_fetches_the_freshness_skip_hides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The index page is walked hourly and archived at most daily; counting only
    # the archived fetches would understate the change rate that gates 02 P2.
    from scraper import main as scraper_main

    monkeypatch.setattr(
        scraper_main.db, "fresh_index_page_keys", lambda *a, **k: {"skip-me"},
    )
    monkeypatch.setattr(scraper_main.db, "index_archive_week", lambda: "2026w33")

    class _Client:
        category_main = 1
        category_type = 2
        locality_district_id = None

    conn = _FakeConn(flag=True)
    archive = scraper_main._index_page_archiver(_Client(), conn, dry_run=False)
    monkeypatch.setattr(scraper_main.db, "fresh_index_page_keys", lambda *a, **k: set())
    archive(0, "https://sreality.cz/api", {"_embedded": {"estates": []}})

    churn = _churn_statements(conn)
    assert len(churn) == 1
    assert churn[0][1][0] == "sreality"
    assert churn[0][1][2] == "index"
    # The one fetch that IS archived must not be counted twice.
    archived = [e for e in conn.executed if "portal_raw_pages" in e[0]]
    assert len(archived) == 1


def test_sreality_index_archiver_records_even_when_the_archive_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scraper import main as scraper_main

    monkeypatch.setattr(scraper_main.db, "index_archive_week", lambda: "2026w33")

    class _Client:
        category_main = 1
        category_type = 2
        locality_district_id = 5

    conn = _FakeConn(flag=True)
    key = "1/2/5/0/2026w33"
    monkeypatch.setattr(scraper_main.db, "fresh_index_page_keys", lambda *a, **k: {key})
    archive = scraper_main._index_page_archiver(_Client(), conn, dry_run=False)
    archive(0, "https://sreality.cz/api", {"_embedded": {"estates": []}})

    assert [e[1][1] for e in _churn_statements(conn)] == [key]
    assert not [e for e in conn.executed if "portal_raw_pages" in e[0]]


def test_bezrealitky_detail_drain_records_the_graphql_advert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scraper import bezrealitky_main
    from scraper.portal import PortalConfig
    from scraper.portal_runner import DrainItem

    monkeypatch.setattr(
        bezrealitky_main.db, "ingest_scraped_listing", lambda *a, **k: (7, "new"),
    )
    monkeypatch.setattr(bezrealitky_main.db, "record_media", lambda *a, **k: 0)

    class _Listing:
        source_id_native = "abc"
        raw = {"id": "abc", "price": 1, "image_urls": []}

    conn = _FakeConn(flag=True)
    portal = bezrealitky_main.BezrealitkyPortal(PortalConfig(
        source="bezrealitky",
        supports_complete_walk=True,
        categories=[{"offer_type": "PRODEJ", "estate_type": "BYT"}],
        split_threshold=None,
    ))
    portal.write_details(conn, [DrainItem("abc", "ok", {"listing": _Listing()})])

    churn = _churn_statements(conn)
    assert len(churn) == 1
    assert churn[0][1][:3] == ("bezrealitky", "abc", "detail")


def test_churn_upsert_sql_is_a_plain_literal_with_the_counter_arithmetic() -> None:
    # tests/sql_corpus.py only discovers ast.Constant SQL — an f-string or a
    # concatenation would silently drop this statement from the PREPARE corpus.
    src = ast.parse(inspect.getsource(db))
    names = {
        t.id
        for node in ast.walk(src)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant)
    }
    assert "_PAYLOAD_CHURN_UPSERT_SQL" in names

    sql = " ".join(db._PAYLOAD_CHURN_UPSERT_SQL.split())
    # First sighting of a key = one fetch, zero changes; every later fetch adds
    # a change only when the corresponding hash actually moved.
    assert "VALUES (%s, %s, %s::location_page_kind, coalesce" in sql
    assert "1, 0, 0," in sql
    assert "fetches = portal_payload_churn.fetches + 1" in sql
    for column in ("raw", "norm"):
        assert (
            f"{column}_changes = portal_payload_churn.{column}_changes + "
            f"(portal_payload_churn.last_{column}_sha256 "
            f"IS DISTINCT FROM EXCLUDED.last_{column}_sha256)::int"
        ) in sql
    assert sql.count("%s") == 10
