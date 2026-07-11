"""listings.geom preserve-if-null on both ingest upserts (the mig-263 rail,
extended to coordinates by the location-resolution layer).

An incoming NULL geom means "the page carried no coords", never "coords
removed": a bare `geom = EXCLUDED.geom` silently wiped geocoded / backfilled
coordinates on the next coords-less refetch — and with them the row's admin
hierarchy freshness and its dedup geo_cell_key (both trigger-derived from geom).
The drain-path guard is Python-level carry-forward (scraper.location); this SQL
rail is the belt-and-braces for every portal, including ones whose parser
regresses. A real move still wins: non-NULL incoming geom replaces the stored
value."""

from __future__ import annotations

import inspect

from scraper import db

_EXPECTED = "geom = COALESCE(EXCLUDED.geom, listings.geom)"


def test_batch_upsert_preserves_geom_if_incoming_null() -> None:
    assert _EXPECTED in db._BATCH_UPSERT_SQL
    assert "geom = EXCLUDED.geom" not in db._BATCH_UPSERT_SQL


def test_single_upsert_preserves_geom_if_incoming_null() -> None:
    # upsert_listing builds its SQL locally (not a module constant), so pin the
    # fragment via the function source — same contract, different construction.
    src = inspect.getsource(db.upsert_listing)
    assert _EXPECTED in src
    assert "geom = EXCLUDED.geom," not in src
