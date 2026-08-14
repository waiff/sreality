"""The append-on-change payload writer — 06 W2a gate (b), both directions.

The gate is behavioural and lives entirely in one `ON CONFLICT` clause plus two
retention statements, so most of this module runs against the REPLAYED SCHEMA
(TEST_DATABASE_URL, the same lane tests/test_payload_churn_live.py uses). A fake
connection can assert that a statement was executed; it cannot tell you that an
unchanged refetch collided instead of appending, that `prp_body_present` held, or
that the version cap kept the first and latest bodies — and those are the whole
deliverable. The offline half covers what is genuinely pure: the encoder, the R2 key
scheme, and the module's DELETE surface.

Nothing here touches production: the live fixture writes rows keyed on a per-test
uuid into a throwaway container.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest

from location_data import payload_norm, payloads
from location_data.payload_norm import (
    NORMALIZER_VERSION,
    PROBE_NORMALIZER_SUFFIX,
    VolatileProfile,
    probe_normalizer_version,
)

_DB_URL = os.environ.get("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — the payload store's semantics run in the CI DB job",
)

_HTML = "text/html"
_JSON = "application/json"


# --------------------------------------------------------------------- offline


def test_the_r2_key_is_derivable_from_the_row_alone() -> None:
    # No list_objects, ever: given a payload row, its object key is a pure function
    # of (source, body_sha256), so a body can be found from the DB alone.
    digest = bytes.fromhex("ab" * 32)

    key = payloads.r2_key("idnes", digest)

    assert key == f"payloads/idnes/ab/{'ab' * 32}.gz"


@pytest.mark.parametrize("source", ["../etc", "idnes/../x", "IDNES", "", "a" * 40])
def test_a_malformed_source_cannot_escape_the_key_prefix(source: str) -> None:
    with pytest.raises(payloads.PayloadError):
        payloads.r2_key(source, b"\x00" * 32)


def test_a_small_body_is_stored_verbatim() -> None:
    body = b'{"price": 1}'

    stored, encoding = payloads.encode_body(body)

    assert (stored, encoding) == (body, "identity")
    assert payloads.decode_body(stored, encoding) == body


def test_gzip_round_trips_byte_for_byte() -> None:
    # Non-UTF-8 on purpose: portals serve windows-1250 pages and the archive must
    # survive them without an encoding decision (06 gate (a) is byte-for-byte).
    body = b"<html>" + bytes(range(256)) * 40 + b"</html>"

    stored, encoding = payloads.encode_body(body)

    assert encoding == "gzip"
    assert len(stored) < len(body)
    assert payloads.decode_body(stored, encoding) == body


def test_the_encoding_is_deterministic() -> None:
    # gzip stamps the current time into its header unless mtime is pinned; without
    # that, re-encoding one body yields different bytes and every content-addressed
    # object would look like it needed rewriting.
    body = b"x" * 20_000

    assert payloads.encode_body(body)[0] == payloads.encode_body(body)[0]


def test_decode_rejects_an_encoding_it_does_not_know() -> None:
    with pytest.raises(payloads.PayloadError):
        payloads.decode_body(b"", "brotli")


def test_an_unconfigured_r2_reports_itself_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scraper import image_storage

    for var in image_storage.R2_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    payloads.reset_store_cache()
    try:
        assert payloads.open_store() is None
    finally:
        payloads.reset_store_cache()


def test_the_r2_default_puts_every_portals_body_in_the_bucket() -> None:
    """The threshold is the whole storage decision, so it is asserted against the
    corpus rather than left as a number in a comment: at 2 KB every portal's mean
    body spills except bezrealitky's JSON, which is what `payload_budget`'s
    Postgres-vs-R2 split models."""
    from location_data import payload_budget

    assert payloads.DEFAULT_R2_THRESHOLD_BYTES == payload_budget.INLINE_THRESHOLD_BYTES
    spilling = [p.source for p in payload_budget.PORTAL_STORAGE
                if p.stored_bytes_per_body > payloads.DEFAULT_R2_THRESHOLD_BYTES]

    assert set(spilling) == {p.source for p in payload_budget.PORTAL_STORAGE} - {"bezrealitky"}


def test_a_body_that_needs_the_bucket_refuses_rather_than_falling_back_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE DEGRADATION CONTRACT. Silently keeping the body in Postgres would rebuild
    the database-resident archive the budget gate exists to refuse — one missing env
    var and ~29x the projected footprint, invisibly. Refusing fails this one payload
    write; `scraper.db.append_payload_if_enabled` catches it, warns, and the walk and
    the drain carry on."""
    from scraper import image_storage

    for var in image_storage.R2_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    payloads.reset_store_cache()
    try:
        with pytest.raises(payloads.PayloadError, match="R2 is not configured"):
            payloads.append_payload(
                object(),  # never reached: the refusal precedes every statement
                source="idnes", source_id_native="x", page_kind="detail",
                # Incompressible, so the gzipped form genuinely clears the
                # 2 KB threshold — b"big" * 20_000 would not, which is the point of
                # deciding placement AFTER compression.
                listing_id=None, body=b"<html>" + os.urandom(20_000).hex().encode(),
                content_type="text/html", http_status=200, contract_version=None,
                observed_at=datetime.now(timezone.utc),
            )
    finally:
        payloads.reset_store_cache()


