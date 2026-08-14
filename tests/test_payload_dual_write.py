"""W2a-2: the payload archive must be invisible until a portal switches it on,
and must never be able to break the scrape it rides in.

`location_data.payloads.append_payload` owns the store's semantics and is tested
against the replayed schema (tests/location_data/test_payloads.py). What is tested
here is the WIRING: which fetches reach it, with which body, how often, and what
happens when it fails — on the hot chokepoint every HTML portal writes through
plus the two portals that stage no body at all.

The archive is stubbed in most of this module, deliberately: a fake connection
cannot tell you that an unchanged refetch collided, and the questions that matter
here are "was it called, once, with the portal's own bytes" — see
tests/test_payload_dual_write_live.py for the same path executed end to end.
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
        last = self._conn.executed[-1][0]
        if "scraper_limits_global" in last:
            return None
        if "FROM portals" in last:
            source = (self._conn.executed[-1][1] or ("?",))[0]
            return (True, [], None, {
                "payload_dual_write": self._conn.enabled_for.get(source, False),
                "payload_index_archive": self._conn.index_enabled_for.get(source, False),
            })
        if "FROM app_settings" in last:
            return None  # the W2a-0 shadow-hash instrument stays off here
        return (1,)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


# W2a-7's third gate is not a flag: the chokepoint refuses a (source, page_kind)
# whose page weight is not in `payload_budget.PORTAL_STORAGE`, so archiving a surface
# cannot silently invalidate the storage ceiling the operator signed. The frozen corpus
# carries only `detail`, and this module is about the WIRING of the other surfaces — so
# it declares them measured for the duration, and asserts the refusal itself in
# `test_an_unmeasured_surface_is_refused_by_the_chokepoint` below.
@pytest.fixture(autouse=True)
def _every_surface_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    from location_data import payload_budget

    monkeypatch.setattr(payload_budget, "PORTAL_STORAGE", (
        *payload_budget.PORTAL_STORAGE,
        *(payload_budget.PortalStorage(p.source, kind, 8_000, 1_000, 1_000, "test")
          for p in payload_budget.PORTAL_STORAGE
          for kind in ("index", "map", "gazetteer", "snapshot", "archive", "none")),
    ))


def test_an_unmeasured_surface_is_refused_by_the_chokepoint(
    appended: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both flags on, page weight unknown, body dropped. Archiving a surface nobody
    has costed would not break anything visibly — it would make the ceiling wrong by
    an unknown amount, and index surfaces are precisely the unprofiled, week-stamped,
    ~100 %-churn ones. Refusing here is what forces the measurement first."""
    from location_data import payload_budget

    monkeypatch.setattr(payload_budget, "PORTAL_STORAGE", tuple(
        p for p in payload_budget.PORTAL_STORAGE if p.page_kind == "detail"))
    conn = _FakeConn("sreality", index_on=("sreality",))

    db.upsert_portal_raw_page(
        conn, source="sreality", source_id_native="k", page_kind="index",
        source_url="u", html='{"_embedded": {}}', http_status=200,
    )

    assert appended == []


class _FakeConn:
    autocommit = True

    def __init__(self, *sources_on: str, index_on: tuple[str, ...] = ()) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.enabled_for = {s: True for s in sources_on}
        # W2a-6's second gate. An index-kind write needs BOTH, so a test about
        # anything else on an index page has to switch this on too — otherwise it
        # would pass on the new gate being off and stop testing what it names.
        self.index_enabled_for = {s: True for s in index_on}

    def cursor(self) -> _Cur:
        return _Cur(self)


@pytest.fixture(autouse=True)
def _cold_gate_cache() -> Any:
    # The limit is cached per source for _FLAG_CACHE_TTL seconds, so every test
    # must start cold or it reads the previous test's portal registry.
    db.clear_app_settings_flag_cache()
    yield
    db.clear_app_settings_flag_cache()


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


# --------------------------------------------------------------- gate is OFF

def test_gate_off_appends_nothing(appended: list[dict[str, Any]]) -> None:
    conn = _FakeConn()

    assert _archive(conn) == 1

    assert appended == []
    writes = [e for e in conn.executed if "INSERT INTO" in e[0]]
    assert len(writes) == 1
    assert "portal_raw_pages" in writes[0][0]


