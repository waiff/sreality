"""S14's readings (E293-E296), each built from the adverts out of cohort 16 that named it.

S14 is a TIGHTENING-ONLY wave (D82): every reading adds a distinguishing fact and none removes
one. Jestřabice is E293: the bažoš row of one seller's two-lot template prints its plot only in
the portal's attribute block (`celková plocha (m2): 312`), so E192/E282 read it as stating no
area and let the 257 m² lot's price path take it. Kovářov is E294/E294b: the attic unit's
`balkon o rozloze 12,6 m2` at 7,590,000 against the terrace unit's `terasu o rozloze 58 m2` at
10,665,000. Lipno-Kobylnice and Polná are E295 (`levou polovinu` / `pravou stranu`, `druhá` /
`čtvrtá zleva`), and the Lipno Forest villas E296 (`VILA ARKTIDA` / `VILA LOUKA REZIDENCE A3`),
a reader that is kept but REFUSED (off in every table, D84).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autodedup.d43 import ClusterRelation
from autodedup.dataset import Listing
from autodedup.indistinguishable import CLUSTER, GATE, PROMOTE, distinguishing_facts, effective_area
from autodedup.settings import Settings
from autodedup.text_facts import (
    block_plot_area, named_villa_units, outdoor_accessory_areas, position_designators,
    residence_codes,
)

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup/settings"
S13 = Settings.from_json(SETTINGS / "w28.json")
S14 = Settings.from_json(SETTINGS / "w29.json")

W29_DIALS = {"d43_block_plot_area", "d43_outdoor_accessory_area", "d43_outdoor_accessory_price",
             "d43_position_designator", "d43_named_villa"}
# E296 is REFUSED with numbers (D84): the reader stays in code, off in every table.
W29_REFUSED = {"d43_named_villa"}
W29_ON = W29_DIALS - W29_REFUSED
S14_E296 = Settings.from_dict({**S14.to_dict(), "d43_named_villa": True})


def variant(**kwargs: object) -> Settings:
    raw = json.loads((SETTINGS / "w28.json").read_text(encoding="utf-8"))
    raw.update(kwargs)
    return Settings.from_dict(raw)


def stamp(day: int) -> str:
    return (datetime(2026, 5, 1, tzinfo=timezone.utc) + timedelta(days=day)).isoformat()


def advert(listing_id: int, source: str, body: str, first: int, last: int, **kwargs: object
           ) -> Listing:
    price = kwargs.get("price")
    fields: dict[str, object] = {
        "source": source, "category_main": "byt", "category_type": "prodej",
        "description": body, "first_seen_at": stamp(first), "last_seen_at": stamp(last),
        "price_history": [[stamp(first), price]] if price else [],
        "location": {"obec_kod": 588601, "granularity": "obec", "granularity_rank": 40},
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def names(a: Listing, b: Listing, cfg: Settings, mode: str = CLUSTER) -> list[str]:
    return [fact.name for fact in distinguishing_facts(a, b, None, cfg, mode)]


# --- E293: Jestřabice, the plot the bažoš attribute block prints --------------------------------
JESTRABICE = (
    "Nabízíme k prodeji pozemek v klidné obci Jestřabice, vedený jako trvalý travní porost. Místo "
    "nabízí příjemné soukromí, klid a možnost využití pro rekreaci, odpočinek, zahradničení či "
    "jako investici do budoucna. Pozemek se nachází v příjemném prostředí obklopeném přírodou a "
    "je ideální pro všechny, kteří hledají únik od ruchu města. Díky své poloze nabízí kombinaci "
    "dostupnosti a klidného venkovského prostředí. Pokud hledáte místo pro relax, zahrádku nebo "
    "pozemek s potenciálem do budoucna, neváhejte mě kontaktovat pro více informací či osobní "
    "prohlídku."
)
BLOCK = " ev.č.: 8483 umístění objektu: Klidná část obce celková plocha (m2): 312"


def lot(listing_id: int, source: str, area: float | None, price: float, first: int, last: int,
        body: str = JESTRABICE) -> Listing:
    return advert(listing_id, source, body, first, last, category_main="pozemek",
                  subtype="zahrada", area_m2=area, price=price)


def test_E293_the_block_prints_the_plot() -> None:
    assert block_plot_area(JESTRABICE + BLOCK) == 312.0
    assert block_plot_area(JESTRABICE) is None
    two = JESTRABICE + " celková plocha (m2): 312 plocha parcely (m2): 257"
    assert block_plot_area(two) is None


def test_E293_the_bazos_row_is_not_area_silent() -> None:
    # 290396 sreality 257 m2 @101,900 (06-12..08-17) against bažoš 18794635 @124,400 (09-09..).
    a = lot(290396, "sreality", 257.0, 101900.0, 42, 108)
    b = lot(18794635, "bazos", None, 124400.0, 131, 146, body=JESTRABICE + BLOCK)
    assert names(a, b, S13) == []
    assert "area" in names(a, b, S14)
    assert "price" in names(a, b, S14)
    assert "area" in names(a, b, S14, PROMOTE)


def test_E293_the_lot_whose_plot_it_prints_stays_open_to_it() -> None:
    a = lot(290423, "sreality", 312.0, 124400.0, 42, 146)
    b = lot(18794635, "bazos", None, 124400.0, 131, 146, body=JESTRABICE + BLOCK)
    assert names(a, b, S14) == []


def test_E293_only_fills_an_empty_LAND_column() -> None:
    filled = lot(1, "bazos", 300.0, 124400.0, 0, 5, body=JESTRABICE + BLOCK)
    assert effective_area(filled, S14) == 300.0
    flat = advert(2, "bazos", "Byt 2+kk celková plocha (m2): 55", 0, 5)
    assert effective_area(flat, S14) is None
    empty = lot(3, "bazos", None, 124400.0, 0, 5, body=JESTRABICE + BLOCK)
    assert effective_area(empty, S13) is None
    assert effective_area(empty, S14) == 312.0


# --- E294 / E294b: Kovářov, the balcony against the terrace ------------------------------------
KOVAROV = (
    "Představujeme vám tento exkluzivní apartmán v první linii v Kovářově s velkorysou terasou a "
    "podmanivým výhledem na Lipenskou přehradu. Apartmán o dispozici 2+kk s vnitřní plochou 58 m2 "
    "je součástí nově dokončeného objektu v unikátní lokalitě vyhlídka Kovářov západ. {unit} "
    "jednotka je promyšleně navržena a sestává ze zádveří, moderní koupelny s WC a prostorného "
    "obývacího pokoje s kuchyňským koutem. Hlavní dominantou je přímý vstup na {what}, která "
    "nabízí dechberoucí panoramatický výhled na vodní hladinu."
)
ATTIC = KOVAROV.format(unit="Podkrovní", what="balkon o rozloze 12,6 m2")
TERRACE = KOVAROV.format(unit="Tato", what="terasu o rozloze 58 m2")


def kovarov(listing_id: int, source: str, body: str, price: float, first: int, last: int
            ) -> Listing:
    return advert(listing_id, source, body, first, last, disposition="2+kk", area_m2=58.0,
                  price=price, category_main="byt")


def test_E294_reads_the_accessory_size_not_the_flat() -> None:
    assert outdoor_accessory_areas(ATTIC) == frozenset({("balcony", 12.6, 1)})
    assert outdoor_accessory_areas(TERRACE) == frozenset({("terrace", 58.0, 0)})
    assert outdoor_accessory_areas("byt 3+1 s balkonem 60m2 1.podlaží") == frozenset()
    assert outdoor_accessory_areas("zastavěnou plochou s terasou 175 m2.") == frozenset()
    assert outdoor_accessory_areas("slunný byt 1+1 s balkonem o celkové výměře 26,4m2.") == frozenset()
    assert outdoor_accessory_areas("užitná plocha cca 150–179 m2 vlastní zahrada") == frozenset()


def test_E294_a_list_of_accessories_abstains() -> None:
    # Hradec Králové 1+1: `dvě lodžie o celkové ploše 2,60 m2 a 1,65 m2` against `4,25 m2`.
    body = "velkou výhodou jsou dvě lodžie o celkové ploše 2,60 m2 a 1,65 m2. byt je"
    assert outdoor_accessory_areas(body) == frozenset()


def test_E294_two_units_live_together_on_one_portal() -> None:
    a = kovarov(444728, "ceskereality", ATTIC, 7590000.0, 60, 73)
    b = kovarov(444729, "ceskereality", TERRACE, 10665000.0, 60, 73)
    assert names(a, b, S13) == []
    got = names(a, b, S14)
    assert "outdoor_accessory" in got and "accessory_price" in got


def test_E294b_the_price_is_not_one_path_across_the_re_post_boundary() -> None:
    # 417800 (06-29..06-30) against 444729 (06-30..07-13): never on sale together.
    a = kovarov(417800, "ceskereality", ATTIC, 7590000.0, 59, 60)
    b = kovarov(444729, "ceskereality", TERRACE, 10665000.0, 61, 73)
    assert names(a, b, S13) == []
    assert names(a, b, S14) == ["accessory_price"]


def test_E294_two_authors_of_one_flat_are_not_read_across_portals() -> None:
    # HK Jana Masaryka 1+kk, one 3,799,999: `lodžii o ploše 3 m2` (agency) / `balkon o rozloze 4 m2`.
    body_a = "Byt je v dobrém stavu a disponuje lodžií o ploše 3 m2, která poskytuje prostor."
    body_b = "K bytu náleží také balkon o rozloze 4 m2, ideální pro ranní kávu nebo večerní relax."
    a = advert(497430, "sreality", body_a, 64, 131, area_m2=35.0, price=3799999.0)
    b = advert(15261751, "bezrealitky", body_b, 102, 134, area_m2=35.0, price=3799999.0)
    assert "outdoor_accessory" not in names(a, b, S14)
    assert "accessory_price" not in names(a, b, S14)


def test_E294_a_house_garden_is_the_plot_not_an_accessory() -> None:
    # Úvaly 4+1: `se zahradou na pozemku 312 m2` @15,490,000 against `o výměře 231 m2` @14,300,000.
    a = advert(36228, "sreality", "Dům 4+1 se zahradou na pozemku o velikosti 312 m2.", 8, 27,
               category_main="dum", area_m2=124.0, price=15490000.0)
    b = advert(186280, "bazos", "Dům 4+1 se zahradou o výměře 231 m2 v klidné ulici.", 34, 87,
               category_main="dum", area_m2=124.0, price=14300000.0)
    assert "accessory_price" not in names(a, b, S14)


# --- E295: Lipno-Kobylnice and Polná ---------------------------------------------------------
LIPNO = ("V Lipně nad Vltavou, v části Kobylnice, nabízíme {half} novostavby apartmánového domu se "
         "dvěma samostatnými byty, která je připravena k okamžitému provozu ubytování.")
POLNA = ("Obchodní prostor o výměře 153 m² umístěný v jednopodlažní budově s 5 obchodními "
         "jednotkami různých velikostí od 144 do 340 m². Jednotka je {pos} zleva při čelním "
         "pohledu.")


def test_E295_reads_the_half_and_the_position() -> None:
    assert position_designators(LIPNO.format(half="levou polovinu")) == {"side": frozenset({"L"})}
    assert position_designators(LIPNO.format(half="pravou stranu")) == {"side": frozenset({"R"})}
    assert position_designators(POLNA.format(pos="druhá")) == {"pos:zleva": frozenset({"2"})}
    assert position_designators("Dům tvoří pravou polovinu dvojdomu.") == {"side": frozenset({"R"})}


def test_E295_a_room_inside_one_unit_is_not_a_side() -> None:
    assert position_designators("Po pravé straně chodby je koupelna, v levé části bytu ložnice.") == {}
    assert position_designators("V případě zájmu prosíme, aby první zpráva obsahovala údaje.") == {}


def test_E295_the_part_letter_is_a_capital_not_the_conjunction() -> None:
    assert position_designators("Prodej objektu, část B, přízemí.") == {"part": frozenset({"B"})}
    assert position_designators("Obytná část a kuchyně jsou propojeny, denní část a noční.") == {}
    assert position_designators("Nabízíme část A i část B areálu.") == {}


def test_E295_a_body_naming_both_sides_is_a_roster() -> None:
    body = ("Dispozice: levá část hlavní budovy 1. NP kanceláře, pravá část hlavní budovy byt 2+1. "
            "Nabízíme levou polovinu domu i pravou polovinu domu.")
    assert "side" not in position_designators(body)


def test_E295_two_halves_of_one_house_are_two_units() -> None:
    a = advert(550158, "idnes", LIPNO.format(half="levou polovinu"), 77, 146,
               category_main="komercni", area_m2=113.0, price=13990000.0)
    b = advert(550159, "idnes", LIPNO.format(half="pravou stranu"), 77, 146,
               category_main="komercni", area_m2=113.0, price=13990000.0)
    assert names(a, b, S13) == []
    assert names(a, b, S14) == ["position_designator"]
    assert names(a, b, S14, PROMOTE) == ["position_designator"]


def test_E295_three_units_of_one_row() -> None:
    units = [advert(i, "sreality", POLNA.format(pos=p), 17, 146, category_main="komercni",
                    area_m2=153.0, price=9865000.0)
             for i, p in ((62329, "druhá"), (62331, "čtvrtá"), (62332, "pátá"))]
    relation = ClusterRelation({u.id: u for u in units}, {}, S14)
    assert not relation.ok(62329, 62331) and not relation.ok(62331, 62332)
    assert ClusterRelation({u.id: u for u in units}, {}, S13).ok(62329, 62331)
    same = advert(206540, "bazos", POLNA.format(pos="druhá"), 35, 36, category_main="komercni",
                  area_m2=153.0, price=9865000.0)
    assert names(units[0], same, S14) == []


# --- E296: Lipno Forest Residences ----------------------------------------------------------
def test_E296_the_named_villa_and_the_residence_code() -> None:
    assert named_villa_units("VILA LOUKA REZIDENCE A3 DELUXE & VIEW") == frozenset({"louka/A3"})
    assert residence_codes("Rezidence A3 nabízí stejnou architekturu jako A2.") == frozenset({"A3"})
    arktida = advert(13410970, "idnes", "VILA ARKTIDA REZIDENCE A3 DELUXE. Rezidence A3 nabízí "
                     "stejnou promyšlenou architekturu jako A2.", 95, 146, area_m2=86.0)
    louka = advert(13666639, "idnes", "VILA LOUKA REZIDENCE A3 DELUXE. Rezidence A3 nabízí "
                   "stejnou promyšlenou architekturu jako A2.", 96, 146, area_m2=86.0)
    a2 = advert(13666413, "sreality", "Rezidence A2 pracuje s prostorem stejně přirozeně.", 96, 146,
                area_m2=86.0)
    twin = advert(13667012, "sreality", "Rezidence A3 nabízí stejnou promyšlenou architekturu "
                  "jako A2.", 96, 146, area_m2=86.0)
    assert names(arktida, louka, S13) == [] and names(arktida, louka, S14_E296) == ["named_villa"]
    assert names(a2, twin, S14_E296) == ["named_villa"]
    assert names(arktida, twin, S14_E296) == []
    # Refused: the sreality twins print no villa, so the fact strands each idnes advert from its
    # own sreality copy and frees the other villa's twins to take it (cohort 16, D84).
    assert names(arktida, louka, S14) == []


# --- the table itself ------------------------------------------------------------------------
def test_every_W29_dial_is_off_by_default() -> None:
    default = Settings()
    assert not any(getattr(default, dial) for dial in W29_DIALS)
    assert not any(getattr(S13, dial) for dial in W29_DIALS)


def test_w29_differs_from_w28_only_in_the_W29_dials() -> None:
    w28, w29 = S13.to_dict(), S14.to_dict()
    assert {key for key in w29 if w28.get(key) != w29[key]} == W29_ON
    assert all(getattr(S14, dial) for dial in W29_ON)
    assert not any(getattr(S14, dial) for dial in W29_REFUSED)


def test_the_w29_holds_are_w29_plus_one_table() -> None:
    w29 = S14.to_dict()
    for name, table in (("w29_rentals_hold", {"pronajem|*": "propose"}),
                        ("w29_land_hold", {"*|pozemek": "propose"})):
        hold = Settings.from_json(SETTINGS / f"{name}.json").to_dict()
        assert {key for key in hold if w29.get(key) != hold[key]} == {"merge_policy"}
        assert hold["merge_policy"] == table


def test_the_gate_reads_the_new_facts_too() -> None:
    a = advert(550158, "idnes", LIPNO.format(half="levou polovinu"), 77, 146,
               category_main="komercni", area_m2=113.0, price=13990000.0)
    b = advert(550259, "ceskereality", LIPNO.format(half="pravou stranu"), 77, 124,
               category_main="komercni", area_m2=113.0, price=14990000.0)
    assert "position_designator" in names(a, b, S14, GATE)


# Replay parity: every earlier settings file, w13..w28 and every hold, byte for byte.
EARLIER_DIGESTS = {
    "w13.json": "ac3e4b9a912353ff978f40e8ac2132ea87cf357aa278c5b769c6a8ad0e9a6018",
    "w13_strata.json": "ae4d2c6930168c496398232749e0c1dd8bedaf3189da3395d3749eb427e935c8",
    "w14.json": "0e7081e6d52d338d7783770254dc7d953ead3c72f605d9d4df13db51060b6f76",
    "w15.json": "4f9b6c25a39feca617c915345fb8691f0a2bf955c426c252cbe769c1ef05e16d",
    "w16_l.json": "bbad5441f078162e08bc499af98b58eea35b7fd851ab3654f5e6e1bb5f798c6b",
    "w16_m.json": "4b10faeebee4fb4a3d1d869e70d4b2ae75a856f00b0ba385aebfa2b99dd193cc",
    "w16_s.json": "0725dc3ae28a381117b489d8669b4c2d2f5748cc35669847bff26a914a514cb0",
    "w17.json": "e35d06197ea40e062139464b411dc7de50a0f04adf541e8e73a1022468c8b55e",
    "w18.json": "48a15b2e840ab6d589f7a79e8a879ec5d1e88b4707bea90d95764e8c158511f9",
    "w19.json": "2d0b5cd410146fb5cfb7589320b08757f064f43fe51ac3cd4ee46427d96e991c",
    "w20.json": "603cc3ae6e441a16def4b74ce7a3327a6ed747009a46df0bf72576aec2d1313e",
    "w21.json": "a51155e68073f1ca68470a5bc701a51e79dc623c44c2b7c396d0dd34d267b2e9",
    "w22.json": "4551fa5d6c8c23b2c312cef8c582c1098e029336a9b319b5c6b672e9d0f5675d",
    "w22_rentals_hold.json": "215ea4d535c28b258b588bf3d21adc4be525fa26852dfcf23c3b347380658e49",
    "w23.json": "ebb763f863b06f9b6353b34bed60e791e35b34c448ae5d1b504bbdd5b50b593b",
    "w23_rentals_hold.json": "43330dc68839350374eeb52eb6dcfc8ec17c016c6382043101aa2a9737017331",
    "w24.json": "161a390e357c7e8620f72bdd25050e4e4944379e9af2045315cf82d5af30150a",
    "w24_rentals_hold.json": "12aba47e34ac5794af7a06c82ce51929351071d5176c23fe6ef38e9d563a1d9e",
    "w25.json": "1f710c54a29627923bbf78c62bf108099615f3934e42f5bed38cf1a8d24fd06c",
    "w25_rentals_hold.json": "7e2831a5021b8c4bddf1a1c9619c4ba5c58cf81bf33dda340370816a32999744",
    "w26.json": "b889f3277cd7a298b3331192f3bb7bf86c83a4d323a3fc013ca793f9887ff24f",
    "w26_rentals_hold.json": "35a4abb6ad2cea1ff13609a6ae1eba4b8f148adcc8f129082233306968e93626",
    "w27.json": "2d1435e4f4489c0989bc4dff8fa4510e9f406fae38e161c66d3f1991785fef45",
    "w27_land_hold.json": "21910cbcd02a141de012bd80b5b9951e132a219f51995181b694af5c175a2cf9",
    "w27_rentals_hold.json": "af33cc82a740295d1d2242322bce0787fd32af6558d2eb291cfc19fea3656369",
    "w28.json": "9c9912913f38156a9045db364217823ab80808efc3c8e62725620f373afa705c",
    "w28_land_hold.json": "b62f3e6f11c3eaf5d0f0c16fe61c5b49f8c5ec89fbf77d904b0319bfe7d45d0b",
    "w28_rentals_hold.json": "f85e16bc183a87cbc7e364e523a0c1e4ed9ed06edea38fc7e0dcf3982c6f02b1",
}


def test_every_earlier_settings_file_is_byte_identical() -> None:
    for name, digest in EARLIER_DIGESTS.items():
        assert hashlib.sha256((SETTINGS / name).read_bytes()).hexdigest() == digest, name
