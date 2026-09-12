"""`extract_pages` — the page half's extraction, across processes.

The pool is a PURE ACCELERATOR: same claims, same order, same per-body isolation as the
main-thread loop it replaced. So every test here is a comparison against that loop rather
than an assertion about the pool's insides — what matters is that a body cannot tell which
side of a process boundary it was extracted on.

The three ways one listing's page can fail (`mine_bodies`' docstring) survive the move
because a worker's exception comes back as a VALUE in that body's slot. The fourth failure
the pool adds — the pool itself dying — is not a failed batch either: the bodies it had not
finished are re-extracted on the main thread.
"""

from __future__ import annotations

from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from location_data import page_readers
from location_data.claims_common import Entry, IntakeRefused, IntakeResult, ListingRow
from location_data.html_scope import ScopeRegister
from tests.location_data.claim_intake_fixtures import entries_for

FETCHED_AT = datetime(2026, 9, 12, 4, 30, tzinfo=UTC)
ROOT = Path(__file__).resolve().parent.parent.parent
REMAX_BODY = (ROOT / "tests" / "fixtures" / "location_w2" / "remax_detail.html").read_bytes()


def _entry(source: str, *, blur_evidence: str = "none") -> Entry:
    return Entry(
        id=7001, source=source, contract_id=1, contract_version=2,
        entry_id=f"{source[:2]}.det.street", surface="html_selector", page_kind="detail",
        locator={"reader": "html_text", "css": "#subject"}, claim_type="street_name",
        extraction_method="html_selector_parse", subject_scope={}, transform=(),
        precision_map={}, default_blur_evidence=blur_evidence,
        default_licence_class="portal", guards=())


def _task(index: int, *, source: str = "remax",
          body: bytes | None = None) -> tuple[ListingRow, page_readers.ArchivedPayload]:
    row = ListingRow(
        listing_id=index, source=source, source_id_native=str(400000 + index),
        raw_json={}, lat=None, lon=None, observed_at=FETCHED_AT, in_mapy_inventory=False)
    payload = page_readers.ArchivedPayload(
        id=9000 + index, source=source, source_id_native=row.source_id_native,
        page_kind="detail", payload_sha256=f"{index:064d}", first_observed_at=FETCHED_AT,
        body=body if body is not None else
        f"<html><body><div id='subject'>Krymska {index}</div></body></html>".encode())
    return row, payload


def _corpus(count: int, **kwargs: Any) -> list[tuple[ListingRow, page_readers.ArchivedPayload]]:
    return [_task(index, **kwargs) for index in range(count)]


REGISTERS = {source: ScopeRegister.from_zones(source, ())
             for source in ("remax", "idnes")}
ENTRIES = {"remax": [_entry("remax")], "idnes": [_entry("idnes")]}


