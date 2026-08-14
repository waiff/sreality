"""Hermetic tests for the 200 x 3 confirmation probe (02 §2.3.2's gate protocol).

The probe is the one W2a component that talks to nine live portals, so what has to be
pinned is its POLITENESS and its BLAST RADIUS, both without a socket:

  * **Sequential, round-major, paced.** One request at a time, all keys in round 1
    before any key is fetched twice, and never two requests inside the minimum interval.
    Driven here on a fake clock — the protocol is arithmetic, not luck.
  * **Churn rows and nothing else.** No payload row, no `portal_raw_pages` row, no
    listing write; the sample read is a SELECT; and the counters land in the probe's OWN
    normalizer cohort so a cadence of minutes cannot contaminate the passive readout the
    storage gate is signed from.
  * **The portal's own client, never a bespoke fetch.** That is what carries the 429/403
    penalisation, `ListingGoneError`, the retry/backoff and the proxied egress.
  * **The body projection matches each portal's passive call site**, or the probe would
    confirm a measurement of something else.
"""

from __future__ import annotations

import ast
import contextlib
import json
import random
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from location_data.payload_norm import (
    MEASURED_VOLATILE_PROFILES,
    PAGE_KIND_DETAIL,
    probe_normalizer_version,
)
from scraper.portal import default_config
from scraper.portal_base import ListingGoneError
from scripts import location_payload_refetch_probe as probe
from tests.sql_corpus import first_keyword

_SOURCE_PATH = Path(probe.__file__)
_SOURCE = _SOURCE_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE, _SOURCE_PATH.name)
_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "location_payload_churn.yml"


