"""06 W2a gate (a): byte-for-byte round trip of a random `portal_raw_pages` sample.

The backfill (`location_data.payload_backfill`) copies 445,191 legacy pages into the
content-addressed payload store, gzipping each body on the way in. This script is the
evidence that nothing was lost doing it: draw a random sample, pull each page's bytes back
out of the store — inflating the gzip, and fetching from R2 when the body spilled — and
compare them against the source table's own bytes, byte for byte. Its printed report is
what the operator signs; it is not a test, and it is deliberately read-only.

`convert_to(html, 'UTF8')` on both sides, never a Python `str` comparison. `html` is a
`text` column, so psycopg would hand back a decoded string and comparing THAT would prove
only that two decoders agree. The archived artefact is bytes, the gate says "byte-for-byte",
and the backfill reads its source through this identical expression — so the two sides are
symmetric by construction rather than by two matching assumptions.

THE SOURCE IS MUTABLE AND THE COMPARISON KNOWS IT. `portal_raw_pages` is latest-wins: a
live drain overwrites `html` on every refetch, while the payload store's 7-day append
floor refuses BY DESIGN to chase that churn (`location_data/payloads.py`). So on a
churning portal the live source legitimately diverges from the stored copy for up to a
week, and a verifier that reads "differs from the live source" as "lossy migration"
cannot pass while any portal is being scraped — which is exactly how the first
full-corpus run (32090281321, 2026-08-18) failed 31/1000 with every failure on a
live-drain portal, refetched 5-13h after storage. The verdict on a byte difference is
therefore two-staged: first the decoded body is re-hashed against the row's own
write-time `body_sha256` — a disagreement THERE is real damage (the store no longer
holds what its writer hashed) and always fails — and only an internally-faithful body
whose source row was refetched AFTER the payload was stored is classed `stale_source`:
reported with its own counter and rows, never silently, but not a gate failure. A body
that differs while its source was never refetched fails as before; there is no benign
story for that. The re-hash also closes the post-write corruption gap for R2-spilled
bodies: content-addressed keys prove key⇒content at WRITE time, and this is the only
place the object is re-checked after it.

Sampling draws the scope's ID POOL and picks from it client-side. Not `TABLESAMPLE SYSTEM`:
that picks whole heap PAGES, and pages cluster by insert order, which clusters by portal —
a page-sampled "random 1,000" can be most of one portal and none of another, exactly the
bias a migration gate must not have. Not a random draw over `[min(id), max(id)]` either,
which is what this file did first and got wrong: with `--source` the scope's rows are
sparsely interleaved across a sequence shared by nine portals, so a fixed 3x oversample
returned a fraction of the rows asked for — and returned it silently, which on a
sign-off tool is the worst failure mode available. Reading the id column for the scope is
an index-only scan of a few hundred thousand bigints (the bodies are never touched), so
the pool is simply taken whole and `random.sample`d down: exact size, uniform over ROWS,
seed-reproducible, and incapable of coming up short. When the scope holds fewer rows than
requested the report says so in its own field and the printed summary leads with it.

READ-ONLY. This script issues no INSERT, UPDATE or DELETE against any table.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg

from location_data import loader_db, payloads
from scraper import db

LOG = logging.getLogger("scripts.location_payload_roundtrip_verify")

DEFAULT_SAMPLE = 1_000

# Bodies come back a chunk at a time, not a thousand at once: at 41-245 KB a page, one
# round trip for the whole sample would hold a quarter of a gigabyte of HTML resident for
# no benefit. Two statements per chunk (the pages, then their payload rows) is ~20 round
# trips for a 1,000-row sample instead of ~2,000.
CHUNK = 100

# A ceiling on the id pool, not on the sample. The table is 445k rows today and the pool is
# an index-only scan of bigints, so this is 10x headroom rather than a live constraint; if
# it ever binds, the draw is a prefix of the id space and the report says so out loud.
MAX_ID_POOL = 5_000_000

TIMEOUT_ENV = "LOCATION_PAYLOAD_VERIFY_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 120

# The scope's whole id column. `html` is never mentioned, so this reads the index, not the
# 14 GB heap.
_CANDIDATE_IDS_SQL = """
SELECT id FROM portal_raw_pages
 WHERE (%(source)s::text IS NULL OR source = %(source)s)
 ORDER BY id
 LIMIT %(max_ids)s
