"""embedding_ab's candidate-pair SQL joins images on the surrogate images.listing_id.

Offline eval (torch/PIL/R2 at runtime); this asserts only the SQL keying so it stays
hermetic. Joining images on sreality_id would drop non-sreality rows once Gate 2 nulls it.
"""

from __future__ import annotations

from scripts.embedding_ab import _PAIRS_SQL


def test_pairs_sql_joins_images_on_surrogate() -> None:
    sql = " ".join(_PAIRS_SQL.split())
    assert "ia.listing_id=l_a.id" in sql
    assert "ib.listing_id=l_b.id" in sql
    assert "ia.sreality_id=g.la" not in sql
    assert "ib.sreality_id=g.lb" not in sql
