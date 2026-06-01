"""Hermetic tests for the remax client URL builders (no network)."""

from __future__ import annotations

from scraper.remax_client import detail_url, index_url


def test_index_url_sale_and_pagination():
    # Page 1 of an agenda is the bare filtered URL (no stranka).
    assert index_url(sale=1) == "https://www.remax-czech.cz/reality/vyhledavani/?sale=1"
    assert index_url(sale=2) == "https://www.remax-czech.cz/reality/vyhledavani/?sale=2"
    assert index_url(sale=1, stranka=1) == "https://www.remax-czech.cz/reality/vyhledavani/?sale=1"
    # Page >= 2 appends stranka.
    assert (
        index_url(sale=1, stranka=3)
        == "https://www.remax-czech.cz/reality/vyhledavani/?sale=1&stranka=3"
    )
    assert (
        index_url(sale=2, stranka=2)
        == "https://www.remax-czech.cz/reality/vyhledavani/?sale=2&stranka=2"
    )
    # No params -> the bare search index.
    assert index_url() == "https://www.remax-czech.cz/reality/vyhledavani/"


def test_detail_url_forms():
    full = "https://www.remax-czech.cz/reality/detail/440872/prodej-bytu"
    assert detail_url(full) == full
    assert detail_url("/reality/detail/440872/prodej-bytu") == full
    assert detail_url("440872") == "https://www.remax-czech.cz/reality/detail/440872/"
