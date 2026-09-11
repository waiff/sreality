"""The payload archive must never be able to break the scrape it rides in.

`location_data.payloads.append_payload` owns the store's semantics and is tested against
the replayed schema (tests/location_data/test_payloads.py). What is tested here is the
WIRING: which fetches reach it, with which body, how often, and what happens when it fails
— on the hot chokepoint every HTML portal writes through plus the two portals that stage
no body at all.

THE RULE IS GRAIN, AND IT IS NOT A FLAG ANY MORE (rule 25 W1-a). A `detail` body is ONE
listing's page and is the claim lane's second substrate, so it is ALWAYS archived; every
other `location_page_kind` — index, map, gazetteer, snapshot, archive, none — is a
whole-SURFACE artefact refetched on a walk cadence (sreality walks its index 24x/day) and
is NEVER archived. The three gates that used to stand here (`payload_dual_write`, a second
`payload_index_archive`, and a "has this surface been weighed" check against a frozen
measurement corpus) are gone with the modules that read them.

The archive is stubbed in most of this module, deliberately: a fake connection cannot tell
you that an unchanged refetch collided, and the questions that matter here are "was it
called, once, with the portal's own bytes" — see tests/test_payload_dual_write_live.py for
the same path executed end to end.
"""

from __future__ import annotations

import hashlib
import json
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
        return (1,)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _FakeConn:
    autocommit = True

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


