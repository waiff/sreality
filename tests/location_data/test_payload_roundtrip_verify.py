"""06 W2a gate (a): the report an operator signs must be able to say FAIL.

A verifier that only ever passes is worse than none — it launders a lossy migration into a
signature. So most of this module feeds it deliberately broken stores: a body that decodes
to different bytes, a page with no payload row at all, a spilled body whose object is
missing, a `byte_size` that disagrees with what it labels. The happy path is checked too,
on the bodies that actually break naive implementations: non-UTF-8 bytes, a ~245 KB page,
and a body that lives in R2 rather than in Postgres.
"""

from __future__ import annotations

import gzip
import hashlib
from datetime import UTC, datetime
from typing import Any

from scripts import location_payload_roundtrip_verify as verifier

BASE_TS = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

_NON_UTF8 = b"<html>\xe8\xed\xf9 windows-1250</html>"
_BIG = b"<html>" + bytes(range(256)) * 960 + b"</html>"


class _FakeR2:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = objects or {}
        self.reads: list[str] = []

    def download_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        return self.objects[key]


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.dispatch(self, " ".join(sql.split()), params or {})

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


class _Conn:
    """In-memory `portal_raw_pages` + `portal_raw_payloads`.

    `pages` maps id -> raw bytes; `payloads` maps a page id to the stored row, or omits it
    entirely for the "never migrated" case.
    """

    def __init__(
        self, pages: dict[int, bytes], payloads: dict[int, dict[str, Any]],
        sources: dict[int, str] | None = None,
    ) -> None:
        self.pages = pages
        self.payloads = payloads
        self.sources = sources or {}
        self.payload_queries = 0

    def _source(self, page_id: int) -> str:
        return self.sources.get(page_id, "bazos")

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Cursor:
        return _Cursor(self)

    def dispatch(self, cur: _Cursor, sql: str, params: Any) -> None:
        cur._result = []
        if "set_config" in sql:
            return
        if sql.startswith("SELECT id FROM portal_raw_pages"):
            scope = [i for i in sorted(self.pages)
                     if params["source"] is None or self._source(i) == params["source"]]
            cur._result = [(i,) for i in scope[:params["max_ids"]]]
            return
        if "convert_to(html" in sql:
            wanted = set(params["ids"])
            cur._result = [
                (i, self._source(i), f"n{i}", "detail", self.pages[i], BASE_TS)
                for i in sorted(self.pages) if i in wanted
            ]
            return
        if sql.startswith("SELECT k.page_id, p.id"):
            self.payload_queries += 1
            out: list[tuple[Any, ...]] = []
            for pos, page_id in enumerate(params["page_id"]):
                row = self.payloads.get(int(page_id))
                if row is None:
                    # The LATERAL missed: an all-NULL right side, not an absent row.
                    out.append((page_id, None, None, None, None, None, None, None, None))
                    continue
                out.append((
                    page_id, row.get("id", page_id), row.get("content_encoding", "gzip"),
                    row.get("body"), row.get("body_r2_key"), row.get("byte_size"),
                    row.get("body_sha256") == params["body_sha256"][pos], 1, BASE_TS,
                ))
            cur._result = out
            return
        raise AssertionError(f"unhandled SQL: {sql[:140]}")


def _archived(raw: bytes, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "content_encoding": "gzip", "body": gzip.compress(raw, mtime=0),
        "body_r2_key": None, "byte_size": len(raw),
        "body_sha256": hashlib.sha256(raw).digest(),
    }
    row.update(overrides)
    return row


def _conn(bodies: dict[int, bytes], **payload_overrides: Any) -> _Conn:
    return _Conn(bodies, {i: _archived(b) for i, b in bodies.items()} | payload_overrides)


# ---------------------------------------------------------------- passing


def test_a_faithful_migration_passes_including_non_utf8_and_a_245kb_body() -> None:
    conn = _conn({1: b"<html>plain</html>", 2: _NON_UTF8, 3: _BIG})

    report = verifier.verify(conn, size=10)

    assert (report.sampled, report.ok) == (3, 0 + 3)
    assert report.passed is True
    assert report.failures == []
    assert report.bytes_compared == len(b"<html>plain</html>") + len(_NON_UTF8) + len(_BIG)


