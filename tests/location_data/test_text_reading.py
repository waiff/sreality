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
    assert list(block["properties"]) == ["ad_kind", "country", *tr.SLOTS] == block["required"]
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


def test_v3_a_property_placed_abroad_admits_no_slot_whatever_the_language() -> None:
    """Pilot run 36679622378: a Croatian apartment ('Chorvátsko, ostrov Vir') and a Slovak
    cottage were answered with a town; 'Vir' folds onto the Czech obec Vír. The register is
    Czech, so a reading that places the property abroad admits nothing but the country."""
    text = ad_haystack("Apartmán Chorvátsko, ostrov Vir", "Ponúkam apartmán v časti Žitna.")
    payload = _payload("offer", town=("Vir", "ostrov Vir"), part_of_town=("Žitna", "v časti Žitna"))
    payload["location"]["country"] = {"value": "hr", "evidence_quote": "Chorvátsko"}
    out = tr.read_location(payload, text)
    assert out["country"] == {"value": "HR", "quote": "Chorvátsko"}
    assert all(out[slot] == EMPTY for slot in tr.SLOTS)
    payload["location"]["country"] = {"value": "CZ", "evidence_quote": None}
    assert tr.read_location(payload, text)["town"]["value"] == "Vir"


def test_v1_a_quote_that_is_not_in_the_advert_drops_that_slot_only() -> None:
    out = tr.read_location(_payload(
        "offer", street=("Masarykova", "na Masarykově náměstí"),
        town=("Hradec Králové", "v Hradci Králové"), house_number_cp=("12", None)), TRIGGER)
    assert out["street"] == out["house_number_cp"] == EMPTY
    assert out["town"]["value"] == "Hradec Králové"
    for payload in (None, {}, {"location": "x"}):
        assert all(c["value"] is None for c in tr.read_location(payload, TRIGGER).values())


# ------------------------------------------------ the claim reader (W3): V2, V4, typed numbers

def _claims(payload: dict[str, Any], text: str) -> dict[str, str]:
    """The bazos@8 reading entries over one reading, as the claim lane runs them."""
    from location_data.claims_intake import READERS, reading_entries
    from tests.location_data import claim_intake_fixtures as fx

    row, reading = fx.listing("bazos", {}), tr.Reading(1, payload, text)
    return {c.claim_type: c.value_text for e in reading_entries(fx.entries_for("bazos"))
            for c in READERS[str(e.reader)].fn(e, row, reading)}


def test_the_trigger_reading_becomes_its_town_part_and_street_claims() -> None:
    claims = _claims(_payload(
        "offer", street=("Štefánikova", "v ulici Štefánikova"),
        town=("Hradec Králové", "v Hradci Králové"), part_of_town=("Třebeš", "části Třebše"),
    ), TRIGGER)
    assert claims == {"obec_name": "Hradec Králové", "cast_obce_name": "Třebeš",
                      "street_name": "Štefánikova"}


def test_v2_a_value_must_be_grounded_in_its_own_quote() -> None:
    """A value word of 3+ letters shares its first three with a quote word, after the lane's
    fold plus ů->o — so inflection passes and an unrelated quote does not."""
    for value, quote in (("Praha", "v Praze"), ("Brno", "v Brně"), ("Plzeň", "v Plzni"),
                         ("Hora", "na Hoře"), ("Dvůr Králové", "ve Dvoře Králové"),
                         ("Sochorova", "ul. A.Sochora"), ("Nový Jičín", "v Novém Jičíně"),
                         ("Aš", "Aš")):
        assert tr._grounded(value, quote), (value, quote)
    for value, quote in (("Masarykova", "v centru města"), ("Aš", "v Aši")):
        assert not tr._grounded(value, quote), (value, quote)


def test_v4_a_number_needs_its_marker_or_the_street_form() -> None:
    cp, co, ev = "house_number_cp", "house_number_co", "house_number_ev"
    for slot, value, quote in ((cp, "12", "č.p. 12"), (cp, "12", "čp.12"),
                               (cp, "12", "Husova 12/4"), (cp, "13", "Hodoviz 13"),
                               (co, "4", "Husova 12/4"), (co, "4", "č.o. 4"),
                               (ev, "13", "chata č.ev. 13")):
        assert tr._numbered(slot, value, quote), (slot, quote)
    for slot, value, quote in ((cp, "12", "č. 12"), (cp, "60", "60.91 m²"),
                               (cp, "12", "parc. č. 12"), (cp, "12", "LV 12"),
                               (cp, "487", "bez č.p. 487"), (co, "4", "Husova 4"),
                               (ev, "13", "chata 13"), (cp, "12a", "č.p. 12a")):
        assert not tr._numbered(slot, value, quote), (slot, quote)


def test_a_cottages_ev_rides_the_cp_entry_marked_and_the_resolver_types_it() -> None:
    from location_data.resolver.normalize import TYP_EV, house_number, normalize_house_number

    text = ad_haystack("Prodej chaty Moravské Prusy", "Chata č.ev. 13, 40 m², č.p. neuvedeno.")
    claims = _claims(_payload("offer", house_number_ev=("13", "Chata č.ev. 13"),
                              house_number_co=("40", "40 m²")), text)
    assert claims == {"house_number_cp": "č.ev. 13"}
    assert house_number(normalize_house_number(claims["house_number_cp"])) == (13, TYP_EV)
    claims = _claims(_payload("offer", house_number_cp=("5", "č.p. 5"),
                              house_number_ev=("13", "Chata č.ev. 13")), text + " č.p. 5")
    assert claims == {"house_number_cp": "5"}