def test_a_small_body_still_archives_with_no_r2_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is scoped to bodies that actually need the bucket — an inline one
    is unaffected, so a fresh deploy with no R2 vars is not a hard stop on the whole
    archive."""
    from scraper import image_storage

    for var in image_storage.R2_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    placement = payloads.plan_placement("idnes", b"{}", b"\x00" * 32)

    assert placement.spills is False
    assert placement.r2_key is None


def test_the_only_delete_target_in_the_module_is_the_payload_store() -> None:
    """`portal_raw_pages` is preservation substrate (W0 item 0o) and the pruner must
    never widen onto it or onto the claim store. Asserted structurally so a future
    edit to _PRUNE_SQL cannot quietly change the target."""
    source = Path(payloads.__file__).read_text(encoding="utf-8").lower()
    targets = [
        line.split("delete from", 1)[1].split()[0]
        for line in source.splitlines()
        if "delete from" in line
    ]

    assert targets == ["portal_raw_payloads"]


def test_every_executed_statement_is_discoverable_by_the_sql_corpus() -> None:
    """The PREPARE sweep only sees module-level `*_SQL` string CONSTANTS; an f-string
    or a runtime-composed statement is invisible to it and would ship untyped."""
    tree = ast.parse(Path(payloads.__file__).read_text(encoding="utf-8"))
    literals = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and target.id.endswith("_SQL")
        and isinstance(node.value, ast.Constant)
    }

    assert literals == {
        "_APPEND_SQL", "_GROUP_LOCK_SQL", "_REPIN_SQL", "_PRUNE_SQL",
        "_ORPHANED_KEYS_SQL",
    }


# --- the cohort stamp cannot disagree with the profile that was applied ---
#
# Offline, on a recording connection, because the property is about the PARAMETERS the
# writer binds — and because the DB half of this file runs only in the advisory
# migrations lane, while this runs in the required one.


class _RecordingCur:
    """Captures executed SQL + bound params and echoes the INSERT's own values back."""

    def __init__(self, conn: "_RecordingConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_RecordingCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))

    def fetchone(self) -> tuple[Any, ...] | None:
        sql, params = self._conn.executed[-1]
        if not sql.startswith("INSERT INTO portal_raw_payloads"):
            return None
        body = params["body"]
        return (1, 1, True, params["body_sha256"], params["byte_size"],
                params["content_encoding"], params["body_r2_key"],
                None if body is None else len(body))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _RecordingConn:
    autocommit = True

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> Any:
        return _RecordingCur(self)

    def cursor(self) -> _RecordingCur:
        return _RecordingCur(self)


def _append_params(**kwargs: Any) -> dict[str, Any]:
    """The params bound into the append INSERT for one call."""
    conn = _RecordingConn()
    payloads.append_payload(
        conn,  # type: ignore[arg-type]
        source=kwargs.pop("source", "idnes"),
        source_id_native="k",
        page_kind=kwargs.pop("page_kind", "detail"),
        listing_id=None,
        body=kwargs.pop("body", b"<html><body><h1>Byt</h1></body></html>"),
        content_type=kwargs.pop("content_type", _HTML),
        http_status=200,
        contract_version=None,
        observed_at=datetime.now(timezone.utc),
        **kwargs,
    )
    return next(
        params for sql, params in conn.executed
        if sql.startswith("INSERT INTO portal_raw_payloads")
    )


def test_the_stamp_follows_the_surface_when_the_writer_resolves_the_profile() -> None:
    assert _append_params()["normalizer_version"] == NORMALIZER_VERSION
    assert _append_params(page_kind="index")["normalizer_version"] == (
        f"{NORMALIZER_VERSION}{payload_norm.BASE_PROFILE_SUFFIX}")


def test_the_stamp_names_the_caller_supplied_profile_not_the_profile_table() -> None:
    """The MAJOR. `volatile=` decides the hash; `normalizer_version` is the permanent
    column that explains it. Derived from the profile TABLE — from whether an entry
    EXISTS for this surface rather than from what was actually applied — the stamp
    asserts "only the generic base was stripped" about a row normalised under a real
    measured profile, and no later reader can detect it: `portal_raw_pages` is
    latest-wins, so the body it describes is gone on the next refetch.

    Latent today (no production caller passes `volatile`, the table is empty, both
    flags OFF) and load-bearing from W2a-3b, which passes contract-sourced selectors
    in exactly this shape."""
    body = b'<html><body><h1>Byt</h1><address>Dlouha 1</address></body></html>'
    # Strips a node the shipped idnes DETAIL profile keeps, so the two instruments
    # genuinely produce different content addresses for the same bytes.
    profile = VolatileProfile(css_selectors=("address",))

    params = _append_params(
        body=body, volatile=profile, normalizer_version="contract@7")

    assert params["normalizer_version"] == "contract@7"
    # ... and the address it explains really is the one that profile produces.
    assert params["payload_sha256"] == payload_norm.normalise(
        body, content_type=_HTML, volatile=profile).norm_sha256
    assert params["payload_sha256"] != payload_norm.normalise(
        body, content_type=_HTML,
        volatile=payload_norm.volatile_profile("idnes", "detail"),
    ).norm_sha256


def test_overriding_the_cohort_alone_leaves_the_projection_untouched() -> None:
    """`record_payload_churn`'s established shape — the confirmation probe files its
    own cadence in its own cohort while hashing with the SAME profile. Overriding the
    label alone can never make the stamp lie about the projection."""
    probed = _append_params(normalizer_version=probe_normalizer_version())
    plain = _append_params()

    assert probed["normalizer_version"] == (
        f"{NORMALIZER_VERSION}{PROBE_NORMALIZER_SUFFIX}")
    assert probed["payload_sha256"] == plain["payload_sha256"]


@pytest.mark.parametrize("unnamed", [None, ""])
def test_an_explicit_profile_without_its_cohort_is_refused_before_any_write(
    unnamed: str | None,
) -> None:
    """A projection nobody can name must not reach a permanent content address.

    The empty string is refused alongside None because an `or`-style fallback would
    otherwise quietly restore the resolver's stamp and re-open exactly this gap. The
    refusal precedes every statement, so there is no row left to explain."""
    conn = _RecordingConn()
    kwargs: dict[str, Any] = {} if unnamed is None else {"normalizer_version": unnamed}

    with pytest.raises(payloads.PayloadError, match="normalizer_version"):
        payloads.append_payload(
            conn,  # type: ignore[arg-type]
            source="idnes", source_id_native="k", page_kind="detail",
            listing_id=None, body=b"<html></html>", content_type=_HTML,
            http_status=200, contract_version=None,
            observed_at=datetime.now(timezone.utc),
            volatile=VolatileProfile(css_selectors=("div",)),
            **kwargs,
        )

    assert conn.executed == []


# ------------------------------------------------------------------------ live


class _FakeStore:
    """Records what would have gone to R2, and how many times."""

    def __init__(self, fail: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads = 0
        self.fail = fail

    def upload_bytes(self, key: str, data: bytes, content_type: str = "") -> None:
        self.uploads += 1
        if self.fail:
            raise RuntimeError("R2 is down")
        self.objects[key] = data


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_DB_URL, autocommit=True) as c:
        yield c


def _key() -> str:
    return f"live-{uuid.uuid4().hex}"


# The cohort the tests below that construct a bespoke projection write under. It is
# not `NORMALIZER_VERSION`: those bodies are NOT hashed by the shipped instrument,
# and saying they were is exactly the disagreement `append_payload` now refuses.
_CONSTRUCTED_COHORT = f"{NORMALIZER_VERSION}+constructed"


def _append(
    conn: psycopg.Connection,
    native: str,
    body: bytes,
    *,
    observed_at: datetime | None = None,
    content_type: str = _JSON,
    page_kind: str = "detail",
    http_status: int | None = 200,
    volatile: VolatileProfile | None = None,
    **kwargs: Any,
) -> payloads.PayloadRef:
    # No profile by default — the (source, page_kind) resolver answers both the
    # projection and its label, which is what the live path does. A test that DOES
    # construct one names its cohort, because `append_payload` refuses the pair
    # otherwise: `normalizer_version` is the only record of which instrument produced
    # a permanent `payload_sha256`, so a constructed profile stamped `payload_norm@3`
    # would be a row claiming a measurement that never happened.
    if volatile is not None:
        kwargs.setdefault("normalizer_version", _CONSTRUCTED_COHORT)
    # THE TIME FLOOR IS OFF BY DEFAULT HERE, and only here. Every test above and below
    # is about the collision/pin/cap semantics, which need several bodies in one group
    # in one wall-clock second — under the shipped 7-day floor those appends would be
    # suppressed and the tests would be measuring the floor instead of what they name.
    # The floor's own behaviour is tested explicitly further down, where it is passed in
    # rather than defaulted, so neither axis can hide a regression in the other.
    kwargs.setdefault("min_append_interval_days", 0)
    return payloads.append_payload(
        conn,
        source="idnes",
        source_id_native=native,
        page_kind=page_kind,
        listing_id=None,
        body=body,
        content_type=content_type,
        http_status=http_status,
        contract_version=1,
        observed_at=observed_at or datetime.now(timezone.utc),
        volatile=volatile,
        **kwargs,
    )


def _rows(conn: psycopg.Connection, native: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, version_seq, pinned, page_kind::text, payload_sha256, body_sha256, "
            "       body, body_r2_key, byte_size, content_encoding, normalizer_version, "
            "       first_observed_at, last_observed_at, http_status "
            "FROM portal_raw_payloads WHERE source_id_native = %s "
            "ORDER BY page_kind::text, version_seq",
            (native,),
        )
        names = [d.name for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


@requires_db
def test_a_changed_body_appends_a_second_row(conn: psycopg.Connection) -> None:
    # 06 W2a gate (b), direction 1.
    native = _key()

    first = _append(conn, native, b'{"street": "Dlouha 1"}')
    second = _append(conn, native, b'{"street": "Dlouha 2"}')

    assert (first.inserted, second.inserted) == (True, True)
    rows = _rows(conn, native)
    assert [r["version_seq"] for r in rows] == [1, 2]
    assert rows[0]["payload_sha256"] != rows[1]["payload_sha256"]
    assert all(r["normalizer_version"] == NORMALIZER_VERSION for r in rows)


@requires_db
def test_an_unchanged_refetch_appends_nothing_and_only_bumps_last_observed_at(
    conn: psycopg.Connection,
) -> None:
    # 06 W2a gate (b), direction 2 — the property the whole store exists for.
    native = _key()
    first_at = datetime.now(timezone.utc) - timedelta(hours=6)
    later = first_at + timedelta(hours=3)

    _append(conn, native, b'{"street": "Dlouha 1"}', observed_at=first_at)
    before = _rows(conn, native)
    again = _append(conn, native, b'{"street": "Dlouha 1"}', observed_at=later)

    assert again.inserted is False
    rows = _rows(conn, native)
    assert len(rows) == 1
    assert rows[0]["first_observed_at"] == before[0]["first_observed_at"] == first_at
    assert rows[0]["last_observed_at"] == later
    assert rows[0]["version_seq"] == before[0]["version_seq"] == 1


@requires_db
def test_a_volatile_only_change_is_not_a_change(conn: psycopg.Connection) -> None:
    # The reason the identity is the NORMALISED hash: byte-different, content-equal.
    native = _key()
    first_body = b'{"a": 1, "b": 2}'

    _append(conn, native, first_body)
    again = _append(conn, native, b'{"b":   2,\n "a": 1}')

    assert again.inserted is False
    rows = _rows(conn, native)
    assert len(rows) == 1
    # The raw hash still names the body observed FIRST — it is forensics, and the
    # second fetch's bytes were never stored.
    assert bytes(rows[0]["body_sha256"]) == hashlib.sha256(first_body).digest()


@requires_db
def test_the_append_is_idempotent_under_replay(conn: psycopg.Connection) -> None:
    # portal_runner._flush_drain_batch re-runs the whole batch write after a
    # transient pooler drop, so the same body arrives two or three times.
    native = _key()

    _append(conn, native, b'{"v": 1}')
    _append(conn, native, b'{"v": 1}')
    third = _append(conn, native, b'{"v": 1}')

    rows = _rows(conn, native)
    assert len(rows) == 1
    assert rows[0]["version_seq"] == third.version_seq == 1


@requires_db
def test_first_observed_at_is_the_payloads_time_not_now(conn: psycopg.Connection) -> None:
    # 06 Rule 1. A backfilled body keeps the time it was actually fetched, or the
    # whole archive reads as having appeared on migration day.
    native = _key()
    fetched_at = datetime(2026, 6, 1, 7, 30, tzinfo=timezone.utc)

    _append(conn, native, b'{"v": 1}', observed_at=fetched_at)

    row = _rows(conn, native)[0]
    assert row["first_observed_at"] == row["last_observed_at"] == fetched_at


@requires_db
def test_an_out_of_order_observation_moves_first_observed_at_backwards(
    conn: psycopg.Connection,
) -> None:
    # W2a-4 backfills portal_raw_pages.fetched_at into a store the live path may
    # already have written, so "first observed" must mean earliest observation.
    native = _key()
    live_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    archived_at = datetime(2026, 6, 1, tzinfo=timezone.utc)

    _append(conn, native, b'{"v": 1}', observed_at=live_at)
    _append(conn, native, b'{"v": 1}', observed_at=archived_at)

    row = _rows(conn, native)[0]
    assert row["first_observed_at"] == archived_at
    assert row["last_observed_at"] == live_at


@requires_db
def test_the_version_cap_evicts_the_oldest_unpinned_bodies(conn: psycopg.Connection) -> None:
    native = _key()

    for i in range(8):
        ref = _append(conn, native, f'{{"v": {i}}}'.encode(), version_cap=5)

    # Five ranks' worth survive, plus version 1 — pinned as the first body and
    # therefore exempt from the cap rather than counted inside it.
    assert [r["version_seq"] for r in _rows(conn, native)] == [1, 4, 5, 6, 7, 8]
    assert ref.evicted_ids
    # The counter never rewinds: the latest is always pinned, so max() is stable.
    assert ref.version_seq == 8


@requires_db
def test_the_first_and_latest_versions_are_never_evicted(conn: psycopg.Connection) -> None:
    native = _key()

    for i in range(6):
        _append(conn, native, f'{{"v": {i}}}'.encode(), version_cap=2)

    rows = _rows(conn, native)
    assert rows[0]["version_seq"] == 1 and rows[0]["pinned"] is True
    assert rows[-1]["version_seq"] == 6 and rows[-1]["pinned"] is True
    # Everything between them beyond the cap is gone.
    assert [r["version_seq"] for r in rows] == [1, 5, 6]


@requires_db
def test_a_body_referenced_by_an_open_contradiction_is_never_evicted(
    conn: psycopg.Connection,
) -> None:
    # P4's third pin: a disputed body is the evidence an arbitration decision rests
    # on, so the cap must not reclaim it while the finding is open.
    #
    # The control arm carries BYTE-IDENTICAL bodies under a different native id, so
    # it also pins the bug the first cut of the pin predicate had: matching on the
    # content address alone made one listing's dispute freeze another listing's
    # history, because the hash is not a listing.
    control, disputed = _key(), _key()
    for native in (control, disputed):
        for i in range(3):
            _append(conn, native, f'{{"v": {i}}}'.encode(), version_cap=2)

    middle = _rows(conn, disputed)[1]
    _open_contradiction(conn, disputed, bytes(middle["payload_sha256"]))
    for native in (control, disputed):
        _append(conn, native, b'{"v": 99}', version_cap=2)

    assert [r["version_seq"] for r in _rows(conn, control)] == [1, 3, 4]
    assert [r["version_seq"] for r in _rows(conn, disputed)] == [1, 2, 3, 4]
    assert _rows(conn, disputed)[1]["pinned"] is True


@requires_db
def test_a_body_a_claim_points_at_is_never_evicted(conn: psycopg.Connection) -> None:
    """The FK, not a policy: location_claims.payload_id references this table with NO
    ACTION (382), so evicting a referenced body raises ForeignKeyViolation and rolls
    back the whole bounded transaction — losing the body just appended, and every
    later append for that listing, permanently.

    The claim here carries NO contradiction: an ordinary mined claim is enough."""
    native = _key()
    for i in range(3):
        _append(conn, native, f'{{"v": {i}}}'.encode(), version_cap=2)
    middle = _rows(conn, native)[1]
    _claim_on_payload(conn, native, int(middle["id"]))

    # Without the pin the FIRST of these raises ForeignKeyViolation and neither
    # version 4 nor version 5 ever exists.
    _append(conn, native, b'{"v": 98}', version_cap=2)
    _append(conn, native, b'{"v": 99}', version_cap=2)

    rows = _rows(conn, native)
    assert [r["version_seq"] for r in rows] == [1, 2, 4, 5]
    assert rows[1]["pinned"] is True and rows[1]["id"] == middle["id"]


@requires_db
def test_an_outage_streak_evicts_itself_not_the_real_history(
    conn: psycopg.Connection,
) -> None:
    """http_status was written and never read, so a 503 interstitial cost a version:
    `version_cap` refetches of an outage evicted the listing's whole real history.
    Unsuccessful fetches rank behind successful ones, so the errors go first."""
    native = _key()
    for i in range(5):
        _append(conn, native, f'{{"real": {i}}}'.encode(), version_cap=5)
    for i in range(8):
        _append(conn, native, f'{{"err": {i}}}'.encode(), version_cap=5, http_status=503)

    rows = _rows(conn, native)
    assert [r["version_seq"] for r in rows] == [1, 2, 3, 4, 5, 13]
    assert [r["http_status"] for r in rows] == [200, 200, 200, 200, 200, 503]


@requires_db
def test_an_unchanged_refetch_describes_the_stored_row_not_the_fetch(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a collision the encode pass is discarded, so a ref built from it would
    advertise an R2 object that was never uploaded and a byte_size the row does not
    have. The second body here is volatile-only different AND incompressible, so
    every advertised field would differ from the row's."""
    native = _key()
    small = b'{"a": 1, "b": 2}'
    # Incompressible, so it clears the spill threshold the small body stays under.
    big = b'{"b":   2,\n "a": 1,\n "x": "' + os.urandom(120_000).hex().encode() + b'"}'
    monkeypatch.setenv(payloads.R2_THRESHOLD_ENV, "10000")
    store = _FakeStore()
    profile = VolatileProfile(json_pointers=("/x",))

    first = _append(conn, native, small, volatile=profile, store=store)
    again = _append(conn, native, big, volatile=profile, store=store)

    assert again.inserted is False
    assert (again.body_r2_key, again.content_encoding) == (None, "identity")
    assert (again.byte_size, again.stored_bytes) == (len(small), len(small))
    assert again.body_sha256 == first.body_sha256 == hashlib.sha256(small).digest()
    assert store.objects == {}


@requires_db
def test_an_evicted_key_still_held_by_a_live_row_is_not_reclaimable(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 keys are content-addressed, so two listings that served byte-identical
    bodies share one object. Handing an evicted row's key to W2a-5's deleter
    unfiltered would destroy the live listing's body."""
    shared, other = _key(), _key()
    body = b"<html>" + b"gone" * 20_000 + b"</html>"
    monkeypatch.setenv(payloads.R2_THRESHOLD_ENV, "1")
    store = _FakeStore()

    shared_ref = _append(conn, shared, body, content_type=_HTML, store=store)
    # In `other` the shared body is version 2, so the first/latest pins do not
    # protect it and the cap reaches it.
    _append(conn, other, b"<html>v1</html>", content_type=_HTML, store=store)
    doomed = _append(conn, other, body, content_type=_HTML, store=store)
    evicting = _append(conn, other, b"<html>v3</html>", content_type=_HTML,
                       store=store, version_cap=1)

    assert doomed.body_r2_key == shared_ref.body_r2_key
    assert evicting.evicted_ids == (doomed.id,)
    assert evicting.evicted_r2_keys == ()  # the object is still `shared`'s body
    assert shared_ref.body_r2_key in store.objects


@requires_db
def test_the_body_is_anchored_to_the_snapshot_it_was_fetched_for(
    conn: psycopg.Connection,
) -> None:
    """location_claims.snapshot_anchor='snapshot' is the DEFAULT anchor, so a body
    the writer cannot snapshot-anchor is a body no anchored claim can join to. And
    fetched_at must be the payload's own time, not migration day."""
    native = _key()
    fetched_at = datetime(2026, 6, 1, 7, 30, tzinfo=timezone.utc)

    _append(conn, native, b'{"v": 1}', observed_at=fetched_at, snapshot_id=4242)
    # A later unanchored sighting of the same body must not un-anchor it.
    _append(conn, native, b'{"v": 1}')

    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot_id, fetched_at FROM portal_raw_payloads "
            "WHERE source_id_native = %s",
            (native,),
        )
        assert cur.fetchone() == (4242, fetched_at)


@requires_db
def test_an_unanchored_body_gains_the_anchor_a_later_fetch_carries(
    conn: psycopg.Connection,
) -> None:
    # W2a-4 backfills bodies that predate their snapshot; the live path anchors them.
    native = _key()

    _append(conn, native, b'{"v": 1}')
    _append(conn, native, b'{"v": 1}', snapshot_id=77)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot_id FROM portal_raw_payloads WHERE source_id_native = %s",
            (native,),
        )
        assert cur.fetchone() == (77,)


@requires_db
def test_a_large_body_round_trips_through_the_stored_bytes(conn: psycopg.Connection) -> None:
    # 06 gate (a)'s unit-level half: what comes out of the column, decoded by its
    # own content_encoding, is the body that went in.
    native = _key()
    body = b"<html>" + bytes(range(256)) * 60 + b"</html>"

    ref = _append(conn, native, body, content_type=_HTML)

    row = _rows(conn, native)[0]
    assert row["content_encoding"] == "gzip"
    assert payloads.decode_body(bytes(row["body"]), row["content_encoding"]) == body
    assert row["byte_size"] == len(body) == ref.byte_size


@requires_db
def test_a_body_over_the_threshold_spills_to_r2_and_leaves_the_column_null(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _key()
    body = b"<html>" + b"spill" * 4000 + b"</html>"
    monkeypatch.setenv(payloads.R2_THRESHOLD_ENV, "1")
    store = _FakeStore()

    ref = _append(conn, native, body, content_type=_HTML, store=store)

    row = _rows(conn, native)[0]
    assert row["body"] is None
    # Keyed on body_sha256 — the hash of what the object HOLDS, not the normalised
    # hash the row is identified by.
    assert row["body_r2_key"] == ref.body_r2_key == payloads.r2_key("idnes", ref.body_sha256)
    assert payloads.decode_body(store.objects[row["body_r2_key"]], "gzip") == body
    # prp_body_present is satisfied by the key alone — the row exists, so it held.
    assert row["byte_size"] == len(body)


@requires_db
def test_two_groups_whose_normalised_bodies_coincide_do_not_share_an_object(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keying on the NORMALISED hash handed one object to two rows whose raw bytes
    differ — the second row's own bytes were never uploaded and every span mined
    from it indexed into the first row's body.

    Not hypothetical: two listings served the same blocked/interstitial page, which
    differs only in a per-request `nonce` — an attribute the profile strips."""
    a, b = _key(), _key()
    filler = b"<p>" + b"z" * 40_000 + b"</p>"
    body_a = b'<html><div nonce="aaaa">blocked</div>' + filler + b"</html>"
    body_b = b'<html><div nonce="bbbb">blocked</div>' + filler + b"</html>"
    monkeypatch.setenv(payloads.R2_THRESHOLD_ENV, "1")
    store = _FakeStore()
    profile = VolatileProfile(strip_attributes=("nonce",))

    ref_a = _append(conn, a, body_a, content_type=_HTML, store=store, volatile=profile)
    ref_b = _append(conn, b, body_b, content_type=_HTML, store=store, volatile=profile)

    assert ref_a.payload_sha256 == ref_b.payload_sha256  # same normalised content
    assert ref_a.body_r2_key != ref_b.body_r2_key
    assert len(store.objects) == 2
    assert payloads.decode_body(store.objects[ref_a.body_r2_key], "gzip") == body_a
    assert payloads.decode_body(store.objects[ref_b.body_r2_key], "gzip") == body_b


@requires_db
def test_a_spilled_body_is_uploaded_once_however_often_it_is_refetched(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The unchanged path must not pay an R2 round trip: the collision short-circuits
    # before the upload, so a 6-hourly refetch of an unchanged page is DB-only.
    native = _key()
    body = b"<html>" + b"spill" * 4000 + b"</html>"
    monkeypatch.setenv(payloads.R2_THRESHOLD_ENV, "1")
    store = _FakeStore()

    _append(conn, native, body, content_type=_HTML, store=store)
    _append(conn, native, body, content_type=_HTML, store=store)
    _append(conn, native, body, content_type=_HTML, store=store)

    assert len(store.objects) == 1
    # The collision short-circuits before the upload, so the two refetches are
    # DB-only: no PUT, and no HEAD either (the HEAD that used to guard the PUT was
    # removed — it doubled the network time held inside the write transaction to
    # save re-writing ~20 KB under a key that is the hash of those same bytes).
    assert store.uploads == 1


@requires_db
def test_an_index_page_is_a_separate_group_from_the_detail_page(
    conn: psycopg.Connection,
) -> None:
    # page_kind is part of the identity, so the same native id archived from two
    # surfaces never collides and neither group's cap touches the other.
    native = _key()

    _append(conn, native, b'{"v": 1}', page_kind="detail")
    _append(conn, native, b'{"v": 1}', page_kind="index")

    rows = _rows(conn, native)
    assert [(r["page_kind"], r["version_seq"]) for r in rows] == [
        ("detail", 1), ("index", 1),
    ]


def _claim_on_payload(conn: psycopg.Connection, native: str, payload_id: int) -> int:
    """One ordinary mined claim carrying the FK — no contradiction, nothing disputed.

    This is what W3 writes for every listing it mines, and it is what the retention
    DELETE has to survive."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO location_claims "
            "  (listing_id, source, source_id_native, snapshot_anchor, payload_id, "
            "   payload_sha256, first_observed_at, claim_type, surface, page_kind, "
            "   extraction_method, extractor_id, extractor_version, value_text, "
            "   licence_class, claim_fingerprint) "
            "SELECT 1, 'idnes', %s, 'unanchored_latest_fetch', p.id, p.payload_sha256, "
            "       now(), 'street_name', 'html_selector', 'detail', "
            "       'html_selector_parse', 'test', '1', 'Dlouha', 'portal', %s "
            "  FROM portal_raw_payloads p WHERE p.id = %s "
            "RETURNING id",
            (native, hashlib.sha256(f"{native}:{payload_id}".encode()).digest(), payload_id),
        )
        return int(cur.fetchone()[0])


def _open_contradiction(
    conn: psycopg.Connection, native: str, payload_sha256: bytes,
) -> None:
    """A claim carrying this body, and an open ledger row pointing at that claim.

    `location_claims` has no `disputed` column by design (it is append-only
    evidence); "disputed" means an OPEN `location_contradictions` row names the claim.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO registry_versions "
            "  (label, kind, source_date, artifact_urls, proj_version, proj_pipeline) "
            "VALUES (%s, 'baseline', current_date, '{}'::jsonb, 'test', 'test') "
            "RETURNING id",
            (f"payload-test-{native}",),
        )
        registry_version_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO location_claims "
            "  (listing_id, source, source_id_native, snapshot_anchor, payload_sha256, "
            "   first_observed_at, claim_type, surface, page_kind, extraction_method, "
            "   extractor_id, extractor_version, value_text, licence_class, "
            "   claim_fingerprint) "
            "VALUES (1, 'idnes', %s, 'unanchored_latest_fetch', %s, now(), 'street_name', "
            "        'html_selector', 'detail', 'html_selector_parse', 'test', '1', "
            "        'Dlouha', 'portal', %s) "
            "RETURNING id",
            (native, payload_sha256, hashlib.sha256(native.encode()).digest()),
        )
        claim_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO location_contradictions "
            "  (listing_id, reconciler_version, registry_version_id, field, rule, "
            "   severity, served_claim_id, auto_action, dedupe_key) "
            "VALUES (1, 'test', %s, 'street_name', 'test_rule', 'major', %s, 'none', %s)",
            (registry_version_id, claim_id, payload_sha256),
        )


@requires_db
def test_a_gzip_body_written_by_the_writer_is_readable_without_the_writer(
    conn: psycopg.Connection,
) -> None:
    # The archive must be readable by anything that knows gzip — a future verifier
    # or an operator with psql — not only by this module.
    native = _key()
    body = b"a" * 50_000

    _append(conn, native, body, content_type=_HTML)

    row = _rows(conn, native)[0]
    assert gzip.decompress(bytes(row["body"])) == body


@requires_db
def test_the_profile_and_the_cohort_follow_the_surface_not_the_portal(
    conn: psycopg.Connection,
) -> None:
    """`volatile=None` resolves by (source, page_kind). Every shipped profile was
    measured by diffing DETAIL pages, so an index body must NOT be addressed through
    one: `payload_sha256` is this store's identity, and a hash taken over the wrong
    projection is permanent — every evidence span into that body inherits it.
    Same bytes, two surfaces, two rows, and the row says which instrument made it."""
    native = _key()
    body = (b'<html><body><h1>Byt 3+1</h1>'
            b'<div class="grid-similar-offers">other listings</div></body></html>')

    detail = payloads.append_payload(
        conn, source="idnes", source_id_native=native, page_kind="detail",
        listing_id=None, body=body, content_type=_HTML, http_status=200,
        contract_version=None, observed_at=datetime.now(timezone.utc), volatile=None,
    )
    index = payloads.append_payload(
        conn, source="idnes", source_id_native=native, page_kind="index",
        listing_id=None, body=body, content_type=_HTML, http_status=200,
        contract_version=None, observed_at=datetime.now(timezone.utc), volatile=None,
    )

    assert detail.body_sha256 == index.body_sha256
    assert detail.payload_sha256 != index.payload_sha256
    cohorts = {r["page_kind"]: r["normalizer_version"] for r in _rows(conn, native)}
    assert cohorts == {
        "detail": NORMALIZER_VERSION,
        "index": f"{NORMALIZER_VERSION}+base",
    }


@requires_db
def test_the_stamp_names_the_profile_that_was_actually_applied(
    conn: psycopg.Connection,
) -> None:
    """The MAJOR, end to end: a caller-supplied profile must reach the row's
    `normalizer_version`, not the profile table's answer for that surface.

    The projection here deletes a node the shipped idnes DETAIL profile keeps, so the
    two instruments produce DIFFERENT content addresses for the same bytes. Under the
    old shape both rows would have been stamped `payload_norm@3` — one of them a lie
    that no later reader could detect, since the body it describes is gone from
    `portal_raw_pages` the moment the page is refetched."""
    measured, constructed = _key(), _key()
    body = (b'<html><body><h1>Byt 3+1</h1>'
            b'<address>Dlouha 1, Praha</address></body></html>')
    # Not in `_IDNES_VOLATILE`: the shipped detail profile keeps this node.
    profile = VolatileProfile(css_selectors=("address",))

    shipped = _append(conn, measured, body, content_type=_HTML)
    caller = _append(conn, constructed, body, content_type=_HTML, volatile=profile,
                     normalizer_version="contract@7")

    assert shipped.payload_sha256 != caller.payload_sha256, (
        "the two profiles must disagree, or this proves nothing")
    assert _rows(conn, measured)[0]["normalizer_version"] == NORMALIZER_VERSION
    assert _rows(conn, constructed)[0]["normalizer_version"] == "contract@7"
# --------------------------------------------------- the per-listing time floor

def test_the_floor_window_is_pure_and_zero_disables_it() -> None:
    """`append_floor_cutoff` is the whole time arithmetic, kept out of SQL so it is
    testable without a database — and so `days=0` is provably an exact no-op rather
    than an off-by-one that suppresses a same-instant body."""
    at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)

    assert payloads.append_floor_cutoff(at, 7) == at - timedelta(days=7)
    # The guard's window is (cutoff, observed_at]; at 0 days that is empty, so no
    # existing row can ever fall inside it.
    assert payloads.append_floor_cutoff(at, 0) == at


def test_the_floor_interval_env_accepts_zero_unlike_every_other_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0 is a real setting here ("no floor"), not the unbounded state
    `loader_db.env_positive_int` refuses — storage stays bounded by the cap, which
    cannot be zeroed. A negative value is still a typo and takes the default."""
    monkeypatch.setenv(payloads.MIN_APPEND_INTERVAL_ENV, "0")
    assert payloads.env_non_negative_int(payloads.MIN_APPEND_INTERVAL_ENV, 7) == 0

    monkeypatch.setenv(payloads.MIN_APPEND_INTERVAL_ENV, "-3")
    assert payloads.env_non_negative_int(payloads.MIN_APPEND_INTERVAL_ENV, 7) == 7

    monkeypatch.setenv(payloads.MIN_APPEND_INTERVAL_ENV, "not-a-number")
    assert payloads.env_non_negative_int(payloads.MIN_APPEND_INTERVAL_ENV, 7) == 7


def test_the_shipped_defaults_are_the_ones_the_budget_was_signed_for() -> None:
    """A cap of 20 permits a 128 GB archive against a subsystem budgeted at 20 GB
    total (location_data/payload_budget.py). If either default moves, the ceiling
    the operator signed moves with it — so pin both here as well as in the budget
    test, where the arithmetic itself lives."""
    assert payloads.DEFAULT_VERSION_CAP == 2
    assert payloads.DEFAULT_MIN_APPEND_INTERVAL_DAYS == 7


@requires_db
def test_the_floor_never_suppresses_a_listings_first_body(
    conn: psycopg.Connection,
) -> None:
    """The one exemption that must hold unconditionally: an empty group satisfies
    neither EXISTS arm, so the guard admits the body. Without this the archive would
    never start for any listing."""
    native = _key()

    ref = _append(conn, native, b'{"v": 1}', min_append_interval_days=7)

    assert (ref.inserted, ref.suppressed) == (True, False)
    assert [r["version_seq"] for r in _rows(conn, native)] == [1]


@requires_db
def test_a_second_body_inside_the_window_is_suppressed(conn: psycopg.Connection) -> None:
    """The bound that makes storage independent of filter quality: a changed body
    that would otherwise append is refused because one was appended 2 days ago."""
    native = _key()
    day0 = datetime(2026, 6, 1, tzinfo=timezone.utc)

    first = _append(conn, native, b'{"v": 1}', observed_at=day0,
                    min_append_interval_days=7)
    second = _append(conn, native, b'{"v": 2}', observed_at=day0 + timedelta(days=2),
                     min_append_interval_days=7)

    assert second.suppressed is True and second.inserted is False
    rows = _rows(conn, native)
    assert len(rows) == 1
    assert bytes(rows[0]["payload_sha256"]) == bytes(first.payload_sha256)
    # The ref describes the body the archive HOLDS, not the one just discarded —
    # a caller told otherwise would believe a body is archived that is not.
    assert second.id == first.id
    assert second.payload_sha256 == first.payload_sha256
    assert second.byte_size == first.byte_size


@requires_db
def test_a_body_past_the_window_is_appended(conn: psycopg.Connection) -> None:
    """The floor delays, it does not drop: the next fetch past the window archives
    the page as it stands then, so a change that persists is always captured."""
    native = _key()
    day0 = datetime(2026, 6, 1, tzinfo=timezone.utc)

    _append(conn, native, b'{"v": 1}', observed_at=day0, min_append_interval_days=7)
    _append(conn, native, b'{"v": 2}', observed_at=day0 + timedelta(days=2),
            min_append_interval_days=7)
    later = _append(conn, native, b'{"v": 3}', observed_at=day0 + timedelta(days=8),
                    min_append_interval_days=7)

    assert later.suppressed is False and later.inserted is True
    assert [r["version_seq"] for r in _rows(conn, native)] == [1, 2]


@requires_db
def test_an_unchanged_refetch_inside_the_window_still_bumps_last_observed_at(
    conn: psycopg.Connection,
) -> None:
    """The floor bounds BODIES, not knowledge. An identical refetch writes no row
    whatever the floor says, so suppressing it would cost nothing but the signal
    that this content is still being served — which is what `last_observed_at`
    feeds and what the hot-window pruner reads."""
    native = _key()
    day0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    later = day0 + timedelta(days=1)

    _append(conn, native, b'{"v": 1}', observed_at=day0, min_append_interval_days=7)
    again = _append(conn, native, b'{"v": 1}', observed_at=later,
                    min_append_interval_days=7)

    assert (again.inserted, again.suppressed) == (False, False)
    rows = _rows(conn, native)
    assert len(rows) == 1
    assert rows[0]["last_observed_at"] == later


@requires_db
def test_the_floor_rate_limits_against_the_bodys_own_era_not_the_newest_row(
    conn: psycopg.Connection,
) -> None:
    """Out-of-order arrivals are the W2a-4 backfill's whole shape: bodies carry the
    time they were really fetched (06 Rule 1). The window is the N days BEFORE this
    observation, so a June body is rate-limited against June neighbours and not
    against an August body the live path already wrote."""
    native = _key()
    august = datetime(2026, 8, 1, tzinfo=timezone.utc)
    june = datetime(2026, 6, 1, tzinfo=timezone.utc)

    _append(conn, native, b'{"v": "live"}', observed_at=august,
            min_append_interval_days=7)
    backfilled = _append(conn, native, b'{"v": "archived"}', observed_at=june,
                         min_append_interval_days=7)

    assert backfilled.suppressed is False and backfilled.inserted is True
    assert len(_rows(conn, native)) == 2


@requires_db
def test_the_floor_and_the_cap_compose_the_floor_first(conn: psycopg.Connection) -> None:
    """The pair is the deliverable: the floor bounds the FLOW (one body per window)
    and the cap bounds the STOCK (cap + the pinned first). Twelve weekly fetches of a
    page that changes every time settle at the cap, and the bodies retained span
    weeks of history rather than the last few hours of it."""
    native = _key()
    day0 = datetime(2026, 6, 1, tzinfo=timezone.utc)

    for week in range(12):
        # Two fetches a week, both changed: the second of each pair is inside the
        # window and must never reach the store.
        for offset in (0, 3):
            _append(conn, native, f'{{"w": {week}, "o": {offset}}}'.encode(),
                    observed_at=day0 + timedelta(days=7 * week + offset),
                    min_append_interval_days=7, version_cap=2)

    rows = _rows(conn, native)
    # 12 admitted bodies, capped to (cap=2) + the pinned first.
    assert [r["version_seq"] for r in rows] == [1, 11, 12]
    assert rows[0]["pinned"] is True and rows[-1]["pinned"] is True
    span = rows[-1]["first_observed_at"] - rows[0]["first_observed_at"]
    assert span >= timedelta(days=70)


@requires_db
def test_the_counters_separate_a_suppressed_write_from_a_quiet_portal(
    conn: psycopg.Connection,
) -> None:
    """A floor that suppresses everything and a portal that stopped changing produce
    the same empty archive diff. These three counters are the only thing that tells
    them apart, which is why they are counted rather than sampled."""
    native = _key()
    day0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    payloads.reset_archive_stats()
    try:
        _append(conn, native, b'{"v": 1}', observed_at=day0, min_append_interval_days=7)
        _append(conn, native, b'{"v": 1}', observed_at=day0 + timedelta(hours=1),
                min_append_interval_days=7)
        _append(conn, native, b'{"v": 2}', observed_at=day0 + timedelta(days=1),
                min_append_interval_days=7)

        stats = payloads.archive_stats()
        assert (stats.appended, stats.unchanged, stats.suppressed) == (1, 1, 1)
        assert stats.decisions == 3
        assert stats.as_dict()["suppressed"] == 1
    finally:
        payloads.reset_archive_stats()


@requires_db
def test_an_eviction_is_counted_in_bytes_as_well_as_rows(
    conn: psycopg.Connection,
) -> None:
    """`evicted_bytes` is what came BACK, and it is the figure a storage sign-off is
    read from — rows alone cannot distinguish a reclaimed 2 KB body from a 250 KB
    one."""
    native = _key()
    payloads.reset_archive_stats()
    try:
        for i in range(4):
            _append(conn, native, f'{{"v": {i}}}'.encode(), version_cap=1)

        stats = payloads.archive_stats()
        assert stats.evicted_rows >= 1
        assert stats.evicted_bytes > 0
    finally:
        payloads.reset_archive_stats()


# ------------------------------------------- R2 as the bodies' home (W2a-7)


@requires_db
def test_a_spilled_row_records_the_size_of_the_object_it_points_at(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`octet_length(body)` is NULL once the bytes are in the bucket, which made
    "bytes reclaimed" a permanent zero the moment spilling became the default — on the
    one figure the storage sign-off is read from. Migration 405's column is where that
    size now lives, and it is the ROW that answers, not an R2 HEAD."""
    native = _key()
    body = b"<html>" + os.urandom(20_000).hex().encode() + b"</html>"
    monkeypatch.setenv(payloads.R2_THRESHOLD_ENV, "2048")
    store = _FakeStore()

    ref = _append(conn, native, body, content_type=_HTML, store=store)

    row = _rows(conn, native)[0]
    assert row["body"] is None and row["body_r2_key"] is not None
    assert ref.stored_bytes == len(store.objects[ref.body_r2_key])
    assert ref.byte_size == len(body) > ref.stored_bytes


@requires_db
def test_evicting_a_spilled_body_still_reports_the_bytes_it_freed(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counter the pruner's report and the archive stats are both read from. With
    bodies in R2 this is the whole of it — there are no inline bytes left to count."""
    native = _key()
    monkeypatch.setenv(payloads.R2_THRESHOLD_ENV, "2048")
    store = _FakeStore()
    payloads.reset_archive_stats()

    for i in range(4):
        _append(conn, native, f"<html>{i}".encode() + os.urandom(20_000).hex().encode(),
                content_type=_HTML, store=store, version_cap=1)

    assert payloads.archive_stats().evicted_rows > 0
    assert payloads.archive_stats().evicted_bytes > 10_000


@requires_db
def test_a_failed_upload_commits_no_row_pointing_at_a_missing_object(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE ORDERING GUARANTEE. The upload runs inside `loader_db.bounded`'s
    transaction, so an R2 outage rolls the metadata row back rather than committing a
    `body_r2_key` that resolves to nothing — a span into a body that does not exist is
    exactly the unverifiability this store exists to end. (The reverse orphan, an
    object nothing references, is harmless: the key is the hash of its bytes.)"""
    native = _key()
    body = b"<html>" + os.urandom(20_000).hex().encode() + b"</html>"
    monkeypatch.setenv(payloads.R2_THRESHOLD_ENV, "2048")

    with pytest.raises(RuntimeError, match="R2 is down"):
        _append(conn, native, body, content_type=_HTML, store=_FakeStore(fail=True))

    assert _rows(conn, native) == []


@requires_db
def test_two_writers_on_one_key_cannot_both_pass_the_floor(
    conn: psycopg.Connection,
) -> None:
    """The floor's check and its insert share one STATEMENT, but under READ COMMITTED
    they do not share a snapshot with a concurrent session: both could find the window
    empty and both insert. The transaction-scoped advisory lock on the group is what
    makes the check-and-insert atomic across sessions — and it serialises the re-pin and
    the prune with it, which is a deadlock class rather than a rounding error.

    Held from a second session rather than raced from a thread, so the test fails in
    BOTH directions: without the lock the append returns immediately."""
    native = _key()
    _append(conn, native, b'{"v": 0}')

    other = psycopg.connect(_DB_URL, autocommit=False)
    try:
        with other.cursor() as cur:
            cur.execute(payloads._GROUP_LOCK_SQL, {
                "source": "idnes", "source_id_native": native, "page_kind": "detail"})
        with pytest.raises(psycopg.Error):
            _append(conn, native, b'{"v": 1}', statement_timeout_s=1)
    finally:
        other.rollback()
        other.close()

    # And once the other session lets go, the same append goes through — the lock is a
    # queue, not a refusal.
    assert _append(conn, native, b'{"v": 1}').inserted is True


@requires_db
def test_the_floor_reports_the_stored_body_from_the_append_statement_itself(
    conn: psycopg.Connection,
) -> None:
    """The floor refuses on nearly every fetch of a high-churn portal, so the path it
    takes is the hot one. It used to issue a SECOND statement to find out what the
    archive actually holds; `_APPEND_SQL`'s fallback arm returns that in the same
    statement — and the SQL-corpus test above pins the module's statement set, so the
    round trip cannot come back unnoticed. What is asserted here is the semantics that
    made the extra statement necessary: the ref describes the STORED body, never the
    discarded fetch."""
    native = _key()
    first = _append(conn, native, b'{"v": 0}', min_append_interval_days=7)
    payloads.reset_archive_stats()

    refused = _append(conn, native, b'{"v": 1}', min_append_interval_days=7)

    assert refused.suppressed is True and refused.inserted is False
    assert (refused.id, refused.payload_sha256) == (first.id, first.payload_sha256)
    assert payloads.archive_stats().suppressed == 1
    assert len(_rows(conn, native)) == 1
