"""Index-archive key + upsert-shape guards (location-data W0 item 0n)."""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone

from scraper import db


def test_index_archive_week_is_iso_week_stamped():
    # Week-stamped keys are what make the index archive ACCUMULATE for
    # delisted listings instead of rolling over in place (review-confirmed
    # critical on 0n's first cut).
    assert db.index_archive_week(datetime(2026, 8, 10, tzinfo=timezone.utc)) == "2026w33"
    assert db.index_archive_week(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "2026w01"
    # ISO week 53 of the previous year.
    assert db.index_archive_week(datetime(2027, 1, 1, tzinfo=timezone.utc)) == "2026w53"


def test_raw_page_upsert_sql_are_plain_literals():
    # The schema-and-sql CI gate only discovers ast.Constant SQL; both upsert
    # forms must stay module-level plain literals (review-confirmed major:
    # the first cut concatenated the guard, silently dropping the statement
    # from the PREPARE corpus).
    src = ast.parse(inspect.getsource(db))
    names = {
        t.id
        for node in ast.walk(src)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant)
    }
    assert {"_RAW_PAGE_UPSERT_SQL", "_RAW_PAGE_UPSERT_GUARDED_SQL"} <= names
    assert "make_interval" in db._RAW_PAGE_UPSERT_GUARDED_SQL
    assert "make_interval" not in db._RAW_PAGE_UPSERT_SQL