def test_a_body_that_spilled_to_r2_is_fetched_and_compared() -> None:
    key = "payloads/bazos/ab/" + "ab" * 32 + ".gz"
    conn = _conn({1: _BIG})
    conn.payloads[1] = _archived(_BIG, body=None, body_r2_key=key)
    store = _FakeR2({key: gzip.compress(_BIG, mtime=0)})

    report = verifier.verify(conn, size=10, store=store)

    assert report.passed is True
    assert report.from_r2 == 1
    assert store.reads == [key]


def test_an_identity_encoded_body_round_trips_too() -> None:
    raw = b'{"small": true}'
    conn = _conn({1: raw})
    conn.payloads[1] = _archived(raw, content_encoding="identity", body=raw)

    assert verifier.verify(conn, size=10).passed is True


# ---------------------------------------------------------------- failing


def test_a_page_with_no_payload_row_is_reported_missing_not_passed() -> None:
    conn = _Conn({1: b"<html>a</html>", 2: b"<html>b</html>"},
                 {1: _archived(b"<html>a</html>")})

    report = verifier.verify(conn, size=10)

    assert report.passed is False
    assert (report.ok, report.missing) == (1, 1)
    assert report.failures[0].status == "missing"
    assert report.failures[0].page_id == 2


def test_bytes_that_differ_are_reported_as_a_mismatch_with_the_offset() -> None:
    raw = b"<html>the real page</html>"
    conn = _Conn({1: raw}, {1: _archived(b"<html>the WRONG page</html>")})

    report = verifier.verify(conn, size=10)

    assert report.passed is False
    assert report.mismatch == 1
    assert "first_diff=" in report.failures[0].detail
    assert "hash_matches=False" in report.failures[0].detail


def test_a_byte_size_that_disagrees_with_the_body_it_labels_fails() -> None:
    """`byte_size` is the DECODED length (migration 403). A row whose body round-trips but
    whose label is wrong would silently corrupt every storage projection read off it."""
    raw = b"<html>page</html>"
    conn = _Conn({1: raw}, {1: _archived(raw, byte_size=len(raw) + 7)})

    report = verifier.verify(conn, size=10)

    assert report.passed is False
    assert report.mismatch == 1
    assert "byte_size" in report.failures[0].detail


def test_a_spilled_body_with_no_object_store_is_unreadable_not_ok() -> None:
    conn = _conn({1: _BIG})
    conn.payloads[1] = _archived(_BIG, body=None, body_r2_key="payloads/bazos/aa/x.gz")

    report = verifier.verify(conn, size=10, store=None)

    assert report.passed is False
    assert report.unreadable == 1
    assert "no object store" in report.failures[0].detail


def test_a_failing_r2_read_is_a_finding_not_a_crash() -> None:
    key = "payloads/bazos/aa/missing.gz"
    conn = _conn({1: _BIG})
    conn.payloads[1] = _archived(_BIG, body=None, body_r2_key=key)

    report = verifier.verify(conn, size=10, store=_FakeR2({}))

    assert report.passed is False
    assert report.unreadable == 1
    assert "R2 read" in report.failures[0].detail


def test_a_corrupt_gzip_member_is_a_finding_not_a_crash() -> None:
    conn = _Conn({1: b"<html>x</html>"},
                 {1: _archived(b"<html>x</html>", body=b"not gzip at all")})

    report = verifier.verify(conn, size=10)

    assert report.passed is False
    assert report.unreadable == 1
    assert "would not decode" in report.failures[0].detail


def test_a_row_with_neither_a_body_nor_a_key_is_reported() -> None:
    conn = _Conn({1: b"<html>x</html>"},
                 {1: _archived(b"<html>x</html>", body=None, body_r2_key=None)})

    report = verifier.verify(conn, size=10)

    assert report.unreadable == 1
    assert "neither body nor R2 key" in report.failures[0].detail


