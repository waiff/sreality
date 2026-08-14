"""Append-on-change writer for the content-addressed payload archive (02 §2.3.2 P1/P4).

`portal_raw_payloads` (migration 382, columns completed by 403) is the store
`location_claims.payload_id` / `payload_sha256` resolve against: without it, a mined
span points into "the page" and stops being verifiable the moment that page is
re-fetched, and `01 §4.2`'s `loc_claim_evidence_payload` CHECK is unsatisfiable in
practice. This module is the only sanctioned way to put a body in it.

Three properties define the write, and each one is load-bearing:

  * **Identity is the NORMALISED hash.** `payload_sha256` is taken over the body with
    the source's volatile paths stripped and key order / whitespace canonicalised
    (`payload_norm.normalise`, reused verbatim — the same function the W2a-0 churn
    instrument is measuring live). `body_sha256` carries the raw bytes' hash for
    forensics and is never the uniqueness key. This is the difference between an
    archive bounded by real content change and one that appends a row every time a
    CSRF token rerolls.
  * **An unchanged refetch appends nothing.** The INSERT collides on
    `(source, source_id_native, page_kind, payload_sha256)` and only bumps
    `last_observed_at`; a changed body appends a new `version_seq`. That pair is
    `06` W2a gate (b), tested in both directions.
  * **Retention runs in the same transaction as the append** (`02 §2.3.2 P4`), not in
    a job that may never be scheduled: re-pin first (first version, latest version,
    any body a claim references, and any body a disputed claim points at), then
    delete unpinned rows beyond the version cap — ranked newest-first, with
    unsuccessful fetches behind successful ones so a portal outage evicts itself
    rather than the listing's real history. Growth is bounded by row count rather
    than by operator diligence.

NOT WIRED. Nothing in the scrape calls this yet — W2a-2 adds the dual-write at
`scraper.db.upsert_portal_raw_page` behind its own flag, and enabling it is gated on
the churn sign-off. Shipping the library first keeps that PR to one chokepoint edit
and lets the write path be constraint-tested before it touches live ingest.

`portal_raw_pages` — the existing latest-wins staging table — is NOT this store and is
never written or deleted here; it is the migration source W2a-4 reads.
"""

from __future__ import annotations

import gzip
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple, Protocol

import psycopg

from location_data import loader_db
from location_data.payload_norm import (
    VolatileProfile,
    normalise,
    normalizer_version_for,
    volatile_profile,
)

LOG = logging.getLogger("location_data.payloads")

# One bounded transaction per append (INSERT + re-pin + prune). Short on purpose:
# this runs inside the drain's batch write, where a stuck statement stalls a lane
# rather than one listing.
WRITE_TIMEOUT_ENV = "LOCATION_PAYLOAD_WRITE_TIMEOUT_S"
DEFAULT_WRITE_TIMEOUT_S = 60

# `persistence.version_cap` (02 §2.3.2 P4) until W2a-3b puts it in the contracts.
VERSION_CAP_ENV = "LOCATION_PAYLOAD_VERSION_CAP"
DEFAULT_VERSION_CAP = 20

# Below this, gzip's ~20-byte header and the CPU cost outweigh the saving, and TOAST
# already compresses inline bytea anyway. Above it the portals' bodies (41-245 KB of
# HTML) compress 5-10x, which is the whole storage projection.
GZIP_MIN_BYTES_ENV = "LOCATION_PAYLOAD_GZIP_MIN_BYTES"
DEFAULT_GZIP_MIN_BYTES = 4096

# P4.3: "if genuine cold storage is wanted, it is R2". A compressed body over this
# size goes to the bucket and Postgres keeps only the key. mmreality's 245 KB pages
# gzip to ~35 KB, so at this default nothing spills today — the threshold exists so
# one pathological portal cannot bloat the table, not as a routine path.
R2_THRESHOLD_ENV = "LOCATION_PAYLOAD_R2_THRESHOLD_BYTES"
DEFAULT_R2_THRESHOLD_BYTES = 262_144

R2_PREFIX = "payloads"

# The key is derived from the hash of the bytes the object HOLDS — `body_sha256`, the
# raw body, never `payload_sha256` — so no listing is ever needed to find a body and no
# list_objects is ever needed to enumerate one. Keying on the normalised hash instead
# would hand one key to two rows whose normalised bodies coincide while their raw bytes
# differ (the same interstitial page under two listings, once the per-request nonce is
# stripped): the second row's own bytes would never be uploaded and its `body_sha256`
# would not match the object it points at. `source` is the only free-form component,
# and it comes from our own portal registry — validated anyway, because a key built
# from unvalidated input is how a path escape gets written (the images lane's _KEY_RE
# is the same boundary).
_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_CONTENT_ENCODINGS = ("identity", "gzip")


