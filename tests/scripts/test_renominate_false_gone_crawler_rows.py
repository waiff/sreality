"""The false-gone re-nomination queues each eligible row once, at VERIFY, in bounded batches.

Hermetic: a ledger-style fake conn keeps a `queue` keyed (source, native_id). The selection
reports a row as `queued` when the fake queue already holds it, and the enqueue INSERT adds
to it, so a second run over the same state is observable. The selection and readout SQL are
PREPAREd against the real schema by tests/test_sql_schema_prepare.py.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from scraper import db
from scripts import renominate_false_gone_crawler_rows as ren


class _Cur:
    def __init__(self, conn: "_LedgerConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if s.startswith("WITH verdict AS"):
            want = params["source"]
            self._rows = [
                (src, nid, url, price, (src, nid) in self._conn.queue, rechecked)
                for src, nid, url, price, rechecked in self._conn.false_gone
                if want is None or src == want
            ]
        elif s == " ".join(ren.PILOT_SUMMARY_SQL.split()):
            self._rows = list(self._conn.summary)
        elif s == " ".join(ren.PILOT_REACTIVATED_SQL.split()):
            self._rows = list(self._conn.reactivated)
        elif s.startswith("INSERT INTO listing_detail_queue"):
            source = params["source"]
            for nid, prio in zip(params["nids"], params["prios"]):
                key = (source, nid)
                self._conn.queue[key] = max(self._conn.queue.get(key, prio), prio)
            self._rows = []
            self.rowcount = len(params["nids"])
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _LedgerConn:
    def __init__(self, false_gone: list[tuple[str, str, str | None, int | None, bool]],
                 queue: dict[tuple[str, str], int] | None = None) -> None:
        self.false_gone = false_gone
        self.queue: dict[tuple[str, str], int] = dict(queue or {})
        self.executed: list[tuple[str, Any]] = []
        self.summary: list[tuple[Any, ...]] = []
        self.reactivated: list[tuple[Any, ...]] = []

    def transaction(self) -> Any:
        raise AssertionError("the re-nomination opens no transaction of its own")

    def cursor(self) -> _Cur:
        return _Cur(self)

    def inserts(self) -> list[dict[str, Any]]:
        return [p for s, p in self.executed if s.startswith("INSERT INTO listing_detail_queue")]


def _rows(source: str, n: int, *, start: int = 1) -> list[tuple[str, str, str, int, bool]]:
    return [(source, f"{source[:2]}{i}", f"https://{source}.example/{i}", 1_000_000 + i, False)
            for i in range(start, start + n)]


def test_a_dry_run_counts_per_source_and_writes_nothing(caplog: pytest.LogCaptureFixture) -> None:
    conn = _LedgerConn(_rows("bazos", 3) + _rows("idnes", 2))
    with caplog.at_level("INFO", logger=ren.LOG.name):
        tallies = ren.run(conn, apply=False, limit=None, batch_size=10)
    assert {s: (t.false_gone, t.taken, t.enqueued) for s, t in tallies.items()} == {
        "bazos": (3, 3, 0), "idnes": (2, 2, 0)}
    assert conn.inserts() == [] and conn.queue == {}
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("RENOMINATE")]
    assert lines == [
        "RENOMINATE source=bazos false_gone=3 already_queued=0 rechecked_gone=0 "
        "eligible=3 would_enqueue=3",
        "RENOMINATE source=idnes false_gone=2 already_queued=0 rechecked_gone=0 "
        "eligible=2 would_enqueue=2",
    ]


def test_apply_writes_bounded_batches_at_verify_priority() -> None:
    conn = _LedgerConn(_rows("bazos", 5))
    tallies = ren.run(conn, apply=True, limit=None, batch_size=2)
    batches = conn.inserts()
    assert [p["nids"] for p in batches] == [["ba1", "ba2"], ["ba3", "ba4"], ["ba5"]]
    assert {pr for p in batches for pr in p["prios"]} == {db.QUEUE_PRIORITY_VERIFY}
    assert batches[0]["refs"] == ["https://bazos.example/1", "https://bazos.example/2"]
    assert batches[0]["prices"] == [1_000_001, 1_000_002]
    assert tallies["bazos"].enqueued == 5
    assert set(conn.queue) == {("bazos", f"ba{i}") for i in range(1, 6)}


def test_the_limit_is_per_source() -> None:
    conn = _LedgerConn(_rows("bazos", 4) + _rows("remax", 4))
    tallies = ren.run(conn, apply=True, limit=3, batch_size=2)
    assert {s: (len(t.eligible), t.enqueued) for s, t in tallies.items()} == {
        "bazos": (4, 3), "remax": (4, 3)}
    assert [p["nids"] for p in conn.inserts() if p["source"] == "remax"] == [
        ["re1", "re2"], ["re3"]]


def test_rows_already_queued_or_rechecked_by_their_portal_are_never_enqueued() -> None:
    rechecked = ("bazos", "ba9", "https://bazos.example/9", None, True)
    conn = _LedgerConn(_rows("bazos", 3) + [rechecked],
                       queue={("bazos", "ba2"): db.QUEUE_PRIORITY_NEW})
    tallies = ren.run(conn, apply=True, limit=None, batch_size=10)
    t = tallies["bazos"]
    assert (t.false_gone, t.already_queued, t.rechecked_gone, t.enqueued) == (4, 1, 1, 2)
    assert [p["nids"] for p in conn.inserts()] == [["ba1", "ba3"]]
    # The row the walk had already queued as NEW keeps its class.
    assert conn.queue[("bazos", "ba2")] == db.QUEUE_PRIORITY_NEW


def test_a_rerun_over_the_same_state_enqueues_nothing() -> None:
    conn = _LedgerConn(_rows("idnes", 3))
    ren.run(conn, apply=True, limit=2, batch_size=10)
    ren.run(conn, apply=True, limit=2, batch_size=10)
    sent = [nid for p in conn.inserts() for nid in p["nids"]]
    assert sent == ["id1", "id2", "id3"]
    assert len(sent) == len(set(sent))


def test_a_duplicate_selection_row_is_counted_once() -> None:
    conn = _LedgerConn(_rows("bazos", 2) + _rows("bazos", 1))
    tallies = ren.run(conn, apply=True, limit=None, batch_size=10)
    assert tallies["bazos"].false_gone == 2
    assert [p["nids"] for p in conn.inserts()] == [["ba1", "ba2"]]


def test_the_source_filter_reaches_the_selection() -> None:
    conn = _LedgerConn(_rows("bazos", 1) + _rows("idnes", 1))
    tallies = ren.run(conn, apply=False, limit=None, batch_size=10, source="idnes")
    assert list(tallies) == ["idnes"]
    assert conn.executed[0][1] == {"source": "idnes"}


@pytest.mark.parametrize("kwargs", [{"batch_size": 0, "limit": None},
                                    {"batch_size": 10, "limit": 0}])
def test_nonpositive_bounds_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ren.run(_LedgerConn([]), apply=True, **kwargs)


def test_the_selection_is_the_false_gone_signature() -> None:
    sql = " ".join(ren.FALSE_GONE_SQL.split())
    for clause in (
        "f.outcome = 'gone'",
        "f.sreality_id < 0",
        "JOIN listings l ON l.sreality_id = f.sreality_id",
        "l.source <> 'sreality'",
        "l.is_active = false",
        # Two-sided: the old writer flipped first and logged the check after.
        "l.inactive_at BETWEEN f.checked_at - interval '1 hour' "
        "AND f.checked_at + interval '1 hour'",
        "(l.last_seen_at IS NULL OR l.last_seen_at <= l.inactive_at)",
        "FROM listing_detail_queue q WHERE q.source = l.source "
        "AND q.native_id = l.source_id_native",
        "c.outcome = 'gone'",
        "COALESCE(pg.gone_at > v.verdict_at, false) AS rechecked",
    ):
        assert clause in sql, clause
    assert "DELETE" not in sql.upper() and "UPDATE" not in sql.upper()


def test_the_cli_defaults_to_the_25_row_pilot_and_all_must_be_typed() -> None:
    ap = ren._build_parser()
    assert ap.parse_args([]).limit == ren.DEFAULT_LIMIT == 25
    assert ap.parse_args(["--limit", "all"]).limit is None
    assert ap.parse_args(["--limit", "ALL"]).limit is None
    assert ap.parse_args(["--limit", "300"]).limit == 300
    for bad in ("0", "-3", "", "everything"):
        with pytest.raises(SystemExit):
            ap.parse_args(["--limit", bad])


def test_a_readout_timestamp_without_an_offset_is_utc() -> None:
    ap = ren._build_parser()
    assert ap.parse_args(["--readout-since", "2026-10-02T10:00"]).readout_since == datetime(
        2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    assert ap.parse_args(["--readout-since", "2026-10-02T10:00Z"]).readout_since == datetime(
        2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(SystemExit):
        ap.parse_args(["--readout-since", "yesterday"])


def test_readout_and_apply_are_refused_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["ren", "--apply", "--readout-since", "2026-10-02T10:00"])
    monkeypatch.setattr(ren.db, "connect", lambda *a, **k: pytest.fail("must not connect"))
    assert ren.main() == 2


_BASELINE = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


def _reactivated(source: str, nid: str, **measures: Any) -> ren.ReactivatedRow:
    """A PILOT_REACTIVATED_SQL row; every measure equal now/before unless overridden."""
    row = {"price_now": 4_000_000, "price_before": 4_000_000, "baseline_at": _BASELINE,
           "fields_now": 7, "fields_before": 7, "params_now": 18, "params_before": 18,
           "images_now": 12, "images_before": 12} | measures
    return ren.ReactivatedRow(source, nid, f"https://{source}.example/{nid}", **row)


def test_the_readout_reports_per_source_and_flags_rows_that_lost_content(
        caplog: pytest.LogCaptureFixture) -> None:
    conn = _LedgerConn([])
    conn.summary = [("bazos", 1, 2, 3, 10, 7, 0, 4, 3), ("idnes", 0, 0, 0, 5, 5, 2, 0, 0)]
    conn.reactivated = [
        _reactivated("idnes", "id1"),
        _reactivated("idnes", "id2", params_now=0, images_now=0),
    ]
    since = datetime.now(timezone.utc) - timedelta(hours=3)
    with caplog.at_level("INFO", logger=ren.LOG.name):
        out = ren.readout(conn, since=since, source=None, max_rows=50)
    assert out["bazos"] == ren.SourceReadout(1, 2, 3, 10, 7, 0, 4, 3)
    assert out["idnes"].written_unseen == 2
    params = {"since": since, "source": None, "verify": db.QUEUE_PRIORITY_VERIFY}
    assert [p for _, p in conn.executed] == [params, params]
    assert conn.inserts() == []
    msgs = [r.getMessage() for r in caplog.records]
    assert ("READOUT source=bazos pending=1 erroring=2 given_up=3 written=10 reactivated=7 "
            "written_unseen=0 gone=4 gave_up=3") in msgs
    # rows = queue (1+2+3) + written + gone across sources; failing = erroring + given_up.
    assert "READOUT TOTAL rows=25 reactivated=12 gone=4 erroring_or_given_up=5 (20.0%)" in msgs
    assert any(r.levelname == "WARNING" and "source=idnes written_unseen=2" in r.getMessage()
               for r in caplog.records)
    assert any(m.startswith("READOUT CHECK rows=1 of reactivated=2 ") for m in msgs)
    rows = [m for m in msgs if m.startswith("READOUT ROW source=")]
    # The CHECK row lists first, whatever the SQL order.
    assert rows == [
        "READOUT ROW source=idnes native_id=id2 fields=7/7 params=0/18 images=0/12 "
        "price=4000000/4000000 baseline=2026-09-20 CHECK=fewer_params,fewer_images "
        "url=https://idnes.example/id2",
        "READOUT ROW source=idnes native_id=id1 fields=7/7 params=18/18 images=12/12 "
        "price=4000000/4000000 baseline=2026-09-20 url=https://idnes.example/id1",
    ]


def test_a_fixed_shape_page_read_as_live_is_flagged_by_its_content() -> None:
    # The review's case: an HTML parser builds the same seven keys for an archive page as
    # for a live advert, so a key count never drops. The emptied values do.
    same = _reactivated("remax", "re1")
    assert ren.check_reasons(same) == []
    assert ren.check_reasons(same._replace(fields_now=4)) == ["fewer_fields"]
    assert ren.check_reasons(same._replace(params_now=2)) == ["fewer_params"]
    assert ren.check_reasons(same._replace(params_now=None)) == ["fewer_params"]
    assert ren.check_reasons(same._replace(images_now=0)) == ["fewer_images"]
    assert ren.check_reasons(same._replace(price_now=None)) == ["price_lost"]
    assert ren.check_reasons(same._replace(price_now=0)) == ["price_lost"]
    # More content, or a measure the portal never had (no `params` in a JSON payload), is fine.
    assert ren.check_reasons(same._replace(images_now=15, params_now=None,
                                           params_before=None)) == []
    assert ren.check_reasons(same._replace(price_now=3_500_000)) == []


def test_no_earlier_snapshot_means_nothing_to_compare_so_it_is_opened() -> None:
    row = _reactivated("bazos", "ba1", baseline_at=None, fields_before=None,
                       params_now=None, params_before=None, images_before=None,
                       price_before=None)
    assert ren.check_reasons(row) == ["no_baseline"]


@pytest.mark.parametrize("source", sorted(ren.HTTP_200_ON_REMOVAL))
def test_every_row_of_a_portal_answering_removed_urls_with_200_is_opened(source: str) -> None:
    assert ren.HTTP_200_ON_REMOVAL == {"ceskereality", "mmreality", "realitymix"}
    assert ren.check_reasons(_reactivated(source, "x1")) == ["removed_url_200"]


def test_the_row_list_cut_keeps_check_rows_and_warns_when_one_is_hidden(
        caplog: pytest.LogCaptureFixture) -> None:
    conn = _LedgerConn([])
    conn.reactivated = ([_reactivated("idnes", f"ok{i}") for i in range(3)]
                        + [_reactivated("idnes", f"bad{i}", images_now=0) for i in range(3)])
    since = datetime.now(timezone.utc) - timedelta(hours=3)
    with caplog.at_level("INFO", logger=ren.LOG.name):
        ren.readout(conn, since=since, max_rows=2)
    shown = [m for m in (r.getMessage() for r in caplog.records)
             if m.startswith("READOUT ROW source=")]
    assert [m.split()[3] for m in shown] == ["native_id=bad0", "native_id=bad1"]
    assert any(r.levelname == "WARNING" and "cut at 2 of 6 rows" in r.getMessage()
               and "1 CHECK rows not shown" in r.getMessage() for r in caplog.records)


def test_the_readout_measures_content_not_top_level_keys() -> None:
    rows = " ".join(ren.PILOT_REACTIVATED_SQL.split())
    filled = "WHERE e.value NOT IN ('null', '\"\"', '[]', '{}')"
    for side in ("l", "b"):
        assert f"jsonb_each({side}.raw_json) e {filled}" in rows
        assert f"jsonb_each({side}.raw_json->'params') e {filled}" in rows
        assert f"jsonb_array_length({side}.raw_json->'image_urls')" in rows
    assert "jsonb_object_keys" not in rows
    assert "LIMIT %(max_rows)s" not in rows


def test_a_readout_older_than_the_completion_ledger_warns(
        caplog: pytest.LogCaptureFixture) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=db.COMPLETION_RETENTION_DAYS + 1)
    with caplog.at_level("INFO", logger=ren.LOG.name):
        ren.readout(_LedgerConn([]), since=since)
    assert any(r.levelname == "WARNING" and "undercount" in r.getMessage()
               for r in caplog.records)


def test_the_readout_finds_the_job_rows_without_the_flip_signature() -> None:
    summary = " ".join(ren.PILOT_SUMMARY_SQL.split())
    rows = " ".join(ren.PILOT_REACTIVATED_SQL.split())
    for sql in (summary, rows):
        for clause in ("f.outcome = 'gone'", "source <> 'sreality'",
                       "c.priority = %(verify)s", "c.enqueued_at >= %(since)s"):
            assert clause in sql, clause
        # A reactivated row's inactive_at is NULL, so the signature cannot find it.
        assert "inactive_at" not in sql
        assert not any(w in sql.upper() for w in ("DELETE", "UPDATE", "INSERT"))
    assert "q.priority = %(verify)s AND q.enqueued_at >= %(since)s" in summary
    assert "j.last_seen_at < c.enqueued_at" in summary
    assert "s.scraped_at < c.enqueued_at" in rows