def test_an_empty_archive_never_reports_a_pass() -> None:
    """Zero of zero is not a signed gate; it is a scope that found nothing."""
    report = verifier.verify(_Conn({}, {}), size=10)

    assert report.sampled == 0
    assert report.passed is False


# ---------------------------------------------------------------- sampling


def test_the_sample_is_drawn_across_the_id_space_not_off_the_front() -> None:
    """Ids run in insert order, which on this table runs portal by portal. Taking the first
    N would sample the earliest portals and none of the latest."""
    conn = _Conn({i: b"<html>x</html>" for i in range(1, 1001)}, {})

    drawn, pool, truncated = verifier.sample_ids(
        conn, source=None, size=50, seed=7, statement_timeout=60)

    assert len(drawn) == 50
    assert (pool, truncated) == (1000, False)
    assert drawn == sorted(drawn)
    assert max(drawn) > 500, "the draw never reached the second half of the id space"


def test_the_same_seed_draws_the_same_sample() -> None:
    conn = _Conn({i: b"<html>x</html>" for i in range(1, 1001)}, {})

    first, _, _ = verifier.sample_ids(conn, source=None, size=25, seed=11,
                                      statement_timeout=60)
    second, _, _ = verifier.sample_ids(conn, source=None, size=25, seed=11,
                                       statement_timeout=60)

    assert first == second


def test_an_archive_smaller_than_the_sample_is_verified_whole() -> None:
    conn = _Conn({i: b"<html>x</html>" for i in range(1, 6)}, {})

    drawn, pool, _ = verifier.sample_ids(conn, source=None, size=1000, seed=1,
                                         statement_timeout=60)

    assert drawn == [1, 2, 3, 4, 5]
    assert pool == 5


def test_a_sparse_source_scope_still_draws_the_full_requested_size() -> None:
    """The bug this closes: one portal's rows are sparsely interleaved across a sequence
    shared by nine, so an id-SPACE draw with a fixed oversample returned a fraction of what
    was asked for — silently. The draw is over ROWS in scope, so it cannot come up short
    while the scope holds enough of them."""
    # 200 bazos rows scattered 1-in-50 through a 10,000-wide id space.
    sources = {i: ("bazos" if i % 50 == 0 else "idnes") for i in range(1, 10_001)}
    conn = _Conn({i: b"<html>x</html>" for i in range(1, 10_001)}, {}, sources)

    drawn, pool, _ = verifier.sample_ids(conn, source="bazos", size=150, seed=3,
                                         statement_timeout=60)

    assert len(drawn) == 150
    assert pool == 200
    assert all(sources[i] == "bazos" for i in drawn)


def test_a_short_sample_is_reported_loudly_and_never_hidden_by_a_bare_pass() -> None:
    bodies = {i: b"<html>x</html>" for i in range(1, 4)}
    conn = _conn(bodies)

    report = verifier.verify(conn, size=1000)

    # It is still a PASS on what it verified — and the report says, in its own field, that
    # it verified three pages rather than the thousand the operator asked for.
    assert report.passed is True
    assert (report.requested, report.sampled, report.shortfall) == (1000, 3, 997)
    assert report.as_dict()["shortfall"] == 997


def test_a_full_sample_reports_no_shortfall() -> None:
    conn = _conn({i: b"<html>x</html>" for i in range(1, 21)})

    report = verifier.verify(conn, size=20)

    assert report.shortfall == 0
    assert report.as_dict()["requested"] == 20


def test_the_payload_lookup_is_batched_not_one_query_per_page() -> None:
    """~4,000 round trips for a 1,000-row sample was the shape before; one statement per
    chunk is the shape now."""
    conn = _conn({i: b"<html>x</html>" for i in range(1, 251)})

    report = verifier.verify(conn, size=250)

    assert report.sampled == 250
    assert conn.payload_queries == 3, "expected one payload query per 100-row chunk"


def test_the_verifier_issues_no_write_statement() -> None:
    """Read-only is a property of the gate, not a habit: it runs against production."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(verifier.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            value = node.value.value
            if isinstance(value, str) and value.strip():
                first = value.strip().split(None, 1)[0].upper()
                assert first not in (
                    "INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP", "ALTER"), value[:120]
