"""The tenth portal's adapter (scraper/web_main.py) and client (scraper/web_client.py).

`web` has no index, so its walk sees nothing and still nominates (the `indexless` seam);
its page decides like every portal's (404/410, a redirect off the page); a shortener to a
portal ad becomes that portal's ad; and one host is asked at most once per 10 s.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from scraper import db, egress, portal_runner, web_client
from scraper.portal import default_config
from scraper.portal_base import ListingGoneError
from scraper.portal_identity import Resolved
from scraper.web_main import SOURCE, WebPortal, left_the_page, main

AD = "https://www.rk-novak.example/byty/15"
PAGE = """<html><head><title>Prodej bytu 2+kk 54 m², Praha 6</title></head><body>
<article><p>Prodáváme byt 2+kk v Praze 6, cena 5 990 000 Kč, ve 2. patře cihlového domu.</p></article>
</body></html>"""


def _portal() -> WebPortal:
    return WebPortal(default_config(SOURCE))


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append((sql, params))

    def fetchone(self) -> Any:
        return (self.conn.active,)

    def fetchall(self) -> Any:
        return self.conn.rows


class _Conn:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None, active: int = 0) -> None:
        self.rows, self.active = rows or [], active
        self.executed: list[tuple[str, Any]] = []
        self.closed = False

    def cursor(self) -> _Cur:
        return _Cur(self)

    def close(self) -> None:
        self.closed = True


# --- the walk: no index, and still a nomination ---------------------------------------------


def test_the_config_is_one_agenda_with_the_shared_limiter() -> None:
    config = default_config(SOURCE)
    assert config.categories == [{"agenda": "all"}] and config.supports_complete_walk
    assert config.limits.shared_rate_limiter is True and config.limits.detail_workers == 2
    portal = _portal()
    assert portal.indexless is True and portal.shared_rate_limiter is True
    assert portal.category_labels(config.categories[0]) == (None, None)


def test_the_indexless_walk_reaches_the_end_and_nominates_null_category_rows(monkeypatch) -> None:
    """Every active web ad due a check is nominated, a NULL category included: its one unit has
    no scope, so the whole agenda is the runner's orphan scope, with no seen set to exclude."""
    rows = [("https://a.example/1", "https://a.example/1", 4_500_000),
            ("https://b.example/x?id=2", "https://b.example/x?id=2", None)]
    conn = _Conn(rows=rows, active=5)
    queued: list[Any] = []
    monkeypatch.setattr(portal_runner.db, "enqueue_presence_checks",
                        lambda c, src, cm, ct, cands, *, active_rows, subtype=None:
                        queued.append((src, cm, ct, cands, active_rows)) or (len(cands), 0))
    portal = _portal()
    monkeypatch.setattr(portal, "connect_index", lambda: conn)
    rc, agg = portal_runner.run_index_walk(portal, dry_run=False)
    assert rc == 0 and conn.closed
    assert queued == [(SOURCE, None, "", rows, 5)]
    unit, orphans = agg["by_category"]   # both NULL-labelled: its unit, then its orphans
    assert [(e["category_main"], e["walk_reached_end"])
            for e in (unit, orphans)] == [(None, True), (None, True)]
    assert (unit["listings_to_verify"], orphans["listings_to_verify"]) == (0, 2)
    presence_sql, params = conn.executed[-1]
    assert "category_main" not in presence_sql and "<> ALL" not in presence_sql
    assert params == (SOURCE,) and db.NOMINABLE_PREDICATE in presence_sql


def test_a_walk_whose_budget_is_already_spent_nominates_nothing(monkeypatch) -> None:
    conn = _Conn(rows=[("https://a.example/1", "https://a.example/1", None)], active=1)
    queued: list[Any] = []
    monkeypatch.setattr(portal_runner.db, "enqueue_presence_checks",
                        lambda *a, **k: queued.append(a) or (1, 0))
    portal = _portal()
    assert portal.walk_category({"agenda": "all"}, conn, False, None, deadline=0.0)[4] is False
    monkeypatch.setattr(portal, "walk_category",
                        lambda c, conn, d, lim, deadline=None: (set(), {}, 0, 0, False))
    monkeypatch.setattr(portal, "connect_index", lambda: conn)
    portal_runner.run_index_walk(portal, dry_run=False)
    assert queued == []


