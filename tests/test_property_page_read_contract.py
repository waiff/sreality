"""The SPA's property page (decision 11) reads `properties_public` through a PostgREST
select string, `PROPERTY_COLS` in frontend/src/lib/queries.ts, which `tsc` cannot see.
Every column it names must be a column of the view's latest definition, or the page
answers a PostgREST 400 in production."""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_location_w3_projection import _columns, _sql

ROOT = Path(__file__).resolve().parents[1]
_CREATES = re.compile(r"create\s+(?:or\s+replace\s+)?view\s+(?:public\.)?properties_public\b", re.I)


def _property_cols() -> list[str]:
    src = (ROOT / "frontend" / "src" / "lib" / "queries.ts").read_text(encoding="utf-8")
    m = re.search(r"\bconst PROPERTY_COLS\s*=(.*?);\n", src, re.DOTALL)
    assert m, "PROPERTY_COLS not found in frontend/src/lib/queries.ts"
    joined = "".join(re.findall(r"'([^']*)'", m.group(1)))
    return [c.split(":")[-1] for c in joined.split(",") if c]  # `alias:column`


def test_the_property_page_reads_only_columns_the_view_has() -> None:
    latest = [p.name for p in sorted((ROOT / "migrations").glob("*.sql"))
              if _CREATES.search(_sql(p.name))][-1]
    view = set(_columns(_sql(latest), "properties_public"))
    missing = sorted(set(_property_cols()) - view)
    assert not missing, f"PROPERTY_COLS names columns properties_public ({latest}) lacks: {missing}"