class _Clock:
    """A fake monotonic clock that only ever moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def tick(self, seconds: float) -> None:
        self.now += seconds


def _pacer(clock: _Clock, interval: float = 1.0) -> probe.Pacer:
    return probe.Pacer(min_interval_s=interval, monotonic=clock.monotonic, sleep=clock.sleep)


def _keys(n: int, source: str = "bazos") -> list[probe.SampleKey]:
    return [
        probe.SampleKey(source=source, native_id=str(i), detail_ref=f"/x/{i}")
        for i in range(1, n + 1)
    ]


def _ok(body: bytes = b"<html>x</html>") -> probe.Fetched:
    return probe.Fetched(kind=probe.KIND_OK, body=body, content_type="text/html")


# ------------------------------------------------------- 1. the protocol's shape


def test_the_rounds_are_round_major_so_a_listing_is_never_hammered_twice_over() -> None:
    clock = _Clock()
    seen: list[str] = []

    counts = probe.probe_rounds(
        _keys(3), rounds=3,
        fetch=lambda key: (seen.append(key.native_id), _ok())[1],
        record=lambda key, result: True,
        pacer=_pacer(clock),
    )

    assert seen == ["1", "2", "3", "1", "2", "3", "1", "2", "3"]
    assert counts.fetches == 9
    assert counts.ok == 9
    assert counts.recorded == 9
    assert counts.rounds_completed == 3


def test_every_fetch_is_spaced_by_at_least_the_minimum_interval() -> None:
    """No network, no real clock: the spacing is the protocol, so it is asserted as
    arithmetic. The portal client's limiter paces too — this is the guarantee that
    survives whichever limiter the run was handed."""
    clock = _Clock()
    stamps: list[float] = []

    probe.probe_rounds(
        _keys(2), rounds=3,
        fetch=lambda key: (stamps.append(clock.monotonic()), _ok())[1],
        record=lambda key, result: True,
        pacer=_pacer(clock, interval=5.0),
    )

    assert len(stamps) == 6
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(gap >= 5.0 for gap in gaps), gaps
    assert clock.slept == [5.0] * 5


def test_a_fetch_that_took_longer_than_the_interval_is_not_slowed_further() -> None:
    """The interval is a floor on the gap, not a delay added to every request — a slow
    portal must not be paced twice for the same second."""
    clock = _Clock()
    pacer = _pacer(clock, interval=5.0)

    def _slow_fetch(key: probe.SampleKey) -> probe.Fetched:
        clock.tick(9.0)
        return _ok()

    probe.probe_rounds(
        _keys(2), rounds=2, fetch=_slow_fetch, record=lambda k, r: True, pacer=pacer,
    )
    assert clock.slept == []


def test_the_probe_is_single_threaded_by_construction() -> None:
    # A pool would defeat both the pacing above and the shared rate ledger's lease size.
    assert "ThreadPoolExecutor" not in _SOURCE
    assert "threading" not in _SOURCE


def test_a_gone_listing_leaves_the_sample_and_the_rest_carry_on() -> None:
    clock = _Clock()
    seen: list[str] = []

    def _fetch(key: probe.SampleKey) -> probe.Fetched:
        seen.append(key.native_id)
        return probe.Fetched(kind=probe.KIND_GONE) if key.native_id == "2" else _ok()

    counts = probe.probe_rounds(
        _keys(3), rounds=3, fetch=_fetch, record=lambda k, r: True, pacer=_pacer(clock),
    )

    assert seen == ["1", "2", "3", "1", "3", "1", "3"]
    assert counts.gone == 1
    assert counts.ok == 6
    assert counts.rounds_completed == 3


def test_a_transient_error_keeps_the_listing_in_the_sample() -> None:
    """A 502 is not an answer — dropping the key on it would quietly shrink the sample
    the rate is computed over."""
    clock = _Clock()
    calls: list[str] = []

    def _fetch(key: probe.SampleKey) -> probe.Fetched:
        calls.append(key.native_id)
        if key.native_id == "1" and calls.count("1") == 1:
            return probe.Fetched(kind=probe.KIND_ERROR, error="502 from portal")
        return _ok()

    counts = probe.probe_rounds(
        _keys(2), rounds=2, fetch=_fetch, record=lambda k, r: True, pacer=_pacer(clock),
    )
    assert calls == ["1", "2", "1", "2"]
    assert counts.errors == 1
    assert counts.ok == 3


def test_a_write_failure_is_counted_without_ending_the_pass() -> None:
    clock = _Clock()
    counts = probe.probe_rounds(
        _keys(2), rounds=2, fetch=lambda key: _ok(), record=lambda k, r: False,
        pacer=_pacer(clock),
    )
    assert counts.ok == 4
    assert counts.recorded == 0
    assert counts.write_errors == 4


def test_the_wall_clock_budget_stops_between_fetches_and_says_so() -> None:
    clock = _Clock()
    pacer = _pacer(clock, interval=5.0)
    counts = probe.probe_rounds(
        _keys(4), rounds=3, fetch=lambda key: _ok(), record=lambda k, r: True,
        pacer=pacer, deadline=clock.monotonic() + 11.0,
    )
    assert counts.stopped_early is True
    assert counts.fetches == 3
    # A round that was cut short is not reported as completed.
    assert counts.rounds_completed == 0


def test_a_budget_stop_leaves_whole_rounds_behind_it_when_it_lands_on_a_boundary() -> None:
    clock = _Clock()
    pacer = _pacer(clock, interval=5.0)
    counts = probe.probe_rounds(
        _keys(2), rounds=3, fetch=lambda key: _ok(), record=lambda k, r: True,
        pacer=pacer, deadline=clock.monotonic() + 6.0,
    )
    assert counts.rounds_completed == 1
    assert counts.stopped_early is True


# ------------------------------------------------ 2. churn rows, and nothing else


def test_the_only_statement_the_module_executes_is_the_sample_select() -> None:
    """Everything else goes through db.record_payload_churn, whose single statement is
    migration 402's upsert. No payload row, no portal_raw_pages row, no listing write."""
    executed = [
        node.args[0]
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("execute", "executemany")
    ]
    assert [n.id for n in executed if isinstance(n, ast.Name)] == ["_SAMPLE_LISTINGS_SQL"]
    assert len(executed) == 1
    assert first_keyword(probe._SAMPLE_LISTINGS_SQL) == "SELECT"


