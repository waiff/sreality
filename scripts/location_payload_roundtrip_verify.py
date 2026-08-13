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

Sampling is by random id, not `ORDER BY random()` and not `TABLESAMPLE`. `ORDER BY random()`
would scan and detoast all 14 GB for a thousand rows. `TABLESAMPLE SYSTEM` picks whole
heap PAGES, and pages cluster by insert order, which clusters by portal — a page-sampled
"random 1,000" can be most of one portal and none of another, which is exactly the bias a
migration gate must not have. Random ids over [min, max] are index-served and uniform over
the id space, and nothing has ever been deleted from this table, so the id space is dense.

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
# Ids are dense (nothing is ever deleted from portal_raw_pages) but not gapless, so ask for
# more candidate ids than rows wanted and take the ones that exist.
OVERSAMPLE = 3
MAX_CANDIDATES = 20_000

TIMEOUT_ENV = "LOCATION_PAYLOAD_VERIFY_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 120

_BOUNDS_SQL = """
SELECT min(id), max(id), count(*) FROM portal_raw_pages
 WHERE (%(source)s::text IS NULL OR source = %(source)s)
"""

# Ids only, so the draw can be narrowed to exactly the wanted size BEFORE any 245 KB body
# is detoasted. NULL means "every row in scope", which is only reached when the archive is
# smaller than the sample.
_EXISTING_IDS_SQL = """
SELECT id FROM portal_raw_pages
 WHERE (%(ids)s::bigint[] IS NULL OR id = ANY (%(ids)s::bigint[]))
   AND (%(source)s::text IS NULL OR source = %(source)s)
"""

_SAMPLE_SQL = """
SELECT id, source, source_id_native, page_kind, convert_to(html, 'UTF8'), fetched_at
  FROM portal_raw_pages
 WHERE id = ANY (%(ids)s::bigint[])
 ORDER BY id
"""