def test_the_probe_lane_is_quiet() -> None:
    portal = _portal()
    assert portal.set_index_page_cap(3) is None
    assert portal.probe_category({"agenda": "all"}, None, True, None, 3)[:4] == (set(), {"found_new": 0, "enqueued": 0}, 0, 0)
    rc, agg = portal_runner.run_index_probe(portal, dry_run=True, probe_pages=2)
    assert rc == 0 and agg["index_pages"] == 0 and agg["listings_enqueued"] == 0


def test_the_cli_dry_run_writes_nothing(monkeypatch) -> None:
    monkeypatch.setattr(db, "connect", lambda *a, **k: pytest.fail("a dry run touched the DB"))
    assert main(["--index-only", "--dry-run", "--max-seconds", "10"]) == 0


# --- the page decides --------------------------------------------------------------------------


class _Client:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def fetch_detail(self, url: str) -> tuple[str, int, str]:
        self.asked.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_a_page_is_parsed_under_the_queued_identity() -> None:
    client = _Client((PAGE, 200, AD + "/"))
    item = _portal().fetch_detail(client, AD, "http://www.rk-novak.example/byty/15/?utm_source=x")
    assert client.asked == ["http://www.rk-novak.example/byty/15/?utm_source=x"]
    assert item.kind == "ok"
    listing = item.payload["listing"]
    assert listing.source_id_native == listing.source_url == AD == item.payload["url"]
    assert (listing.price_czk, listing.disposition, listing.category_type) == (5_990_000, "2+kk", "prodej")


@pytest.mark.parametrize("status", [404, 410])
def test_a_404_or_410_is_gone(status: int) -> None:
    item = _portal().fetch_detail(_Client(ListingGoneError(AD, status)), AD, AD)
    assert item.kind == "gone"


def test_a_fetch_error_is_an_error_the_queue_counts() -> None:
    item = _portal().fetch_detail(_Client(requests.HTTPError("503")), AD, AD)
    assert item.kind == "error" and "503" in (item.error or "")
    refused = _portal().fetch_detail(_Client(egress.EgressRefused("10.0.0.1")), AD, AD)
    assert refused.kind == "error"


def test_a_redirect_off_the_page_is_gone() -> None:
    for final in ("https://www.rk-novak.example/", "https://www.rk-novak.example/byty",
                  "https://elsewhere.example/byty/15"):
        assert _portal().fetch_detail(_Client((PAGE, 200, final)), AD, AD).kind == "gone", final


def test_a_noncanonical_queue_id_is_an_error_never_a_second_row() -> None:
    item = _portal().fetch_detail(_Client((PAGE, 200, AD)), AD + "/", AD + "/")
    assert item.kind == "error" and "canonical" in (item.error or "")


@pytest.mark.parametrize("requested, final, left", [
    (AD, AD, False),
    (AD, AD + "/", False),
    ("http://rk-novak.example/byty/15", AD, False),             # scheme + www
    (AD, "https://www.rk-novak.example/byty/15-novy-slug", False),  # a sibling slug
    (AD, "https://www.rk-novak.example/cs/byty/15", False),     # a locale prefix
    (AD, "https://www.rk-novak.example/byty/15?foto=1", False),
    (AD + "?utm_source=fb", AD, False),                         # only tracking dropped
    (AD, "https://www.rk-novak.example/", True),                # the home page
    (AD, "https://www.rk-novak.example/byty", True),            # the listing index
    (AD, "https://jiny-web.example/byty/15", True),             # another site
    ("https://x.example/detail?id=5", "https://x.example/detail", True),  # the id dropped
    ("https://x.example/detail?id=5", "https://x.example/detail?id=5&lang=cs", False),
])
def test_what_leaves_the_page(requested: str, final: str, left: bool) -> None:
    assert left_the_page(requested, final) is left


# --- a shortener to a portal ad is that portal's ad -----------------------------------------------

SREALITY_AD = "https://www.sreality.cz/detail/prodej/byt/2+kk/praha-dejvice/2557083724"
IDNES_AD = "https://reality.idnes.cz/detail/prodej/byt/praha/6ad7ca9d45dd870c0a0b152a/"


