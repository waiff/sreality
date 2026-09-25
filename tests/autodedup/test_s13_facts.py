"""S13's readings (E280-E291), each built from the advert out of cohort 15 that named it.

Cohort 15's hazard reader found 28 objects whose same-body re-post chains S12 splits. Most,
K. J. Erbena 299/10 is E281: the idnes train and the sreality/bazos train of one 2+kk are held
apart by ONE pair, idnes `1` against sreality `0` in the storey column while both bodies print
`v 1. patře`. Starovičky (LV 1585) is E282: the price cut 225,342 -> 196,452 between two
postings of one text is a fact only because the cluster relation never carries the containment
slot. Lužice Meadows is E283, Dolní Věstonice's two 1,941 m² twins E284 and its 834 m² plot at
5,844 Kč/m² E285, Cammerswalde E286, Valtice / Úvaly and Popice E287, Koldům 1580 E288, Český
Jiřetín E289, Bohnice / Kostřínská 583/6 E290 and Břeclav `na zvolenci 43` E291. E280 is the
printed area prevailing over the columns (43,79 m² printed, 43.0 and 43.8 stored).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autodedup.dataset import Listing
from autodedup.demonstrate import corroboration_warrant, demonstration_shortfall
from autodedup.features import attribute_conflicts
from autodedup.fingerprint import build_fingerprint
from autodedup.d43 import ClusterRelation
from autodedup.guards import cluster_invariants_ok
from autodedup.indistinguishable import (
    CLUSTER, GATE, PROMOTE, distinguishing_facts, per_m2_path, price_paths_agree, text_containment,
)
from autodedup.settings import Settings

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup/settings"
S12 = Settings.from_json(SETTINGS / "w27.json")


def variant(**kwargs: object) -> Settings:
    raw = json.loads((SETTINGS / "w27.json").read_text(encoding="utf-8"))
    raw.update(kwargs)
    return Settings.from_dict(raw)


def stamp(day: int) -> str:
    return (datetime(2026, 5, 1, tzinfo=timezone.utc) + timedelta(days=day)).isoformat()


def advert(listing_id: int, source: str, body: str, first: int, last: int, **kwargs: object
           ) -> Listing:
    price = kwargs.get("price")
    fields: dict[str, object] = {
        "source": source,
        "category_main": "byt",
        "category_type": "prodej",
        "description": body,
        "first_seen_at": stamp(first),
        "last_seen_at": stamp(last),
        "price_history": [[stamp(first), price]] if price else [],
        "location": {"obec_kod": 567027, "granularity": "obec", "granularity_rank": 20},
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def names(a: Listing, b: Listing, cfg: Settings, mode: str = CLUSTER,
          feats: dict[str, tuple[float, bool]] | None = None) -> list[str]:
    return [fact.name for fact in distinguishing_facts(a, b, feats, cfg, mode)]


# --- E281: Most, K. J. Erbena 299/10 ----------------------------------------------------------
ERBENA = (
    "Hledáte moderní bydlení v jedné z nejklidnějších a nejvyhledávanějších lokalit města Most? "
    "Právě jste ho našli! Nabízíme k prodeji krásný byt o rozměrech 43,79m² 2+kk v osobním "
    "vlastnictví, který prošel kompletní rekonstrukcí a nachází se v 1. patře udržovaného "
    "třípatrového domu po celkové revitalizaci. Byt disponuje zasklenou lodžií, sklepní kójí a "
    "praktickou komorou, které poskytují dostatek úložného prostoru."
)
PHOTOS = {"phash_tight_matches": (5.0, True)}


def erbena(listing_id: int, source: str, floor: int | None, price: float, first: int,
           last: int, area: float = 43.0, body: str = ERBENA) -> Listing:
    return advert(listing_id, source, body, first, last, floor=floor, price=price,
                  area_m2=area, disposition="2+kk")


def test_E281_the_storey_agreement_is_the_floor_fact_not_the_raw_column() -> None:
    # 13424806 idnes floor 1 (Aug 4-20) against 18985700 sreality floor 0 (Sep 22-24).
    a = erbena(13424806, "idnes", 1, 2690000.0, 0, 16)
    b = erbena(18985700, "sreality", 0, 2490000.0, 60, 62)
    assert "floor" not in names(a, b, S12)
    assert "price" in names(a, b, S12, feats=PHOTOS)
    assert "price" not in names(a, b, variant(d43_price_sequential_storey_fact=True), feats=PHOTOS)


def test_E281_a_storey_the_floor_fact_reads_still_refuses_the_excuse() -> None:
    a = erbena(13424806, "idnes", 1, 2690000.0, 0, 16)
    b = erbena(18985700, "sreality", 4, 2490000.0, 60, 62)
    cfg = variant(d43_price_sequential_storey_fact=True)
    assert "floor" in names(a, b, cfg)
    assert "price" in names(a, b, cfg, feats=PHOTOS)


# --- E280: the printed area prevails over the two columns --------------------------------------
def test_E280_both_bodies_print_one_area_so_the_columns_are_not_two() -> None:
    a = erbena(17879685, "idnes", None, 2590000.0, 0, 27, area=43.0)
    b = erbena(18919087, "idnes", None, 2490000.0, 27, 34, area=47.0)
    assert "area" in names(a, b, S12, GATE)
    assert "area" not in names(a, b, variant(d43_printed_area_prevails=True), GATE)


def test_E280_a_body_that_prints_another_figure_keeps_the_area_fact() -> None:
    a = erbena(17879685, "idnes", None, 2590000.0, 0, 27, area=43.0)
    b = erbena(18919087, "idnes", None, 2490000.0, 27, 34, area=47.0,
               body=ERBENA.replace("43,79", "47,20"))
    assert "area" in names(a, b, variant(d43_printed_area_prevails=True), GATE)


def test_E280_never_inside_a_development() -> None:
    body = ERBENA + " Novostavba v rezidenčním projektu, developer nabízí další jednotky."
    a = erbena(1, "idnes", None, 2590000.0, 0, 27, area=43.0, body=body)
    b = erbena(2, "idnes", None, 2590000.0, 0, 27, area=47.0, body=body)
    assert "area" in names(a, b, variant(d43_printed_area_prevails=True), GATE)


def test_E280_the_cluster_area_invariant_reads_the_printed_figure() -> None:
    cfg = variant(d43_printed_area_prevails=True)
    a = erbena(17879685, "idnes", None, 2590000.0, 0, 27, area=43.0)
    b = erbena(18919087, "idnes", None, 2590000.0, 27, 34, area=47.5)
    listings = {a.id: a, b.id: b}
    members = [build_fingerprint(a, [], cfg), build_fingerprint(b, [], cfg)]
    assert cluster_invariants_ok(members, S12, frozenset(),
                                 ClusterRelation(listings, {}, S12)) == "area_spread"
    assert cluster_invariants_ok(members, cfg, frozenset(),
                                 ClusterRelation(listings, {}, cfg)) != "area_spread"


# --- E282: Starovičky LV 1585, a price cut between two postings of one text --------------------
STAROVICKY = (
    "Nabízíme k prodeji soubor zemědělských pozemků v katastrálním území Starovičky, vhodných "
    "jak pro přímé hospodaření, tak jako stabilní a výnosná investice. HLAVNÍ VÝHODY NABÍDKY "
    "-> výlučné vlastnictví -> pachtovní smlouva s krátkou výpovědní lhůtou -> vysoké "
    "pachtovné, zajímavý výnosový potenciál. Jedná se o prodej 100 %, tedy pozemky budou po "
    "převodu ve vašem výlučném vlastnictví. Celková rozloha činí 5 778 m². Pozemky jsou "
    "zapsány na LV 1585 pod parcelními čísly 3750, 3778, 3937 a 3938."
)


def starovicky(listing_id: int, source: str, price: float, first: int, last: int,
               body: str = STAROVICKY) -> Listing:
    return advert(listing_id, source, body, first, last, category_main="pozemek",
                  area_m2=5778.0, price=price)


def test_E282_the_body_identity_is_read_off_the_texts_where_no_feature_row_exists() -> None:
    a = starovicky(178589, "idnes", 225342.0, 33, 73)
    b = starovicky(15043251, "sreality", 196452.0, 101, 143)
    assert text_containment(a, b) == 1.0
    assert "price" in names(a, b, S12)
    assert "price" not in names(a, b, variant(d43_price_sequential_text_identity=True))


def test_E282_a_rewritten_body_does_not_demonstrate_identity() -> None:
    # Brod nad Dyjí: the last re-post is a new text under a new agency (containment 0.15-0.30).
    rewritten = ("Prodej zavedeného penzionu s pěti apartmány pro patnáct osob, vinařská obec, "
                 "pozemek 532 m², parkování na vlastním pozemku, okamžité převzetí provozu, "
                 "vhodné jako investice i jako rodinné bydlení s podnikáním, klidná lokalita.")
    a = starovicky(41393, "sreality", 225342.0, 0, 30)
    b = starovicky(18850157, "realitymix", 196452.0, 101, 113, body=rewritten)
    assert "price" in names(a, b, variant(d43_price_sequential_text_identity=True))


def test_E282_a_body_too_short_to_say_anything_is_no_identity() -> None:
    a = starovicky(1, "idnes", 225342.0, 33, 73, body="Pozemek Starovičky, 5 778 m².")
    b = starovicky(2, "sreality", 196452.0, 101, 143, body="Pozemek Starovičky, 5 778 m².")
    assert text_containment(a, b) is None
    assert "price" in names(a, b, variant(d43_price_sequential_text_identity=True))


def test_E282_two_prices_live_together_stay_two() -> None:
    a = starovicky(178589, "idnes", 225342.0, 33, 120)
    b = starovicky(15043251, "sreality", 196452.0, 60, 143)
    assert "price" in names(a, b, variant(d43_price_sequential_text_identity=True))


# --- E283: Lužice Meadows, re-posts of one plot on the honest clock --------------------------
MEADOWS = (
    "Hledáte místo, kde si postavíte dům podle svých představ a zároveň budete mít přírodu i "
    "město na dosah? Právě pro vás je určen pozemek o výměře 1 215 m2 v rámci 2. etapy "
    "rezidenčního projektu Lužice Meadows v obci Lužice u Mostu. Druhá etapa navazuje na "
    "úspěšnou první část projektu, o kterou byl mezi kupujícími velký zájem. Nyní máte možnost "
    "vybrat si z pozemků, které patří k tomu nejzajímavějšímu v okolí Mostu."
)
ONE_BODY = {"containment_max": (1.0, True)}


def meadows(listing_id: int, first: int, last: int, inactive: int | None = None) -> Listing:
    return advert(listing_id, "ceskereality", MEADOWS, first, last, category_main="pozemek",
                  area_m2=1215.0, price=4252500.0,
                  inactive_at=stamp(inactive) if inactive is not None else None)


def test_E283_the_detection_stamp_may_not_make_a_re_post_a_template() -> None:
    # 10595349 last sighted 07-31, detected gone 09-07; 13404483 sighted 08-04..08-06.
    a, b = meadows(10595349, 50, 58, inactive=95), meadows(13404483, 62, 64, inactive=96)
    assert corroboration_warrant(a, b, ONE_BODY, S12) is None
    assert corroboration_warrant(a, b, ONE_BODY,
                                 variant(demonstrate_sequential_honest_clock=True)) == "body"


def test_E283_two_plots_of_one_project_on_sale_together_stay_uncorroborated() -> None:
    a, b = meadows(10595349, 50, 90), meadows(13404483, 62, 95)
    assert corroboration_warrant(a, b, ONE_BODY,
                                 variant(demonstrate_sequential_honest_clock=True)) is None


# --- E284: Dolní Věstonice, identical twins on ceskereality ------------------------------------
VESTONICE = (
    "Hledáte prostor, kde vás sousedé nebudou rušit u ranní kávy? Nabízíme unikátní stavební "
    "parcelu v srdci nejvyhledávanější vinařské lokality, která díky své rozloze 1 941 m2 "
    "garantuje absolutní soukromí a nekonečné možnosti. Pozemek je plně zasíťovaný (elektřina, "
    "voda, kanalizace) a dle územního plánu určen k výstavbě rodinného domu. Novostavba, "
    "rezidenční lokalita."
)


def twin(listing_id: int, price: float = 5046600.0, source: str = "ceskereality") -> Listing:
    return advert(listing_id, source, VESTONICE, 63, 82, category_main="pozemek",
                  area_m2=1941.0, price=price)


def test_E284_identical_twins_on_one_portal_are_corroborated() -> None:
    a, b = twin(479732), twin(479740)
    assert corroboration_warrant(a, b, ONE_BODY, S12) is None
    assert corroboration_warrant(a, b, ONE_BODY, variant(demonstrate_identical_twin=True)) == "twin"


def test_E284_two_prices_are_not_twins() -> None:
    a, b = twin(479732), twin(479740, price=5246600.0)
    assert corroboration_warrant(a, b, ONE_BODY, variant(demonstrate_identical_twin=True)) is None


def test_E284_two_portals_are_not_twins() -> None:
    a, b = twin(479732), twin(479740, source="sreality")
    assert corroboration_warrant(a, b, ONE_BODY, variant(demonstrate_identical_twin=True)) is None


# --- E285: a per-m² price times the stated area ------------------------------------------------
def plot834(listing_id: int, source: str, price: float) -> Listing:
    return advert(listing_id, source, VESTONICE.replace("1 941", "834"), 30, 74,
                  category_main="pozemek", area_m2=834.0, price=price)


def test_E285_the_per_m2_price_is_the_totals_path() -> None:
    a, b = plot834(169773, "idnes", 4873896.0), plot834(409471, "realitymix", 5844.0)
    assert per_m2_path(a, b, 0.001)
    assert not price_paths_agree(a, b, 0.005)
    assert "price" in names(a, b, S12)
    assert "price" not in names(a, b, variant(d43_price_per_m2_path=True))


def test_E285_a_price_that_is_not_the_area_multiple_stays_a_fact() -> None:
    a, b = plot834(169773, "idnes", 4873896.0), plot834(409471, "realitymix", 6100.0)
    assert not per_m2_path(a, b, 0.001)
    assert "price" in names(a, b, variant(d43_price_per_m2_path=True))


# --- E286: Cammerswalde, one house as `dům` and as `komerční` ----------------------------------
def cammerswalde(listing_id: int, main: str, subtype: str, sub_cb: int, condition: str) -> Listing:
    return advert(listing_id, "sreality", "Dům v Cämmerswalde " * 20, 0, 130,
                  category_main=main, subtype=subtype, area_m2=280.0, price=1699000.0,
                  attrs={"category_sub_cb": sub_cb, "condition": condition,
                         "usable_area": 280.0})


def test_E286_the_cross_type_subtype_is_not_a_contradiction() -> None:
    a = cammerswalde(50399, "komercni", "apartmany", 57, "pred_rekonstrukci")
    b = cammerswalde(90130, "dum", "vicegeneracni_dum", 54, "v_rekonstrukci")
    assert {c[0] for c in attribute_conflicts(a, b, S12)} >= {"subtype", "category_sub_cb"}
    kept = {c[0] for c in attribute_conflicts(a, b, variant(attr_cross_type_subtype_skip=True))}
    assert "subtype" not in kept and "category_sub_cb" not in kept and "condition" in kept


def test_E286_one_category_keeps_its_subtype() -> None:
    a = cammerswalde(1, "dum", "rodinny_dum", 37, "dobry")
    b = cammerswalde(2, "dum", "vicegeneracni_dum", 54, "dobry")
    kept = {c[0] for c in attribute_conflicts(a, b, variant(attr_cross_type_subtype_skip=True))}
    assert "subtype" in kept


# --- E287: Valtice / Úvaly and Popice ---------------------------------------------------------
UVALY = ("Ve výhradním zastoupení nabízíme ke koupi jedinečný vinný sklep s ubytováním a vlastní "
         "vinicí v lokalitě Úvaly u Valtic. Celková plocha pozemku činí 3 205 m² a díky "
         "přístupu z obou stran je pozemek dobře využitelný.")


def sklep(listing_id: int, source: str, main: str, estate: float, body: str = UVALY) -> Listing:
    return advert(listing_id, source, body, 0, 118, category_main=main, area_m2=101.0,
                  price=5390000.0, attrs={"estate_area": estate})


def test_E287a_a_plot_column_its_own_body_contradicts_is_not_a_parcel() -> None:
    a, b = sklep(89263, "sreality", "dum", 3205.0), sklep(435526, "realitymix", "komercni", 101.0)
    assert "plot_area" in names(a, b, S12)
    assert "plot_area" not in names(a, b, variant(d43_plot_column_body_prevails=True))


def test_E287a_two_bodies_printing_two_plots_keep_the_fact() -> None:
    a = sklep(89263, "sreality", "dum", 3205.0)
    b = sklep(435526, "realitymix", "komercni", 1101.0, body=UVALY.replace("3 205", "1 101"))
    assert "plot_area" in names(a, b, variant(d43_plot_column_body_prevails=True))


def test_E287b_a_plot_column_that_echoes_the_floor_area_is_not_a_parcel() -> None:
    body = "Vinný sklípek s apartmánem 3+kk a výhledem na Pálavu. " * 5
    a = advert(60825, "sreality", body, 0, 130, category_main="dum", area_m2=50.0,
               price=6900000.0, attrs={"estate_area": 174.0})
    b = advert(221554, "idnes", body, 20, 130, category_main="dum", area_m2=50.0,
               price=6900000.0, attrs={"estate_area": 50.0})
    assert "plot_area" in names(a, b, S12)
    assert "plot_area" not in names(a, b, variant(d43_plot_column_echo=True))
    c = advert(3, "idnes", body, 20, 130, category_main="dum", area_m2=60.0,
               price=6900000.0, attrs={"estate_area": 120.0})
    assert "plot_area" in names(a, c, variant(d43_plot_column_echo=True))


# --- E288: Koldům 1580 ------------------------------------------------------------------------
KOLDUM = ("Nabízím k prodeji družstevní byt o dispozici 2+kk s lodžií a celkovou výměrou 53 m², "
          "z čehož 4 m² tvoří lodžie, umístěný v 1. nadzemním podlaží domu Koldům 1580 v "
          "Litvínově, část Horní Litvínov. Byt má plně bezbariérový přístup z ulice i z "
          "parkoviště – ideální pro seniory, osoby s omezenou hybností nebo rodiny s malými "
          "dětmi. Dispozice: Byt tvoří vstupní chodba, prostorný obývací pokoj s kuchyňským "
          "koutem, samostatná ložnice s přímým vstupem na lodžii orientovanou do klidné zeleně, "
          "koupelna s vanou a samostatná toaleta.")


def koldum(listing_id: int, source: str, floor: int, body: str = KOLDUM) -> Listing:
    return advert(listing_id, source, body, 0, 20, floor=floor, area_m2=53.0,
                  disposition="2+kk", price=1570000.0)


def test_E288_one_printed_storey_on_both_bodies_outvotes_the_columns() -> None:
    a, b = koldum(16302, "sreality", 12), koldum(18124833, "bazos", 0)
    assert "floor" in names(a, b, S12)
    assert "floor" not in names(a, b, variant(d43_floor_column_body_prevails=True))


def test_E288_two_printed_storeys_keep_the_column() -> None:
    a = koldum(16302, "sreality", 12)
    b = koldum(18124833, "bazos", 0, body=KOLDUM.replace("1. nadzemním", "3. nadzemním"))
    assert "floor" in names(a, b, variant(d43_floor_column_body_prevails=True))


def test_E288_two_edited_texts_keep_their_columns() -> None:
    # Velká Brána (Horoměřice): two lettings `ve 4.NP` under two edited texts, columns 3 and 2.
    a = koldum(68449, "sreality", 3)
    b = koldum(18788687, "sreality", 2,
               body=KOLDUM.replace("Byt má plně", "Nájemné 21 000 Kč, kauce 25 600 Kč. Byt má"))
    assert "floor" in names(a, b, variant(d43_floor_column_body_prevails=True))


# --- W28's guard: no relaxation reaches two bodies that print two storeys ---------------------
PARDUBICE = ("Nabízím k pronájmu světlý a prakticky řešený byt o dispozici 1+1, který se nachází "
             "v {n}. nadzemním podlaží bytového domu na sídlišti Dubina v Pardubicích. Byt má "
             "výměru 36 m² a náleží k němu sklep o velikosti cca 2 m². Dispozici tvoří samostatný "
             "pokoj, kuchyň, koupelna s vanou a s WC a chodba s vestavěnou skříní.")


def test_the_printed_area_does_not_prevail_across_two_printed_storeys() -> None:
    a = advert(14657, "sreality", PARDUBICE.format(n=2), 5, 11, area_m2=36.0, price=11000.0,
               category_type="pronajem", disposition="1+1")
    b = advert(18670116, "sreality", PARDUBICE.format(n=1), 124, 129, area_m2=38.0,
               price=11000.0, category_type="pronajem", disposition="1+1")
    cfg = variant(d43_printed_area_prevails=True)
    assert "area" in names(a, b, cfg, PROMOTE)
    c = advert(3, "sreality", PARDUBICE.format(n=2), 124, 129, area_m2=38.0, price=11000.0,
               category_type="pronajem", disposition="1+1")
    assert "area" not in names(a, c, cfg, PROMOTE)


# --- E289: Český Jiřetín, a re-post that re-shot its gallery ---------------------------------
JIRETIN = ("Hledáte pro sebe nové bydlení v rodinném domě, které by bylo možné kombinovat s "
           "podnikáním? BONO reality Vám v zastoupení vlastníka nemovitosti nabízí ke koupi "
           "víceúčelovou stavbu, která nabízí jak rodinné bydlení v prvním patře, tak také "
           "komerční v podobě restaurace v přízemí. Nemovitost se nachází v obci Český Jiřetín.")
INTERIOR = {"tag_room_clip_min2": (0.872, True)}


def jiretin(listing_id: int, main: str, first: int, last: int, price: float) -> Listing:
    return advert(listing_id, "bazos", JIRETIN, first, last, category_main=main,
                  area_m2=108.0, price=price, disposition="3+1")


def test_E289_a_one_text_re_post_may_re_shoot_its_gallery() -> None:
    a, b = jiretin(205238, "komercni", 0, 4, 5650000.0), jiretin(15464113, "dum", 70, 110, 5350000.0)
    assert "interior" in names(a, b, S12, feats=INTERIOR)
    assert "interior" not in names(a, b, variant(d43_interior_sequential_repost=True),
                                   feats=INTERIOR)


def test_E289_two_galleries_on_sale_together_keep_the_interior_fact() -> None:
    a, b = jiretin(205238, "komercni", 0, 90, 5650000.0), jiretin(15464113, "dum", 70, 110, 5650000.0)
    assert "interior" in names(a, b, variant(d43_interior_sequential_repost=True),
                               feats=INTERIOR)


# --- E290: Bohnice, Kostřínská 583/6 ----------------------------------------------------------
KOSTRINSKA = ("Nabízíme k prodeji byt 2+kk o užitné ploše 42 m², situovaný v panelovém domě s "
              "výtahem v klidné a dobře dostupné části Prahy 8 – Bohnicích. Byt v osobním "
              "vlastnictví a nabízí možnost okamžitého nastěhování i postupného přizpůsobení "
              "vlastním představám. Dispozice je ideální pro jednotlivce, pár nebo jako "
              "investiční příležitost. K bytu náleží sklep o velikosti 2 m², který poskytne "
              "praktický úložný prostor. Výhodou je také možnost venkovního parkování bez "
              "modrých zón. V okolí je kompletní občanská vybavenost a dobrá dopravní "
              "dostupnost. Pro více informací nebo domluvení prohlídky mě neváhejte kontaktovat.")


def kostrinska(listing_id: int, floor: int, first: int, last: int,
               inactive: int | None = None) -> Listing:
    return advert(listing_id, "ceskereality", KOSTRINSKA, first, last, floor=floor,
                  area_m2=42.0, disposition="2+kk", price=6550000.0, broker_key="k",
                  inactive_at=stamp(inactive) if inactive is not None else None,
                  location={"obec_kod": 554782, "granularity": "address_point",
                            "granularity_rank": 100, "street_key": "kostrinska",
                            "house_number": "583/6", "ruian_adm_kod": 22322124})


def test_E290_a_same_feed_re_post_whose_storey_column_drifted_is_one_flat() -> None:
    a, b = kostrinska(18575821, 5, 88, 89, inactive=100), kostrinska(18633509, 6, 90, 107)
    assert "floor" in names(a, b, S12)
    assert "floor" not in names(a, b, variant(d43_floor_same_feed_sequential=True))


def test_E290_two_texts_a_day_apart_are_two_flats() -> None:
    # Most: 19915 `ve třetím patře` at 2,020,000 and, a day later, another agency's 30053
    # `ve 2. patře` at 1,890,000 — one portal, two texts, two flats.
    a = kostrinska(19915, 5, 88, 89, inactive=100)
    b = kostrinska(30053, 6, 90, 107)
    b.description = ("Toužíte po bydlení, které si můžete upravit přesně podle svých představ? "
                     "Nabízím vám družstevní byt o dispozici 2+1 a celkové ploše 52 m², který se "
                     "nachází v klidné a vyhledávané části města. Po vstupu do bytu se ocitnete v "
                     "prostorné chodbě, která nabízí dostatek místa pro velkou šatní skříň nebo "
                     "jiný úložný prostor. Z chodby se dostanete do hlavní obytné místnosti.")
    assert "floor" in names(a, b, variant(d43_floor_same_feed_sequential=True))


def test_E290_two_storeys_on_sale_together_stay_two_flats() -> None:
    a, b = kostrinska(18575821, 5, 60, 100), kostrinska(18633509, 6, 70, 107)
    assert "floor" in names(a, b, variant(d43_floor_same_feed_sequential=True))


# --- E291: Břeclav, `na zvolenci 43` ----------------------------------------------------------
ZVOLENCI = ("Prodám ideální 1/3 rodinného domu ve městě Břeclav na zvolenci 43. Zastavěná plocha "
            "domu je 158m2, zahrada 201m2 a další zahrada (ta mi patří celá) 68m2. V domě bydlí "
            "pouze jedna osoba (spolumajitel), který dům užívá. Ideální jako investice! Cena pevná")


def zvolenci(listing_id: int, obec: int, first: int, last: int, price: float,
             source: str = "bazos") -> Listing:
    return advert(listing_id, source, ZVOLENCI, first, last, category_main="dum", area_m2=158.0,
                  price=price, location={"obec_kod": obec, "granularity": "obec",
                                         "granularity_rank": 40})


def test_E291_the_resolver_may_not_part_one_text_re_posted() -> None:
    a = zvolenci(18429630, 584436, 116, 140, 600000.0)
    b = zvolenci(18934943, 584291, 140, 143, 550000.0)
    assert demonstration_shortfall(a, b, S12, False, 0.0) == ("obec", "contradiction")
    assert demonstration_shortfall(
        a, b, variant(demonstrate_obec_one_text_sequential=True), False, 0.0) is None


def test_E291_two_portals_keep_the_obec_demonstration() -> None:
    a = zvolenci(18429630, 584436, 116, 140, 600000.0)
    b = zvolenci(18934943, 584291, 140, 143, 550000.0, source="sreality")
    assert demonstration_shortfall(
        a, b, variant(demonstrate_obec_one_text_sequential=True), False, 0.0) is not None


# --- the settings files -------------------------------------------------------------------------
W28_DIALS = {
    "d43_printed_area_prevails", "d43_price_sequential_storey_fact",
    "d43_price_sequential_text_identity", "demonstrate_sequential_honest_clock",
    "demonstrate_identical_twin", "d43_price_per_m2_path", "attr_cross_type_subtype_skip",
    "d43_plot_column_body_prevails", "d43_plot_column_echo", "d43_floor_column_body_prevails",
    "d43_interior_sequential_repost", "d43_floor_same_feed_sequential",
    "demonstrate_obec_one_text_sequential",
}


def test_every_W28_dial_is_off_by_default() -> None:
    default = Settings()
    assert not any(getattr(default, dial) for dial in W28_DIALS)
    assert not any(getattr(S12, dial) for dial in W28_DIALS)


S13 = Settings.from_json(SETTINGS / "w28.json")


def test_w28_differs_from_w27_only_in_the_W28_dials() -> None:
    w27 = S12.to_dict()
    w28 = S13.to_dict()
    assert {key for key in w28 if w27.get(key) != w28[key]} == W28_DIALS
    assert all(getattr(S13, dial) for dial in W28_DIALS)


def test_the_w28_holds_are_w28_plus_one_table() -> None:
    w28 = S13.to_dict()
    for name, table in (("w28_rentals_hold", {"pronajem|*": "propose"}),
                        ("w28_land_hold", {"*|pozemek": "propose"})):
        hold = Settings.from_json(SETTINGS / f"{name}.json").to_dict()
        assert {key for key in hold if w28.get(key) != hold[key]} == {"merge_policy"}
        assert hold["merge_policy"] == table


# Replay parity: every earlier settings file, w13..w27 and every hold, byte for byte.
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
}


def test_every_earlier_settings_file_is_byte_identical() -> None:
    for name, digest in EARLIER_DIGESTS.items():
        assert hashlib.sha256((SETTINGS / name).read_bytes()).hexdigest() == digest, name