@pytest.fixture
def pooled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two workers and a floor of one body, so the pooled path is the one under test.

    `LOCATION_INTAKE_WORKERS` rather than a patched `extraction_workers`: a CI runner that
    reports a single CPU would otherwise take the main-thread path and every assertion
    below would pass without a process ever starting."""
    monkeypatch.setenv(page_readers.EXTRACTION_WORKERS_ENV, "2")
    monkeypatch.setattr(page_readers, "PARALLEL_MIN_BODIES", 1)


def _serial(tasks: list[tuple[ListingRow, page_readers.ArchivedPayload]],
            entries: dict[str, list[Entry]] | None = None,
            ) -> list[IntakeResult | Exception]:
    return page_readers._extract_serially(
        tasks, entries_by_source=entries or ENTRIES, registers=REGISTERS,
        max_value_bytes=2 * 1024 * 1024)


def _pages(tasks: list[tuple[ListingRow, page_readers.ArchivedPayload]],
           entries: dict[str, list[Entry]] | None = None,
           ) -> list[IntakeResult | Exception]:
    return page_readers.extract_pages(
        tasks, entries_by_source=entries or ENTRIES, registers=REGISTERS,
        max_value_bytes=2 * 1024 * 1024)


def _comparable(outcomes: list[IntakeResult | Exception]) -> list[Any]:
    """Two exceptions with the same message are never `==`, and one that crossed a process
    boundary is a different object again — so an exception compares as (type, message)."""
    return [outcome if isinstance(outcome, IntakeResult) else (type(outcome), str(outcome))
            for outcome in outcomes]


# ------------------------------------------------------------------ same answers

def test_the_pool_returns_the_main_thread_s_claims_in_the_main_thread_s_order(
    pooled: None,
) -> None:
    """THE property the whole change rests on. Results are collected by submission order,
    not completion order, so a body's claims land where a reader of the log — or the stamp
    list built beside them — expects them."""
    tasks = _corpus(40)

    assert _pages(tasks) == _serial(tasks)
    values = [outcome.claims[0].value_text for outcome in _pages(tasks)]
    assert values == [f"Krymska {index}" for index in range(40)]


def test_a_real_portal_body_reads_the_same_across_the_process_boundary(
    pooled: None,
) -> None:
    """The synthetic bodies above pin the ORDER; this pins the CONTENT. remax's shipped
    contract, its exclusion-zone register and a captured detail page — everything a worker
    has to carry across a pickle — against the same page extracted here."""
    entries = {"remax": entries_for("remax")}
    registers = {"remax": ScopeRegister.from_zones("remax", ())}
    tasks = _corpus(16, body=REMAX_BODY)

    pooled_outcomes = page_readers.extract_pages(
        tasks, entries_by_source=entries, registers=registers,
        max_value_bytes=2 * 1024 * 1024)
    serial_outcomes = page_readers._extract_serially(
        tasks, entries_by_source=entries, registers=registers,
        max_value_bytes=2 * 1024 * 1024)

    assert pooled_outcomes == serial_outcomes
    assert pooled_outcomes[0].claims, "the fixture must exercise at least one reader"


# ------------------------------------------------------------------ isolation

def test_a_refusal_in_a_worker_costs_one_body_and_is_returned_not_raised(
    pooled: None,
) -> None:
    """B2's regression, on the far side of a process boundary. `blur_evidence='detected'`
    is a class this lane may not emit (06 §6.6 rule 7), so `assert_stampable` refuses every
    idnes body here — and the remax bodies interleaved with them still come back with their
    claims, in their own slots, for `mine_bodies` to count and commit."""
    entries = {"remax": [_entry("remax")], "idnes": [_entry("idnes", blur_evidence="detected")]}
    tasks = [_task(index, source="idnes" if index % 2 else "remax") for index in range(20)]

    outcomes = _pages(tasks, entries)

    assert _comparable(outcomes) == _comparable(_serial(tasks, entries))
    for index, outcome in enumerate(outcomes):
        if index % 2:
            assert isinstance(outcome, IntakeRefused), index
        else:
            assert isinstance(outcome, IntakeResult) and outcome.claims, index


# ------------------------------------------------------------------ the pool's own rails

def test_a_batch_below_the_floor_never_starts_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """A --limit run, a drain's last batch, and every test that monkeypatches a reader: the
    handful of bodies they carry is extracted here, where an in-process patch still holds
    and a fresh interpreter per worker would cost more than the parse it parallelises."""
    monkeypatch.setenv(page_readers.EXTRACTION_WORKERS_ENV, "4")

    def _never(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a pool was started for a batch below the floor")

    monkeypatch.setattr(page_readers, "ProcessPoolExecutor", _never)
    tasks = _corpus(page_readers.PARALLEL_MIN_BODIES - 1)

    assert _pages(tasks) == _serial(tasks)


def test_one_worker_is_the_main_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runner that reports one CPU pays no IPC to use it."""
    monkeypatch.setenv(page_readers.EXTRACTION_WORKERS_ENV, "1")
    monkeypatch.setattr(page_readers, "PARALLEL_MIN_BODIES", 1)

    def _never(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a pool was started for a single worker")

    monkeypatch.setattr(page_readers, "ProcessPoolExecutor", _never)
    tasks = _corpus(20)

    assert _pages(tasks) == _serial(tasks)


def test_a_pool_that_breaks_mid_batch_finishes_the_rest_on_the_main_thread(
    pooled: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The OOM killer takes a worker and every pending future raises — including the ones
    whose bodies were fine. A batch is one transaction carrying nine portals' payload
    claims, so it must not be the pool's death that fails it: the two bodies already
    returned are kept and the rest are re-extracted by the code the lane ran before it had
    a pool."""
    class _HalfBrokenPool:
        def __init__(self, **_kwargs: Any) -> None:
            self.submitted = 0

        def __enter__(self) -> _HalfBrokenPool:
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

        def submit(self, fn: Any, task: Any) -> Future[IntakeResult]:
            future: Future[IntakeResult] = Future()
            self.submitted += 1
            if self.submitted > 2:
                future.set_exception(BrokenProcessPool("A process in the pool died"))
            else:
                future.set_result(page_readers._extract_serially(
                    [task], entries_by_source=ENTRIES, registers=REGISTERS,
                    max_value_bytes=2 * 1024 * 1024)[0])
            return future

    monkeypatch.setattr(page_readers, "ProcessPoolExecutor", _HalfBrokenPool)
    tasks = _corpus(10)

    outcomes = _pages(tasks)

    assert outcomes == _serial(tasks)
    assert all(isinstance(outcome, IntakeResult) for outcome in outcomes)
    assert "the extraction pool broke after 2/10 bodies" in caplog.text