@pytest.fixture
def appended(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every append_payload call the scrape makes, as its kwargs."""
    calls: list[dict[str, Any]] = []

    def _record(conn: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("location_data.payloads.append_payload", _record)
    return calls


_PAGE = "<html><body><h1>Byt 3+1</h1></body></html>\n"


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


# ------------------------------------------------ a detail body is always archived

def test_a_detail_page_appends_exactly_one_payload(
    appended: list[dict[str, Any]],
) -> None:
    conn = _FakeConn()

    assert _archive(conn) == 1

    assert len(appended) == 1
    call = appended[0]
    assert call["source"] == "idnes"
    assert call["source_id_native"] == "123"
    assert call["page_kind"] == "detail"
    assert call["body"] == _PAGE.encode("utf-8")
    assert call["content_type"] == "text/html"
    assert call["http_status"] == 200
    assert call["contract_version"] is None
    assert call["observed_at"].tzinfo is not None


def test_the_archive_costs_no_gate_read_at_all(
    appended: list[dict[str, Any]],
) -> None:
    """The gate used to be two cached SELECTs per source plus an app_settings flag read.
    It is a page_kind comparison now, so fifty pages ask the database nothing beyond the
    fifty staging writes themselves."""
    conn = _FakeConn()

    for _ in range(50):
        _archive(conn)

    assert [e for e in conn.executed if e[0].startswith("SELECT")] == []
    assert len([e for e in conn.executed if "INSERT INTO" in e[0]]) == 50
    assert len(appended) == 50


def test_a_non_detail_body_never_touches_the_body_thunk() -> None:
    # sreality's index payload is multi-MB and this hook sits in the hourly walk: a
    # surface that is never archived must not serialise, encode or even read it.
    conn = _FakeConn()
    calls: list[int] = []

    db.append_payload_if_enabled(
        conn, source="sreality", source_id_native="k", page_kind="index",
        body=lambda: calls.append(1) or b"{}",
    )

    assert calls == []


def test_the_freshness_guard_does_not_suppress_the_archive(
    appended: list[dict[str, Any]],
) -> None:
    # A staging row young enough to skip says nothing about whether the CONTENT
    # moved; an append-on-change archive that drops a changed body cannot
    # recover it, and an unchanged one costs a no-op DO UPDATE.
    conn = _FakeConn()

    _archive(conn, refresh_after_hours=24.0)

    assert len(appended) == 1


def test_a_body_that_is_json_is_archived_as_json(
    appended: list[dict[str, Any]],
) -> None:
    # The chokepoint takes both HTML and JSON through one `html` parameter, and
    # content_type decides how the body normalises — so it is sniffed, never assumed.
    conn = _FakeConn()

    db.upsert_portal_raw_page(
        conn, source="mmreality", source_id_native="55",
        source_url="u", page_kind="detail", html='{"property": {}}',
        http_status=200,
    )

    assert appended[0]["content_type"] == "application/json"


@pytest.mark.parametrize("module_name,class_name", [
    ("bazos", "BazosPortal"),
    ("idnes", "IdnesPortal"),
    ("remax", "RemaxPortal"),
    ("maxima", "MaximaPortal"),
    ("mmreality", "MmRealityPortal"),
    ("realitymix", "RealitymixPortal"),
    ("ceskereality", "CeskerealityPortal"),
])
def test_every_html_portal_archives_one_body_per_detail_write(
    module_name: str, class_name: str, appended: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One chokepoint edit, seven portals, zero per-portal branches (rule #21).
    import importlib

    from scraper.portal_runner import DrainItem

    module = importlib.import_module(f"scraper.{module_name}_main")
    monkeypatch.setattr(module.db, "ingest_scraped_listing", lambda *a, **k: (7, "new"))
    monkeypatch.setattr(module.db, "record_media", lambda *a, **k: 0)
    monkeypatch.setattr(module.db, "mark_portal_page_parsed", lambda *a, **k: None)

    class _Listing:
        raw = {"image_urls": []}

    item = DrainItem("42", "ok", {
        "url": "https://x/y", "html": _PAGE, "status": 200, "listing": _Listing(),
    })
    conn = _FakeConn()
    # write_details reads only module-level SOURCE, so skip the PortalConfig.
    object.__new__(getattr(module, class_name)).write_details(conn, [item])

    assert len(appended) == 1
    assert appended[0]["source"] == module_name
    assert appended[0]["source_id_native"] == "42"
    assert appended[0]["page_kind"] == "detail"
    assert appended[0]["body"] == _PAGE.encode("utf-8")


def test_a_replayed_batch_re_appends_the_SAME_bytes(
    appended: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _flush_drain_batch replays the whole write op after a transient pooler
    # drop. The archive needs no observation token for that (the churn counters
    # do) precisely because the append is content-addressed: identical bytes ->
    # identical payload_sha256 -> the store's ON CONFLICT bumps last_observed_at
    # instead of writing a second version.
    from scraper import idnes_main
    from scraper.portal_runner import DrainItem

    monkeypatch.setattr(idnes_main.db, "ingest_scraped_listing", lambda *a, **k: (7, "new"))
    monkeypatch.setattr(idnes_main.db, "record_media", lambda *a, **k: 0)
    monkeypatch.setattr(idnes_main.db, "mark_portal_page_parsed", lambda *a, **k: None)

    class _Listing:
        raw = {"image_urls": []}

    items = [DrainItem("42", "ok", {
        "url": "https://x/y", "html": _PAGE, "status": 200, "listing": _Listing(),
    })]
    conn = _FakeConn()
    portal = object.__new__(idnes_main.IdnesPortal)
    portal.write_details(conn, items)
    portal.write_details(conn, items)

    assert len(appended) == 2
    first, second = appended
    assert first["body"] == second["body"]
    assert (first["source"], first["source_id_native"], first["page_kind"]) == (
        second["source"], second["source_id_native"], second["page_kind"])


# ------------------------------------------------- the two portals with no
# ------------------------------------------------- chokepoint of their own

def test_sreality_detail_archives_the_unwrapped_untrimmed_estate_json(
    appended: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 02 section 2.3.2 P3: sreality's body is the entire estate JSON, untrimmed
    # — that is what makes the portal re-minable from the archive alone.
    from scraper import main as scraper_main
    from scraper.portal_runner import DrainItem

    monkeypatch.setattr(scraper_main.db, "write_detail_batch", lambda *a, **k: {})
    raw = {"name": "Byt 3+1", "locality": {"value": "Praha"}, "_embedded": {"x": [1]}}
    conn = _FakeConn()
    items = [
        DrainItem("1", "ok", scraper_main.FetchResult(1, "ok", raw=raw)),
        DrainItem("2", "gone", scraper_main.FetchResult(2, "gone")),
        DrainItem("3", "error", scraper_main.FetchResult(3, "error", source="fetch")),
    ]

    scraper_main.SrealityPortal().write_details(conn, items)

    assert len(appended) == 1
    call = appended[0]
    assert (call["source"], call["source_id_native"], call["page_kind"]) == (
        "sreality", "1", "detail")
    assert call["content_type"] == "application/json"
    assert json.loads(call["body"]) == raw
    # No status is carried on FetchResult; NULL ranks WITH the successes in the
    # store's retention order, so it is the honest value, not a fabricated 200.
    assert call["http_status"] is None


def test_sreality_index_pages_stage_week_stamped_and_are_never_archived(
    appended: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The index archiver still STAGES into portal_raw_pages (latest-wins, week-stamped
    # keys, or it would roll over in place instead of accumulating) — and the
    # append-on-change payload archive never sees it: an index page re-orders on every
    # walk, so one listing's claims can never be mined from it.
    from scraper import main as scraper_main

    monkeypatch.setattr(scraper_main.db, "index_archive_week", lambda: "2026w33")
    monkeypatch.setattr(scraper_main.db, "fresh_index_page_keys", lambda *a, **k: set())

    class _Client:
        category_main = 1
        category_type = 2
        locality_district_id = 5

    conn = _FakeConn()
    archive = scraper_main._index_page_archiver(_Client(), conn, dry_run=False)
    archive(20, "https://sreality.cz/api", {"_embedded": {"estates": []}})

    staged = [e for e in conn.executed if "portal_raw_pages" in e[0]]
    assert staged and "1/2/5/20/2026w33" in str(staged[0][1])
    assert appended == []


def test_a_freshness_skipped_index_page_stages_nothing(
    appended: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The client-side freshness skip keeps the highest-churn artefact in the system off
    # the staging table on every hourly walk. It costs nothing now that index bodies are
    # never payload-archived: the only thing behind the skip is a latest-wins staging row.
    from scraper import main as scraper_main

    monkeypatch.setattr(scraper_main.db, "index_archive_week", lambda: "2026w33")
    key = "1/2/all/0/2026w33"
    monkeypatch.setattr(scraper_main.db, "fresh_index_page_keys", lambda *a, **k: {key})

    class _Client:
        category_main = 1
        category_type = 2
        locality_district_id = None

    conn = _FakeConn()
    archive = scraper_main._index_page_archiver(_Client(), conn, dry_run=False)
    archive(0, "https://sreality.cz/api", {"_embedded": {"estates": []}})

    assert appended == []
    assert not [e for e in conn.executed if "portal_raw_pages" in e[0]]


def test_sreality_probe_category_never_archives(
    appended: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The realtime delta probe runs every few minutes and walks the same index
    # pages; archiving from it would multiply the index archive's write rate by
    # the probe cadence for no new information.
    from scraper import main as scraper_main
    from scraper.rate_limit import RateLimiter

    class _Client:
        per_page = 20
        result_size = 1

        def fetch_index_page(self, offset: int) -> list[dict[str, Any]]:
            return [{"hash_id": 7, "price_czk": 1}]

    monkeypatch.setattr(scraper_main, "_build_client", lambda *a, **k: _Client())
    monkeypatch.setattr(scraper_main.db, "index_summary", lambda *a, **k: {})
    monkeypatch.setattr(scraper_main.db, "enqueue_detail", lambda *a, **k: 1)
    monkeypatch.setattr(scraper_main.db, "touch_listings", lambda *a, **k: None)
    conn = _FakeConn()

    scraper_main.SrealityPortal().probe_category(
        (1, 1), conn, False, RateLimiter(10.0), probe_pages=2,
    )

    assert appended == []
    assert not [e for e in conn.executed if "portal_raw_pages" in e[0]]


def test_remax_page_capped_probe_still_never_archives(
    appended: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # remax's guard is `archive_ok = conn is not None and not self._max_pages`:
    # a transient probe fetch must not claim a page's daily archive slot ahead
    # of the full 6h walk. It gates the payload archive for the same reason.
    from types import SimpleNamespace

    from scraper import remax_main
    from scraper.portal import PortalConfig

    category = {"category_main": "byt", "category_type": "prodej", "sale": 1}

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        def fetch_index(self, *, sale: Any = None, stranka: Any = None) -> Any:
            return ("<html><body>x</body></html>", 200)

    class _Limiter:
        def acquire(self) -> None: ...
        def penalize(self) -> None: ...

    monkeypatch.setattr(
        remax_main, "parse_index",
        lambda _h: SimpleNamespace(total=0, next_offset=None, items=[]),
    )
    monkeypatch.setattr(remax_main, "RemaxClient", _Client)
    monkeypatch.setattr(remax_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(remax_main.db, "enqueue_detail", lambda *a, **k: 0)
    monkeypatch.setattr(remax_main.db, "touch_listings", lambda *a, **k: None)
    monkeypatch.setattr(remax_main.db, "index_archive_week", lambda: "2026w33")
    monkeypatch.setattr(remax_main.db, "fresh_index_page_keys", lambda *a, **k: set())

    # BOTH gates on, so the empty result is the _max_pages guard's doing.
    conn = _FakeConn()
    portal = remax_main.RemaxPortal(PortalConfig(
        source="remax", supports_complete_walk=True,
        categories=[category], split_threshold=None,
    ), max_pages=1)
    portal.walk_category(category, conn, False, _Limiter())

    assert appended == []
    assert not [e for e in conn.executed if "portal_raw_pages" in e[0]]


def test_bezrealitky_archives_the_query_beside_the_data(
    appended: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 02 section 2.3.2 P3: a graphql payload is only as wide as its query, so the
    # body carries the query text + sha256 alongside the VERBATIM response —
    # never listing.raw, which the parser has already added image_urls to.
    from scraper import bezrealitky_main
    from scraper.portal import PortalConfig
    from scraper.portal_runner import DrainItem

    monkeypatch.setattr(
        bezrealitky_main.db, "ingest_scraped_listing", lambda *a, **k: (7, "new"),
    )
    monkeypatch.setattr(bezrealitky_main.db, "record_media", lambda *a, **k: 0)

    advert = {"id": "abc", "price": 1, "address": "Dlouhá 1"}

    class _Listing:
        source_id_native = "abc"
        raw = {**advert, "image_urls": ["https://img/1.jpg"]}

    conn = _FakeConn()
    portal = bezrealitky_main.BezrealitkyPortal(PortalConfig(
        source="bezrealitky", supports_complete_walk=True,
        categories=[{"offer_type": "PRODEJ", "estate_type": "BYT"}],
        split_threshold=None,
    ))
    portal.write_details(
        conn, [DrainItem("abc", "ok", {"listing": _Listing(), "advert": advert})],
    )

    assert len(appended) == 1
    call = appended[0]
    assert (call["source"], call["source_id_native"], call["page_kind"]) == (
        "bezrealitky", "abc", "detail")
    assert call["content_type"] == "application/json"
    body = json.loads(call["body"])
    assert body["data"] == advert
    assert "image_urls" not in body["data"]
    assert "advert(id: $id)" in body["query"]
    assert body["query_sha256"] == hashlib.sha256(
        body["query"].encode("utf-8")).hexdigest()


def test_bezrealitky_skips_the_archive_when_the_verbatim_advert_is_absent(
    appended: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Falling back to listing.raw would archive the parser's derived keys as if
    # the portal had sent them, which is exactly what a substrate must not do.
    from scraper import bezrealitky_main
    from scraper.portal import PortalConfig
    from scraper.portal_runner import DrainItem

    monkeypatch.setattr(
        bezrealitky_main.db, "ingest_scraped_listing", lambda *a, **k: (7, "new"),
    )
    monkeypatch.setattr(bezrealitky_main.db, "record_media", lambda *a, **k: 0)

    class _Listing:
        source_id_native = "abc"
        raw = {"id": "abc", "image_urls": []}

    conn = _FakeConn()
    portal = bezrealitky_main.BezrealitkyPortal(PortalConfig(
        source="bezrealitky", supports_complete_walk=True, categories=[],
        split_threshold=None,
    ))
    portal.write_details(conn, [DrainItem("abc", "ok", {"listing": _Listing()})])

    assert appended == []


# ------------------------------------------------------------- failure modes

def test_an_append_failure_never_reaches_the_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _FakeConn()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("payload store unreachable")

    monkeypatch.setattr("location_data.payloads.append_payload", boom)

    assert _archive(conn) == 1
    assert [e for e in conn.executed if "INSERT INTO portal_raw_pages" in e[0]]


def test_a_body_thunk_that_raises_is_swallowed_like_any_other_failure(
    monkeypatch: pytest.MonkeyPatch, appended: list[dict[str, Any]],
) -> None:
    conn = _FakeConn()

    def boom() -> bytes:
        raise TypeError("Object of type Decimal is not JSON serializable")

    db.append_payload_if_enabled(
        conn, source="sreality", source_id_native="k", page_kind="detail", body=boom,
    )

    assert appended == []


# --------------------------------------------- the grain rule, surface by surface

def _archive_index(conn: _FakeConn, source: str = "sreality") -> int | None:
    return db.upsert_portal_raw_page(
        conn,
        source=source,
        source_id_native="1/2/all/0/2026w33",
        source_url="https://sreality.cz/api",
        page_kind="index",
        html='{"_embedded": {"estates": []}}',
        http_status=200,
    )


def test_an_index_page_stages_but_never_archives(appended: list[dict[str, Any]]) -> None:
    conn = _FakeConn()

    assert _archive_index(conn) == 1

    assert appended == []


def test_every_non_detail_page_kind_is_refused_and_detail_is_not(
    appended: list[dict[str, Any]],
) -> None:
    """The invariant is about GRAIN, not about the word "index": `detail` is one
    listing's body — the substrate the claim lane mines — and every other
    `location_page_kind` label is a whole-surface artefact on a walk cadence."""
    conn = _FakeConn()
    for kind in ("index", "map", "gazetteer", "snapshot", "archive", "none"):
        db.append_payload_if_enabled(
            conn, source="sreality", source_id_native=f"k/{kind}",
            page_kind=kind, body=b"{}",
        )
    assert appended == []

    db.append_payload_if_enabled(
        conn, source="sreality", source_id_native="k", page_kind="detail", body=b"{}",
    )
    assert [c["page_kind"] for c in appended] == ["detail"]


def test_the_gate_is_a_pure_function_of_the_page_kind() -> None:
    for source in ("sreality", "bazos", "idnes", "bezrealitky", "maxima",
                   "mmreality", "remax", "ceskereality", "realitymix"):
        assert db._payload_archive_enabled(source, "detail") is True
        assert db._payload_archive_enabled(source, "index") is False