class PayloadError(RuntimeError):
    """The body could not be archived; the caller decides whether that is fatal."""


class EvictedBody(NamedTuple):
    """One row a retention statement removed, and what it was holding.

    * `byte_size` is the body as fetched; `stored_bytes` is what Postgres was holding
      after compression, and is None exactly when the body lived in R2.
    * Both retention statements RETURN this column order — the cap here and the hot
      window in `payload_prune` — so the two paths report one set of figures. The cap
      used to return only (id, key), which silently left every capped row out of the
      bytes-reclaimed number the storage sign-off is read from.
    """

    id: int
    r2_key: str | None
    byte_size: int
    stored_bytes: int | None


class ObjectStore(Protocol):
    """The two R2 operations this module needs.

    Declared locally rather than imported from `location_data.archive`: that module
    pulls in the RÚIAN CSV reader for one dataclass, and this one is on the scraper's
    import path from W2a-2 onward. `scraper.image_storage.R2Client` satisfies both.
    """

    def upload_bytes(self, key: str, data: bytes, content_type: str = ...) -> None: ...
    def object_size(self, key: str) -> int | None: ...


@dataclass(frozen=True, slots=True)
class PayloadRef:
    """What the caller needs to know about the row that is now stored.

    Every field describes the ROW, not the fetch: on a collision (`inserted` False)
    the stored body is the one an earlier fetch wrote, so `body_sha256`,
    `content_encoding`, `byte_size` and `body_r2_key` come back from the row. A
    caller that trusted the just-fetched values would GET an R2 object that was
    never uploaded.

    `stored_bytes` is None exactly when that cannot be answered from the row: a
    spilled body this call did not write, whose object size Postgres does not carry
    and which is not worth an R2 round trip on the unchanged path.
    """

    id: int
    payload_sha256: bytes
    body_sha256: bytes
    version_seq: int
    inserted: bool
    byte_size: int
    stored_bytes: int | None
    content_encoding: str
    body_r2_key: str | None
    evicted_ids: tuple[int, ...]
    evicted_r2_keys: tuple[str, ...]


def r2_key(source: str, body_sha256: bytes) -> str:
    """`payloads/<source>/<sha[:2]>/<sha>.gz` — derivable from the row alone.

    `body_sha256`, because that is the hash of what the object holds (the RAW body,
    gzipped); see the note on `_SOURCE_RE`.
    """
    if not _SOURCE_RE.match(source):
        raise PayloadError(f"refusing to build an R2 key for source={source!r}")
    digest = body_sha256.hex()
    return f"{R2_PREFIX}/{source}/{digest[:2]}/{digest}.gz"


def encode_body(body: bytes, *, gzip_min_bytes: int | None = None) -> tuple[bytes, str]:
    """(stored bytes, content_encoding) — deterministic, so the same body always
    encodes to the same bytes.

    `mtime=0`: gzip stamps the current time into its header by default, which would
    make two encodings of one body differ and turn every re-encode into a spurious
    object rewrite. It has no effect on `payload_sha256` (that is taken over the
    normalised body, before encoding), only on reproducibility.
    """
    threshold = gzip_min_bytes if gzip_min_bytes is not None else (
        loader_db.env_positive_int(GZIP_MIN_BYTES_ENV, DEFAULT_GZIP_MIN_BYTES))
    if len(body) <= threshold:
        return body, "identity"
    return gzip.compress(body, mtime=0), "gzip"


def decode_body(stored: bytes, content_encoding: str) -> bytes:
    """Inverse of `encode_body`. The round-trip verifier's half of W2a gate (a)."""
    if content_encoding == "identity":
        return stored
    if content_encoding == "gzip":
        return gzip.decompress(stored)
    raise PayloadError(f"unknown content_encoding {content_encoding!r}")


