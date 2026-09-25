"""Shape gate for migration 562: the apply scope row names the W2 trial (decision 1).

Offline, no DB. The value must be exactly what `autodedup.apply` parses into a LIVE scope (sales,
the three trial blocks, every scope key spelled out), written onto the row 558 seeded and nowhere
else, and loud when that row is missing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from autodedup import apply as A
from autodedup import legacy_retire as L

_MIGRATION = (Path(__file__).resolve().parent.parent / "migrations"
              / "562_autodedup_trial_scope.sql")
TRIAL = {"town:563510", "town:577626", "quarter:490245"}


def _code() -> str:
    body = "\n".join(line.split("--")[0] for line in
                     _MIGRATION.read_text(encoding="utf-8").lower().splitlines())
    return re.sub(r"\s+", " ", body)


def _value() -> dict:
    literal = re.search(r"set value = '(\{.*?\})'::jsonb", _MIGRATION.read_text(encoding="utf-8"))
    assert literal is not None
    return json.loads(literal.group(1))


def test_the_value_parses_as_a_live_scope_of_sales_in_the_three_trial_blocks() -> None:
    value = _value()
    assert set(value) == set(A.SCOPE_KEYS)
    scope = A.effective_scope(value, {}, live=True)
    assert scope.category_types == frozenset({"prodej"})
    assert scope.blocks == frozenset(TRIAL)
    assert scope.listing_ids is None and scope.all_blocks is False
    assert scope.max_clusters_per_run == A.DEFAULT_MAX_CLUSTERS_PER_RUN
    assert scope.live_problems() == []
    assert L.area_params(scope.blocks) == {"towns": [563510, 577626], "quarters": [490245]}


def test_it_updates_the_one_seeded_row_and_fails_loudly_without_it() -> None:
    code = _code()
    assert "update public.app_settings" in code
    assert re.findall(r"where key = '([a-z_]+)'", code) == [A.SCOPE_SETTING]
    assert "if not found then raise exception" in code
    for forbidden in ("insert ", "delete ", "drop ", "alter ", "create "):
        assert forbidden not in code, forbidden