def test_gate_off_costs_two_cached_selects_and_nothing_else(
    appended: list[dict[str, Any]],
) -> None:
    # Pin the shape: the gate resolves through the standard limit precedence
    # (global underlay, then the portal row), and that is ALL it may cost.
    conn = _FakeConn()

    for _ in range(50):
        _archive(conn)

    reads = [e for e in conn.executed if e[0].startswith("SELECT")]
    # Three gate reads for fifty pages: the W2a-0 shadow-hash flag, then the two
    # SELECTs behind this gate — the global underlay and the portal row.
    assert len(reads) == 3
    assert reads[0][1] == ("location_payload_shadow_hash",)
    assert "scraper_limits_global" in reads[1][0]
    assert "FROM portals" in reads[2][0]
    assert len([e for e in conn.executed if "INSERT INTO" in e[0]]) == 50


def test_gate_off_never_touches_the_body() -> None:
    # sreality's index payload is multi-MB and this hook sits in the hourly walk:
    # a disabled archive must not serialise, encode or even read it.
    conn = _FakeConn()
    calls: list[int] = []

    db.append_payload_if_enabled(
        conn, source="sreality", source_id_native="k", page_kind="index",
        body=lambda: calls.append(1) or b"{}",
    )

    assert calls == []


def test_the_gate_is_per_portal_not_global(appended: list[dict[str, Any]]) -> None:
    # The cache is keyed by SOURCE: enabling the archive for one portal must not
    # start writing another portal's bodies.
    conn = _FakeConn("idnes")

    _archive(conn)
    db.upsert_portal_raw_page(
        conn, source="bazos", source_id_native="9", source_url="u",
        page_kind="detail", html=_PAGE, http_status=200,
    )

    assert [c["source"] for c in appended] == ["idnes"]


# ---------------------------------------------------------------- gate is ON

def test_gate_on_appends_exactly_one_payload_per_page(
    appended: list[dict[str, Any]],
) -> None:
    conn = _FakeConn("idnes")

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


def test_the_freshness_guard_does_not_suppress_the_archive(
    appended: list[dict[str, Any]],
) -> None:
    # A staging row young enough to skip says nothing about whether the CONTENT
    # moved; an append-on-change archive that drops a changed body cannot
    # recover it, and an unchanged one costs a no-op DO UPDATE.
    conn = _FakeConn("idnes")

    _archive(conn, refresh_after_hours=24.0)

    assert len(appended) == 1