# The group is bounded by the version cap, and the hash-match test is what picks the row
# this page should have become: ordering by it first means a group carrying later live-path
# versions still yields the backfilled body when one is there, and yields SOMETHING when it
# is not — so a genuine mismatch is reportable instead of indistinguishable from "no row at
# all". `prp_native` (source, source_id_native, page_kind, first_observed_at desc) serves
# the lookup.
_PAYLOAD_SQL = """
SELECT id, content_encoding, body, body_r2_key, byte_size,
       (body_sha256 = %(body_sha256)s) AS hash_matches, version_seq, first_observed_at
  FROM portal_raw_payloads
 WHERE source = %(source)s
   AND source_id_native = %(source_id_native)s
   AND page_kind = %(page_kind)s::location_page_kind
 ORDER BY (body_sha256 = %(body_sha256)s) DESC, version_seq, id
 LIMIT 1
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
    sampled: int = 0
    ok: int = 0
    missing: int = 0
    mismatch: int = 0
    unreadable: int = 0
    from_r2: int = 0
    bytes_compared: int = 0
    failures: list[Verdict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.sampled > 0 and self.ok == self.sampled

    def as_dict(self) -> dict[str, Any]:
        return {
            "sampled": self.sampled, "ok": self.ok, "missing": self.missing,
            "mismatch": self.mismatch, "unreadable": self.unreadable,
            "from_r2": self.from_r2, "bytes_compared": self.bytes_compared,
            "passed": self.passed,
            "failures": [
                {"page_id": v.page_id, "source": v.source,
                 "source_id_native": v.source_id_native, "page_kind": v.page_kind,
                 "status": v.status, "detail": v.detail}
                for v in self.failures
            ],
        }


def sample_ids(
    conn: psycopg.Connection, *, source: str | None, size: int, seed: int | None,
    statement_timeout: int,
) -> list[int]:
    """Exactly `size` ids that exist (fewer only when the archive holds fewer).

    Two steps on purpose. Drawing candidate ids over [min, max] and then narrowing to the
    ones that exist keeps the draw uniform over the ID SPACE; narrowing in a second
    id-only query keeps it uniform over the ROWS as well. Taking the first `size` of the
    candidates instead would sample the lowest ids — the oldest pages, and on a table
    written portal by portal, disproportionately the earliest portals.
    """
    rng = random.Random(seed)
    with loader_db.bounded(conn, statement_timeout) as cur:
        cur.execute(_BOUNDS_SQL, {"source": source})
        row = cur.fetchone()
    if not row or row[0] is None:
        return []
    lo, hi, total = int(row[0]), int(row[1]), int(row[2])

    candidates: list[int] | None = None
    if total > size:
        want = min(MAX_CANDIDATES, size * OVERSAMPLE)
        span = hi - lo + 1
        candidates = (list(range(lo, hi + 1)) if span <= want
                      else sorted(rng.sample(range(lo, hi + 1), want)))

    with loader_db.bounded(conn, statement_timeout) as cur:
        cur.execute(_EXISTING_IDS_SQL, {"ids": candidates, "source": source})
        existing = [int(r[0]) for r in cur.fetchall()]
    if len(existing) <= size:
        return sorted(existing)
    return sorted(rng.sample(existing, size))


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
    report = Report()
    ids = sample_ids(conn, source=source, size=size, seed=seed, statement_timeout=timeout)
    if not ids:
        return report

    with loader_db.bounded(conn, timeout) as cur:
        cur.execute(_SAMPLE_SQL, {"ids": ids})
        pages = cur.fetchall()

    for page_id, page_source, native, page_kind, raw_body, _fetched_at in pages:
        raw = bytes(raw_body)
        report.sampled += 1
        report.bytes_compared += len(raw)
        with loader_db.bounded(conn, timeout) as cur:
            cur.execute(_PAYLOAD_SQL, {
                "source": page_source, "source_id_native": native,
                "page_kind": page_kind, "body_sha256": hashlib.sha256(raw).digest(),
            })
            row = cur.fetchone()
        verdict = _compare(
            raw, row, store=store, page_id=int(page_id), source=page_source,
            native=native, page_kind=page_kind, report=report)
        if verdict.status == "ok":
            report.ok += 1
        else:
            report.failures.append(verdict)
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
    report: Report,
) -> Verdict:
    def verdict(status: str, detail: str = "") -> Verdict:
        return Verdict(page_id, source, native, page_kind, status, detail)

    if row is None:
        report.missing += 1
        return verdict("missing", "no portal_raw_payloads row for this page's key")

    payload_id, encoding, body, r2_key, byte_size, hash_matches, version_seq, first_at = row
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

    if decoded != raw:
        report.mismatch += 1
        # `hash_matches=False` with a row present means the store holds a DIFFERENT body
        # for this page — a later live-path version rather than a lossy copy — and
        # version_seq / first_observed_at are what tell those two apart.
        return verdict(
            "mismatch",
            f"payload {payload_id}: {len(raw)} source bytes vs {len(decoded)} decoded "
            f"(byte_size={byte_size}, hash_matches={bool(hash_matches)}, "
            f"version_seq={version_seq}, first_observed_at={first_at}, "
            f"first_diff={_first_diff(raw, decoded)})")
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
    print(f"{'sampled':>10}{'ok':>8}{'missing':>9}{'mismatch':>10}{'unreadable':>12}"
          f"{'from_r2':>9}{'MB':>10}")
    print(f"{report.sampled:>10}{report.ok:>8}{report.missing:>9}{report.mismatch:>10}"
          f"{report.unreadable:>12}{report.from_r2:>9}"
          f"{report.bytes_compared / 1e6:>10.1f}")
    if report.failures:
        print("\nfailures (first 25):")
        for v in report.failures[:25]:
            print(f"  page {v.page_id} {v.source}/{v.source_id_native} [{v.page_kind}] "
                  f"{v.status}: {v.detail}")
    print()
    if report.sampled == 0:
        print("NO ROWS SAMPLED — the archive is empty for this scope; nothing was verified.")
    elif report.passed:
        print(f"PASS — all {report.ok} sampled pages round-trip byte-for-byte "
              "(06 W2a gate (a)).")
    else:
        print(f"FAIL — {report.sampled - report.ok} of {report.sampled} sampled pages did "
              "not round-trip.")


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
