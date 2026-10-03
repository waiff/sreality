"""Shape gate for migration 582: the live apply scope admits share sales (E932, N9 2026-10-01).

Offline, no DB. The value is migration 570's row with `podil` added to `category_types` and
nothing else moved (the three trial blocks, the 600 cap), parses as a LIVE scope, is written
onto the row 558 seeded and nowhere else, and is loud when that row is missing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from autodedup import apply as A

_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
_MIGRATION = _MIGRATIONS / "582_autodedup_apply_scope_share_sales.sql"
_PRIOR = _MIGRATIONS / "570_autodedup_lane_restore_blocks.sql"
TRIAL = {"town:563510", "town:577626", "quarter:490245"}


def _code() -> str:
    body = "\n".join(line.split("--")[0] for line in
                     _MIGRATION.read_text(encoding="utf-8").lower().splitlines())
    return re.sub(r"\s+", " ", body)


def _value(path: Path) -> dict:
    literal = re.search(r"set value = '(\{.*?\})'::jsonb", path.read_text(encoding="utf-8"))
    assert literal is not None
    return json.loads(literal.group(1))


def test_the_value_is_570s_row_plus_podil_and_parses_as_a_live_scope() -> None:
    prior, value = _value(_PRIOR), _value(_MIGRATION)
    assert set(value) == set(A.SCOPE_KEYS)
    assert value["category_types"] == [*prior["category_types"], "podil"]
    assert {k: v for k, v in value.items() if k != "category_types"} == {
        k: v for k, v in prior.items() if k != "category_types"}
    scope = A.effective_scope(value, {}, live=True)
    assert scope.category_types == frozenset({"prodej", "pronajem", "podil"})
    assert scope.blocks == frozenset(TRIAL)
    assert scope.listing_ids is None and scope.all_blocks is False
    assert scope.max_clusters_per_run == 600
    assert scope.live_problems() == []


def test_a_share_advert_is_inside_the_scope_an_auction_still_outside() -> None:
    """The regression E932 names: a sale group that absorbs its `podil` twin is refused WHOLE
    under 570 (`group_outside`); under 582 it is inside. An auction stays outside."""
    before = A.effective_scope(_value(_PRIOR), {}, live=True)
    after = A.effective_scope(_value(_MIGRATION), {}, live=True)
    sale = A.Member(1, 10, "prodej", "pozemek", obec_kod=563510)
    share = A.Member(2, 20, "podil", "pozemek", obec_kod=563510)
    auction = A.Member(3, 30, "drazba", "pozemek", obec_kod=563510)
    assert before.group_outside([sale]) is None
    assert before.group_outside([sale, share]) == A.OUT_CATEGORY
    assert after.group_outside([sale, share]) is None
    assert after.outside(auction) == A.OUT_CATEGORY


def test_it_updates_the_one_seeded_row_and_fails_loudly_without_it() -> None:
    code = _code()
    assert "update public.app_settings" in code
    assert re.findall(r"where key = '([a-z_]+)'", code) == [A.SCOPE_SETTING]
    assert "if not found then raise exception" in code
    assert "updated_by = 'migration 582'" in code
    for forbidden in ("insert ", "delete ", "drop ", "alter ", "create "):
        assert forbidden not in code, forbidden
