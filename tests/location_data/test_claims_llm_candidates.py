"""claims_llm@2: the candidate-driven caller, tested hermetically.

No DB, no network, no provider: a scripted client answers by tool name, the obec index is
a handful of points, the gazetteer a fake. Everything here is about the caller's own rules
— which list it offers, when it retries, what it refuses — and about the answer it
assembles, which `extract_payload` (tested in test_claims_llm.py) then reads unchanged.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from location_data.claims_llm import (
    ADDRESS_PROMPT,
    ADDRESS_TOOL,
    TOWN_PROMPT,
    TOWN_TOOL,
    CallHints,
    CandidateCaller,
    FieldAnswer,
    assemble_answer,
)
from location_data.town_candidates import ObecIndex, ObecPoint

CHEB = ObecPoint(code=554481, name="Cheb", lat=50.0796, lon=12.3740)
AS = ObecPoint(code=554499, name="Aš", lat=50.2239, lon=12.1950)          # ~20 km from Cheb
PRAHA = ObecPoint(code=554782, name="Praha", lat=50.0755, lon=14.4378)
LHOTA_A = ObecPoint(code=1001, name="Lhota", lat=50.10, lon=12.40)          # near Cheb
LHOTA_B = ObecPoint(code=1002, name="Lhota", lat=49.20, lon=16.60)          # near Brno
INDEX = ObecIndex((CHEB, AS, PRAHA, LHOTA_A, LHOTA_B))

BLOCKS = {
    "title": "Prodej bytu 3+1 v Chebu",
    "description": "Byt v ulici Sokolovská 234, část Háje. Dojezd do Prahy 2 hodiny.",
}
NULL = {"value": None, "quote": None, "confidence": "low"}


class FakeGazetteer:
    """Only the two closed-list questions the caller asks; the rest is never consulted."""

    def __init__(self, parts=None, streets=None):
        self._parts = parts if parts is not None else {554481: ["Háje", "Skalka"]}
        self._streets = streets if streets is not None else {554481: ["Sokolovská", "Dlouhá"]}
        self.asked: list[tuple[str, int]] = []

    def parts_of_obec(self, obec_kod: int) -> list[str]:
        self.asked.append(("parts", obec_kod))
        return list(self._parts.get(obec_kod, []))

    def streets_of_obec(self, obec_kod: int) -> list[str]:
        self.asked.append(("streets", obec_kod))
        return list(self._streets.get(obec_kod, []))

    def name_exists(self, name_norm): return True
    def obec_codes_for_name(self, name_norm): return []
    def street_in_obec(self, obec_kod, street_norm): return True
    def address_point_exists(self, **kwargs): return True
    def obec_codes_for_psc(self, psc): return []


class ScriptedClient:
    """Answers by tool name, in order; records every call. $0.001 per call."""

    def __init__(self, **answers: list[dict[str, Any]]) -> None:
        self._queues = {name: list(items) for name, items in answers.items()}
        self.calls: list[dict[str, Any]] = []

    def call(self, *, called_for, messages, system, tools, tool_choice, model, max_tokens):
        name = tools[0]["name"]
        self.calls.append({
            "tool": name, "user": messages[0]["content"], "system": system,
            "model": model, "called_for": called_for, "tool_choice": tool_choice,
        })
        queue = self._queues.get(name) or []
        payload = queue.pop(0) if queue else {}
        return SimpleNamespace(
            tool_calls=[{"name": name, "input": json.dumps(payload, ensure_ascii=False)}],
            cost_usd=0.001, duration_ms=10, input_tokens=100, output_tokens=10,
            llm_call_id=1)


def town(obec, quote, confidence="high") -> dict[str, Any]:
    return {"obec": obec, "quote": quote, "confidence": confidence}


def env(value, quote=None, confidence="high") -> dict[str, Any]:
    return {"value": value, "quote": quote if quote is not None else value,
            "confidence": confidence}


def address(**fields: dict[str, Any]) -> dict[str, Any]:
    return {name: fields.get(name, NULL) for name in ("cast_obce", "street", "house_number")}


def caller(client, gazetteer=None, radius_km: float = 15.0) -> CandidateCaller:
    return CandidateCaller(None, model="gpt-5.6-luna", index=INDEX,
                           gazetteer=gazetteer or FakeGazetteer(), client=client,
                           radius_km=radius_km)


def _offered(user: str, heading: str) -> str:
    return user.split(heading, 1)[1]


def test_the_town_is_picked_from_the_list_and_the_address_within_it():
    client = ScriptedClient(
        pick_obec=[town("Cheb", "v Chebu")],
        record_address=[address(cast_obce=env("Háje", "část Háje"),
                                street=env("Sokolovská", "ulici Sokolovská 234"),
                                house_number=env("234", "Sokolovská 234"))])
    c = caller(client)
    answer, cost = c.answer(BLOCKS, CallHints(lat=CHEB.lat, lon=CHEB.lon, psc="350 02"))

    assert [x["tool"] for x in client.calls] == ["pick_obec", "record_address"]
    assert cost == pytest.approx(0.002)
    first = client.calls[0]
    assert first["system"] == TOWN_PROMPT and first["tool_choice"] == TOWN_TOOL["name"]
    assert first["called_for"] == "extract_location_claims"
    assert first["model"] == "gpt-5.6-luna"
    # The list: text-matched (Cheb, "do Prahy" → Praha) ∪ within 15 km of the pin (Cheb,
    # the near Lhota) — and never the postcode.
    listed = _offered(first["user"], "SEZNAM OBCÍ")
    assert "Cheb" in listed and "Praha" in listed and "Lhota" in listed
    assert "Aš" not in listed
    assert "350 02" not in first["user"] and "35002" not in first["user"]
    second = client.calls[1]
    assert second["system"] == ADDRESS_PROMPT and second["tool_choice"] == ADDRESS_TOOL["name"]
    assert "OBEC: Cheb" in second["user"]
    # Only registry names that occur in the text are offered.
    assert "Háje" in _offered(second["user"], "SEZNAM ČÁSTÍ OBCE")
    assert "Skalka" not in second["user"]
    assert "Sokolovská" in _offered(second["user"], "SEZNAM ULIC")
    assert "Dlouhá" not in second["user"]

    # Assembled: the obec's quote is in the title, the rest in the description; the fields
    # this version does not ask for are absent, never null.
    assert answer["from_title"]["obec"] == {"value": "Cheb", "quote": "v Chebu",
                                            "confidence": "high"}
    assert answer["from_description"]["obec"] == NULL
    assert answer["from_description"]["street"]["value"] == "Sokolovská"
    assert answer["from_description"]["cast_obce"]["value"] == "Háje"
    assert answer["from_description"]["house_number"]["value"] == "234"
    assert answer["from_title"]["street"] == NULL
    for field in ("psc", "landmark", "lokalita_line"):
        assert field not in answer["from_description"] and field not in answer["from_title"]
    # The picked obec's code rides along for the gazetteer gates.
    assert answer["obec_kod"] == CHEB.code
    assert c.stats == {"town_from_list": 1, "address_called": 1}


def test_a_pick_far_from_the_pin_is_refused_and_not_retried():
    """The first smoke run's defect: "ve Václavicích u Hrádku nad Nisou" — the obec
    Václavice near Benešov, 150 km from the pin, was on the list through the text and the
    model took it. The list no longer carries it; and were the national retry to name a far
    town, that is refused too rather than claimed."""
    # The list at Cheb no longer holds Praha even though the text names it.
    client = ScriptedClient(pick_obec=[town(None, None, "low"), town("Praha", "do Prahy")])
    c = caller(client)
    answer, _cost = c.answer(BLOCKS, CallHints(lat=CHEB.lat, lon=CHEB.lon))
    assert "Praha" not in _offered(client.calls[0]["user"], "SEZNAM OBCÍ")
    assert [x["tool"] for x in client.calls] == ["pick_obec", "pick_obec"]
    assert answer == {"from_description": {}, "from_title": {}}
    assert c.stats == {"town_list_abstained": 1, "town_rejected_far": 1}
    # Without a pin there is no distance to judge: a far name from the text stands.
    client = ScriptedClient(pick_obec=[town("Praha", "do Prahy")], record_address=[address()])
    answer, _cost = caller(client).answer(BLOCKS, CallHints())
    assert answer["from_description"]["obec"]["value"] == "Praha"


def test_an_out_of_list_pick_is_an_abstention_and_an_anchored_ad_gets_the_national_retry():
    client = ScriptedClient(
        pick_obec=[town("Brno", "v Chebu"), town("Cheb", "v Chebu")],
        record_address=[address()])
    c = caller(client)
    answer, cost = c.answer(BLOCKS, CallHints(lat=CHEB.lat, lon=CHEB.lon))
    assert [x["tool"] for x in client.calls] == ["pick_obec", "pick_obec", "record_address"]
    # The retry offers the NATIONAL list — every obec, Aš included.
    assert "Aš" in _offered(client.calls[1]["user"], "SEZNAM OBCÍ")
    assert answer["from_title"]["obec"]["value"] == "Cheb"
    assert c.stats == {"town_list_abstained": 1, "town_from_national": 1,
                       "address_called": 1}
    assert cost == pytest.approx(0.003)


def test_an_unanchored_abstention_gets_no_retry_and_no_address_call():
    client = ScriptedClient(pick_obec=[town(None, None, "low")])
    c = caller(client)
    answer, cost = c.answer(BLOCKS, CallHints())
    assert [x["tool"] for x in client.calls] == ["pick_obec"]
    assert answer == {"from_description": {}, "from_title": {}}
    assert c.stats == {"town_list_abstained": 1}
    assert cost == pytest.approx(0.001)


def test_no_candidates_and_no_anchor_skips_every_call():
    client = ScriptedClient()
    c = caller(client)
    answer, cost = c.answer(
        {"title": "Krásný byt", "description": "Slunný, po rekonstrukci."}, CallHints())
    assert client.calls == [] and cost == 0.0
    assert answer == {"from_description": {}, "from_title": {}}
    assert c.stats == {"skipped_no_anchor": 1}


def test_a_postcode_alone_anchors_the_national_list():
    client = ScriptedClient(pick_obec=[town("Cheb", "Krásný byt")],
                            record_address=[address()])
    c = caller(client)
    c.answer({"title": "Krásný byt", "description": "Slunný."}, CallHints(psc="350 02"))
    assert [x["tool"] for x in client.calls] == ["pick_obec", "record_address"]
    assert "Praha" in _offered(client.calls[0]["user"], "SEZNAM OBCÍ")
    assert c.stats == {"town_from_national": 1, "address_called": 1}


def test_the_registry_spelling_wins_and_a_low_confidence_pick_is_an_abstention():
    client = ScriptedClient(
        pick_obec=[town("cheb", "v Chebu")],
        record_address=[address(street=env("sokolovska", "ulici Sokolovská", "low"),
                                house_number=env("234", "Sokolovská 234", "medium"))])
    answer, _cost = caller(client).answer(BLOCKS, CallHints(lat=CHEB.lat, lon=CHEB.lon))
    assert answer["from_title"]["obec"]["value"] == "Cheb"
    assert "street" not in answer["from_description"]
    assert answer["from_description"]["house_number"]["confidence"] == "medium"


def test_an_out_of_list_street_or_part_is_dropped_not_claimed():
    client = ScriptedClient(
        pick_obec=[town("Cheb", "v Chebu")],
        record_address=[address(cast_obce=env("Skalka", "část Háje"),
                                street=env("Dlouhá", "ulici Sokolovská"))])
    answer, _cost = caller(client).answer(BLOCKS, CallHints(lat=CHEB.lat, lon=CHEB.lon))
    assert answer["from_title"]["obec"]["value"] == "Cheb"
    assert "street" not in answer["from_description"]
    assert "cast_obce" not in answer["from_description"]


def test_a_homonymous_town_without_a_pin_keeps_the_town_and_offers_empty_lists():
    gazetteer = FakeGazetteer()
    client = ScriptedClient(pick_obec=[town("Lhota", "v Lhotě")],
                            record_address=[address(house_number=env("12", "č.p. 12"))])
    blocks = {"title": "Chalupa v Lhotě", "description": "Chalupa č.p. 12 na samotě."}
    c = caller(client, gazetteer)
    answer, _cost = c.answer(blocks, CallHints(psc="350 02"))
    assert answer["from_title"]["obec"]["value"] == "Lhota"
    assert gazetteer.asked == []
    assert "(prázdný)" in client.calls[1]["user"]
    assert answer["from_description"]["house_number"]["value"] == "12"
    assert c.stats == {"town_from_list": 1, "obec_ambiguous": 1, "address_called": 1}


def test_with_a_pin_the_nearer_homonym_supplies_the_lists():
    gazetteer = FakeGazetteer(parts={1001: ["Dolní Lhota"]}, streets={1001: ["Hlavní"]})
    client = ScriptedClient(pick_obec=[town("Lhota", "v Lhotě")],
                            record_address=[address(street=env("Hlavní", "Hlavní 5"),
                                                    house_number=env("5", "Hlavní 5"))])
    blocks = {"title": "Dům v Lhotě", "description": "Dům na ulici Hlavní 5."}
    answer, _cost = caller(client, gazetteer).answer(
        blocks, CallHints(lat=CHEB.lat, lon=CHEB.lon))
    assert gazetteer.asked == [("parts", 1001), ("streets", 1001)]
    assert "Hlavní" in _offered(client.calls[1]["user"], "SEZNAM ULIC")
    assert answer["from_description"]["street"]["value"] == "Hlavní"


def test_a_model_that_skips_the_forced_tool_reads_as_an_abstention():
    class NoTool:
        def call(self, **kwargs):
            return SimpleNamespace(tool_calls=[], cost_usd=0.002)

    c = caller(NoTool())
    answer, cost = c.answer(BLOCKS, CallHints())
    assert answer == {"from_description": {}, "from_title": {}}
    assert cost == pytest.approx(0.002)
    assert c.stats == {"town_list_abstained": 1}


def test_assemble_answer_places_each_quote_description_first_and_omits_the_rest():
    blocks = {"title": "Byt v Chebu", "description": "Byt v Chebu, ulice Dlouhá 5."}
    picks = {
        "obec": FieldAnswer("Cheb", "v Chebu", "high"),
        "street": FieldAnswer("Dlouhá", "ulice Dlouhá 5", "medium"),
        "house_number": FieldAnswer("5", "nikde", "high"),
    }
    out = assemble_answer(blocks, picks)
    # A quote present in both blocks lands in the description: the lane's own ladder.
    assert out["from_description"]["obec"]["value"] == "Cheb"
    assert out["from_title"]["obec"] == NULL
    assert out["from_description"]["street"]["confidence"] == "medium"
    # An unlocatable quote stays visible (the extraction records it), never dropped here.
    assert out["from_description"]["house_number"]["quote"] == "nikde"
    assert "cast_obce" not in out["from_description"] and "cast_obce" not in out["from_title"]
    assert assemble_answer(blocks, {}) == {"from_description": {}, "from_title": {}}


@pytest.mark.parametrize("hints, expected", [
    (CallHints(), False),
    (CallHints(lat=50.0, lon=12.0), True),
    (CallHints(lat=50.0), False),
    (CallHints(psc="350 02"), True),
    (CallHints(psc="9876"), False),
])
def test_anchored_means_a_pin_or_a_five_digit_postcode(hints, expected):
    assert hints.anchored is expected