def test_a_redirect_to_a_portal_ad_rekeys_it() -> None:
    item = _portal().fetch_detail(_Client(("<html></html>", 200, SREALITY_AD)),
                                  "https://bit.example/abc", "https://bit.example/abc")
    assert item.kind == "ok"
    target = item.payload["rekey"]
    assert isinstance(target, Resolved) and (target.source, target.native_id) == ("sreality", "2557083724")


def test_the_write_step_queues_the_portal_ad_records_the_web_gone_and_writes_the_rest(monkeypatch) -> None:
    calls: dict[str, list[Any]] = {"enqueue": [], "gone": [], "default": []}
    monkeypatch.setattr(db, "enqueue_detail",
                        lambda c, src, entries: calls["enqueue"].append((src, entries)) or len(entries))
    monkeypatch.setattr(portal_runner, "record_gone_verdict",
                        lambda c, src, nid, now=None: calls["gone"].append((src, nid)) or "no_row")
    monkeypatch.setattr(portal_runner.PortalDefaults, "write_details",
                        lambda self, c, items: calls["default"].append([i.native_id for i in items])
                        or {"new": len(items), "updated": 0, "unchanged": 0, "images_discovered": 0})
    portal = _portal()
    sreality = portal.fetch_detail(_Client(("", 200, SREALITY_AD)), "https://bit.example/a", "https://bit.example/a")
    idnes = portal.fetch_detail(_Client(("", 200, IDNES_AD)), "https://bit.example/b", "https://bit.example/b")
    page = portal.fetch_detail(_Client((PAGE, 200, AD)), AD, AD)
    out = portal.write_details(object(), [sreality, idnes, page])
    assert calls["enqueue"] == [
        ("sreality", [("2557083724", None, None, db.QUEUE_PRIORITY_NEW)]),
        ("idnes", [("6ad7ca9d45dd870c0a0b152a", IDNES_AD, None, db.QUEUE_PRIORITY_NEW)]),
    ]
    assert calls["gone"] == [(SOURCE, "https://bit.example/a"), (SOURCE, "https://bit.example/b")]
    assert calls["default"] == [[AD]] and out["new"] == 1


def test_a_flush_of_only_rekeys_writes_no_listing(monkeypatch) -> None:
    monkeypatch.setattr(db, "enqueue_detail", lambda *a: 1)
    monkeypatch.setattr(portal_runner, "record_gone_verdict", lambda *a, **k: "no_row")
    monkeypatch.setattr(portal_runner.PortalDefaults, "write_details",
                        lambda *a: pytest.fail("nothing to write"))
    portal = _portal()
    item = portal.fetch_detail(_Client(("", 200, SREALITY_AD)), "https://bit.example/a", "https://bit.example/a")
    assert portal.write_details(object(), [item]) == {
        "new": 0, "updated": 0, "unchanged": 0, "images_discovered": 0}


def test_a_never_written_rekeyed_row_is_dequeued_as_gone_through_the_runners_step(monkeypatch) -> None:
    """The real gone step: no listing row, so the web queue row completes 'gone' (the ingest
    poll reads it) and nothing is flipped."""
    completed: list[Any] = []
    monkeypatch.setattr(db, "enqueue_detail", lambda *a: 1)
    monkeypatch.setattr(db, "gone_evidence", lambda c, src, nid: (False, None))
    monkeypatch.setattr(db, "mark_listing_inactive", lambda *a: pytest.fail("flipped an unborn row"))
    monkeypatch.setattr(db, "complete_detail",
                        lambda c, src, ids, outcome="written": completed.append((src, ids, outcome)))
    portal = _portal()
    item = portal.fetch_detail(_Client(("", 200, SREALITY_AD)), "https://bit.example/a", "https://bit.example/a")
    portal.write_details(object(), [item])
    assert completed == [(SOURCE, ["https://bit.example/a"], "gone")]


# --- the client: egress, per-host pacing, gone statuses ---------------------------------------


@pytest.fixture
def fresh_hosts(monkeypatch) -> dict[str, float]:
    hosts: dict[str, float] = {}
    monkeypatch.setattr(web_client, "_HOST_NEXT_AT", hosts)
    return hosts


