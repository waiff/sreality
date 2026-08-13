"""The 445k-row migration of `portal_raw_pages` into the payload store (06 §6.4, gate (a)).

Two things can go wrong in a resumable batch migration, and both are silent: it can claim
ground it never covered, and it can lose bytes on the way through. The fake connection here
is a small query engine over in-memory `portal_raw_pages` / `portal_raw_payloads` /
`location_claim_batches` tables rather than an assertion recorder, because the invariant
under test is "every source row is migrated exactly once across a sequence of budgeted
runs" — only executing the keyset arithmetic can show that.

A third thing must never happen at all: a DELETE reaching the source table. That is
`tests/test_portal_raw_pages_guard.py`'s job, and one test here pins the assumption the
guard silently rests on — that its regex does not fire on `portal_raw_payloads`.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from location_data import payload_backfill, payloads
from location_data.payload_backfill import PAGE_KIND_MAP, encode_for_archive, run
from location_data.payload_norm import NORMALIZER_VERSION

BASE_TS = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
_MODULE = Path(payload_backfill.__file__)


class _Page:
    def __init__(
        self, page_id: int, *, source: str = "bazos", page_kind: str = "detail",
        body: bytes | None = None, http_status: int | None = 200,
    ) -> None:
        self.id = page_id
        self.source = source
        self.source_id_native = f"n{page_id}"
        self.page_kind = page_kind
        self.body = body if body is not None else f"<html><p>page {page_id}</p></html>".encode()
        self.http_status = http_status
        self.fetched_at = BASE_TS + timedelta(minutes=page_id)

    def record(self) -> tuple[Any, ...]:
        return (self.id, self.source, self.source_id_native, self.page_kind, self.body,
                self.http_status, self.fetched_at)


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
    def __init__(self, pages: list[_Page]) -> None:
        self.pages = pages
        self.payloads: list[dict[str, Any]] = []
        self.batches: list[dict[str, Any]] = []
        self.read_ids: list[int] = []
        self.now = BASE_TS
        self.timeouts: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Cursor:
        return _Cursor(self)

    def dispatch(self, cur: _Cursor, sql: str, params: Any) -> None:
        cur._result = []
        if "set_config" in sql:
            self.timeouts.append(params["statement_timeout"])
            return
        if "to_regclass" in sql:
            cur._result = [(params["name"],)]
            return
        if sql.startswith("SELECT 1 FROM portal_raw_pages WHERE source"):
            if any(p.source == params["source"] for p in self.pages):
                cur._result = [(1,)]
            return
        if sql.startswith("INSERT INTO location_claim_batches"):
            self.now += timedelta(minutes=1)
            batch = {
                "id": len(self.batches) + 1, "started_at": self.now,
                "lane": params["lane"], "source": params["source"],
                "extractor_version": params["extractor_version"],
                "scan_mode": params["scan_mode"], "resumable": params["resumable"],
                "outcome": "running", "cursor_after_id": None, "row_count": 0,
            }
            self.batches.append(batch)
            cur._result = [(batch["id"],)]
            return
        if sql.startswith("SELECT extractor_version, row_count"):
            candidates = [
                b for b in self.batches
                if b["lane"] == params["lane"]
                and b["source"] == params["source"]
                and b["scan_mode"] == params["scan_mode"]
                and b["outcome"] in ("ok", "stopped")
                and b["row_count"] > 0
            ]
            if candidates:
                last = max(candidates, key=lambda b: (b["started_at"], b["id"]))
                cur._result = [(last["extractor_version"], last["row_count"])]
            return
        if sql.startswith("UPDATE location_claim_batches"):
            batch = self.batches[params["batch_id"] - 1]
            batch["outcome"] = params["outcome"]
            batch["cursor_after_id"] = params["cursor_after_id"]
            batch["row_count"] = params["row_count"]
            return
        if sql.startswith("SELECT outcome, cursor_after_id"):
            candidates = [
                b for b in self.batches
                if b["lane"] == params["lane"]
                and b["source"] == params["source"]
                and b["scan_mode"] == params["scan_mode"]
                and b["resumable"]
                and b["outcome"] in ("ok", "stopped", "failed")
            ]
            if candidates:
                last = max(candidates, key=lambda b: (b["started_at"], b["id"]))
                cur._result = [(last["outcome"], last["cursor_after_id"])]
            return
        if "FROM portal_raw_pages" in sql:
            rows = [p for p in sorted(self.pages, key=lambda p: p.id)
                    if p.id > params["after_id"]
                    and (params["source"] is None or p.source == params["source"])]
            rows = rows[:params["batch_size"]]
            self.read_ids.extend(p.id for p in rows)
            cur._result = [p.record() for p in rows]
            return
        if sql.startswith("INSERT INTO portal_raw_payloads"):
            cur._result = self._insert_payloads(params)
            return
        raise AssertionError(f"unhandled SQL: {sql[:140]}")

    def _insert_payloads(self, params: dict[str, Any]) -> list[tuple[Any, ...]]:
        inserted: list[tuple[Any, ...]] = []
        for i in range(len(params["source"])):
            key = (params["source"][i], params["source_id_native"][i],
                   params["page_kind"][i], params["payload_sha256"][i])
            if any(p["key"] == key for p in self.payloads):
                continue
            row = {
                "id": len(self.payloads) + 1, "key": key,
                "source": params["source"][i],
                "source_id_native": params["source_id_native"][i],
                "page_kind": params["page_kind"][i],
                "payload_sha256": params["payload_sha256"][i],
                "body_sha256": params["body_sha256"][i],
                "content_type": params["content_type"][i],
                "content_encoding": params["content_encoding"][i],
                "body": params["body"][i],
                "byte_size": params["byte_size"][i],
                "http_status": params["http_status"][i],
                "fetched_at": params["fetched_at"][i],
                "first_observed_at": params["fetched_at"][i],
                "last_observed_at": params["fetched_at"][i],
                "normalizer_version": params["normalizer_version"],
                "version_seq": 1, "pinned": True,
                "listing_id": None, "contract_version": None, "snapshot_id": None,
            }
            self.payloads.append(row)
            inserted.append((row["id"],))
        return inserted


def _run(conn: _Conn, **kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "source": None, "batch_size": 10, "max_seconds": None, "limit": None,
        "start_after_id": 0, "statement_timeout": 60, "dry_run": False, "note": None,
        "force": False,
    }
    defaults.update(kwargs)
    return run(conn, **defaults)


# ---------------------------------------------------------------- the copy


def test_every_page_is_migrated_exactly_once_with_the_values_06_requires() -> None:
    conn = _Conn([_Page(i) for i in range(1, 26)])

    stats = _run(conn)

    assert stats["outcome"] == "ok"
    assert stats["pages"] == 25
    assert stats["inserted"] == 25
    assert len(conn.payloads) == 25
    row = conn.payloads[0]
    page = conn.pages[0]
    # 06 Rule 1: the body keeps the time it was really fetched, never migration day, and it
    # is simultaneously the first and the latest version of that page.
    assert row["first_observed_at"] == page.fetched_at
    assert row["last_observed_at"] == page.fetched_at
    assert row["fetched_at"] == page.fetched_at
    assert (row["version_seq"], row["pinned"]) == (1, True)
    assert row["content_encoding"] == "gzip"
    assert row["http_status"] == 200
    assert row["normalizer_version"] == NORMALIZER_VERSION
    assert row["byte_size"] == len(page.body)
    assert gzip.decompress(row["body"]) == page.body
    assert row["body_sha256"] == hashlib.sha256(page.body).digest()


def test_the_body_round_trips_byte_for_byte_including_non_utf8_and_a_245kb_page() -> None:
    # Portals serve windows-1250 pages and mmreality's are 245 KB; the archive must survive
    # both without an encoding decision anywhere (06 gate (a) is byte-for-byte).
    big = b"<html>" + bytes(range(256)) * 960 + b"</html>"
    assert len(big) > 245_000
    conn = _Conn([_Page(1, body=b"<html>\xe8\xed\xf9 non-utf8</html>"), _Page(2, body=big)])

    _run(conn)

    for stored, page in zip(conn.payloads, conn.pages):
        assert gzip.decompress(stored["body"]) == page.body
        assert stored["byte_size"] == len(page.body)


def test_a_rerun_over_already_migrated_pages_inserts_nothing() -> None:
    conn = _Conn([_Page(i) for i in range(1, 11)])

    first = _run(conn)
    second = _run(conn, start_after_id=0)

    assert first["inserted"] == 10
    assert second["inserted"] == 0
    assert second["skipped_existing"] == 10
    assert len(conn.payloads) == 10


def test_the_identity_hash_is_the_normalised_one_so_volatile_bytes_do_not_split_a_page() -> None:
    """`payload_sha256` is the content ADDRESS (02 §2.3.2 P1) and `body_sha256` is the raw
    bytes, forensics only. Two spellings of one document must share the address, or the
    migration would hand the store two identities for the same page."""
    a = b'{"a": 1, "b": 2}'
    b = b'{"b":2,\n   "a":1}'

    da, dbb = encode_for_archive(a, source="bazos"), encode_for_archive(b, source="bazos")

    assert da["payload_sha256"] == dbb["payload_sha256"]
    assert da["body_sha256"] != dbb["body_sha256"]
    # byte_size is the DECODED length of the artefact as fetched, not of the normal form.
    assert (da["byte_size"], dbb["byte_size"]) == (len(a), len(b))


def test_the_encoder_is_the_live_writers_so_the_two_paths_cannot_drift() -> None:
    """A hand-rolled `gzip.compress` here would keep working while silently diverging from
    what the live writer stores for the same content — and the round-trip verifier decodes
    both through `decode_body`, so it could never see the difference."""
    body = b"<html>" + b"x" * 9000 + b"</html>"

    derived = encode_for_archive(body, source="bazos")

    assert (derived["stored"], derived["content_encoding"]) == payloads.encode_body(
        body, gzip_min_bytes=0)
    assert payloads.decode_body(derived["stored"], derived["content_encoding"]) == body


def test_a_zero_length_body_is_labelled_identity_not_an_empty_gzip_member() -> None:
    conn = _Conn([_Page(1, body=b"")])

    _run(conn)

    row = conn.payloads[0]
    assert row["content_encoding"] == "identity"
    assert row["byte_size"] == 0
    # The label has to be honest or the verifier would try to inflate empty bytes and
    # report a corrupt member on a page that migrated perfectly.
    assert payloads.decode_body(row["body"], row["content_encoding"]) == b""


def test_the_content_type_is_sniffed_because_portal_raw_pages_never_recorded_one() -> None:
    """Seven HTML portals and two JSON archivers all wrote into a column called `html`."""
    assert encode_for_archive(b"<html></html>", source="bazos")["content_type"] == "text/html"
    assert encode_for_archive(b'{"x":1}', source="sreality")["content_type"] == (
        "application/json")


def test_a_dry_run_opens_no_batch_row_and_writes_no_payload() -> None:
    conn = _Conn([_Page(i) for i in range(1, 6)])

    stats = _run(conn, dry_run=True)

    assert stats["pages"] == 5
    assert stats["batch_id"] is None
    assert conn.payloads == []
    assert conn.batches == []


def test_every_statement_runs_under_a_bounded_timeout() -> None:
    """`statement_timeout = 0` is right for a COPY and wrong for a batched migration: it is
    how a lane wedges for two hours without emitting a line.

    Two budgets, deliberately: the batch statements get the run's, and the terminal stamp
    gets a short ceiling of its own — a one-row UPDATE by primary key that hangs would
    strand the batch row at 'running'.
    """
    conn = _Conn([_Page(i) for i in range(1, 6)])

    _run(conn, statement_timeout=45)

    assert conn.timeouts
    assert set(conn.timeouts) == {"45s", f"{payload_backfill._STAMP_TIMEOUT_S}s"}
    assert conn.timeouts[-1] == f"{payload_backfill._STAMP_TIMEOUT_S}s"


# ---------------------------------------------------------------- page_kind mapping


def test_the_page_kind_map_covers_the_source_check_and_only_enum_labels() -> None:
    """`portal_raw_pages` is free text under a CHECK; the target column is an enum. Both
    sides are asserted from the migrations rather than assumed to be a name match."""
    root = Path(__file__).resolve().parents[2] / "migrations"
    source_check = re.search(
        r"page_kind\s+text not null check \(page_kind in \(([^)]*)\)\)",
        (root / "099_portal_raw_pages.sql").read_text(encoding="utf-8"))
    assert source_check
    source_values = set(re.findall(r"'([a-z_]+)'", source_check.group(1)))
    enum_block = re.search(
        r"create type location_page_kind as enum \(([^)]*)\)",
        (root / "380_location_w1_enums_and_config.sql").read_text(encoding="utf-8"))
    assert enum_block
    enum_labels = set(re.findall(r"'([a-z_]+)'", enum_block.group(1)))

    assert source_values == set(PAGE_KIND_MAP), (
        "portal_raw_pages' page_kind CHECK and PAGE_KIND_MAP have diverged")
    assert set(PAGE_KIND_MAP.values()) <= enum_labels


def test_an_unmappable_page_kind_is_skipped_and_counted_never_a_crash() -> None:
    # A later migration widening the source CHECK must not be able to kill a 445k-row
    # migration mid-flight with an enum cast error.
    pages = [_Page(1), _Page(2, page_kind="map"), _Page(3)]
    conn = _Conn(pages)

    stats = _run(conn)

    assert stats["outcome"] == "ok"
    assert stats["unmapped_page_kind"] == 1
    assert stats["inserted"] == 2
    # The cursor still moved past the skipped row, or the lane would wedge on it forever.
    assert stats["cursor_after_id"] == 3


# ---------------------------------------------------------------- resume semantics


def test_a_budget_stopped_run_is_stamped_stopped_and_resumes_where_it_left_off() -> None:
    conn = _Conn([_Page(i) for i in range(1, 26)])

    first = _run(conn, limit=10)
    assert first["outcome"] == "stopped"
    assert first["reached_end"] is False
    assert conn.batches[0]["cursor_after_id"] == 10

    second = _run(conn, limit=10)
    assert second["resumed"] is True
    assert second["resumed_from_id"] == 10
    assert second["outcome"] == "stopped"

    third = _run(conn, limit=10)
    assert third["outcome"] == "ok"
    assert third["reached_end"] is True
    # Every source row read exactly once across the three budgeted runs — no re-walked
    # prefix, nothing skipped.
    assert conn.read_ids == list(range(1, 26))
    assert len(conn.payloads) == 25


def test_a_max_seconds_stop_never_stamps_ok() -> None:
    conn = _Conn([_Page(i) for i in range(1, 26)])

    stats = _run(conn, batch_size=5, max_seconds=-1)

    # The budget is checked before the first batch, so this run copies nothing at all — and
    # the one thing it must not do is claim the archive was migrated.
    assert stats["stopped_early"] is True
    assert stats["reached_end"] is False
    assert stats["outcome"] == "stopped"
    assert conn.batches[0]["outcome"] == "stopped"


def test_a_finished_scan_is_not_resumed_from() -> None:
    """'ok' means the keyset ran off the end of the table. The next pass legitimately
    starts over at the beginning rather than picking up the terminal cursor."""
    conn = _Conn([_Page(i) for i in range(1, 6)])

    first = _run(conn)
    assert first["outcome"] == "ok"

    second = _run(conn)

    assert second["resumed"] is False
    assert second["resumed_from_id"] == 0
    assert conn.read_ids == list(range(1, 6)) * 2


def test_a_cursor_is_never_resumed_across_a_different_scan_mode() -> None:
    conn = _Conn([_Page(i) for i in range(1, 26)])
    _run(conn, limit=10)
    assert conn.batches[0]["outcome"] == "stopped"
    # The predecessor now looks like another lane's keyset: a bare id and an (id, ts) pair
    # mean different things, so crossing them would skip an arbitrary slice.
    conn.batches[0]["scan_mode"] = "incremental"

    stats = _run(conn, limit=10)

    assert stats["resumed"] is False
    assert stats["resumed_from_id"] == 0


def test_a_failed_predecessor_is_not_resumed_from() -> None:
    """A run that raised left its cursor wherever the exception found it; that position
    certifies nothing about the rows behind it."""
    conn = _Conn([_Page(i) for i in range(1, 26)])
    _run(conn, limit=10)
    conn.batches[0]["outcome"] = "failed"

    stats = _run(conn, limit=10)

    assert stats["resumed"] is False
    assert stats["resumed_from_id"] == 0


def test_an_operator_anchored_run_neither_resumes_nor_becomes_a_resumable_cursor() -> None:
    conn = _Conn([_Page(i) for i in range(1, 26)])

    anchored = _run(conn, start_after_id=15, limit=5)

    assert anchored["resumed"] is False
    assert conn.read_ids == [16, 17, 18, 19, 20]
    assert conn.batches[0]["resumable"] is False

    # And the next unanchored run ignores it entirely, starting at the beginning.
    following = _run(conn, limit=5)
    assert following["resumed_from_id"] == 0


def test_a_stopped_run_of_another_source_is_not_resumed_from() -> None:
    conn = _Conn([_Page(i, source="bazos") for i in range(1, 11)]
                 + [_Page(i, source="idnes") for i in range(11, 21)])

    _run(conn, source="bazos", limit=5)
    stats = _run(conn, source="idnes", limit=5)

    assert stats["resumed"] is False
    assert conn.read_ids[-5:] == [11, 12, 13, 14, 15]


def test_an_unknown_source_is_refused_rather_than_stamped_ok_over_an_untouched_portal() -> None:
    conn = _Conn([_Page(1, source="bazos")])

    with pytest.raises(payload_backfill.BackfillRefused):
        _run(conn, source="bzos")

    assert conn.batches == []


# ---------------------------------------------------------------- normaliser cohorts


def test_a_rewalk_under_a_different_normaliser_is_refused_without_force() -> None:
    """The duplicate this closes is PERMANENT. `payload_sha256` is the normalised hash, so
    a NORMALIZER_VERSION bump stops ON CONFLICT from firing and every re-walked page
    appends a SECOND version_seq=1 pinned row — and this lane never runs the re-pin/cap,
    so nothing can ever evict it."""
    conn = _Conn([_Page(i) for i in range(1, 11)])
    first = _run(conn)
    assert first["outcome"] == "ok"
    conn.batches[0]["extractor_version"] = "payload_backfill@1+payload_norm@0"

    with pytest.raises(payload_backfill.BackfillRefused) as excinfo:
        _run(conn)

    assert "payload_norm@0" in str(excinfo.value)
    assert len(conn.payloads) == 10, "the refused run must not have written anything"
    # Same normaliser is still the tested idempotent no-op, not a refusal.
    conn.batches[0]["extractor_version"] = payload_backfill.EXTRACTOR_VERSION
    assert _run(conn)["inserted"] == 0


def test_force_allows_the_rewalk_once_the_operator_owns_the_decision() -> None:
    conn = _Conn([_Page(i) for i in range(1, 6)])
    _run(conn)
    conn.batches[0]["extractor_version"] = "payload_backfill@1+payload_norm@0"

    stats = _run(conn, force=True)

    assert stats["outcome"] == "ok"


def test_resuming_across_a_normaliser_bump_is_allowed_because_no_page_is_revisited() -> None:
    """A resume walks only ground no earlier run reached, so it cannot duplicate a page —
    it just leaves the archive spanning two cohorts, which `normalizer_version` records."""
    conn = _Conn([_Page(i) for i in range(1, 26)])
    _run(conn, limit=10)
    conn.batches[0]["extractor_version"] = "payload_backfill@1+payload_norm@0"

    stats = _run(conn, limit=10)

    assert stats["resumed"] is True
    assert conn.read_ids == list(range(1, 21))


def test_the_batch_row_records_the_normaliser_it_wrote_under() -> None:
    conn = _Conn([_Page(1)])

    _run(conn)

    assert conn.batches[0]["extractor_version"] == payload_backfill.EXTRACTOR_VERSION
    assert NORMALIZER_VERSION in conn.batches[0]["extractor_version"]


# ---------------------------------------------------------------- terminal stamping


def test_a_failing_terminal_stamp_never_leaves_the_row_at_running() -> None:
    """Stranded at 'running' the row is invisible to the resume lookup, so the next
    dispatch would silently restart the whole scan from id 0."""
    conn = _Conn([_Page(i) for i in range(1, 6)])
    calls = {"n": 0}
    original = _Conn.dispatch

    def flaky(self: _Conn, cur: _Cursor, sql: str, params: Any) -> None:
        if sql.startswith("UPDATE location_claim_batches") and params["outcome"] == "ok":
            calls["n"] += 1
            raise RuntimeError("connection reset during the terminal stamp")
        original(self, cur, sql, params)

    conn.dispatch = flaky.__get__(conn, _Conn)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        _run(conn)

    assert calls["n"] == 1
    assert conn.batches[0]["outcome"] == "failed"
    assert conn.batches[0]["cursor_after_id"] == 5


# ---------------------------------------------------------------- the source table


def test_the_module_never_deletes_truncates_or_drops_anything() -> None:
    """The blast radius of this lane is one verb away from permanent data loss, so the
    check is on the module's own SQL rather than only on the repo-wide guard."""
    text = _MODULE.read_text(encoding="utf-8")
    tree = ast.parse(text)
    statements = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    for const in statements:
        first = const.value.strip().split(None, 1)[0].upper() if const.value.strip() else ""
        assert first not in ("DELETE", "TRUNCATE", "DROP"), const.value[:120]
    assert not re.search(r"(?is)\b(delete\s+from|truncate|drop\s+table)\b", text)


