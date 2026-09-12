"""bodies/s for the page half's extraction, serial vs pooled, with no database.

THE LANE'S MEASUREMENT IS ITS LOG (`INTAKE bodies-first batch ... N bodies/s`) — this is
how the same number is taken on a machine that is not the runner, and on the runner without
spending the bucket. It replicates the COMMITTED fixture bodies into a corpus and drives
`page_readers.extract_pages`, the lane's own entry point, so what it times is what ships.

    python3 -m scripts.bench_page_extraction --bodies 300 --workers 1,2,4

Read the absolute rate as a floor, not as the lane's: the fixture bodies are anonymised
captures (~57 KB) and the corpus is one page parsed many times, while production bodies are
41-245 KB of nine portals' markup. The RATIO between the two rows is what transfers.
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from location_data import contracts, page_readers
from location_data.claims_common import Entry, IntakeResult, ListingRow
from location_data.html_scope import ScopeRegister

ROOT = Path(__file__).resolve().parent.parent
BODY_DIRS = ("tests/fixtures/portal_html", "tests/fixtures/location_w2",
             "tests/fixtures/location_w2a_refetch")
FETCHED_AT = datetime(2026, 9, 12, 6, 0, tzinfo=UTC)
MAX_VALUE_BYTES = 2 * 1024 * 1024


def _entries(contract: contracts.PortalContract) -> list[Entry]:
    """The git contract in the shape the lane reads out of `portal_contract_entries`."""
    return [
        Entry(id=1000 + index, source=contract.source, contract_id=1,
              contract_version=contract.version, entry_id=entry.entry_id,
              surface=entry.surface, page_kind=entry.page_kind, locator=entry.locator,
              claim_type=entry.claim_type, extraction_method=entry.extraction_method,
              subject_scope=entry.subject_scope, transform=tuple(entry.transform),
              precision_map=entry.precision_map,
              default_blur_evidence=entry.default_blur_evidence,
              default_licence_class=entry.default_licence_class,
              guards=tuple(entry.guards))
        for index, entry in enumerate(contract.entries)
    ]


def corpus(bodies: int) -> tuple[list[tuple[ListingRow, page_readers.ArchivedPayload]],
                                 dict[str, list[Entry]], dict[str, ScopeRegister]]:
    by_source = {c.source: c for c in contracts.load_all()}
    entries = {s: _entries(c) for s, c in by_source.items()}
    registers = {s: ScopeRegister.from_zones(s, c.exclusion_zones)
                 for s, c in by_source.items()}
    seeds = [
        (path.name.split("_")[0], path.read_bytes())
        for directory in BODY_DIRS
        for path in sorted((ROOT / directory).glob("*.html"))
        if path.name.split("_")[0] in by_source
        and page_readers.page_entries(entries[path.name.split("_")[0]], "detail")
    ]
    if not seeds:
        raise SystemExit("no fixture body matches a page-capable contract")
    print(f"seed bodies {len(seeds)} from {len({s for s, _ in seeds})} portals, "
          f"mean {sum(len(b) for _, b in seeds) / len(seeds) / 1024:.0f} KiB")
    tasks = []
    for index in range(bodies):
        source, body = seeds[index % len(seeds)]
        row = ListingRow(listing_id=index, source=source,
                         source_id_native=str(100000 + index), raw_json={}, lat=None,
                         lon=None, observed_at=FETCHED_AT)
        tasks.append((row, page_readers.ArchivedPayload(
            id=index, source=source, source_id_native=row.source_id_native,
            page_kind="detail", payload_sha256=f"{index:064d}",
            first_observed_at=FETCHED_AT, body=body)))
    return tasks, entries, registers


def measure(tasks, entries, registers, workers: int) -> tuple[float, int]:
    os.environ[page_readers.EXTRACTION_WORKERS_ENV] = str(workers)
    started = time.monotonic()
    outcomes = page_readers.extract_pages(
        tasks, entries_by_source=entries, registers=registers,
        max_value_bytes=MAX_VALUE_BYTES)
    elapsed = time.monotonic() - started
    claims = sum(len(o.claims) for o in outcomes if isinstance(o, IntakeResult))
    return elapsed, claims


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bodies", type=int, default=300)
    parser.add_argument("--workers", default=f"1,2,{os.cpu_count() or 1}",
                        help="comma-separated pool widths; 1 is the main-thread baseline")
    args = parser.parse_args()

    tasks, entries, registers = corpus(args.bodies)
    print(f"os.cpu_count()={os.cpu_count()} bodies={len(tasks)}")
    # The forkserver is started ONCE per process and every later pool forks from that, so a
    # run's second batch onwards never pays for it. One throwaway pool puts the widths below
    # in the steady state the lane actually spends its budget in.
    measure(tasks[:page_readers.PARALLEL_MIN_BODIES], entries, registers, 2)
    baseline: float | None = None
    expected: int | None = None
    for workers in [int(w) for w in args.workers.split(",")]:
        elapsed, claims = measure(tasks, entries, registers, workers)
        baseline = baseline if baseline is not None else elapsed
        # A pooled run that mined fewer claims than the serial one is not a faster lane.
        if expected is None:
            expected = claims
        elif claims != expected:
            raise SystemExit(f"workers={workers} produced {claims} claims, not {expected}")
        print(f"workers={workers:<3d} {len(tasks) / elapsed:8.1f} bodies/s  "
              f"{elapsed:6.2f}s  claims={claims}  speedup={baseline / elapsed:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