def test_one_host_is_asked_at_most_once_per_ten_seconds(fresh_hosts) -> None:
    assert web_client.reserve_host_slot("https://www.a.example/1", now=100.0) == 0.0
    assert web_client.reserve_host_slot("https://a.example/2", now=101.0) == pytest.approx(9.0)
    assert web_client.reserve_host_slot("https://a.example/3", now=101.0) == pytest.approx(19.0)
    assert web_client.reserve_host_slot("https://b.example/1", now=101.0) == 0.0
    assert web_client.reserve_host_slot("https://a.example/4", now=200.0) == 0.0


class _Limiter:
    refused, refused_reason, deadline = False, None, None

    def __init__(self, log: list[str]) -> None:
        self.log = log

    def acquire(self) -> None:
        self.log.append("limiter")


def _fake_fetch(log: list[str], status: int = 200, body: bytes = b"<html>ok</html>",
                content_type: str | None = "text/html; charset=utf-8") -> Any:
    def fetch(url: str, *, session: Any, **_: Any) -> egress.FetchResult:
        log.append(f"fetch {url}")
        return egress.FetchResult(status, url + "#final", body, content_type)
    return fetch


def test_the_client_waits_for_the_host_then_the_limiter_then_fetches(monkeypatch, fresh_hosts) -> None:
    log: list[str] = []
    monkeypatch.setattr(web_client.time, "sleep", lambda s: log.append(f"sleep {s:.0f}"))
    monkeypatch.setattr(web_client.egress, "fetch", _fake_fetch(log))
    client = web_client.WebClient(limiter=_Limiter(log))
    client.fetch_detail("https://a.example/1")
    html, status, final = client.fetch_detail("https://a.example/2")
    assert (html, status, final) == ("<html>ok</html>", 200, "https://a.example/2#final")
    assert log[:2] == ["limiter", "fetch https://a.example/1"]
    assert log[2].startswith("sleep ") and float(log[2].split()[1]) > 9
    assert log[3:] == ["limiter", "fetch https://a.example/2"]


@pytest.mark.parametrize("status, raised", [(404, ListingGoneError), (410, ListingGoneError),
                                            (500, requests.HTTPError), (403, requests.HTTPError)])
def test_the_client_raises_on_a_bad_status(monkeypatch, fresh_hosts, status: int, raised: type) -> None:
    monkeypatch.setattr(web_client.egress, "fetch", _fake_fetch([], status=status))
    with pytest.raises(raised):
        web_client.WebClient().fetch_detail("https://a.example/1")


def test_a_refused_run_fetches_nothing_and_never_waits_for_a_host(monkeypatch, fresh_hosts) -> None:
    from scraper.rate_ledger import RateBudgetUnavailable

    log: list[str] = []
    monkeypatch.setattr(web_client.time, "sleep", lambda s: log.append(f"sleep {s:.0f}"))
    monkeypatch.setattr(web_client.egress, "fetch", _fake_fetch(log))
    limiter = _Limiter(log)
    limiter.refused, limiter.refused_reason = True, "deadline"
    fresh_hosts["a.example"] = 1e12  # a slot far away: a reserving call would sleep for it
    with pytest.raises(RateBudgetUnavailable):
        web_client.WebClient(limiter=limiter).fetch_detail("https://a.example/1")
    assert log == [] and fresh_hosts["a.example"] == 1e12


def test_a_host_slot_past_the_deadline_refuses_the_run_without_reserving_it(fresh_hosts) -> None:
    assert web_client.reserve_host_slot("https://a.example/1", now=100.0, not_after=115.0) == 0.0
    assert web_client.reserve_host_slot("https://a.example/2", now=100.0, not_after=115.0) == 10.0
    assert web_client.reserve_host_slot("https://a.example/3", now=100.0, not_after=115.0) is None
    assert fresh_hosts["a.example"] == pytest.approx(120.0)  # the refused call reserved nothing