def test_every_sql_literal_is_a_plain_constant_the_corpus_can_discover() -> None:
    """`tests/sql_corpus.py` finds executed SQL by AST. An f-string is invisible to it, and
    this project has shipped an unPREPAREd statement that way before."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [getattr(t, "id", "") for t in node.targets]
            if any(n.endswith("_SQL") for n in names):
                assert isinstance(node.value, ast.Constant), names


def test_the_raw_pages_guard_regex_does_not_false_positive_on_portal_raw_payloads() -> None:
    """The guard protects `portal_raw_pages`; this wave's target is `portal_raw_payloads`.
    "pages" is not a substring of "payloads", so the two never collide — but the whole W2a
    programme now writes DELETEs against a table whose name differs from the protected one
    by four characters, and an assumption that load-bearing is asserted, not assumed."""
    from tests.test_portal_raw_pages_guard import _FORBIDDEN

    for allowed in (
        "DELETE FROM portal_raw_payloads WHERE id = 1",
        "delete from public.portal_raw_payloads p USING ranked r WHERE p.id = r.id",
        'TRUNCATE TABLE "portal_raw_payloads"',
        "DROP TABLE IF EXISTS portal_raw_payloads",
    ):
        assert not _FORBIDDEN.search(allowed), allowed

    # And it still fires on the real thing, including the spellings that motivated it.
    for forbidden in (
        "DELETE FROM portal_raw_pages WHERE id = 1",
        "delete from only public.portal_raw_pages",
        'TRUNCATE TABLE ONLY "portal_raw_pages"',
        "DROP TABLE IF EXISTS portal_raw_pages",
    ):
        assert _FORBIDDEN.search(forbidden), forbidden