"""

_SAMPLE_SQL = """
SELECT id, source, source_id_native, page_kind, convert_to(html, 'UTF8'), fetched_at
  FROM portal_raw_pages
 WHERE id = ANY (%(ids)s::bigint[])
 ORDER BY id
"""

# One statement per chunk, not per page: a LATERAL carrying the per-group `LIMIT 1` gives
# the same row the per-row query did, in one round trip.
#
# The group is bounded by the version cap, and the hash-match test is what picks the row
# this page should have become: ordering by it first means a group carrying later live-path
# versions still yields the backfilled body when one is there, and yields SOMETHING when it
# is not — so a genuine mismatch is reportable instead of indistinguishable from "no row at
# all". `prp_native` (source, source_id_native, page_kind, first_observed_at desc) serves
# the lookup.
#
# LEFT JOIN ... ON true, not an inner join: a page with no payload row at all must come back
# as a NULL row rather than vanish from the result, or "never migrated" would silently
# shrink the sample instead of failing the gate.
_PAYLOADS_SQL = """
SELECT k.page_id, p.id, p.content_encoding, p.body, p.body_r2_key, p.byte_size,
       (p.body_sha256 = k.body_sha256) AS hash_matches, p.version_seq,
       p.first_observed_at, p.fetched_at, p.body_sha256
  FROM unnest(%(page_id)s::bigint[], %(source)s::text[], %(source_id_native)s::text[],
              %(page_kind)s::text[], %(body_sha256)s::bytea[])
    AS k(page_id, source, source_id_native, page_kind, body_sha256)
  LEFT JOIN LATERAL (
      SELECT pp.id, pp.content_encoding, pp.body, pp.body_r2_key, pp.byte_size,
             pp.body_sha256, pp.version_seq, pp.first_observed_at, pp.fetched_at
        FROM portal_raw_payloads pp
       WHERE pp.source = k.source
         AND pp.source_id_native = k.source_id_native
         AND pp.page_kind = k.page_kind::location_page_kind
       ORDER BY (pp.body_sha256 = k.body_sha256) DESC, pp.version_seq, pp.id
       LIMIT 1
  ) p ON true
"""


class BodyStore(Protocol):
    def download_bytes(self, key: str) -> bytes: ...


@dataclass
class Verdict:
    page_id: int
    source: str
    source_id_native: str
    page_kind: str
    status: str
    detail: str = ""


@dataclass
class Report:
    requested: int = 0
    sampled: int = 0
    pool: int = 0
    pool_truncated: bool = False
    ok: int = 0
    missing: int = 0
    mismatch: int = 0
    unreadable: int = 0
    stale_source: int = 0
    from_r2: int = 0
    bytes_compared: int = 0
    failures: list[Verdict] = field(default_factory=list)
    stale: list[Verdict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Stale-source rows do not fail the gate — the store is internally faithful and
        the divergence is the SOURCE moving after storage, which the append floor
        guarantees will happen on any churning portal. They are still reported in full:
        a PASS carrying stale rows says so in its own counter, never as a bare tick."""
        return (self.sampled > 0
                and self.ok + self.stale_source == self.sampled
                and self.missing == 0 and self.mismatch == 0 and self.unreadable == 0)

    @property
    def shortfall(self) -> int:
        """How many fewer pages were verified than asked for.

        Never swallowed by a bare PASS: with the pool-based draw this can only mean the
        scope holds fewer rows than requested, and an operator signing "1,000 pages
        round-trip" has to be able to see that it was in fact thirty-three.
        """
        return max(0, self.requested - self.sampled)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested, "sampled": self.sampled,
            "shortfall": self.shortfall, "pool": self.pool,
            "pool_truncated": self.pool_truncated,
            "ok": self.ok, "missing": self.missing,
            "mismatch": self.mismatch, "unreadable": self.unreadable,
            "stale_source": self.stale_source,
            "from_r2": self.from_r2, "bytes_compared": self.bytes_compared,
            "passed": self.passed,
            "failures": [
                {"page_id": v.page_id, "source": v.source,
                 "source_id_native": v.source_id_native, "page_kind": v.page_kind,
                 "status": v.status, "detail": v.detail}
                for v in self.failures
            ],
            "stale": [
                {"page_id": v.page_id, "source": v.source,
                 "source_id_native": v.source_id_native, "page_kind": v.page_kind,
                 "status": v.status, "detail": v.detail}
                for v in self.stale
            ],
        }


