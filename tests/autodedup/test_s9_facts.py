"""S9's readings (E240, E241, E242, E243), each built from the advert that named it.

Every case is a real pair out of the eleven cohorts. The mode S9 was written for is ONE
letting agency's flats at different house numbers of one street, grouped at street grain:
Kapitána Jasioka in Havířov (735/48 ground floor, no balcony, against 734/46 first floor with
a large balcony, both at 8,150), Slovenského národního povstání (657/1a against 655/1c, two
bodies with no shared sentence), Resslova (509/10 at 13,100 against 512/4 at 13,550) and
náměstí Jiřího z Poděbrad in Praha-Vinohrady (1561/9 unfurnished against 1554/6 with a
walk-in closet). None of those bodies prints its number, so E242 reads the stored column.

The bodies that DO print one are Ostrava's: `na ulici Krasnoarmejců 2080/8` against `na ulici
Krasnoarmejců 2079/10`, and `Veverkova 1514/7` against `Veverkova 1512/3` — two flats each
time. Against them stand the three shapes a printed number must refuse, all of them real:
the corner building that prints both its streets (`Svitavská 29/Vranovská 49` in Brno,
`Měděná 3061/4` re-posted on bazos as `Železná 3068/20`), the body re-posted with its address
corrected (`Hybešova 2090/30` to `2091/30a` in Kuřim, otherwise the same text), and the
Freyova 1+kk the resolver filed at 236/5 and then 235/7 while BOTH bodies print `Freyova
5/236` — which is E241's veto.
"""

from __future__ import annotations

import json
from pathlib import Path

from autodedup.dataset import Listing
from autodedup.indistinguishable import (
    CLUSTER, GATE, distinguishing_facts, offered_plan_space)
from autodedup.settings import Settings
from autodedup.text_facts import (
    plan_headline_area, priced_letting_plan, printed_house_numbers,
    printed_house_numbers_meet)

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup/settings"
S8 = Settings.from_json(SETTINGS / "w23.json")
S9 = Settings.from_json(SETTINGS / "w24.json")


def variant(**kwargs: object) -> Settings:
    raw = json.loads((SETTINGS / "w24.json").read_text(encoding="utf-8"))
    raw.update(kwargs)
    return Settings.from_dict(raw)


def stamp(day: int) -> str:
    return f"2026-0{1 + day // 28}-{1 + day % 28:02d}T00:00:00+00:00"