# `version_seq` is computed in the INSERT itself so an append is ONE round trip and
# cannot interleave with a read-then-write from another worker. Concurrent appends
# for the same key can still land on the same number (the identity constraint is on
# payload_sha256, not on version_seq), which is cosmetic: the cap orders by
# version_seq with `id` as the tiebreaker.
#
# `first_observed_at` / `fetched_at` use least(), not the insert value: bodies do not
# always arrive in observation order (W2a-4 backfills `portal_raw_pages.fetched_at`
# into a store the live path may already have written), and "first observed" must mean
# the earliest observation, not the earliest write.
#
# `snapshot_id` fills in but never overwrites: a body first archived without an anchor
# (the unanchored live path, or W2a-4's backfill of a page that predates its snapshot)
# gains one the moment an anchored fetch of the SAME body arrives, and an anchored row
# is never re-anchored by a later unanchored sighting.
#
# The RETURNING list is the stored row, not the fetch: see PayloadRef.
_APPEND_SQL = """
INSERT INTO portal_raw_payloads
    (source, source_id_native, listing_id, page_kind, payload_sha256, body_sha256,
     content_type, content_encoding, body, body_r2_key, byte_size, http_status,
     contract_version, normalizer_version, snapshot_id, pinned, version_seq,
     first_observed_at, last_observed_at, fetched_at)
VALUES
    (%(source)s, %(source_id_native)s, %(listing_id)s,
     %(page_kind)s::location_page_kind, %(payload_sha256)s, %(body_sha256)s,
     %(content_type)s, %(content_encoding)s, %(body)s, %(body_r2_key)s,
     %(byte_size)s, %(http_status)s, %(contract_version)s,
     %(normalizer_version)s, %(snapshot_id)s, true,
     (SELECT coalesce(max(prior.version_seq), 0) + 1
        FROM portal_raw_payloads prior
       WHERE prior.source = %(source)s
         AND prior.source_id_native = %(source_id_native)s
         AND prior.page_kind = %(page_kind)s::location_page_kind),
     %(observed_at)s, %(observed_at)s, %(observed_at)s)
ON CONFLICT (source, source_id_native, page_kind, payload_sha256) DO UPDATE
   SET last_observed_at  = greatest(EXCLUDED.last_observed_at,
                                    portal_raw_payloads.last_observed_at),
       first_observed_at = least(EXCLUDED.first_observed_at,
                                 portal_raw_payloads.first_observed_at),
       fetched_at        = least(EXCLUDED.fetched_at,
                                 portal_raw_payloads.fetched_at),
       snapshot_id       = coalesce(portal_raw_payloads.snapshot_id,
                                    EXCLUDED.snapshot_id)
RETURNING id, version_seq, (xmax = 0) AS inserted, body_sha256, byte_size,
          content_encoding, body_r2_key, octet_length(body) AS inline_bytes
"""

# P4's pin predicate, recomputed AUTHORITATIVELY over the whole group rather than
# only setting new pins: the row that was the latest before this append has to LOSE
# its pin, or the cap never bites and the archive grows without bound.
#
# "Disputed" has no column of its own — `location_claims` carries no status, by
# design (a claim is append-only evidence). A claim is disputed exactly when an OPEN
# contradiction points at it, in any of the three roles the ledger records: the
# served value, the claimed value, or the evidence behind the finding.
#
# The disputed lookup goes through the claim's payload_sha256 (the content address
# 01 §4.2 keeps alongside payload_id precisely so a claim resolves by content) and is
# scoped BOTH ways: to this group's hashes, so it reads through the partial index
# `location_claims_payload` instead of scanning the claim store, and to this group's
# (source, source_id_native), because a hash alone is not a listing — two listings
# whose normalised bodies coincide would otherwise pin each other's history.
#
# The payload_id arm is not a policy choice, it is the FK: 382 declares
# `location_claims.payload_id references portal_raw_payloads(id)` with NO ACTION, so a
# referenced body CANNOT be deleted — the cap either pins it or the DELETE raises
# ForeignKeyViolation and rolls back the whole bounded transaction, losing the body
# just appended and every later append for that group with it. It is also the right
# answer on the merits: an evidence span that indexes into a deleted body is exactly
# the unverifiability this store exists to end. It costs nothing at read time because
# 403 ships the partial index on payload_id that makes it an index probe rather than
# the seq scan of the claim store an unindexed FK would have been. The pin set stays
# small: claims dedupe on a TIME-FREE fingerprint (01 §4.2.1), so a listing has one
# claim per distinct VALUE, not one per fetched version.
_REPIN_SQL = """
WITH grp AS (
    SELECT id, payload_sha256, version_seq
      FROM portal_raw_payloads
     WHERE source = %(source)s
       AND source_id_native = %(source_id_native)s
       AND page_kind = %(page_kind)s::location_page_kind
),
edges AS (
    SELECT min(version_seq) AS first_seq, max(version_seq) AS latest_seq FROM grp
),
disputed AS (
    SELECT DISTINCT c.payload_sha256
      FROM location_claims c
     WHERE c.payload_sha256 IN (SELECT g.payload_sha256 FROM grp g)
       AND c.source = %(source)s
       AND c.source_id_native = %(source_id_native)s
       AND EXISTS (
           SELECT 1
             FROM location_contradictions_open o
            WHERE o.served_claim_id = c.id
               OR o.claimed_claim_id = c.id
               OR c.id = ANY (o.evidence_claim_ids))
),
want AS (
    SELECT g.id,
           (coalesce(g.version_seq = e.first_seq, false)
            OR coalesce(g.version_seq = e.latest_seq, false)
            OR EXISTS (SELECT 1 FROM location_claims c WHERE c.payload_id = g.id)
            OR EXISTS (SELECT 1 FROM disputed d
                        WHERE d.payload_sha256 = g.payload_sha256)) AS pinned
      FROM grp g CROSS JOIN edges e
)
UPDATE portal_raw_payloads p
   SET pinned = w.pinned
  FROM want w
 WHERE p.id = w.id
   AND p.pinned IS DISTINCT FROM w.pinned
"""