def sample_ids(
    conn: psycopg.Connection, *, source: str | None, size: int, seed: int | None,
    statement_timeout: int,
) -> tuple[list[int], int, bool]:
    """(ids, pool size, pool was truncated) — exactly `size` ids unless the scope has fewer.

    Uniform over ROWS, not over the id space: the two differ sharply under `--source`,
    where one portal's rows are sparsely interleaved across a sequence shared by nine.
    """
    rng = random.Random(seed)
    with loader_db.bounded(conn, statement_timeout) as cur:
        cur.execute(_CANDIDATE_IDS_SQL, {"source": source, "max_ids": MAX_ID_POOL})
        pool = [int(r[0]) for r in cur.fetchall()]
    truncated = len(pool) >= MAX_ID_POOL
    if len(pool) <= size:
        return pool, len(pool), truncated
    return sorted(rng.sample(pool, size)), len(pool), truncated


def verify(
    conn: psycopg.Connection,
    *,
    source: str | None = None,
    size: int = DEFAULT_SAMPLE,
    seed: int | None = None,
    store: BodyStore | None = None,
    statement_timeout: int | None = None,
) -> Report:
    timeout = statement_timeout if statement_timeout is not None else (
        loader_db.env_timeout_s(TIMEOUT_ENV, DEFAULT_TIMEOUT_S))
    report = Report(requested=size)
    ids, pool, truncated = sample_ids(
        conn, source=source, size=size, seed=seed, statement_timeout=timeout)
    report.pool, report.pool_truncated = pool, truncated
    if truncated:
        LOG.warning("VERIFY id pool hit the %d ceiling; the draw is a PREFIX of the id "
                    "space, not a uniform sample of it", MAX_ID_POOL)
    if not ids:
        return report

    for start in range(0, len(ids), CHUNK):
        chunk = ids[start:start + CHUNK]
        with loader_db.bounded(conn, timeout) as cur:
            cur.execute(_SAMPLE_SQL, {"ids": chunk})
            pages = cur.fetchall()
        if not pages:
            continue

        bodies: dict[int, bytes] = {}
        keys: dict[str, list[Any]] = {
            "page_id": [], "source": [], "source_id_native": [], "page_kind": [],
            "body_sha256": [],
        }
        for page_id, page_source, native, page_kind, raw_body, _fetched_at in pages:
            raw = bytes(raw_body)
            bodies[int(page_id)] = raw
            keys["page_id"].append(int(page_id))
            keys["source"].append(page_source)
            keys["source_id_native"].append(native)
            keys["page_kind"].append(page_kind)
            keys["body_sha256"].append(hashlib.sha256(raw).digest())

        with loader_db.bounded(conn, timeout) as cur:
            cur.execute(_PAYLOADS_SQL, keys)
            found = {int(r[0]): r[1:] for r in cur.fetchall()}

        for page_id, page_source, native, page_kind, _raw_body, page_fetched_at in pages:
            raw = bodies[int(page_id)]
            report.sampled += 1
            report.bytes_compared += len(raw)
            row = found.get(int(page_id))
            # A LATERAL miss comes back as an all-NULL right side; `id IS NULL` is what
            # distinguishes it from a real row.
            if row is not None and row[0] is None:
                row = None
            verdict = _compare(
                raw, row, store=store, page_id=int(page_id), source=page_source,
                native=native, page_kind=page_kind, page_fetched_at=page_fetched_at,
                report=report)
            if verdict.status == "ok":
                report.ok += 1
            elif verdict.status == "stale_source":
                report.stale.append(verdict)
            else:
                report.failures.append(verdict)

    if report.shortfall:
        LOG.warning("VERIFY drew only %d of the %d pages requested — the scope holds %d "
                    "rows. The gate covers what was verified, not what was asked for.",
                    report.sampled, report.requested, report.pool)
    return report


