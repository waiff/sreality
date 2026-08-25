"""decide() rules of the unit-price quarantine — confirmation, never magnitude.

The true total was never on the page, so this backfill cannot repair; it can
only stop a rate being read as a total. That makes the false-positive cost the
whole design: a cheap-but-real listing must survive, and a row whose price cell
cannot be read must be LEFT and counted, never guessed at from its magnitude.
"""

from __future__ import annotations

import pytest

from scripts.backfill_unit_price_masquerade import (
    KEEP,
    QUARANTINE,
    UNCONFIRMED,
    decide,
    price_text_from_fragment,
)

# The three portals' real cell shapes, straight off production staged pages.
PER_AREA_CELLS = [
    ("ceskereality", "100 Kč za m²/měsíc"),
    ("ceskereality", "105 Kč za m²/rok"),
    ("ceskereality", "105 533 Kč za m² Spočítat hypotéku"),
    ("realitymix", "45 Kč / (za m 2 ) Nabídněte cenu"),
    ("realitymix", "850 Kč / (za m 2 )"),
]

# Cheap but REAL: a garage, a storage unit, a small monthly rent. Magnitude puts
# these squarely inside the damaged band; the cell is what saves them.
CHEAP_BUT_REAL_CELLS = [
    ("ceskereality", "968 Kč za měsíc"),
    ("ceskereality", "833 Kč za rok"),
    ("ceskereality", "700 Kč za měsíc"),
    ("bazos", "250 Kč"),
    ("bazos", "170 Kč"),
    ("realitymix", "900 Kč / (za měsíc)"),
]


@pytest.mark.parametrize("source,cell", PER_AREA_CELLS)
def test_per_area_cell_is_quarantined(source: str, cell: str) -> None:
    assert decide(source, cell, 100)[0] == QUARANTINE


@pytest.mark.parametrize("source,cell", CHEAP_BUT_REAL_CELLS)
def test_cheap_but_real_cell_survives(source: str, cell: str) -> None:
    assert decide(source, cell, 250)[0] == KEEP


def test_a_total_with_a_per_area_note_is_still_a_total():
    # The marker test is ANCHORED to the text right after the amount, so a
    # parenthesised Kč/m² NOTE beside a real total never convicts the total.
    assert decide("ceskereality", "4 990 000 Kč (4 008 Kč/m²)", 4_990_000)[0] == KEEP


def test_magnitude_alone_never_quarantines():
    # The largest confirmed ceskereality masquerade is 979,620 Kč "za m²" and
    # the smallest surviving real price is double digits: the band overlaps
    # completely, which is why only the cell decides.
    assert decide("ceskereality", "979 620 Kč za m²/měsíc", 979_620)[0] == QUARANTINE
    assert decide("ceskereality", "10 Kč za měsíc", 10)[0] == KEEP


@pytest.mark.parametrize("cell", [None, "", "   ", "Cena dohodou"])
def test_an_unreadable_cell_is_left_alone_not_guessed(cell: str | None) -> None:
    verdict, _reason = decide("realitymix", cell, 136)
    assert verdict == UNCONFIRMED


def test_bazos_bare_cell_confirms_nothing():
    # Measured across all 26,592 priced active bazos rows: ZERO carry any
    # per-area marker. The basis lives in prose, so bazos is left whole.
    assert decide("bazos", "379 Kč", 379)[0] == KEEP


def test_a_row_with_no_stored_price_is_a_no_op():
    assert decide("ceskereality", "100 Kč za m²/měsíc", None)[0] == KEEP


def test_realitymix_fragment_round_trips_through_the_portal_extractor():
    # The staged page is 72 kB; only this 379-byte <tr> is fetched, so it has to
    # survive being re-wrapped in a <table> before the portal's own selector.
    fragment = (
        '<tr class="advert-description__short-props-price">\n\t\t\t\t<td>Cena:</td>\n'
        '\t\t\t\t<td>45 Kč  / <span>(za m<sup>2</sup>)</span>&nbsp;'
        '<button class="x" data-toggle-form-propose-price-detail>Nabídněte cenu</button>\n'
        "\t\t\t\t</td>\n\t\t\t</tr>"
    )
    text = price_text_from_fragment("realitymix", fragment)
    assert text is not None and "45" in text
    assert decide("realitymix", text, 45)[0] == QUARANTINE