# Pinned rows occupy ranks: with a cap of 20 and two pins inside it, eighteen
# ordinary versions survive. A pin OUTSIDE the cap (the first version, once the group
# is deeper than the cap) survives regardless — that is what "exempt" means.
#
# UNSUCCESSFUL FETCHES RANK LAST. `http_status` was written and never read, which made
# an error body cost a version: a portal outage lasting `version_cap` refetches (idnes
# serving a 503 interstitial whose request id is not in the volatile profile appends
# one row per 6-hourly fetch) evicted the listing's ENTIRE real history except the
# first-version pin, irreversibly — `portal_raw_pages` is latest-wins, so there is
# nothing to restore from. Ranking non-2xx bodies behind 2xx ones makes the outage
# evict itself instead. A NULL status ranks WITH the successes on purpose: W2a-4's
# backfill from `portal_raw_pages` has no status to carry, and those bodies are the
# oldest real history in the store.
#
# `NULLS LAST` and the `id` tiebreaker: DESC sorts NULLs first in Postgres, which
# would let a version_seq-less row masquerade as the newest and shield the real
# newest from the cap; and ties in version_seq must not reshuffle between runs.
_PRUNE_SQL = """
WITH ranked AS (
    SELECT id, pinned,
           row_number() OVER (
               ORDER BY (http_status IS NULL
                         OR http_status BETWEEN 200 AND 299) DESC,
                        version_seq DESC NULLS LAST, id DESC) AS rn
      FROM portal_raw_payloads
     WHERE source = %(source)s
       AND source_id_native = %(source_id_native)s
       AND page_kind = %(page_kind)s::location_page_kind
)
DELETE FROM portal_raw_payloads p
 USING ranked r
 WHERE p.id = r.id
   AND NOT r.pinned
   AND r.rn > %(version_cap)s::integer
RETURNING p.id, p.body_r2_key, p.byte_size, octet_length(p.body)
"""

# An R2 key is content-addressed, so two rows in DIFFERENT groups that fetched
# byte-identical bodies (one portal's "listing removed" page under two listings) share
# one object. Handing an evicted row's key to W2a-5's deleter unfiltered would then
# destroy the body of a live row. Only keys no surviving row still points at are
# reported as reclaimable.
_ORPHANED_KEYS_SQL = """
SELECT DISTINCT k.key
  FROM unnest(%(keys)s::text[]) AS k(key)
 WHERE NOT EXISTS (SELECT 1 FROM portal_raw_payloads p
                    WHERE p.body_r2_key = k.key)
"""

def repin_group(
    cur: psycopg.Cursor,
    *,
    source: str,
    source_id_native: str,
    page_kind: str,
) -> None:
    """Recompute `pinned` authoritatively across one (listing, page_kind) group.

    * The ONE definition of pinned: first version, latest version, a body a claim
      points at, a body a disputed claim's content address names.
    * Shared with `payload_prune`, which re-asserts it on a cadence — a contradiction
      that opens or closes without a new fetch changes the answer, and no append comes
      along to notice.
    """
    cur.execute(_REPIN_SQL, {
        "source": source,
        "source_id_native": source_id_native,
        "page_kind": page_kind,
    })