def test_the_module_names_no_write_target_of_its_own() -> None:
    for table in ("portal_raw_pages", "portal_raw_payloads", "listing_snapshots"):
        assert table not in probe._SAMPLE_LISTINGS_SQL
    assert not re.search(
        r"\b(insert|update|delete|truncate)\b", probe._SAMPLE_LISTINGS_SQL, re.I,
    )


def test_a_recorded_fetch_lands_in_the_probes_own_cohort() -> None:
    conn = _FakeConn()
    written = probe.record_fetch(
        conn,
        probe.SampleKey(source="bazos", native_id="77", detail_ref="/a/77"),
        probe.Fetched(kind=probe.KIND_OK, body=b"<html>a</html>", content_type="text/html"),
        cohort=probe_normalizer_version(),
    )
    assert written is True
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "portal_payload_churn" in sql
    assert params[0] == "bazos"
    assert params[1] == "77"
    assert params[2] == probe.PAGE_KIND
    assert params[3] == probe_normalizer_version()
    assert params[3].endswith("+probe"), "the passive cohort must stay untouched"


def test_the_probe_cohort_is_distinct_from_the_passive_one() -> None:
    from location_data.payload_norm import NORMALIZER_VERSION

    assert probe_normalizer_version() != NORMALIZER_VERSION
    assert probe_normalizer_version().startswith(NORMALIZER_VERSION)


def test_a_churn_write_failure_is_swallowed_and_reported_not_raised() -> None:
    written = probe.record_fetch(
        _ExplodingConn(),
        probe.SampleKey(source="bazos", native_id="9", detail_ref=None),
        probe.Fetched(kind=probe.KIND_OK, body=b"x", content_type="text/html"),
        cohort=probe_normalizer_version(),
    )
    assert written is False


