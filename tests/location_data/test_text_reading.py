"""The plain-text location reading (location reader W1): the block the text lane asks for,
and `read_location`'s text-only checks — V1 (the quote is in the advert), V3 (an offer)."""

from __future__ import annotations

from typing import Any

from location_data import text_reading as tr
from scraper.bazos_parser import ad_haystack
from toolkit import description_extraction as tx

# Listing 18909736, the trigger: street, part and town stated, no house number anywhere.
TRIGGER = ad_haystack(
    "Prodej bytu 3+1 60.91 m² Štefánikova, Hradec Králové",
    "Byt 3+1 se nachází ve 4. patře domu v ulici Štefánikova v klidné části Třebše v Hradci "
    "Králové.")
EMPTY = {"value": None, "quote": None}


def _payload(kind: str, **slots: tuple[str, str | None]) -> dict[str, Any]:
    block = {s: dict(zip(("value", "evidence_quote"), slots.get(s, (None, None))))
             for s in tr.SLOTS}
    return {"location": {"ad_kind": {"value": kind, "evidence_quote": None}, **block}}


def test_the_block_is_every_slot_quoted_with_a_closed_ad_kind_and_a_tight_prompt() -> None:
    block = tx.extraction_tool(())["input_schema"]["properties"]["location"]
    assert list(block["properties"]) == ["ad_kind", *tr.SLOTS] == block["required"]
    assert block["properties"]["ad_kind"]["properties"]["value"]["enum"] == list(tr.AD_KINDS)
    for slot in tr.SLOTS:
        assert block["properties"][slot]["required"] == ["value", "evidence_quote"]
        assert block["properties"][slot]["properties"]["value"]["type"] == ["string", "null"]
    assert tr.LOCATION_PROMPT in tx._SYSTEM_PROMPT
    assert len(tr.LOCATION_PROMPT.splitlines()) <= 12


def test_the_trigger_advert_reads_street_town_and_part_and_no_number() -> None:
    out = tr.read_location(_payload(
        "offer", street=("Štefánikova", "v ulici Štefánikova"),
        town=("Hradec Králové", "v Hradci Králové"), part_of_town=("Třebeš", "části Třebše"),
    ), TRIGGER)
    assert out["ad_kind"]["value"] == "offer"
    assert out["street"] == {"value": "Štefánikova", "quote": "v ulici Štefánikova"}
    assert (out["town"]["value"], out["part_of_town"]["value"]) == ("Hradec Králové", "Třebeš")
    assert out["house_number_cp"] == out["house_number_co"] == out["house_number_ev"] == EMPTY


def test_an_exchange_advert_keeps_the_property_it_offers() -> None:
    """final-plan D3: offer and exchange emit claims; the prompt asks for the OFFERED one."""
    text = ad_haystack("Vyměním byt 2+1 Zlín", "Můj byt v ulici Dlouhá, hledám Sadovou.")
    out = tr.read_location(_payload("exchange", street=("Dlouhá", "v ulici Dlouhá")), text)
    assert out["ad_kind"]["value"] == "exchange"
    assert out["street"] == {"value": "Dlouhá", "quote": "v ulici Dlouhá"}


def test_v3_a_wanted_or_non_property_advert_yields_no_location_whatever_was_read() -> None:
    text = ad_haystack("Koupím byt", "Koupím byt v ulici Sadová ve Zlíně.")
    for kind in ("wanted", "not_property", "garbage"):
        out = tr.read_location(_payload(kind, street=("Sadová", "v ulici Sadová")), text)
        assert all(out[slot] == EMPTY for slot in tr.SLOTS), kind
    assert out["ad_kind"] == EMPTY


def test_v1_a_quote_that_is_not_in_the_advert_drops_that_slot_only() -> None:
    out = tr.read_location(_payload(
        "offer", street=("Masarykova", "na Masarykově náměstí"),
        town=("Hradec Králové", "v Hradci Králové"), house_number_cp=("12", None)), TRIGGER)
    assert out["street"] == out["house_number_cp"] == EMPTY
    assert out["town"]["value"] == "Hradec Králové"
    for payload in (None, {}, {"location": "x"}):
        assert all(c["value"] is None for c in tr.read_location(payload, TRIGGER).values())