def prune_group(
    cur: psycopg.Cursor,
    *,
    source: str,
    source_id_native: str,
    page_kind: str,
    version_cap: int,
) -> list[EvictedBody]:
    """Evict unpinned bodies ranked beyond the cap; returns what was removed.

    * Reads `pinned`, never recomputes it: call `repin_group` FIRST, in the same
      transaction, or the cap ranks against a stale pin set.
    """
    cur.execute(_PRUNE_SQL, {
        "source": source,
        "source_id_native": source_id_native,
        "page_kind": page_kind,
        "version_cap": version_cap,
    })
    return [EvictedBody(*row) for row in cur.fetchall()]


def orphaned_r2_keys(cur: psycopg.Cursor, keys: Sequence[str]) -> tuple[str, ...]:
    """Of `keys`, the ones no surviving payload row still points at."""
    if not keys:
        return ()
    cur.execute(_ORPHANED_KEYS_SQL, {"keys": list(keys)})
    return tuple(str(row[0]) for row in cur.fetchall())


_STORE: ObjectStore | None = None


def _open_store() -> ObjectStore | None:
    """The platform's R2 client, or None when R2 is not configured.

    Unconfigured is NOT an error here (unlike `archive.open_store`, where an
    unarchived registry vintage is unreproducible): a body that cannot spill simply
    stays inline in Postgres, which satisfies `prp_body_present` and loses nothing.
    """
    global _STORE
    if _STORE is None:
        from scraper import image_storage

        if not image_storage.is_configured():
            return None
        _STORE = image_storage.R2Client.from_env(max_pool_connections=4)
    return _STORE


def reset_store_cache() -> None:
    """Drop the cached R2 client (tests; a process that must re-read the env)."""
    global _STORE
    _STORE = None