def test_the_probe_does_not_ride_the_passive_flag() -> None:
    """`record_payload_churn_if_enabled` is gated on location_payload_shadow_hash — the
    passive lane's switch. A probe the operator dispatched must record either way."""
    called = {
        node.func.attr
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "record_payload_churn_if_enabled" not in called
    assert "record_payload_churn" in called


# ------------------------------------------------- 3. the portal's own client only


def test_every_portal_the_instrument_measures_has_a_probe_client() -> None:
    assert set(probe.PROBE_CLIENTS) == set(MEASURED_VOLATILE_PROFILES)
    # ...and it probes the SURFACE those profiles were measured on. Profiles are
    # keyed by (source, page_kind), so a probe pointed at another page_kind would
    # be reporting the residue of a profile the live path there does not apply.
    assert probe.PAGE_KIND == PAGE_KIND_DETAIL
    assert all(
        PAGE_KIND_DETAIL in surfaces for surfaces in MEASURED_VOLATILE_PROFILES.values()
    )


def test_the_module_never_opens_its_own_http_connection() -> None:
    # Not importing requests is what forces every fetch through BasePortalClient._request
    # — the 429/403 penalisation, the retry/backoff, ListingGoneError and the proxy.
    assert not re.search(r"^import requests|^from requests", _SOURCE, re.M)
    for verb in ("requests.get(", "requests.post(", "urlopen("):
        assert verb not in _SOURCE


def test_the_client_is_the_portals_own_and_is_handed_the_limiter(monkeypatch) -> None:
    built: dict[str, Any] = {}

    class _Client:
        def __init__(self, *, limiter: Any) -> None:
            built["limiter"] = limiter

    class _Module:
        BazosClient = _Client

    monkeypatch.setattr(probe.importlib, "import_module", lambda name: _Module)
    sentinel = object()
    client = probe.build_client("bazos", sentinel)
    assert isinstance(client, _Client)
    assert built["limiter"] is sentinel


def test_the_limiter_leases_the_small_probe_batch() -> None:
    """DEFAULT_LEASE_N would hold 20 shared slots a one-request-per-second probe never
    spends — starving the live walk it is supposed to run beside."""
    assert "build_rate_limiter(" in _SOURCE
    assert "lease_n=PROBE_LEASE_N" in _SOURCE
    assert probe.PROBE_LEASE_N < 20


def test_a_proxied_portal_is_skipped_when_the_proxy_is_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("SCRAPER_PROXY_URL", raising=False)
    assert probe.proxy_unavailable("mmreality") is True
    assert probe.proxy_unavailable("ceskereality") is True
    assert probe.proxy_unavailable("bazos") is False
    monkeypatch.setenv("SCRAPER_PROXY_URL", "http://proxy.example:8080")
    assert probe.proxy_unavailable("mmreality") is False


# --------------------------------------------------- 4. the body projection per portal


def test_an_html_portals_body_is_the_page_bytes_sniffed_like_the_archive_path() -> None:
    result = probe.fetch_body("bazos", _HtmlClient("<html> a </html>"), _keys(1)[0])
    assert result.kind == probe.KIND_OK
    assert result.body == b"<html> a </html>"
    assert result.content_type == "text/html"


def test_srealitys_body_is_the_unwrapped_estate_json_as_its_passive_site_hashes_it() -> None:
    estate = {"hash_id": 42, "name": "Byt 2+kk"}
    result = probe.fetch_body("sreality", _JsonClient(estate), _keys(1, "sreality")[0])
    assert result.content_type == "application/json"
    assert json.loads(result.body.decode("utf-8")) == estate


def test_bezrealitkys_body_carries_the_parsers_derived_image_urls() -> None:
    advert = {
        "id": "5551", "uri": "byt-praha", "offerType": "PRODEJ", "estateType": "BYT",
        "mainImage": {"url": "https://cdn.example/a.jpg"},
    }
    result = probe.fetch_body("bezrealitky", _JsonClient(advert), _keys(1, "bezrealitky")[0])
    body = json.loads(result.body.decode("utf-8"))
    assert body["id"] == "5551"
    assert "image_urls" in body, "the passive site hashes ScrapedListing.raw, not the advert"


def test_a_bezrealitky_parse_fault_degrades_to_the_bare_advert() -> None:
    body = probe._bezrealitky_raw({"not": "an advert"})
    assert body == {"not": "an advert"}


def test_a_gone_listing_and_a_broken_one_are_distinguished() -> None:
    gone = probe.fetch_body("bazos", _RaisingClient(ListingGoneError("u", 404)), _keys(1)[0])
    assert gone.kind == probe.KIND_GONE
    broken = probe.fetch_body("bazos", _RaisingClient(RuntimeError("boom")), _keys(1)[0])
    assert broken.kind == probe.KIND_ERROR
    assert "boom" in (broken.error or "")


# ------------------------------------------------------------------ 5. dry run


def test_a_dry_run_without_a_database_fetches_nothing_and_writes_nothing(monkeypatch) -> None:
    def _no_client(source: str, limiter: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("a dry run must not build a portal client")

    monkeypatch.setattr(probe, "build_client", _no_client)
    monkeypatch.setattr(probe, "portal_config", lambda conn, source: _FastConfig())
    counts = probe.probe_source(
        None, "bazos", listings=200, rounds=3, rate_per_s=1_000.0, dry_run=True,
        deadline=None, statement_timeout_s=30,
    )
    assert counts.fetches == counts.ok == 9  # 3 synthetic keys x 3 rounds
    assert counts.write_errors == 0


def test_a_dry_run_does_not_pace_because_it_makes_no_request(monkeypatch) -> None:
    intervals: list[float] = []
    real_pacer = probe.Pacer

    def _spy(*, min_interval_s: float, **kwargs: Any) -> probe.Pacer:
        intervals.append(min_interval_s)
        return real_pacer(min_interval_s=min_interval_s, **kwargs)

    monkeypatch.setattr(probe, "Pacer", _spy)
    monkeypatch.setattr(probe, "portal_config", lambda conn, source: _FastConfig())
    monkeypatch.setattr(probe, "build_rate_limiter", lambda *a, **k: None)
    probe.probe_source(
        None, "bazos", listings=200, rounds=1, rate_per_s=1.0, dry_run=True,
        deadline=None, statement_timeout_s=30,
    )
    assert intervals == [0.0]


def test_the_sample_size_and_rounds_are_the_protocols_numbers() -> None:
    assert probe.DEFAULT_LISTINGS == 200
    assert probe.DEFAULT_ROUNDS == 3


def test_the_configured_portal_rate_wins_whenever_it_is_politer(monkeypatch) -> None:
    seen: dict[str, float] = {}

    class _Limits:
        detail_rate = 0.25
        shared_rate_limiter = False

    class _Config:
        limits = _Limits()

    monkeypatch.setattr(probe, "portal_config", lambda conn, source: _Config())
    monkeypatch.setattr(
        probe, "build_rate_limiter",
        lambda source, rate, shared, lease_n=0: seen.setdefault("rate", rate),
    )
    monkeypatch.setattr(probe, "build_client", lambda source, limiter: _HtmlClient("<i>"))
    probe.probe_source(
        None, "bazos", listings=1, rounds=1, rate_per_s=1.0, dry_run=True,
        deadline=None, statement_timeout_s=30,
    )
    assert seen["rate"] == 0.25


# --------------------------------------------------------------- 6. the lane's rails


def test_the_lane_takes_a_lease_row_and_never_an_advisory_lock() -> None:
    # The transaction-mode pooler strands a session advisory lock; location_jobs is the
    # CAS row every location lane uses instead.
    assert "lease.held(" in _SOURCE
    assert "pg_advisory" not in _SOURCE
    assert probe.CONCURRENCY_GROUP == "location-payload-churn"
    assert probe.JOB_NAME == "location_payload_refetch_probe"


def test_a_dry_run_takes_no_lease_so_it_cannot_stamp_the_lane_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`lease.held` releases as 'ok' and stamps location_jobs.last_success_at, so a dry
    run that took the lease would make the location_jobs_stale monitor (migration 384)
    read a lane that fetched nothing as healthy — and `--dry-run` promises no writes."""
    taken: list[str] = []

    @contextlib.contextmanager
    def _held(conn: Any, job_name: str, **kwargs: Any) -> Any:
        taken.append(job_name)
        yield True

    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://unused/db")
    monkeypatch.setattr(probe.lease, "held", _held)
    monkeypatch.setattr(probe.db, "connect", lambda: contextlib.nullcontext(_FakeConn()))
    monkeypatch.setattr(probe, "run", lambda conn, sources, **kwargs: {})

    assert probe.main(["--dry-run", "--source", "bazos"]) == 0
    assert taken == []
    # ... and a real run still does take it.
    assert probe.main(["--source", "bazos"]) == 0
    assert taken == [probe.JOB_NAME]


def test_the_sample_read_is_bounded_by_a_transaction_local_timeout() -> None:
    assert "loader_db.bounded(conn, statement_timeout_s)" in _SOURCE
    assert probe.DEFAULT_STATEMENT_TIMEOUT_S > 0


def test_the_workflow_passes_its_inputs_as_a_bash_array_not_a_split_string() -> None:
    """`--source "bazos, idnes"` (a space after the comma, the natural typing) would
    word-split out of an unquoted "$ARGS" into `--source bazos,` plus a stray positional
    `idnes`, and argparse would reject the whole dispatch."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "ARGS=()" in text
    assert 'ARGS+=(--source "$SOURCE")' in text
    assert 'python -m scripts.location_payload_refetch_probe "${ARGS[@]}"' in text
    assert 'python -m scripts.location_payload_churn_report "${ARGS[@]}"' in text
    assert 'ARGS=""' not in text
    assert not re.search(r"python -m scripts\.\S+ \$ARGS", text)


def test_the_workflow_is_dispatch_only_and_cannot_fire_itself() -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML parses the bare `on:` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert workflow["concurrency"]["group"] == "location-batch"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    job = workflow["jobs"]["churn"]
    assert job["concurrency"]["group"] == probe.CONCURRENCY_GROUP
    assert job["concurrency"]["cancel-in-progress"] is False
    assert job["timeout-minutes"] == 55


def test_the_default_budget_finishes_inside_the_jobs_ceiling() -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    ceiling_s = workflow["jobs"]["churn"]["timeout-minutes"] * 60
    assert probe.DEFAULT_MAX_SECONDS < ceiling_s
    # ... and EVERY portal's protocol run fits inside that budget at the rate it will
    # actually be paced at — the politer of the lane's default and the portal's own
    # configured detail rate. bazos is the binding one (0.6 req/s -> ~17 min).
    fetches = probe.DEFAULT_LISTINGS * probe.DEFAULT_ROUNDS
    for source in probe.PROBE_CLIENTS:
        configured = default_config(source).limits.detail_rate
        rate = min(probe.PROBE_RATE_PER_S, configured) if configured > 0 else probe.PROBE_RATE_PER_S
        assert fetches / rate <= probe.DEFAULT_MAX_SECONDS, source


def test_an_unknown_source_is_rejected_before_a_single_request() -> None:
    assert probe.parse_sources("bazos,idnes") == ["bazos", "idnes"]
    assert sorted(probe.parse_sources("")) == sorted(probe.PROBE_CLIENTS)
    with pytest.raises(ValueError):
        probe.parse_sources("bazos,nosuchportal")


def test_a_blank_dispatch_shuffles_so_repeats_do_not_burn_on_the_same_portals() -> None:
    """A blank dispatch asks for nine portals — ~90 min of paced fetching against a
    45-minute budget, so it ALWAYS truncates. In insertion order the last three portals
    would then never be reached, however many times the operator repeats the dispatch."""
    orders = {tuple(probe.parse_sources("")) for _ in range(40)}
    assert len(orders) > 1, "a blank dispatch must not always walk the same order"
    firsts = {order[0] for order in orders}
    assert len(firsts) > 1
    # Deterministic under an injected generator, so nothing here is flaky by design.
    seeded = probe.parse_sources("", rng=random.Random(7))
    assert seeded == probe.parse_sources("", rng=random.Random(7))
    assert sorted(seeded) == sorted(probe.PROBE_CLIENTS)


def test_an_explicit_source_list_keeps_the_operators_order() -> None:
    assert probe.parse_sources("remax,bazos,idnes") == ["remax", "bazos", "idnes"]


# ---------------------------------------------------------------------- fakes


class _FastLimits:
    """A portal whose configured rate is not the politer one, so the test paces at the
    argument instead of sleeping through the real 2 req/s default."""

    detail_rate = 1_000.0
    shared_rate_limiter = False


class _FastConfig:
    limits = _FastLimits()


class _HtmlClient:
    def __init__(self, html: str) -> None:
        self._html = html

    def fetch_detail(self, ref: str) -> tuple[str, int]:
        return self._html, 200


class _JsonClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def get_detail(self, native_id: Any) -> dict[str, Any]:
        return self._payload


class _RaisingClient:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def fetch_detail(self, ref: str) -> tuple[str, int]:
        raise self._error


class _Cursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))


class _FakeConn:
    autocommit = True

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


class _ExplodingConn(_FakeConn):
    def cursor(self) -> _Cursor:
        raise RuntimeError("pooler dropped the connection")