def _compare(
    raw: bytes,
    row: tuple[Any, ...] | None,
    *,
    store: BodyStore | None,
    page_id: int,
    source: str,
    native: str,
    page_kind: str,
    page_fetched_at: Any,
    report: Report,
) -> Verdict:
    def verdict(status: str, detail: str = "") -> Verdict:
        return Verdict(page_id, source, native, page_kind, status, detail)

    if row is None:
        report.missing += 1
        return verdict("missing", "no portal_raw_payloads row for this page's key")

    (payload_id, encoding, body, r2_key, byte_size, hash_matches, version_seq,
     first_at, payload_fetched_at, body_sha256) = row
    stored: bytes
    if body is not None:
        stored = bytes(body)
    elif r2_key:
        if store is None:
            report.unreadable += 1
            return verdict("unreadable",
                           f"payload {payload_id} spilled to R2 ({r2_key}) and no object "
                           "store is configured")
        try:
            stored = store.download_bytes(r2_key)
        except Exception as exc:  # noqa: BLE001 - one unreadable body is a finding, not a crash
            report.unreadable += 1
            return verdict("unreadable", f"R2 read of {r2_key} failed: {exc}")
        report.from_r2 += 1
    else:
        # prp_body_present should make this unreachable; report it rather than trust it.
        report.unreadable += 1
        return verdict("unreadable", f"payload {payload_id} has neither body nor R2 key")

    try:
        decoded = payloads.decode_body(stored, str(encoding))
    except Exception as exc:  # noqa: BLE001 - a corrupt member is exactly what this looks for
        report.unreadable += 1
        return verdict("unreadable", f"payload {payload_id} would not decode: {exc}")

    # The store's own write-time hash is the fidelity oracle, not the live source: a body
    # whose decoded bytes no longer hash to its row's `body_sha256` is damaged no matter
    # what the source says, and for an R2-spilled body this is the only post-write check
    # the object ever gets (the content-addressed key proved key⇒content at write time).
    internally_faithful = (body_sha256 is not None
                           and hashlib.sha256(decoded).digest() == bytes(body_sha256))

    if decoded != raw:
        if not internally_faithful:
            report.mismatch += 1
            return verdict(
                "mismatch",
                f"payload {payload_id}: decoded body does not hash to its own "
                f"body_sha256 — store-side damage (byte_size={byte_size}, "
                f"version_seq={version_seq}, first_observed_at={first_at})")
        source_moved = (page_fetched_at is not None and payload_fetched_at is not None
                        and page_fetched_at > payload_fetched_at)
        if source_moved:
            # The source row was refetched after this payload was stored, and the append
            # floor (location_data/payloads.py) deliberately does not chase refetch churn
            # — the live bytes drifting off the stored copy is the designed behaviour,
            # not a lossy migration. Reported, never failed.
            report.stale_source += 1
            return verdict(
                "stale_source",
                f"payload {payload_id}: source refetched at {page_fetched_at}, "
                f"{len(raw)} live bytes vs {len(decoded)} stored "
                f"(stored fetched_at={payload_fetched_at}, version_seq={version_seq}, "
                f"first_diff={_first_diff(raw, decoded)})")
        report.mismatch += 1
        return verdict(
            "mismatch",
            f"payload {payload_id}: {len(raw)} source bytes vs {len(decoded)} decoded, "
            f"and the source was never refetched after storage — no benign story "
            f"(byte_size={byte_size}, hash_matches={bool(hash_matches)}, "
            f"version_seq={version_seq}, first_observed_at={first_at}, "
            f"source fetched_at={page_fetched_at}, "
            f"stored fetched_at={payload_fetched_at}, "
            f"first_diff={_first_diff(raw, decoded)})")
    if not internally_faithful:
        report.mismatch += 1
        return verdict("mismatch",
                       f"payload {payload_id}: body round-trips against the source but "
                       f"does not hash to its own body_sha256 — the row mislabels the "
                       f"body it holds")
    if byte_size is not None and int(byte_size) != len(raw):
        report.mismatch += 1
        return verdict("mismatch",
                       f"payload {payload_id}: body round-trips but byte_size={byte_size} "
                       f"disagrees with the {len(raw)} bytes it holds")
    return verdict("ok")