def test_a_body_that_is_json_is_archived_as_json(
    appended: list[dict[str, Any]],
) -> None:
    # The chokepoint takes both HTML and JSON through one `html` parameter (the
    # sreality index archiver is the JSON one), and content_type decides how the
    # body normalises — so it is sniffed, never assumed.
    conn = _FakeConn("sreality", index_on=("sreality",))

    db.upsert_portal_raw_page(
        conn, source="sreality", source_id_native="1/2/all/0/2026w33",
        source_url="u", page_kind="index", html='{"_embedded": {}}',
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
    conn = _FakeConn(module_name)
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
    conn = _FakeConn("idnes")
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
    conn = _FakeConn("sreality")
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


def test_sreality_index_pages_are_archived_week_stamped(
    appended: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The index archiver goes through the chokepoint, so it needs no call site of
    # its own — but its keys must stay week-stamped (db.index_archive_week) or
    # the archive rolls over in place instead of accumulating.
    from scraper import main as scraper_main

    monkeypatch.setattr(scraper_main.db, "index_archive_week", lambda: "2026w33")
    monkeypatch.setattr(scraper_main.db, "fresh_index_page_keys", lambda *a, **k: set())

    class _Client:
        category_main = 1
        category_type = 2
        locality_district_id = 5

    conn = _FakeConn("sreality", index_on=("sreality",))
    archive = scraper_main._index_page_archiver(_Client(), conn, dry_run=False)
    archive(20, "https://sreality.cz/api", {"_embedded": {"estates": []}})

    assert len(appended) == 1
    assert appended[0]["source_id_native"] == "1/2/5/20/2026w33"
    assert appended[0]["page_kind"] == "index"
    assert appended[0]["content_type"] == "application/json"


def test_a_freshness_skipped_index_page_is_not_archived(
    appended: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pins today's behaviour, and it is a KNOWN GAP, not a preference: the skip
    # keeps the highest-churn artefact in the system from being archived on every
    # hourly walk, but it also drops an index page that genuinely CHANGED inside
    # the freshness window — which is exactly what upsert_portal_raw_page refuses
    # to do everywhere it controls. W2a-6's index-coverage audit measures it before
    # P2 decides the fix; this test is where that decision will show up.
    from scraper import main as scraper_main

    monkeypatch.setattr(scraper_main.db, "index_archive_week", lambda: "2026w33")
    key = "1/2/all/0/2026w33"
    monkeypatch.setattr(scraper_main.db, "fresh_index_page_keys", lambda *a, **k: {key})

    class _Client:
        category_main = 1
        category_type = 2
        locality_district_id = None

    # BOTH gates on, so what this pins is the skip and not W2a-6's index gate.
    conn = _FakeConn("sreality", index_on=("sreality",))
    archive = scraper_main._index_page_archiver(_Client(), conn, dry_run=False)
    archive(0, "https://sreality.cz/api", {"_embedded": {"estates": []}})

    assert appended == []


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
    # BOTH gates on: the probe archives nothing because it never attaches the
    # on_page archiver, not because a flag happened to be off.
    conn = _FakeConn("sreality", index_on=("sreality",))

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
    conn = _FakeConn("remax", index_on=("remax",))
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

    conn = _FakeConn("bezrealitky")
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

    conn = _FakeConn("bezrealitky")
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
    conn = _FakeConn("idnes")

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("payload store unreachable")

    monkeypatch.setattr("location_data.payloads.append_payload", boom)

    assert _archive(conn) == 1
    assert [e for e in conn.executed if "INSERT INTO portal_raw_pages" in e[0]]


def test_a_body_thunk_that_raises_is_swallowed_like_any_other_failure(
    monkeypatch: pytest.MonkeyPatch, appended: list[dict[str, Any]],
) -> None:
    conn = _FakeConn("sreality")

    def boom() -> bytes:
        raise TypeError("Object of type Decimal is not JSON serializable")

    db.append_payload_if_enabled(
        conn, source="sreality", source_id_native="k", page_kind="detail", body=boom,
    )

    assert appended == []


def test_a_limit_read_failure_reads_as_off_and_is_not_retried_per_page(
    monkeypatch: pytest.MonkeyPatch, appended: list[dict[str, Any]],
) -> None:
    # An unreadable registry row must not re-ask twice for every page of a walk.
    import scraper.portal as portal_module

    calls: list[int] = []

    def boom(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise RuntimeError("portals unreachable")

    monkeypatch.setattr(portal_module, "load_portal_config", boom)
    conn = _FakeConn("idnes")

    for _ in range(10):
        assert _archive(conn) == 1

    assert appended == []
    assert len(calls) == 1


# ------------------------------------------- W2a-6: the index-only second gate

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


def test_an_index_page_needs_both_gates(appended: list[dict[str, Any]]) -> None:
    # AND, never OR: index bodies are the highest-churn artefact in the system
    # (02 section 2.3.2 P2), so enabling the archive for a portal must not enable
    # its index surface as a side effect.
    conn = _FakeConn("sreality")

    assert _archive_index(conn) == 1

    assert appended == []


def test_the_index_gate_alone_archives_nothing(appended: list[dict[str, Any]]) -> None:
    # The narrowing direction: payload_index_archive can never open a path
    # payload_dual_write has closed, on any page_kind.
    conn = _FakeConn(index_on=("sreality",))

    assert _archive_index(conn) == 1
    assert _archive(conn) == 1

    assert appended == []


def test_both_gates_on_archives_the_index_body(appended: list[dict[str, Any]]) -> None:
    conn = _FakeConn("sreality", index_on=("sreality",))

    assert _archive_index(conn) == 1

    assert len(appended) == 1
    assert appended[0]["page_kind"] == "index"


def test_the_index_gate_does_not_hold_back_detail_bodies(
    appended: list[dict[str, Any]],
) -> None:
    # The whole point of splitting the flags: a portal whose detail churn signs
    # off cheaply keeps archiving detail while its index surface stays closed.
    conn = _FakeConn("idnes")

    assert _archive(conn) == 1

    assert len(appended) == 1
    assert appended[0]["page_kind"] == "detail"


def test_the_index_gate_is_per_portal(appended: list[dict[str, Any]]) -> None:
    conn = _FakeConn("sreality", "remax", index_on=("remax",))

    _archive_index(conn, source="sreality")
    _archive_index(conn, source="remax")

    assert [c["source"] for c in appended] == ["remax"]


def test_both_gates_come_from_one_cached_registry_read(
    appended: list[dict[str, Any]],
) -> None:
    # Two limits, still ONE load_portal_config per source per TTL: this read sits
    # on a per-page path, and a second cache entry would double it.
    conn = _FakeConn("sreality", index_on=("sreality",))

    for _ in range(10):
        _archive_index(conn)

    assert len(appended) == 10
    assert len([e for e in conn.executed if "FROM portals" in e[0]]) == 1


def test_the_index_gate_off_never_touches_the_body() -> None:
    # sreality's index payload is multi-MB on the hourly walk; the second gate
    # has to short-circuit ahead of the body thunk exactly like the first.
    conn = _FakeConn("sreality")
    calls: list[int] = []

    db.append_payload_if_enabled(
        conn, source="sreality", source_id_native="k", page_kind="index",
        body=lambda: calls.append(1) or b"{}",
    )

    assert calls == []


def test_the_detail_gate_off_never_touches_the_body() -> None:
    # The twin of the index case, and NOT redundant with it: with only the index
    # kind covered, reordering the two gate checks so the body thunk is evaluated
    # between them passes every other test in this file while making every detail
    # HTML body encode on a walk with the archive off.
    conn = _FakeConn()
    calls: list[int] = []

    db.append_payload_if_enabled(
        conn, source="idnes", source_id_native="k", page_kind="detail",
        body=lambda: calls.append(1) or b"<html></html>",
    )

    assert calls == []


def test_a_map_body_needs_the_second_gate_too(appended: list[dict[str, Any]]) -> None:
    # ceskereality's /mapa/ surface declares `archive: true` in its contract and is
    # SURFACE grain, not listing grain — 500 markers per request, refetched on the
    # walk cadence. An allowlist naming only 'index' would archive it on every walk
    # with the second gate deliberately off.
    conn = _FakeConn("ceskereality")

    db.append_payload_if_enabled(
        conn, source="ceskereality", source_id_native="mapa/byt/prodej",
        page_kind="map", body=b'{"markers": []}',
    )

    assert appended == []


def test_a_gazetteer_body_needs_the_second_gate_too(
    appended: list[dict[str, Any]],
) -> None:
    # bezrealitky's Region.boundaryGeoJson gazetteer, same reasoning.
    conn = _FakeConn("bezrealitky")

    db.append_payload_if_enabled(
        conn, source="bezrealitky", source_id_native="region/praha",
        page_kind="gazetteer", body=b'{"type": "FeatureCollection"}',
    )

    assert appended == []


def test_every_non_detail_page_kind_passes_the_second_gate(
    appended: list[dict[str, Any]],
) -> None:
    # The invariant is about GRAIN: `detail` is one listing's body, every other
    # location_page_kind label is a whole-surface artefact on a walk cadence.
    kinds = ("index", "map", "gazetteer", "snapshot", "archive", "none")
    conn = _FakeConn("sreality")
    for kind in kinds:
        db.append_payload_if_enabled(
            conn, source="sreality", source_id_native=f"k/{kind}",
            page_kind=kind, body=b"{}",
        )
    assert appended == []

    # The limit is cached per SOURCE, so the second registry only takes effect
    # once the first one's entry is dropped.
    db.clear_app_settings_flag_cache()
    conn = _FakeConn("sreality", index_on=("sreality",))
    for kind in kinds:
        db.append_payload_if_enabled(
            conn, source="sreality", source_id_native=f"k/{kind}",
            page_kind=kind, body=b"{}",
        )
    assert [c["page_kind"] for c in appended] == list(kinds)


def test_only_detail_rides_the_dual_write_limit_alone(
    appended: list[dict[str, Any]],
) -> None:
    conn = _FakeConn("sreality")

    db.append_payload_if_enabled(
        conn, source="sreality", source_id_native="k", page_kind="detail", body=b"{}",
    )

    assert [c["page_kind"] for c in appended] == ["detail"]
