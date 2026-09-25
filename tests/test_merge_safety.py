"""Merge safety (migration 559): one price-step definition, no status event on a merge, and
the operator's rulings written with the review pages' own statements.

Hermetic: the SQL text. The executed half — a step never spans two adverts, a merge writes no
status row, a detach restores the absorbed property's own state, the rulings land in
`autodedup.verdicts` — runs against the replayed schema in tests/test_merge_safety_live.py;
the rulings' own cases are tests/test_property_merge_set.py and tests/test_detach_listing.py.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import toolkit.property_identity as pi
from autodedup import ui_sql as usql

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "559_merge_safety.sql"
OP = "operator@example.com"


# --- 1. one price-step definition ------------------------------------------------------


def test_every_price_step_reader_reads_the_one_view_and_none_keeps_a_window():
    from api import notifications as nf
    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    readers = {
        "rollup": _RECOMPUTE_BATCH_SQL,
        "watchdog": inspect.getsource(nf._recent_price_drops),
        "collection monitor": inspect.getsource(nf.match_monitored_collections_once),
    }
    for name, source in readers.items():
        assert "listing_price_steps" in source, f"the {name} must read the one step view"
        assert "lag(" not in source, f"the {name} keeps its own price-step window"


def test_the_step_view_compares_an_advert_only_with_its_own_previous_price():
    sql = " ".join(MIGRATION.read_text().split())
    view = sql.split("create view listing_price_steps", 1)[1].split(";", 1)[0]
    assert "p.listing_id = s.listing_id" in view
    assert "property_id =" not in view, "a step keyed on the property spans adverts"
    assert "(p.scraped_at, p.id) < (s.scraped_at, s.id)" in view
    assert "order by p.scraped_at desc, p.id desc limit 1" in view
    assert "s.price_czk <> prev.price_czk" in view
    # No window: a view with one cannot take the callers' join scope (batch, monitored).
    assert " over " not in view.lower()
    assert "revoke all on listing_price_steps from anon, authenticated" in sql
    assert "security_invoker = true" in sql


# --- 2. no status event on a merge -----------------------------------------------------


def _trigger_body() -> str:
    sql = " ".join(MIGRATION.read_text().split())
    return sql.split("create or replace function log_property_status_event()", 1)[1].split("$$;", 1)[0]


def test_the_status_trigger_skips_a_retirement():
    assert "elsif NEW.status = 'merged_away' then null;" in _trigger_body()


def test_an_unmerge_logs_only_where_the_propertys_own_history_disagrees():
    """A pre-559 absorbed property ends on the merge's false 'inactive': its reactivation must
    log 'active', or the chart reads it inactive for good. One that still reads its restored
    state gets nothing."""
    body = _trigger_body()
    branch = body.split("elsif OLD.status = 'merged_away' then", 1)[1].split("elsif", 1)[0]
    assert "where e.property_id = NEW.id order by e.event_at desc, e.id desc limit 1" in branch
    assert ") is distinct from NEW.is_active then insert into property_status_events" in branch
    assert "values (NEW.id, NEW.is_active, now())" in branch


def test_the_status_log_stays_with_its_own_property():
    from toolkit.operator_state import OPERATOR_STATE_TABLES

    assert "property_status_events" not in {t[0] for t in OPERATOR_STATE_TABLES}


def test_the_merge_retires_and_the_detach_reactivates_in_one_statement_each():
    """Both halves must touch `status` and `is_active` together, or the trigger sees a
    plain is_active flip on an active row and logs it."""
    merge = " ".join(inspect.getsource(pi.merge_properties).split())
    assert "SET status = 'merged_away', merged_into = %s, merged_at = now(), is_active = false" in merge
    reactivate = " ".join(pi._REACTIVATE_SQL.split())
    assert "SET status = 'active', merged_into = NULL, merged_at = NULL, is_active = EXISTS (" \
        in reactivate
    assert "_REACTIVATE_SQL" in inspect.getsource(pi.detach_listing)


# --- 3. rulings, in the review pages' own statements ----------------------------------------


def test_the_ruling_writes_are_the_review_pages_own_statements():
    source = inspect.getsource(pi.record_rulings)
    for name in ("VERDICT_PAIR_UPSERT_SQL", "MUST_NOT_LINK_UPSERT_SQL", "MUST_NOT_LINK_RETRACT_SQL"):
        assert f"usql.{name}" in source
    assert "'pair'" in usql.VERDICT_PAIR_UPSERT_SQL
    assert "different" in usql.NEGATIVE_VERDICTS, "the adapter's negatives read this value"
