"""Migration 574 drops `autodedup.verdicts`' per-decider unique indexes (E920 i): once it is applied,
a runtime statement whose conflict target names them raises 42P10 on EVERY operator write. The
writes append on change (`ui_sql.VERDICT_PAIR_APPEND_SQL` / `VERDICT_CLUSTER_APPEND_SQL`, valid on
both sides of 574) and `toolkit.property_identity.record_ruling` is the one pair writer. This census
reads every runtime source file, so a branch that still spells the old conflict target (an older
PR rebased onto this one) fails here, before 574 can reach it.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from autodedup import ui_sql as usql
from tests.sql_corpus import RUNTIME_DIRS, _source_files
from toolkit import property_identity as pi

_ROOT = Path(__file__).resolve().parent.parent
_OLD_TARGET = re.compile(r"on\s+conflict\s*\(\s*kind\b", re.IGNORECASE)
_OLD_INDEXES = re.compile(
    r"autodedup_verdicts_(pair_uidx|cluster_uidx|cluster_gen_uidx)", re.IGNORECASE)


def test_no_runtime_statement_spells_the_per_decider_conflict_target():
    offenders: list[str] = []
    scanned = 0
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        scanned += 1
        for pattern in (_OLD_TARGET, _OLD_INDEXES):
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(_ROOT)}:{line}")
    assert scanned > 100 and "autodedup" in RUNTIME_DIRS
    assert not offenders, (
        "autodedup.verdicts is append-only once migration 574 is applied: write through "
        "record_ruling / VERDICT_*_APPEND_SQL, not ON CONFLICT on the dropped per-decider "
        f"indexes: {offenders}")


def test_the_pair_writers_all_go_through_record_ruling():
    assert "usql.VERDICT_PAIR_APPEND_SQL" in inspect.getsource(pi.record_ruling)
    rulings = inspect.getsource(pi.record_rulings)
    assert "record_ruling(" in rulings and "reasons=reasons" in rulings
    assert "ON CONFLICT DO NOTHING" in usql.VERDICT_PAIR_APPEND_SQL
    assert "ON CONFLICT DO NOTHING" in usql.VERDICT_CLUSTER_APPEND_SQL
