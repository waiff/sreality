"""Every sreality write path stamps the (source, source_id_native) natural key.

source_id_native (migration 091) is the portal-native half of the natural key the
listing-identity refactor relies on. The sreality detail-drain (write_detail_batch)
historically never stamped it, so new sreality rows accumulated NULLs — the hole
migration 314 backfills and enforces (CHECK source_id_native IS NOT NULL). These
tests pin the inline stamp on every INSERT path so the NOT-NULL invariant can't
regress: a heal-after-insert would be rejected by the constraint at insert time,
so the value must be present in the INSERT itself.

Same source-inspection contract as test_db_geom_preserve — the fake-conn harness
can't observe real column writes, and (per the fake-conn/DB-constraint lesson) it
can't catch the constraint either; the SQL-correctness CI gate PREPAREs the changed
SQL against the replayed schema, and the value is verified live on apply."""

from __future__ import annotations

import inspect

from scraper import db


def test_batch_upsert_stamps_and_heals_source_id_native() -> None:
    # The drain is sreality-only, whose native id IS its sreality_id.
    assert "source_id_native" in db._BATCH_UPSERT_SQL
    assert "j.sreality_id::text" in db._BATCH_UPSERT_SQL
    # Preserve-if-null on conflict: heal a legacy NULL, never clobber a set value.
    assert (
        "source_id_native = COALESCE(listings.source_id_native, EXCLUDED.source_id_native)"
        in db._BATCH_UPSERT_SQL
    )


def test_single_upsert_stamps_and_heals_source_id_native() -> None:
    src = inspect.getsource(db.upsert_listing)
    # Inline INSERT stamp (bound param) + preserve-if-null heal on conflict.
    assert "%(source_id_native)s" in src
    assert (
        "source_id_native = COALESCE(listings.source_id_native, EXCLUDED.source_id_native)"
        in src
    )
    # Falls back to sreality_id::text when the caller supplies no native id.
    assert 'row.get("source_id_native") or str(sreality_id)' in src


def test_ingest_carries_native_id_into_the_insert_row() -> None:
    # Non-sreality ingest must put its portal id in the row so upsert_listing
    # stamps it inline, not only via the post-insert source/source_url UPDATE.
    src = inspect.getsource(db.ingest_scraped_listing)
    assert 'row["source_id_native"] = listing.source_id_native' in src
