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
        # The portal registry, for W2a-2's payload_dual_write limit read: no
        # operational override, so the limit resolves to its baked-in False and
        # the archive stays out of this instrument's way.
        if "scraper_limits_global" in last:
            return None
        if "FROM portals" in last:
            return (True, [], None, {})
        if "FROM app_settings" in last:
            return None if self._conn.flag is None else (self._conn.flag,)
        return (1,)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _FakeConn:
    autocommit = True

    def __init__(self, flag: Any = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.flag = flag

    def cursor(self) -> _Cur:
        return _Cur(self)


@pytest.fixture(autouse=True)
def _no_cached_flag() -> Any:
    # The flag is cached per process for _FLAG_CACHE_TTL seconds (the per-item
    # SELECT would defeat write_detail_batch's ~4-round-trip design), so every
    # test here must start from a cold cache or it reads the previous test's flag.
    db.clear_app_settings_flag_cache()
    yield
    db.clear_app_settings_flag_cache()


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


def test_both_gates_off_cost_one_lookup_each_and_nothing_more() -> None:
    # Pin the shape so the gates-off path can't grow anything more. Two gates
    # ride this chokepoint now: the shadow-hash flag (one app_settings read) and
    # W2a-2's payload_dual_write limit (the standard two-SELECT limit
    # resolution). Both are cached per process — see the next test.
    conn = _FakeConn(flag=None)

    _archive(conn)

    assert len(conn.executed) == 4
    assert "FROM app_settings" in conn.executed[0][0]
    assert conn.executed[0][1] == ("location_payload_shadow_hash",)
    assert "INSERT INTO portal_raw_pages" in conn.executed[1][0]
    assert "scraper_limits_global" in conn.executed[2][0]
    assert "FROM portals" in conn.executed[3][0]


def test_flag_is_read_once_per_process_not_once_per_item() -> None:
    # write_detail_batch is ~4 round-trips per 100-listing batch by design; a
    # live read per item would make it ~104, i.e. seconds of Frankfurt-pooler RTT
    # per flush inside the realtime worker's time-budgeted drain lane.
    conn = _FakeConn(flag=None)

    for _ in range(50):
        _archive(conn)

    gate_reads = [
        e for e in conn.executed
        if "FROM app_settings" in e[0] or "FROM portals" in e[0]
    ]
    # One shadow-hash flag read + the two SELECTs behind the payload limit.
    assert len(gate_reads) == 3
    assert len([e for e in conn.executed if "INSERT INTO" in e[0]]) == 50


def test_flag_cache_expires_so_a_flip_reaches_the_always_on_worker() -> None:
    # The bound the cache trades for: an operator flipping the flag is picked up
    # by the always-on worker within _FLAG_CACHE_TTL seconds (a cron run starts
    # with an empty cache, so it always reads live).
    assert db._FLAG_CACHE_TTL <= 60.0
    conn = _FakeConn(flag=None)

    assert db._app_settings_flag_cached(conn, "k", ttl=-1.0) is False
    conn.flag = True
    assert db._app_settings_flag_cached(conn, "k", ttl=-1.0) is True


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
    source, native, page_kind, version = params[0], params[1], params[2], params[3]
    raw_sha, norm_sha = params[6], params[7]
    byte_size, norm_size, observation = params[8], params[9], params[10]
    assert (source, native, page_kind) == ("idnes", "123", "detail")
    assert len(raw_sha) == 32 and len(norm_sha) == 32
    assert raw_sha != norm_sha
    assert byte_size == len(_PAGE.encode("utf-8"))
    # The idnes profile drops the ad slot, the analytics script and the chrome,
    # so the normalised projection is strictly smaller than the fetched page.
    assert 0 < norm_size < byte_size
    assert version.startswith("payload_norm@")
    assert observation


def test_flag_on_still_writes_the_raw_page() -> None:
    conn = _FakeConn(flag=True)

    assert _archive(conn) == 1
    assert any("portal_raw_pages" in e[0] for e in conn.executed)


def test_record_churn_false_suppresses_the_hook_entirely() -> None:
    conn = _FakeConn(flag=True)

    _archive(conn, record_churn=False)

    # Not even the flag read: the churn hook is skipped whole. (The two limit
    # SELECTs behind W2a-2's payload gate are a different gate and still run.)
    assert _churn_statements(conn) == []
    assert not [e for e in conn.executed if e[1] == ("location_payload_shadow_hash",)]
    assert [e for e in conn.executed if "INSERT INTO" in e[0]][0][0].startswith(
        "INSERT INTO portal_raw_pages"
    )


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


def test_flag_off_never_touches_the_body() -> None:
    # sreality's index payload is multi-MB and this hook sits in the hourly walk:
    # a disabled instrument must not serialise, encode or even read it.
    conn = _FakeConn(flag=None)
    calls: list[int] = []

    db.record_payload_churn_if_enabled(
        conn, source="sreality", source_id_native="k", page_kind="index",
        body=lambda: calls.append(1) or b"{}",
    )

    assert calls == []


def test_a_body_thunk_that_raises_is_swallowed_like_any_other_failure() -> None:
    # Serialisation happens INSIDE the guard, so a value json.dumps cannot encode
    # warns instead of killing the 100-item flush it is riding in.
    conn = _FakeConn(flag=True)

    def boom() -> bytes:
        raise TypeError("Object of type Decimal is not JSON serializable")

    db.record_payload_churn_if_enabled(
        conn, source="sreality", source_id_native="k", page_kind="detail", body=boom,
    )

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
    assert pa[6] != pb[6]
    assert pa[7] == pb[7]


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


def test_a_replayed_batch_carries_the_same_observation_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _flush_drain_batch retries the WHOLE write op on a transient pooler drop
    # and the connection is autocommit, so the first attempt's churn upserts are
    # already committed. The counter bump is only safe inside that op because the
    # token identifying the FETCH is minted on the DrainItem, not per call —
    # migration 402's `WHERE ... last_observation IS DISTINCT FROM ...` then makes
    # the replay a no-op instead of a second `fetches + 1`.
    from scraper import main as scraper_main
    from scraper.portal_runner import DrainItem

    monkeypatch.setattr(scraper_main.db, "write_detail_batch", lambda *a, **k: {})
    items = [DrainItem("1", "ok", scraper_main.FetchResult(1, "ok", raw={"a": 1}))]

    conn = _FakeConn(flag=True)
    scraper_main.SrealityPortal().write_details(conn, items)
    scraper_main.SrealityPortal().write_details(conn, items)

    tokens = [e[1][10] for e in _churn_statements(conn)]
    assert len(tokens) == 2
    assert tokens[0] == tokens[1]


_BATCHED_HTML_PORTALS = [
    ("bazos", "BazosPortal"),
    ("idnes", "IdnesPortal"),
    ("remax", "RemaxPortal"),
    ("maxima", "MaximaPortal"),
    ("mmreality", "MmRealityPortal"),
    ("realitymix", "RealitymixPortal"),
    ("ceskereality", "CeskerealityPortal"),
]


@pytest.mark.parametrize("module_name,class_name", _BATCHED_HTML_PORTALS)
def test_every_html_portal_writer_threads_the_observation_token(
    module_name: str, class_name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # All seven stage their detail body through upsert_portal_raw_page, inside a
    # write_details that _flush_drain_batch replays wholesale on a transient drop.
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
    conn = _FakeConn(flag=True)
    # write_details reads only module-level SOURCE, so skip the PortalConfig.
    object.__new__(getattr(module, class_name)).write_details(conn, [item])

    churn = _churn_statements(conn)
    assert len(churn) == 1
    assert churn[0][1][10] == item.observation_id


def test_two_fetches_of_the_same_listing_carry_different_tokens() -> None:
    # The flip side: a genuine refetch must still count, so the token is per
    # DrainItem (one fetch), not per listing.
    from scraper.portal_runner import DrainItem

    assert DrainItem("1", "ok").observation_id != DrainItem("1", "ok").observation_id


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


def test_remax_index_walk_records_the_fetch_its_freshness_skip_hides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Uniform semantics across portals: the denominator is FETCHES, never archive
    # writes. remax skips re-staging a body its freshness guard would discard —
    # counting only what it stages would measure this portal's index rate over a
    # different denominator than sreality's, and the two would not be comparable.
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
    monkeypatch.setattr(
        remax_main.db, "fresh_index_page_keys", lambda *a, **k: {"1/1/2026w33"},
    )

    conn = _FakeConn(flag=True)
    portal = remax_main.RemaxPortal(PortalConfig(
        source="remax", supports_complete_walk=True,
        categories=[category], split_threshold=None,
    ))
    portal.walk_category(category, conn, False, _Limiter())

    churn = _churn_statements(conn)
    assert [(e[1][0], e[1][1], e[1][2]) for e in churn] == [
        ("remax", "1/1/2026w33", "index"),
    ]
    assert not [e for e in conn.executed if "portal_raw_pages" in e[0]]


def test_ceskereality_index_walk_records_the_fetch_its_freshness_skip_hides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from scraper import ceskereality_main
    from scraper.portal import PortalConfig

    monkeypatch.setattr(
        ceskereality_main, "parse_index",
        lambda _h: SimpleNamespace(total=0, items=[]),
    )

    class _Client:
        def fetch_search(self, url: str) -> Any:
            return ("<html><body>x</body></html>", 200)

    conn = _FakeConn(flag=True)
    portal = ceskereality_main.CeskerealityPortal(PortalConfig(
        source="ceskereality", supports_complete_walk=True,
        categories=[], split_threshold=None,
    ))
    # v2/ prefix: the key space is per WALK SHAPE — the dead v1 subdomain/facet
    # keys and these kraj keys share one UNIQUE(source, source_id_native, page_kind).
    key = "v2/prodej/byty/praha/all/1/2026w33"
    portal._walk_slice(
        _Client(), "prodej", "byty", "praha",
        conn=conn, archive_week="2026w33", fresh_keys={key},
    )

    churn = _churn_statements(conn)
    assert [(e[1][0], e[1][1], e[1][2]) for e in churn] == [
        ("ceskereality", key, "index"),
    ]
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
    assert "VALUES (%s, %s, %s::location_page_kind, %s, coalesce" in sql
    assert "1, 0, 0," in sql
    assert "fetches = portal_payload_churn.fetches + 1" in sql
    for column in ("raw", "norm"):
        assert (
            f"{column}_changes = portal_payload_churn.{column}_changes + "
            f"(portal_payload_churn.last_{column}_sha256 "
            f"IS DISTINCT FROM EXCLUDED.last_{column}_sha256)::int"
        ) in sql
    # normalizer_version is part of the conflict target, not an in-place stamp:
    # a profile change must open a clean cohort, not relabel accumulated counters.
    assert (
        "ON CONFLICT (source, source_id_native, page_kind, normalizer_version)"
    ) in sql
    assert "normalizer_version = EXCLUDED.normalizer_version" not in sql
    # ... and the replay guard that keeps a retried batch from double-counting.
    assert (
        "WHERE portal_payload_churn.last_observation "
        "IS DISTINCT FROM EXCLUDED.last_observation"
    ) in sql
    assert sql.count("%s") == 11


def _churn_params(page_kind: str, html: str) -> tuple[Any, ...]:
    """The bound params of the one churn upsert a single archived page produces."""
    conn = _FakeConn(flag=True)
    db.upsert_portal_raw_page(
        conn, source="bazos", source_id_native="k", source_url="u",
        page_kind=page_kind, html=html, http_status=200,
    )
    return _churn_statements(conn)[0][1]


def test_the_profile_and_the_cohort_are_resolved_by_source_AND_page_kind() -> None:
    """The instrument's whole output is a hash and the cohort it is counted in, and
    both are a function of the SURFACE. bazos's measured `div.inzeratyview` is the
    per-listing view counter: one node on a detail page, one per CARD on an index page
    (21 on a live one), so applying the detail profile to an index body strips content
    that was never diffed. Same bytes here, two page_kinds, and the two must differ in
    both the normalised hash and the cohort — or nothing downstream can tell them apart.
    """
    body = (
        '<html><body><div class="inzerat"><h2>Byt 3+1</h2>'
        '<div class="inzeratyview">Vidělo: 7 lidí</div></div></body></html>'
    )

    detail = _churn_params("detail", body)
    index = _churn_params("index", body)

    # params: (source, native, page_kind, normalizer_version, ..., raw, norm, ...)
    from location_data import payload_norm

    declared = payload_norm.contract_profiles().profile("bazos", "detail")
    digest = payload_norm.profile_digest(declared)[:payload_norm.PROFILE_DIGEST_CHARS]
    assert detail[2] == "detail" and index[2] == "index"
    # The declared surface names a digest of the profile its contract declares; the
    # undeclared one names the normaliser's own base and nothing else (W2a-3e).
    assert detail[3] == f"payload_norm@3+profile@{digest}"
    assert index[3] == "payload_norm@3+base"
    assert detail[6] == index[6], "the RAW hash is the bytes as fetched, surface-blind"
    assert detail[7] != index[7], "the NORMALISED hash must follow the surface's profile"