def listing(listing_id: int, street: str = "kapitana jasioka", number: str | None = None,
            first: int = 0, last: int = 40, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": "bezrealitky",
        "category_main": "byt",
        "category_type": "pronajem",
        "first_seen_at": stamp(first),
        "last_seen_at": stamp(last),
        "location": {"obec_kod": 555088, "street_key": street, "house_number": number,
                     "granularity": "address_point", "granularity_rank": 100,
                     "is_address_grain": True},
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def names(a: Listing, b: Listing, cfg: Settings) -> list[str]:
    return [fact.name for fact in distinguishing_facts(a, b, None, cfg, GATE)]


# --- E240: the number the advert PRINTS ------------------------------------------------------
KRASNOARMEJCU_A = (
    "Nabízíme k pronájmu byt 2+1 na ulici Krasnoarmejců 2080/8 v Ostravě-Zábřehu, "
    "který právě prochází kompletní rekonstrukcí. Po jejím dokončení nabídne moderní "
    "a komfortní bydlení ve vysokém standardu. Součástí bytu bude nová kuchyňská "
    "linka vybavená elektrickou varnou deskou a myčkou nádobí. Koupelna bude zcela "
    "nová se sprchovým koutem, moderním umyvadlem, zrcadlem a elegantními obklady v "
    "nadčasovém provedení. V kuchyni a předsíni budou položeny nové plovoucí podlahy "
    "v dekoru dubu, samozřejmostí jsou nové interiérové i vstupní bezpečnostní dveře, "
    "nové rozvody elektřiny, vody a odpadů, moderní osvětlení, výmalba i renovované "
    "radiátory. Ostrava-Zábřeh nabízí výbornou občanskou vybavenost i dopravní "
    "dostupnost. V docházkové vzdálenosti se nachází obchody, školy, školky, zastávky "
    "MHD i dostatek zeleně pro odpočinek a volnočasové aktivity. Neváhejte nás "
    "kontaktovat a domluvte si prohlídku. Rádi vám představíme váš nový domov po "
    "dokončení rekonstrukce. "
)
KRASNOARMEJCU_B = (
    "Nabízíme k pronájmu byt 2+1 v Ostravě-Zábřehu na ulici Krasnoarmejců 2079/10, "
    "který právě prochází rozsáhlou rekonstrukcí. Po dokončení nabídne moderní a "
    "příjemné bydlení bez nutnosti dalších investic. Součástí bude nová kuchyňská "
    "linka s elektrickou varnou deskou a místem pro pračku. Koupelna bude kompletně "
    "zrekonstruována s novou vanou, umyvadlem a moderními obklady v elegantním "
    "odstínu slonové kosti. Samostatné WC bude rovněž nově upraveno. V obou pokojích, "
    "kuchyni a předsíni budou položeny nové PVC podlahy v hnědém dekoru dřeva. "
    "Rekonstrukce zahrnuje také nové rozvody, úpravy elektroinstalace, výmalbu, "
    "renovaci radiátorů a nové protipožární vstupní dveře v dekoru dubu. Byt nabídne "
    "moderní, svěží a prakticky řešený interiér vhodný pro pohodlné každodenní "
    "bydlení. Neváhejte nás kontaktovat a domluvte si prohlídku. "
)


def test_printed_reader_takes_both_orders_as_one_set() -> None:
    both = printed_house_numbers("na adrese Freyova 5/236, Praha 9 – Vysočany", "freyova")
    column = printed_house_numbers("na adrese Freyova 236/5, Praha 9", "freyova")
    assert both == frozenset({frozenset({"5", "236"})})
    assert printed_house_numbers_meet(both, column)


def test_printed_reader_reads_the_cp_anchor_and_the_street() -> None:
    assert printed_house_numbers("dům č.p. 1561/9 po rekonstrukci", "x") == frozenset(
        {frozenset({"1561", "9"})})
    assert printed_house_numbers(
        "Nabízíme pronájem na adrese Pod Harfou 1066/9, Praha 9", "pod harfou"
    ) == frozenset({frozenset({"1066", "9"})})


def test_printed_reader_refuses_a_size_a_year_and_a_list_of_houses() -> None:
    assert printed_house_numbers("byt o rozloze 46,9 m² v Praze", "v horni stromce") == frozenset()
    assert printed_house_numbers("dům postavený v roce 1899", "slezska") == frozenset()
    # A developer naming its houses is a list, and a list refuses nothing.
    assert printed_house_numbers(
        "prodáváme domy č.p. 12, č.p. 14 a č.p. 16 v této ulici", "x") == frozenset()


def test_two_printed_numbers_of_one_street_are_a_fact() -> None:
    a = listing(1, street="krasnoarmejcu", number="2080/8", source="sreality",
                description=KRASNOARMEJCU_A, price=11950, area_m2=49.0)
    b = listing(2, street="krasnoarmejcu", number="2079/10", source="sreality",
                description=KRASNOARMEJCU_B, price=12650, area_m2=49.0)
    assert "printed_house_number" in names(a, b, S9)
    assert "printed_house_number" not in names(a, b, S8)


def test_a_corner_building_prints_both_its_streets_and_is_not_a_fact() -> None:
    body = ("Ve výhradním zastoupení majitele nabízím k prodeji bytovou jednotku v OV s "
            "dispozičním řešením 1+kk v Brně na ulici Svitavská 29/Vranovská 49 v prvním "
            "patře (2NP) bytového domu. Plocha bytové jednotky je 27,62 m2.")
    a = listing(1, street="vranovska", number="834/49", source="sreality", description=body,
                category_type="prodej", price=4199000, area_m2=29.0)
    b = listing(2, street="svitavska", number="834/29", source="sreality", description=body,
                category_type="prodej", price=4199000, area_m2=29.0)
    assert "printed_house_number" not in names(a, b, S9)


def test_a_body_re_posted_with_its_address_corrected_is_not_a_fact() -> None:
    shared = (" Součástí bytu je kuchyňská linka se spotřebiči (myčka, trouba, digestoř, "
              "varná deska, mikrovlnka a lednice). Součástí bytu je i sklep a společná "
              "kolárna umístěná v suterénu domu. Vytápění a ohřev vody v bytě je řešen "
              "centrálně plynovým kotlem umístěným v suterénu domu. Měsíční nájemné činí "
              "22 900 Kč plus 3 500 Kč zálohy na služby domu pro dvě osoby.")
    a = listing(1, street="hybesova", number="2090/30", source="sreality", price=22900,
                area_m2=67.0, description="Byt 3+kk na adrese Hybešova 2090/30, Kuřim." + shared)
    b = listing(2, street="hybesova", number=None, source="idnes", price=22900, area_m2=67.0,
                description="Byt 3+kk na adrese Hybešova 2091/30a, Kuřim." + shared)
    assert "printed_house_number" not in names(a, b, S9)


# --- E241: what the bodies print outranks what the resolver filed ----------------------------
FREYOVA_A = (
    "Nabízíme k dlouhodobému pronájmu světlý a útulný byt 1+kk o výměře 18 m² s vybudovaným "
    "spacím patrem o velikosti 7 m², situovaný v kompletně zrekonstruovaném domě na adrese "
    "Freyova 5/236, Praha 9 – Vysočany. Byt je ideální pro 1 až 2 dospělé osoby a bude volný "
    "od 31. 8. 2026. Součástí vybavení je kuchyňská linka a sklokeramická varná deska."
)
FREYOVA_B = (
    "K dlouhodobému pronájmu od 31. 8. 2026 nabízíme hezký a prakticky řešený byt 1+kk s "
    "patrem o velikosti 7 m², který se nachází v rekonstruovaném domě na adrese Freyova "
    "5/236, Praha 9 – Vysočany. Byt je vhodný pro jednu až dvě dospělé osoby a nabízí "
    "praktické bydlení s výbornou dostupností do centra Prahy, vybaven je pračkou a vanou."
)


def test_a_printed_agreement_vetoes_the_stored_conflict() -> None:
    a = listing(1, street="freyova", number="236/5", source="sreality", description=FREYOVA_A,
                price=19900, area_m2=19.0, first=0, last=20)
    b = listing(2, street="freyova", number="235/7", source="sreality", description=FREYOVA_B,
                price=19900, area_m2=18.0, first=24, last=36)
    assert printed_house_numbers_meet(
        printed_house_numbers(FREYOVA_A, "freyova"),
        printed_house_numbers(FREYOVA_B, "freyova"))
    assert "stored_house_number" not in names(a, b, S9)
    assert "printed_house_number" not in names(a, b, S9)


# --- E242: the stored number, under its four guards ------------------------------------------
JASIOKA_A = (
    "Představte si domov, kde se snoubí moderní design, pohodlí a skvělá dostupnost. "
    "Právě takový je tento krásný byt 2+kk, který se nachází v nově revitalizovaném a "
    "zatepleném domě v lokalitě, jež v posledních letech prochází výraznou proměnou a "
    "stává se stále vyhledávanějším místem pro život. Interiér bytu prošel pečlivou a "
    "nadstandardní rekonstrukcí, díky které nabízí příjemnou atmosféru a vysoký "
    "komfort bydlení. Hlavní obytné místnosti zdobí moderní plovoucí podlahy v "
    "elegantním dekoru dřeva, které prostoru dodávají teplo a útulnost. Kuchyň a "
    "vstupní část bytu jsou vybaveny světlou dlažbou, jež podtrhuje čistý a vzdušný "
    "charakter celého interiéru. Srdcem bytu je stylová kuchyňská linka v moderní "
    "kombinaci bílé a antracitové barvy. Je navržena tak, aby potěšila nejen svým "
    "vzhledem, ale i praktičností při každodenním používání. Na stejnou úroveň "
    "navazuje i koupelna, která zaujme současným designem a promyšleným uspořádáním. "
    "Najdete zde prostorný sprchový kout, toaletu a dostatek místa pro pračku i "
    "sušičku. Velkou předností tohoto bydlení je také jeho okolí. V bezprostřední "
    "blízkosti se nachází množství zeleně, upravených dvorků a odpočinkových zón, "
    "které vytvářejí příjemné prostředí pro každodenní život. Rodiny ocení dostupnost "
    "mateřské i základní školy, stejně jako kompletní občanskou vybavenost. Jen pár "
    "minut od domu se nachází obchodní dům Globus a výborné dopravní spojení "
    "zajišťuje nedaleké vlakové nádraží. Do samotného centra města se navíc pohodlně "
    "dostanete za pouhých deset minut chůze. Pokud hledáte moderní, světlé a kvalitní "
    "bydlení v lokalitě s budoucností, pak je tento byt přesně tím místem, kde se "
    "budete cítit opravdu doma. Přijďte se přesvědčit osobně – možná právě zde začíná "
    "vaše nová životní kapitola. "
)
JASIOKA_B = (
    "Hledáte domov, který spojuje moderní design, pohodlí a skvělou lokalitu? Právě "
    "jste ho našli. Nabízíme k pronájmu nádherný byt o dispozici 2+kk s prostorným "
    "balkónem, který prošel nadstandardní rekonstrukcí a nachází se v kompletně "
    "revitalizovaném a zatepleném bytovém domě. Tady si budete užívat nejen stylové "
    "bydlení, ale také klidné okolí s množstvím zeleně. Srdcem bytu je světlý obývací "
    "pokoj propojený s moderní kuchyní, která je vybavena elegantní kuchyňskou linkou "
    "s vestavěnými spotřebiči. Odtud je přímý vstup na velký balkón, ideální pro "
    "ranní kávu, večerní sklenku vína nebo chvíle odpočinku po náročném dni. V celém "
    "bytě jsou položeny kvalitní moderní plovoucí podlahy. Koupelna zaujme nadčasovým "
    "designem v příjemných hnědo-béžových tónech, nabízí prostorný sprchový kout a "
    "toaletu. Samostatná ložnice poskytuje dostatek prostoru pro pohodlné spaní i "
    "úložné řešení. Lokalita nabízí vše, co ke spokojenému životu potřebujete. V "
    "docházkové vzdálenosti najdete základní i mateřskou školu, vlakové nádraží, "
    "obchodní centrum Globus, dostatek parkovacích míst, dětská hřiště i spoustu "
    "zeleně. Navíc se jedná o oblast, která právě prochází výraznou proměnou a stává "
    "se stále atraktivnějším místem pro bydlení. Moderní byt, velký balkón, skvělá "
    "lokalita a příznivá cena – kombinace, která se na trhu objevuje jen zřídka. "
    "Neváhejte nás kontaktovat a domluvte si prohlídku. Tento byt si vás získá hned "
    "při první návštěvě. "
)


def test_two_stored_numbers_of_one_street_are_a_fact_for_rentals() -> None:
    a = listing(1, number="735/48", description=JASIOKA_A, price=8150, area_m2=39.0)
    b = listing(2, number="734/46", description=JASIOKA_B, price=8150, area_m2=38.0)
    assert "stored_house_number" in names(a, b, S9)
    assert "stored_house_number" not in names(a, b, S8)


def test_two_entrances_of_one_house_share_the_cp_and_are_not_a_fact() -> None:
    a = listing(1, street="fischerova", number="701/11", description=JASIOKA_A, price=15000,
                area_m2=27.0)
    b = listing(2, street="fischerova", number="701/13", description=JASIOKA_B, price=15500,
                area_m2=28.0)
    assert "stored_house_number" not in names(a, b, S9)


def test_a_price_and_an_area_that_did_not_move_refuse_the_stored_number() -> None:
    a = listing(1, street="italska", number="384/1", description=JASIOKA_A, price=30000,
                area_m2=79.0)
    b = listing(2, street="480/3", description=JASIOKA_B, price=30000, area_m2=79.0)
    b.location.street_key, b.location.house_number = "italska", "480/3"
    assert "stored_house_number" not in names(a, b, S9)


def test_one_body_posted_twice_refuses_the_stored_number() -> None:
    body = ("Nabízím k pronájmu světlý a velmi prostorný byt po rekonstrukci ve třetím patře "
            "cihlového domu s výtahem, v bytě je prostorná předsíň, tři samostatné pokoje a "
            "kuchyň se zimní zahradou, byt je částečně zařízen a u metra.")
    a = listing(1, street="ondrickova", number="2128/12", source="sreality", description=body,
                price=38990, area_m2=116.0, first=0, last=20)
    b = listing(2, street="ondrickova", number="1774/28", source="sreality", description=body,
                price=37990, area_m2=116.0, first=20, last=40)
    assert "stored_house_number" not in names(a, b, S9)


def test_a_sale_does_not_read_the_stored_number() -> None:
    a = listing(1, street="slezska", number="981/57", description=JASIOKA_A,
                category_type="prodej", price=10500000, area_m2=68.0)
    b = listing(2, street="slezska", number="832/55", description=JASIOKA_B,
                category_type="prodej", price=10900000, area_m2=68.0)
    assert "stored_house_number" not in names(a, b, S9)


def test_a_coarser_grain_has_not_stated_an_address() -> None:
    a = listing(1, number="735/48", description=JASIOKA_A, price=8150, area_m2=39.0)
    b = listing(2, number="734/46", description=JASIOKA_B, price=8150, area_m2=38.0)
    b.location.granularity = "street"
    assert "stored_house_number" not in names(a, b, S9)


def test_a_number_on_one_side_only_is_not_a_conflict() -> None:
    a = listing(1, number="735/48", description=JASIOKA_A, price=8150, area_m2=39.0)
    b = listing(2, number=None, description=JASIOKA_B, price=8150, area_m2=38.0)
    assert "stored_house_number" not in names(a, b, S9)
    assert "printed_house_number" not in names(a, b, S9)


def test_the_stored_limb_holds_when_the_dial_is_off() -> None:
    a = listing(1, number="735/48", description=JASIOKA_A, price=8150, area_m2=39.0)
    b = listing(2, number="734/46", description=JASIOKA_B, price=8150, area_m2=38.0)
    assert "stored_house_number" not in names(a, b, variant(d43_stored_house_number="off"))


# --- E243: the interior floor, inside the cell it was cut in ---------------------------------
def test_a_shared_photograph_puts_a_pair_outside_the_interior_cell() -> None:
    a = listing(1, number="735/48", description=JASIOKA_A, price=8150, area_m2=39.0)
    b = listing(2, number="735/48", description=JASIOKA_A, price=8150, area_m2=39.0)
    weak = {"tag_room_clip_min2": (0.87, True), "phash_tight_matches": (7.0, True)}
    assert "interior" in [f.name for f in distinguishing_facts(a, b, weak, S8, CLUSTER)]
    assert "interior" not in [f.name for f in distinguishing_facts(a, b, weak, S9, CLUSTER)]


def test_without_a_shared_photograph_the_interior_floor_still_holds() -> None:
    a = listing(1, number="735/48", description=JASIOKA_A, price=8150, area_m2=39.0)
    b = listing(2, number="735/48", description=JASIOKA_A, price=8150, area_m2=39.0)
    weak = {"tag_room_clip_min2": (0.87, True), "phash_tight_matches": (0.0, True)}
    assert "interior" in [f.name for f in distinguishing_facts(a, b, weak, S9, CLUSTER)]


# --- E244: which room of a priced letting plan an advert is ----------------------------------
# The Na Poříčí office building in Frýdek-Místek, let under one plan that the two portals print
# DIFFERENTLY: the bazos list opens with 302b, the idnes list omits it and offers 109 and 411a
# instead. The blind read of cohort 11 called this pair DIFFERENT.
NA_PORICI_BAZOS = (
    "Ev.č. 00063 Nabízíme k pronájmu kanceláře na ul. Na Poříčí. - Kancelář č. 302b – "
    "18,51m2, nájemné 4.123,-/měsíc, zálohy na služby: 1.920,- Kč včetně DPH/měsíc - "
    "Kancelář č. 308b – 20,02m2, nájemné 4.459,-/měsíc, zálohy na služby: 2.077,- Kč včetně "
    "DPH/měsíc - Kancelář č. 310 - 14m2, nájemné 3.134,-/měsíc, zálohy na služby: 1.460,- Kč "
    "včetně DPH/měsíc - Kancelář č. 311 – 14,80m2, nájemné 3.297,-/měsíc, zálohy na na "
    "služby: 1.536,- Kč včetně DPH/měsíc K dispozici je společná kuchyňka a WC na každém "
    "patře, výtah, parkovací místo v ceně. Budova je velmi dobře udržovaná, čistá."
)
NA_PORICI_IDNES = (
    "Pronájem kanceláře, 20 m² - Frýdek-Místek - Frýdek Nabízíme k pronájmu kanceláře na ul. "
    "Na Poříčí. - Kancelář č. 109 – 45m², nájemné 10,024,-/měsíc, zálohy na na služby: "
    "4.669,- Kč včetně DPH/měsíc - Kancelář č. 308b – 20,02m², nájemné 4.459,-/měsíc, zálohy "
    "na služby: 2.077,- Kč včetně DPH/měsíc - Kancelář č. 310 - 14m², nájemné 3.134,-/měsíc, "
    "zálohy na služby: 1.460,- Kč včetně DPH/měsíc - Kancelář č. 311 – 14,80m², nájemné "
    "3.297,-/měsíc, zálohy na na služby: 1.536,- Kč včetně DPH/měsíc K dispozici je společná "
    "kuchyňka a WC na každém patře, výtah, parkovací místo v ceně."
)


def office(listing_id: int, body: str, **kwargs: object) -> Listing:
    return listing(listing_id, street="na porici", category_main="komercni",
                   description=body, **kwargs)


def test_the_plan_reader_takes_each_priced_row() -> None:
    plan = priced_letting_plan(NA_PORICI_BAZOS)
    assert plan["302b"] == (18.51, 4123.0)
    assert plan["310"] == (14.0, 3134.0)
    assert plan_headline_area(NA_PORICI_IDNES) == 20.0


def test_an_advert_resolves_to_its_own_row_by_column_then_by_headline() -> None:
    a = office(1, NA_PORICI_BAZOS, price=4123, area_m2=18.0)
    b = office(2, NA_PORICI_IDNES, price=4123, area_m2=18.0, source="idnes")
    assert offered_plan_space(a, S9) == "302b"
    # The idnes plan does not contain 302b at all, so the size the body LEADS with answers.
    assert offered_plan_space(b, S9) == "308b"
    assert "plan_space" in names(a, b, S9)
    assert "plan_space" not in names(a, b, S8)


def test_two_adverts_on_one_row_of_the_plan_are_not_a_fact() -> None:
    a = office(1, NA_PORICI_BAZOS, price=4459, area_m2=20.0)
    b = office(2, NA_PORICI_IDNES, price=4459, area_m2=20.0, source="idnes")
    assert offered_plan_space(a, S9) == offered_plan_space(b, S9) == "308b"
    assert "plan_space" not in names(a, b, S9)


def test_a_plan_that_cannot_say_which_row_says_nothing() -> None:
    twins = ("Nabízíme kanceláře. - Kancelář č. 210 - 14m2, nájemné 3.134,-/měsíc - "
             "Kancelář č. 310 - 14m2, nájemné 3.134,-/měsíc")
    a = office(1, twins, price=3134, area_m2=14.0)
    assert offered_plan_space(a, S9) is None


def test_a_body_that_publishes_no_plan_says_nothing() -> None:
    a = office(1, "Nabízíme k pronájmu kancelář č. 302b o výměře 18,51 m2.", price=4123,
               area_m2=18.0)
    assert offered_plan_space(a, S9) is None