def test_same_host_ads_never_sleep_the_drain_past_its_deadline(monkeypatch, fresh_hosts) -> None:
    """Five ads of one agency in one chunk would sleep 40 s for their host slots. With a 15 s
    budget the drain fetches what starts in time; the rest refuse the run (no sleep) and go
    back to the queue untouched: no attempt spent, nothing completed."""
    from scraper.rate_limit import RateLimiter

    class _Budget(RateLimiter):
        def __init__(self) -> None:
            super().__init__(1000.0)
            self._at = web_client.time.monotonic() + 15.0

        @property
        def deadline(self) -> float | None:
            return self._at

        def acquire(self) -> None:
            return None

    slept: list[float] = []
    monkeypatch.setattr(web_client.time, "sleep", slept.append)
    monkeypatch.setattr(web_client.egress, "fetch",
                        lambda url, **_: egress.FetchResult(200, url, PAGE.encode(), "text/html"))
    budget = _Budget()
    monkeypatch.setattr(portal_runner, "build_rate_limiter", lambda *a, **kw: budget)
    ads = [f"https://www.rk-novak.example/byty/{i}" for i in range(5)]
    claims = iter([[(ad, ad, None, None) for ad in ads], []])
    log: dict[str, list[Any]] = {"failed": [], "released": [], "complete": []}
    monkeypatch.setattr(db, "reclaim_stale_claims", lambda c, src: 0)
    monkeypatch.setattr(db, "claim_detail_batch", lambda c, src, n: next(claims))
    monkeypatch.setattr(db, "queue_priorities", lambda c, src, ids: {i: db.QUEUE_PRIORITY_VERIFY for i in ids})
    monkeypatch.setattr(db, "fail_detail", lambda c, src, nid, *a, **k: log["failed"].append(nid))
    monkeypatch.setattr(db, "release_claims", lambda c, src, ids: log["released"].extend(ids) or len(ids))
    monkeypatch.setattr(db, "complete_detail",
                        lambda c, src, ids, outcome="written": log["complete"].append((sorted(ids), outcome)))
    monkeypatch.setattr(db, "upsert_portal_raw_page", lambda c, **kw: 1)
    monkeypatch.setattr(db, "mark_portal_page_parsed", lambda c, page_id: None)
    from scraper import listing_write
    monkeypatch.setattr(listing_write, "write_listings", lambda c, writes: [])
    portal = _portal()
    monkeypatch.setattr(portal, "connect_drain", lambda: _Conn())
    rc, agg = portal_runner.run_detail_drain(portal, None, False, 2, 1.0, max_seconds=60)
    assert rc == 0 and budget.refused and budget.refused_reason == "deadline"
    assert sum(slept) < 15.0 and len(slept) == 1
    assert len(log["released"]) == 3 and set(log["released"]) < set(ads)
    assert log["failed"] == [] and agg["errors"] == 0
    written = [ids for ids, outcome in log["complete"] if outcome == "written"]
    assert sorted(i for ids in written for i in ids) == sorted(set(ads) - set(log["released"]))


def test_a_shortener_to_a_portal_ad_is_one_request_and_a_rekey(monkeypatch, fresh_hosts) -> None:
    """The client never requests the portal's page (sreality's human page would bounce
    through its login loop; any portal's page is that portal's client's to fetch)."""
    asked: list[str] = []

    class _Hop:
        status_code, headers = 301, {"Location": SREALITY_AD}

        def __enter__(self) -> "_Hop":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    class _Session:
        def request(self, method: str, url: str, **kw: Any) -> _Hop:
            asked.append(url)
            return _Hop()

    client = web_client.WebClient()
    client._session = _Session()  # type: ignore[assignment]
    item = _portal().fetch_detail(client, "https://bit.example/abc", "https://bit.example/abc")
    assert asked == ["https://bit.example/abc"]
    assert item.kind == "ok" and item.payload["rekey"].source == "sreality"
    assert item.payload["rekey"].native_id == "2557083724"


def test_only_a_portal_ad_is_stopped_before() -> None:
    assert web_client.is_portal_ad(SREALITY_AD) and web_client.is_portal_ad(IDNES_AD)
    assert not web_client.is_portal_ad("https://www.rk-novak.example/byty/15")
    assert not web_client.is_portal_ad("https://www.sreality.cz/")