def append_payload(
    conn: psycopg.Connection,
    *,
    source: str,
    source_id_native: str,
    page_kind: str,
    listing_id: int | None,
    body: bytes,
    content_type: str,
    http_status: int | None,
    contract_version: int | None,
    observed_at: datetime,
    snapshot_id: int | None = None,
    volatile: VolatileProfile | None = None,
    version_cap: int | None = None,
    store: ObjectStore | None = None,
    statement_timeout_s: int | None = None,
) -> PayloadRef:
    """Archive one fetched body, appending only if its normalised content changed.

    `observed_at` is the PAYLOAD's own observation time and lands in
    `first_observed_at` AND `fetched_at` (06 Rule 1) — never `now()`, so a backfilled
    body keeps the time it was actually fetched instead of reading as having appeared
    on migration day.

    `snapshot_id` anchors the body to the `listing_snapshots` row it belongs to, which
    is what `location_claims.snapshot_anchor='snapshot'` — the default anchor — needs
    on the other side of the join. None is the honest value for a fetch with no
    snapshot yet; a later anchored fetch of the same body fills it in.

    `volatile` None resolves the measurement-phase profile for this (source,
    page_kind) SURFACE — never for the source alone: `payload_sha256` is the
    archive's identity, so a detail profile mis-applied to an index body would bake
    a hash taken over the wrong projection into every span that ever points at it.
    A surface with no measured profile gets `payload_norm.BASE_PROFILE` and stamps
    `normalizer_version` with the `+base` suffix, so which instrument produced a row's
    content address is readable off the row. W2a-3b replaces those with the contract's
    declared `persistence.volatile_paths`.

    Retention (re-pin + cap) runs only when a row was actually appended: an unchanged
    refetch cannot have changed the group's membership, and paying two extra
    statements per fetch on the common path is exactly the cost this store exists to
    avoid. The scheduled pruner (W2a-5) is what re-asserts pins after a contradiction
    opens or closes without a new fetch.
    """
    if not content_type:
        raise PayloadError("content_type is required — it decides how the body normalises")
    profile = volatile if volatile is not None else volatile_profile(source, page_kind)
    norm = normalise(body, content_type=content_type, volatile=profile)

    stored, encoding = encode_body(body)
    cap = version_cap if version_cap is not None else loader_db.env_positive_int(
        VERSION_CAP_ENV, DEFAULT_VERSION_CAP)
    timeout_s = statement_timeout_s if statement_timeout_s is not None else (
        loader_db.env_timeout_s(WRITE_TIMEOUT_ENV, DEFAULT_WRITE_TIMEOUT_S))

    threshold = loader_db.env_positive_int(R2_THRESHOLD_ENV, DEFAULT_R2_THRESHOLD_BYTES)
    if len(stored) > threshold and encoding != "gzip":
        stored, encoding = gzip.compress(body, mtime=0), "gzip"
    key: str | None = None
    if len(stored) > threshold:
        store = store if store is not None else _open_store()
        if store is None:
            # Not fatal: a large body inline is a TOASTed bytea, which is what the
            # store held before the threshold existed. Losing the append would be
            # worse than storing it in Postgres.
            LOG.warning(
                "PAYLOAD R2 unconfigured; %s/%s stays inline (%d stored bytes)",
                source, source_id_native, len(stored),
            )
        else:
            key = r2_key(source, norm.raw_sha256)

    params: dict[str, Any] = {
        "source": source,
        "source_id_native": source_id_native,
        "listing_id": listing_id,
        "page_kind": page_kind,
        "payload_sha256": norm.norm_sha256,
        "body_sha256": norm.raw_sha256,
        "content_type": content_type,
        "content_encoding": encoding,
        "body": None if key else stored,
        "body_r2_key": key,
        "byte_size": norm.byte_size,
        "http_status": http_status,
        "contract_version": contract_version,
        "normalizer_version": normalizer_version_for(source, page_kind),
        "snapshot_id": snapshot_id,
        "observed_at": observed_at,
    }
    group = {
        "source": source,
        "source_id_native": source_id_native,
        "page_kind": page_kind,
    }

    with loader_db.bounded(conn, timeout_s) as cur:
        cur.execute(_APPEND_SQL, params)
        row = cur.fetchone()
        if row is None:  # pragma: no cover - RETURNING always yields on upsert
            raise PayloadError(f"payload append returned no row for {source}/{source_id_native}")
        payload_id, version_seq, inserted = int(row[0]), int(row[1]), bool(row[2])
        # The row as stored. On a collision this is what an EARLIER fetch wrote, and
        # it is what the caller must be told about — `inline_bytes` is NULL only when
        # that body lives in R2, the one question the row cannot answer and the
        # unchanged path must not pay an R2 HEAD to learn.
        row_body_sha256, row_byte_size = bytes(row[3]), int(row[4])
        row_encoding, row_key = str(row[5]), row[6]
        row_stored_bytes = None if row[7] is None else int(row[7])

        # INSIDE the transaction and only for a genuinely new body: an upload that
        # fails must roll the row back, because a committed row whose body_r2_key
        # points at nothing has lost the substrate every span into it needs. The
        # reverse orphan (object uploaded, transaction rolled back) is harmless —
        # the key is content-addressed, so the next append reuses it.
        if key and inserted and store is not None:
            if store.object_size(key) is None:
                store.upload_bytes(key, stored, "application/gzip")

        evicted_ids: tuple[int, ...] = ()
        evicted_keys: tuple[str, ...] = ()
        if inserted:
            repin_group(cur, **group)
            evicted = prune_group(cur, **group, version_cap=cap)
            evicted_ids = tuple(row.id for row in evicted)
            evicted_keys = orphaned_r2_keys(
                cur, [row.r2_key for row in evicted if row.r2_key])

    if evicted_ids:
        # The orphaned R2 objects are handed to W2a-5's pruner, which owns "report
        # bytes reclaimed"; deleting them here would need a delete verb this store
        # protocol deliberately does not have.
        LOG.info(
            "PAYLOAD evicted source=%s key=%s rows=%d r2=%d cap=%d",
            source, source_id_native, len(evicted_ids), len(evicted_keys), cap,
        )

    return PayloadRef(
        id=payload_id,
        payload_sha256=norm.norm_sha256,
        # The four below come from the ROW, which on the insert path is what this
        # call wrote and on a collision is what an EARLIER fetch wrote — the encode
        # pass above was discarded, so reporting it would advertise an R2 object that
        # was never uploaded and a byte_size the store does not have.
        body_sha256=row_body_sha256,
        version_seq=version_seq,
        inserted=inserted,
        byte_size=row_byte_size,
        stored_bytes=len(stored) if inserted else row_stored_bytes,
        content_encoding=row_encoding,
        body_r2_key=row_key,
        evicted_ids=evicted_ids,
        evicted_r2_keys=evicted_keys,
    )