def test_a_missing_fragment_yields_no_text():
    assert price_text_from_fragment("realitymix", None) is None
    assert price_text_from_fragment("ceskereality", "<tr><td>x</td></tr>") is None


# --- the read loop: exhaustive by construction, and never silently truncated --
#
# The three portals hold 220,456 priced rows. A single capped `LIMIT` swept the
# first N by `id ASC` and then logged like a clean finish, so the block it
# skipped was the NEWEST inventory — and because the whole set came back in one
# statement, the cluster's 120s statement_timeout could cancel the pass with no
# counts and no resume cursor. Both are loop shape, so both are tested here.

import logging

from scripts import backfill_unit_price_masquerade as mod


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict | None = None) -> None:
        params = params or {}
        if "GROUP BY source" in sql:
            self._rows = [("ceskereality", len(self._conn.corpus))]
        elif "FROM listings l" in sql:
            if self._conn.raise_on_select:
                raise RuntimeError("statement timeout")
            after, page = params["after"], params["page"]
            self._rows = [r for r in self._conn.corpus if r[0] > after][:page]
            self._conn.pages.append(len(self._rows))
        elif "UPDATE listings" in sql:
            self._conn.quarantined.append(params["id"])
            self._rows = []
        else:
            self._rows = []

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, corpus: list[tuple], raise_on_select: bool = False) -> None:
        self.corpus = corpus
        self.raise_on_select = raise_on_select
        self.pages: list[int] = []
        self.quarantined: list[int] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _corpus(n: int) -> list[tuple]:
    # Every other row carries the anchored marker, so the quarantine count is a
    # direct read-out of how much of the corpus the loop actually reached.
    return [
        (i, "ceskereality", str(i), "komercni", "pronajem", 900 + i, 100, 310.0,
         "100 Kč za m²/měsíc" if i % 2 else "968 Kč za měsíc")
        for i in range(1, n + 1)
    ]


def _run(monkeypatch, corpus: list[tuple], argv: list[str],
         raise_on_select: bool = False) -> tuple[_FakeConn, list[str]]:
    conn = _FakeConn(corpus, raise_on_select=raise_on_select)
    monkeypatch.setattr(mod.db, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(mod.db, "mark_properties_dirty", lambda *a, **k: None)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://x")
    monkeypatch.setattr(mod.sys, "argv", ["backfill", *argv])
    return conn, []


def test_page_size_trims_to_whatever_limit_has_left() -> None:
    assert mod.page_size(None, 199_000, 5000) == 5000
    assert mod.page_size(200_000, 199_000, 5000) == 1000
    assert mod.page_size(200_000, 200_000, 5000) == 0


def test_the_sweep_walks_past_the_batch_boundary(monkeypatch) -> None:
    conn, _ = _run(monkeypatch, _corpus(12), ["--batch-size", "5", "--write"])
    assert mod.main() == 0
    assert conn.pages == [5, 5, 2]
    assert conn.quarantined == [1, 3, 5, 7, 9, 11]


def test_a_run_capped_by_limit_says_so(monkeypatch, caplog) -> None:
    conn, _ = _run(monkeypatch, _corpus(12), ["--batch-size", "5", "--limit", "5"])
    with caplog.at_level(logging.WARNING, logger=mod.LOG.name):
        assert mod.main() == 0
    assert sum(conn.pages) == 5
    assert "BACKFILL INCOMPLETE" in caplog.text
    assert "--after 5" in caplog.text


def test_an_exhaustive_run_does_not_cry_incomplete(monkeypatch, caplog) -> None:
    _run(monkeypatch, _corpus(12), ["--batch-size", "5"])
    with caplog.at_level(logging.WARNING, logger=mod.LOG.name):
        assert mod.main() == 0
    assert "BACKFILL INCOMPLETE" not in caplog.text


def test_a_cancelled_statement_still_reports_counts_and_a_cursor(
    monkeypatch, caplog
) -> None:
    # A statement_timeout kill used to propagate before any summary line ran, so
    # the operator got a bare traceback with no counts and nothing to resume from.
    _run(monkeypatch, _corpus(12), ["--batch-size", "5"], raise_on_select=True)
    with caplog.at_level(logging.INFO, logger=mod.LOG.name):
        with pytest.raises(RuntimeError):
            mod.main()
    assert "BACKFILL done examined=0" in caplog.text
    assert "BACKFILL INCOMPLETE" in caplog.text
