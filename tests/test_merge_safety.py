"""Merge safety (migration 559): one price-step definition, a retire and a reactivation that
each set a property's state in one statement, and the operator's rulings written with the
review pages' own statements.

Hermetic: the SQL text. The executed half — a step never spans two adverts, merging then
detaching gives back every original property, the rulings land in `autodedup.verdicts` — runs
against the replayed schema in tests/test_merge_safety_live.py; the rulings' own cases are
tests/test_property_merge_set.py and tests/test_detach_listing.py.
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
    """The rollup's one window orders the lineage's links (MS10), never an advert's snapshots."""
    import re

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
    windows = re.findall(r"\w+\([^()]*\)\s+over\s*\([^)]*\)", _RECOMPUTE_BATCH_SQL, re.I)
    assert windows == ["lead(m.last_price) OVER (PARTITION BY m.pid ORDER BY m.hop)"]


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


# --- 2. a property's state, set in one statement ---------------------------------------


def test_the_merge_retires_and_the_detach_reactivates_in_one_statement_each():
    """Both halves set `status` and `is_active` together: a retired property is never left
    active, and one a detach brings back is active exactly when one of its ads is (the
    executed half is tests/test_merge_safety_live.py)."""
    retire = " ".join(pi._RETIRE_SQL.split())
    assert "SET status = 'merged_away', merged_into = %s, merged_at = now(), is_active = false" \
        in retire
    reactivate = " ".join(pi._REACTIVATE_SQL.split())
    assert "SET status = 'active', merged_into = NULL, merged_at = NULL, is_active = EXISTS (" \
        in reactivate


# --- 3. rulings, in the review pages' own statements ----------------------------------------


def test_the_ruling_writes_are_the_review_pages_own_statements():
    source = inspect.getsource(pi.record_ruling)
    for name in ("VERDICT_PAIR_APPEND_SQL", "MUST_NOT_LINK_UPSERT_SQL", "MUST_NOT_LINK_RETRACT_SQL"):
        assert f"usql.{name}" in source
    assert "record_ruling(" in inspect.getsource(pi.record_rulings)
    # A bare operator veto is written down as its `different` BEFORE the newer word lands.
    assert source.index("usql.VERDICT_PAIR_FROM_VETO_SQL") < source.index(
        "usql.VERDICT_PAIR_APPEND_SQL")
    veto = " ".join(usql.VERDICT_PAIR_FROM_VETO_SQL.split())
    assert "m.source = 'operator'" in veto and "AND NOT EXISTS (SELECT 1 FROM autodedup.verdicts" in veto
    assert "'pair'" in usql.VERDICT_PAIR_APPEND_SQL
    assert "different" in usql.NEGATIVE_VERDICTS, "the adapter's negatives read this value"