def test_the_client_session_is_the_egress_one() -> None:
    session = web_client.WebClient()._session
    assert isinstance(session.get_adapter("https://a.example/"), egress._PinnedAdapter)
    assert session.headers["Accept"].startswith("text/html")


def test_a_page_is_decoded_by_its_declared_charset() -> None:
    body = "Prodej bytu, Plzeň".encode("cp1250")
    assert web_client.decode_html(body, "text/html; charset=windows-1250") == "Prodej bytu, Plzeň"
    meta = b'<meta charset="windows-1250">' + body
    assert web_client.decode_html(meta, "text/html").endswith("Plzeň")
    assert web_client.decode_html("Plzeň".encode(), None) == "Plzeň"
    assert web_client.decode_html(b"x", "text/html; charset=nonsense") == "x"


# --- end to end through the shared drain ------------------------------------------------------


def test_the_shared_drain_runs_the_tenth_portal_unchanged(monkeypatch, fresh_hosts) -> None:
    """One claim of each outcome through the real `run_detail_drain`: a page written by the
    default flush, a 404 recorded gone, a shortener to sreality re-keyed. The drain learns
    nothing of `web`: it builds the client through the factory with its own limiter."""
    from scraper import listing_write
    from scraper.rate_limit import RateLimiter

    pages = {
        AD: egress.FetchResult(200, AD, PAGE.encode(), "text/html; charset=utf-8"),
        "https://dead.example/1": egress.FetchResult(404, "https://dead.example/1", b"", None),
        "https://bit.example/x": egress.FetchResult(200, SREALITY_AD, b"<html></html>", "text/html"),
    }
    monkeypatch.setattr(web_client.egress, "fetch", lambda url, **_: pages[url])
    monkeypatch.setattr(web_client.time, "sleep", lambda s: None)
    built: list[Any] = []
    monkeypatch.setattr(portal_runner, "build_rate_limiter",
                        lambda src, rate, shared, **kw: built.append((src, shared)) or RateLimiter(1000.0))
    log: dict[str, list[Any]] = {"complete": [], "enqueue": [], "written": [], "staged": []}
    claims = iter([[(AD, AD, None, None), ("https://dead.example/1", "https://dead.example/1", None, None),
                    ("https://bit.example/x", "https://bit.example/x", None, None)], []])
    monkeypatch.setattr(db, "reclaim_stale_claims", lambda c, src: 0)
    monkeypatch.setattr(db, "claim_detail_batch", lambda c, src, n: next(claims))
    monkeypatch.setattr(db, "queue_priorities", lambda c, src, ids: {i: db.QUEUE_PRIORITY_NEW for i in ids})
    monkeypatch.setattr(db, "gone_evidence", lambda c, src, nid: (False, None))
    monkeypatch.setattr(db, "complete_detail",
                        lambda c, src, ids, outcome="written": log["complete"].append((src, sorted(ids), outcome)))
    monkeypatch.setattr(db, "enqueue_detail", lambda c, src, entries: log["enqueue"].append((src, entries)) or 1)
    monkeypatch.setattr(db, "upsert_portal_raw_page",
                        lambda c, **kw: log["staged"].append(kw["source_id_native"]) or 1)
    monkeypatch.setattr(db, "mark_portal_page_parsed", lambda c, page_id: None)
    monkeypatch.setattr(listing_write, "write_listings",
                        lambda c, writes: log["written"].append([w.source_id_native for w in writes]) or [])
    portal = _portal()
    monkeypatch.setattr(portal, "connect_drain", lambda: _Conn())
    rc, agg = portal_runner.run_detail_drain(portal, None, False, 2, 1.0, max_seconds=60)
    assert rc == 0 and built == [(SOURCE, True)]
    assert log["written"] == [[AD]] and log["staged"] == [AD]
    assert log["enqueue"] == [("sreality", [("2557083724", None, None, db.QUEUE_PRIORITY_NEW)])]
    assert sorted(log["complete"]) == sorted([
        (SOURCE, ["https://dead.example/1"], "gone"),
        (SOURCE, ["https://bit.example/x"], "gone"),
        (SOURCE, sorted([AD, "https://bit.example/x"]), "written"),
    ])
    assert agg["errors"] == 0