def _first_diff(a: bytes, b: bytes) -> int:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))


def _open_store() -> BodyStore | None:
    from scraper import image_storage

    if not image_storage.is_configured():
        return None
    return image_storage.R2Client.from_env(max_pool_connections=4)


def _print(report: Report) -> None:
    if report.shortfall:
        # Above the table, not buried in it: a PASS on 33 pages and a PASS on 1,000 look
        # identical everywhere else, and only one of them is the gate the operator meant.
        print(f"!! SHORT SAMPLE — verified {report.sampled} of the {report.requested} "
              f"pages requested ({report.shortfall} short); the scope holds "
              f"{report.pool} rows.\n")
    if report.pool_truncated:
        print(f"!! ID POOL TRUNCATED at {MAX_ID_POOL} — the draw is a prefix of the id "
              f"space, not a uniform sample.\n")
    print(f"{'requested':>11}{'sampled':>9}{'ok':>8}{'missing':>9}{'mismatch':>10}"
          f"{'unreadable':>12}{'stale_src':>11}{'from_r2':>9}{'MB':>10}")
    print(f"{report.requested:>11}{report.sampled:>9}{report.ok:>8}{report.missing:>9}"
          f"{report.mismatch:>10}{report.unreadable:>12}{report.stale_source:>11}"
          f"{report.from_r2:>9}{report.bytes_compared / 1e6:>10.1f}")
    if report.failures:
        print("\nfailures (first 25):")
        for v in report.failures[:25]:
            print(f"  page {v.page_id} {v.source}/{v.source_id_native} [{v.page_kind}] "
                  f"{v.status}: {v.detail}")
    if report.stale:
        print(f"\nstale-source (informational, {report.stale_source} total — the source "
              "row was refetched after storage and the append floor by design does not "
              "chase refetch churn; first 10):")
        for v in report.stale[:10]:
            print(f"  page {v.page_id} {v.source}/{v.source_id_native} [{v.page_kind}] "
                  f"{v.detail}")
    print()
    if report.sampled == 0:
        print("NO ROWS SAMPLED — the archive is empty for this scope; nothing was verified.")
    elif report.passed:
        if report.stale_source:
            scope = f"{report.ok} of {report.sampled}"
        elif report.shortfall:
            scope = f"{report.ok} of the {report.requested} requested"
        else:
            scope = f"all {report.ok}"
        stale_note = (f" ({report.stale_source} stale-source rows reported above, "
                      "not failures)" if report.stale_source else "")
        print(f"PASS — {scope} sampled pages round-trip byte-for-byte "
              f"(06 W2a gate (a)){stale_note}.")
    else:
        print(f"FAIL — {report.missing + report.mismatch + report.unreadable} of "
              f"{report.sampled} sampled pages did not round-trip.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=DEFAULT_SAMPLE)
    parser.add_argument("--source", default=None)
    parser.add_argument("--seed", type=int, default=None,
                        help="reproduce an earlier sample")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    with db.connect() as conn:
        report = verify(conn, source=args.source, size=args.size, seed=args.seed,
                        store=_open_store())
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        _print(report)
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
