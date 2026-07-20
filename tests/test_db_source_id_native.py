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


def test_single_upsert_stamps_source_inline_to_avoid_natkey_collision() -> None:
    # `source` MUST be in the INSERT (not only the post-insert UPDATE): its column
    # default is 'sreality', so an insert stamping only source_id_native would write
    # ('sreality', <native_id>) and could collide with a real sreality row on the
    # UNIQUE(source, source_id_native) index, which ON CONFLICT (sreality_id) does not
    # arbitrate (unique_violation -> ingest aborts -> portal drain wedges).
    src = inspect.getsource(db.upsert_listing)
    assert "%(source)s" in src
    assert 'row.get("source") or "sreality"' in src


def test_ingest_carries_full_natural_key_into_the_insert_row() -> None:
    # Non-sreality ingest must put BOTH source and its portal id in the row so
    # upsert_listing stamps the natural-key pair inline, not via the post-insert UPDATE.
    src = inspect.getsource(db.ingest_scraped_listing)
    assert 'row["source"] = listing.source' in src
    assert 'row["source_id_native"] = listing.source_id_native' in src
