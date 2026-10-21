"""api/notification_render.py, hermetic: one Czech label per change_kind the CHECK allows, the same keys as the
bell's English map, the frozen prices only, no per-m² figure and nothing of a sharer."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from api import notification_render as nr
from tests.migration_defs import MIGRATIONS

NBSP = " "
REPO = Path(__file__).resolve().parents[2]


def _check_values() -> set[str]:
    """The newest notification_dispatches_change_kind_ck the migrations add."""
    defining = [p for p in MIGRATIONS.glob("*.sql")
                if "add constraint notification_dispatches_change_kind_ck" in p.read_text(encoding="utf-8")]
    newest = max(defining, key=lambda p: int(p.name.split("_", 1)[0]))
    sql = " ".join(newest.read_text(encoding="utf-8").split())
    check = sql.split("add constraint notification_dispatches_change_kind_ck check (", 1)[1].split(");", 1)[0]
    return set(re.findall(r"'(\w+)'", check))


def test_every_check_value_has_one_label_and_every_label_one_value() -> None:
    """Migration 656 narrowed the CHECK to the nine kinds the producers write, so no value is never pushed."""
    allowed = _check_values()
    assert set(nr.TITLES) == allowed
    assert set(nr.SHARED_BODIES) == {k for k in allowed if k.startswith("shared_")}


def test_the_bells_english_map_has_the_same_keys() -> None:
    src = (REPO / "frontend" / "src" / "pages" / "Notifications.tsx").read_text(encoding="utf-8")
    block = src.split("const CHANGE_META", 1)[1].split("};", 1)[0]
    assert set(re.findall(r"^\s*(\w+):\s*\{\s*label:", block, re.MULTILINE)) == set(nr.TITLES)


@pytest.mark.parametrize(("row", "title", "body"), [
    ({"change_kind": "new", "display_label": "Praha 5 – Smíchov", "category_main": "byt", "disposition": "2+kk",
      "price_unit": "za nemovitost", "trigger_price_czk": 6_490_000},
     "Nový inzerát · Praha 5 – Smíchov", f"Byt 2+kk · 6{NBSP}490{NBSP}000{NBSP}Kč"),
    ({"change_kind": "new", "display_label": "Brno", "category_main": "byt", "disposition": "3+1",
      "price_unit": "za mesic", "trigger_price_czk": 24_500},
     "Nový inzerát · Brno", f"Byt 3+1 · 24{NBSP}500{NBSP}Kč/měs"),
    ({"change_kind": "price_drop", "display_label": "Kolín", "trigger_price_czk": 4_200_000,
      "prev_price_czk": 4_500_000},
     "Zlevněno · Kolín", f"4{NBSP}500{NBSP}000{NBSP}Kč → 4{NBSP}200{NBSP}000{NBSP}Kč"),
    ({"change_kind": "price_rise", "display_label": "Kolín", "trigger_price_czk": 21_000, "prev_price_czk": 20_000,
      "price_unit": "za mesic"},
     "Zdraženo · Kolín", f"20{NBSP}000{NBSP}Kč/měs → 21{NBSP}000{NBSP}Kč/měs"),
    ({"change_kind": "inactive", "display_label": "Beroun", "category_main": "dum", "subtype": "vila"},
     "Staženo z nabídky · Beroun", "Vila"),
    ({"change_kind": "reactivated", "display_label": "Beroun", "category_main": "pozemek"},
     "Znovu v nabídce · Beroun", "Pozemek"),
    ({"change_kind": "new_source", "display_label": None, "category_main": "byt", "disposition": "1+kk"},
     "Nový zdroj nabídky", "Byt 1+kk"),
    ({"change_kind": "new", "display_label": "Ostrava"}, "Nový inzerát · Ostrava", ""),
    ({"change_kind": "price_drop", "display_label": "Kolín", "trigger_price_czk": 4_200_000, "category_main": "byt"},
     "Zlevněno · Kolín", "Byt"),
    ({"change_kind": "shared_collection", "display_label": "Praha", "actor_email": "a@b.cz", "share_name": "x"},
     "Sdíleno s vámi", "Sbírka"),
    ({"change_kind": "shared_dashboard"}, "Sdíleno s vámi", "Dashboard"),
    ({"change_kind": "shared_pipeline", "actor_email": "owner@x.cz"}, "Sdíleno s vámi", "Pipeline"),
    ({"change_kind": "unknown_kind"}, "Nové upozornění", ""),
    ({}, "Nové upozornění", ""),
], ids=lambda v: v.get("change_kind", "none") if isinstance(v, dict) else None)
def test_each_kind_renders_from_the_frozen_columns(row: dict, title: str, body: str) -> None:
    assert nr.render(row) == nr.Rendered(title, body)


def test_no_text_carries_a_per_m2_figure_a_sender_or_a_url() -> None:
    rows = [{"change_kind": kind, "display_label": "Praha", "category_main": "byt", "disposition": "2+kk",
             "trigger_price_czk": 5_000_000, "prev_price_czk": 5_500_000, "actor_email": "x@y.cz",
             "share_name": "https://evil",
             "price_per_m2": 91_535} for kind in nr.TITLES]
    for row in rows:
        out = nr.render(row)
        text = out.title + out.body
        assert "m²" not in text and "91" not in text, "rule 23: never a per-m² number"
        assert "@" not in text and "http" not in text
