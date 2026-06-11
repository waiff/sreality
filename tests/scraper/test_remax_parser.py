"""Hermetic tests for scraper.remax_parser against hand-authored fixtures that
mirror the real remax-czech.cz markup: the search cards (a `div.pl-items__item`
carrying `data-url` / `data-price` / `data-gps` / `data-title`), the
`pd-detail-info__row` -> `__label` / `__value` spec block, the clean integer
`data-advert-price`, the `data-gps` DMS coordinates, and the
`mlsf.remax-czech.cz/data//zs/{id}/..._th350.jpg` gallery (stripped to the
full-resolution original).
"""

from __future__ import annotations

from scraper.remax_parser import (
    _norm_furnished,
    _norm_ownership,
    category_from_typ,
    category_of,
    index_price,
    parse_detail,
    parse_index,
    subtype_of,
    type_of,
)


def test_norm_furnished_canonical_codes():
    # The "Vybaveno" spec row carries a yes/no answer; we store the canonical
    # sreality code (ano/ne/castecne), never the Czech label.
    assert _norm_furnished("Ano") == "ano"
    assert _norm_furnished("Ne") == "ne"
    assert _norm_furnished("Nevybaveno") == "ne"
    assert _norm_furnished("Částečně") == "castecne"
    assert _norm_furnished(None) is None


def test_norm_ownership_canonical_only():
    assert _norm_ownership("Osobní") == "osobni"
    assert _norm_ownership("Družstevní") == "druzstevni"
    assert _norm_ownership("Státní") == "statni"
    assert _norm_ownership("Obecní") == "statni"
    # Unmapped labels collapse to None, never leak through (e.g. "ostatni").
    assert _norm_ownership("Ostatní") is None
    assert _norm_ownership(None) is None

_DETAIL_URL = (
    "https://www.remax-czech.cz/reality/detail/440872/"
    "prodej-bytu-2-kk-v-osobnim-vlastnictvi-45-m2-praha-3-zizkov"
)

INDEX_HTML = """
<!DOCTYPE html><html><body>
<div class="pl-results-head">Zobrazujeme výsledky 1-21 z celkem <span>6 124</span> nalezených</div>
<div class="pl-items">
  <div class="pl-items__item"
       data-gps="50°05&#039;26.1&quot;N,14°29&#039;33.4&quot;E"
       data-display-address="Seifertova, Praha 3 - Žižkov"
       data-img="https://mlsf.remax-czech.cz/data//zs/440872/3387561_th350.jpg"
       data-price="9&nbsp;962&nbsp;000&nbsp;Kč <small>(za nemovitost)</small>"
       data-title="Prodej bytu 2+kk v osobním vlastnictví 45 m², Praha 3 - Žižkov"
       data-url="/reality/detail/440872/prodej-bytu-2-kk-v-osobnim-vlastnictvi-45-m2-praha-3-zizkov">
    <a class="pl-items__link" href="/reality/detail/440872/prodej-bytu-2-kk-v-osobnim-vlastnictvi-45-m2-praha-3-zizkov">odkaz</a>
  </div>
  <div class="pl-items__item"
       data-gps="49°01&#039;37.3&quot;N,15°51&#039;10&quot;E"
       data-display-address="Roztoky"
       data-price="Dohodou"
       data-title="Prodej chaty / chalupy 108 m², Roztoky"
       data-url="/reality/detail/440889/prodej-chaty-chalupy-108-m2-roztoky">
  </div>
</div>
</body></html>
"""

DETAIL_HTML = """
<!DOCTYPE html><html>
<head><title>Prodej bytu 2+kk, 45 m² Praha 3 - Žižkov | RE/MAX</title></head>
<body>
<h1 class="pd-header__title">
  Prodej bytu 2+kk v osobním vlastnictví 45 m², Praha 3 - Žižkov
  (ID 259-NP01246)
</h1>
<div class="pd-price-box">
  <span class="pd-price" data-advert-price="9962000">9 962 000 Kč</span>
</div>
<div class="pd-map" data-gps="50°05&#039;26.1&quot;N,14°29&#039;33.4&quot;E"
     data-address="Na vrcholu, Praha 3 - Žižkov, Praha"></div>
<div class="pd-detail-info">
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Číslo zakázky:</div><div class="pd-detail-info__value">ID 259-NP01246</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Dispozice:</div><div class="pd-detail-info__value">2+kk</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Číslo podlaží:</div><div class="pd-detail-info__value">8</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Počet podlaží v objektu:</div><div class="pd-detail-info__value">8</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Užitná plocha:</div><div class="pd-detail-info__value">45 m²</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Druh objektu:</div><div class="pd-detail-info__value">Cihlová</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Stav objektu:</div><div class="pd-detail-info__value">Velmi dobrý</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Vlastnictví:</div><div class="pd-detail-info__value">Osobní</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Typ nemovitosti:</div><div class="pd-detail-info__value">Byty</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Výtah:</div><div class="pd-detail-info__value">Ano</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Sklep:</div><div class="pd-detail-info__value">Ne</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Vybaveno:</div><div class="pd-detail-info__value">Ano</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Energetická náročnost budovy:</div><div class="pd-detail-info__value">C</div></div>
</div>
<div class="pd-detail-text">K prodeji nabízíme byt 2+kk v žádané lokalitě.</div>
<div class="pd-gallery">
  <a data-fancybox="g" href="https://mlsf.remax-czech.cz/data//zs/440872/3387561.jpg">
    <img data-thumb="https://mlsf.remax-czech.cz/data//zs/440872/3387561_th350.jpg"></a>
  <a data-fancybox="g" href="https://mlsf.remax-czech.cz/data//zs/440872/3387583.jpg">
    <img data-thumb="https://mlsf.remax-czech.cz/data//zs/440872/3387583_th350.jpg"></a>
</div>
<section class="pd-similar">
  <div class="pl-items__item" data-gps="49°00&#039;00&quot;N,15°00&#039;00&quot;E"
       data-price="1 000 000 Kč" data-title="Prodej domu, Jihlava"
       data-url="/reality/detail/999999/prodej-domu-jihlava">
    <img data-thumb="https://mlsf.remax-czech.cz/data//zs/999999/1_th350.jpg">
  </div>
</section>
</body></html>
"""

RENT_HTML = """
<!DOCTYPE html><html>
<head><title>Pronájem garážového stání, Praha 10</title></head>
<body>
<h1>Pronájem garážového stání, Praha 10 - Strašnice (ID 100-G001)</h1>
<div class="pd-price-box"><span class="pd-price" data-advert-price="2500">2 500 Kč</span></div>
<div class="pd-detail-info">
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Typ nemovitosti:</div><div class="pd-detail-info__value">Malé objekty, garáže</div></div>
</div>
</body></html>
"""

NAJEMNI_DUM_HTML = """
<!DOCTYPE html><html>
<head><title>Prodej nájemního domu 1 250 m², Brno | RE/MAX</title></head>
<body>
<h1 class="pd-header__title">Prodej nájemního domu 1 250 m², Brno (ID 100-ND001)</h1>
<div class="pd-price-box"><span class="pd-price" data-advert-price="32500000">32 500 000 Kč</span></div>
<div class="pd-detail-info">
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Typ nemovitosti:</div><div class="pd-detail-info__value">Nájemní domy</div></div>
  <div class="pd-detail-info__row"><div class="pd-detail-info__label">Celková plocha:</div><div class="pd-detail-info__value">1250 m²</div></div>
</div>
</body></html>
"""


def test_parse_index_total_and_cards():
    page = parse_index(INDEX_HTML)
    assert page.total == 6124
    assert len(page.items) == 2

    first = page.items[0]
    assert first.source_id_native == "440872"
    assert first.detail_path.endswith("praha-3-zizkov")
    assert "Prodej bytu" in (first.title or "")
    assert "Žižkov" in (first.address or "")
    assert index_price(first.price_text) == 9_962_000

    second = page.items[1]
    assert second.source_id_native == "440889"
    assert index_price(second.price_text) is None  # "Dohodou"


def test_parse_index_ids_only():
    page = parse_index(INDEX_HTML)
    assert {it.source_id_native for it in page.items} == {"440872", "440889"}


def test_category_from_typ():
    assert category_from_typ("Byty") == "byt"
    assert category_from_typ("Domy a vily") == "dum"
    assert category_from_typ("Chaty a chalupy") == "dum"
    assert category_from_typ("Pozemky") == "pozemek"
    assert category_from_typ("Kanceláře") == "komercni"
    assert category_from_typ("Malé objekty, garáže") == "ostatni"
    assert category_from_typ(None) is None
    # The live 2026 coarse vocabulary (verified against the full active walk).
    assert category_from_typ("Chaty a rekreační objekty") == "dum"
    assert category_from_typ("Hotely, penziony a restaurace") == "komercni"
    # Nájemní domy land under komercni — every portal keys cinzovni_dum there.
    assert category_from_typ("Nájemní domy") == "komercni"


def test_subtype_of():
    # legacy fine-vocabulary correspondences (typ only)
    assert subtype_of("Kanceláře") == "kancelar"
    assert subtype_of("Obchodní") == "obchodni_prostor"
    assert subtype_of("Sklady") == "sklad"
    assert subtype_of("Výroba") == "vyroba"
    assert subtype_of("Zemědělské objekty") == "zemedelsky"
    assert subtype_of("Historické objekty") == "pamatka_jine"
    # live coarse vocabulary: "Nájemní domy" is specific enough to map
    assert subtype_of("Nájemní domy") == "cinzovni_dum"
    # ambiguous combined groups are deliberately left without a subtype —
    # including "Hotely, penziony a restaurace", whose "restaurace" tail must
    # NOT mis-label a hotel as a restaurant
    assert subtype_of("Domy a vily") is None
    assert subtype_of("Chaty a chalupy") is None
    assert subtype_of("Chaty a rekreační objekty") is None
    assert subtype_of("Hotely, penziony a restaurace") is None
    assert subtype_of(None) is None


def test_subtype_of_url_noun():
    # The detail-URL noun is the per-listing type signal the coarse typ lost
    # (real production URLs).
    assert subtype_of(
        "Hotely, penziony a restaurace",
        "https://www.remax-czech.cz/reality/detail/430672/prodej-ubytovaciho-zarizeni-48-m2-praha-4-chodov",
    ) == "ubytovani"
    assert subtype_of(
        "Hotely, penziony a restaurace",
        "https://www.remax-czech.cz/reality/detail/440891/prodej-restaurace-70-m2-volyne",
    ) == "restaurace"
    assert subtype_of(
        "Nájemní domy",
        "https://www.remax-czech.cz/reality/detail/1/prodej-najemniho-cinzovniho-domu-450-m2-brno",
    ) == "cinzovni_dum"
    assert subtype_of(
        "Komerční prostory",
        "https://www.remax-czech.cz/reality/detail/2/pronajem-kancelarskych-prostor-120-m2-ostrava",
    ) == "kancelar"
    # generic nouns stay unmapped
    assert subtype_of(
        "Domy a vily",
        "https://www.remax-czech.cz/reality/detail/434384/prodej-domu-135-m2-zapy",
    ) is None
    assert subtype_of(
        "Chaty a rekreační objekty",
        "https://www.remax-czech.cz/reality/detail/439916/prodej-chaty-chalupy-38-m2-novy-malin",
    ) is None


def test_category_of_title_fallback():
    assert category_of(None, "Prodej bytu 2+kk, Praha") == "byt"
    assert category_of(None, "Prodej chaty / chalupy 108 m², Roztoky") == "dum"
    assert category_of(None, "Pronájem obchodních prostor Ostrava") == "komercni"
    assert category_of(None, "Pronájem garážového stání") == "ostatni"
    # The index walk slices nájemní domy into komercni from the title alone,
    # so the index slice and the detail parse can't disagree.
    assert category_of(None, "Prodej nájemního domu 1 250 m², Brno") == "komercni"
    assert category_of(None, "Prodej rodinného domu 135 m², Zápy") == "dum"
    # Detail "Typ nemovitosti" wins over the title noun.
    assert category_of("Byty", "Prodej domu s garáží") == "byt"


def test_type_of():
    assert type_of("Prodej bytu 2+kk") == "prodej"
    assert type_of("Pronájem garážového stání") == "pronajem"
    assert type_of("/reality/detail/1/pronajem-bytu") == "pronajem"
    assert type_of(None) is None


def test_index_price_parsing():
    assert index_price("9\xa0962\xa0000\xa0Kč") == 9_962_000
    assert index_price("4 900 000 Kč <small>(za nemovitost)</small>") == 4_900_000
    assert index_price("Info o ceně") is None
    assert index_price("Dohodou") is None
    assert index_price(None) is None


def test_parse_detail_full():
    listing = parse_detail(DETAIL_HTML, source_url=_DETAIL_URL)
    assert listing.source == "remax"
    assert listing.source_id_native == "440872"
    assert listing.source_url == _DETAIL_URL
    assert listing.category_main == "byt"        # from "Typ nemovitosti: Byty"
    assert listing.category_type == "prodej"     # from the title verb
    assert listing.price_czk == 9_962_000        # clean data-advert-price integer
    assert listing.price_unit == "za nemovitost"
    assert listing.area_m2 == 45.0
    assert listing.disposition == "2+kk"
    # data-gps DMS -> decimal, CZ-bbox-guarded.
    assert listing.lat is not None and 50.08 < listing.lat < 50.10
    assert listing.lon is not None and 14.48 < listing.lon < 14.50
    assert "Žižkov" in (listing.locality or "")
    assert listing.district == "Žižkov"
    assert listing.floor == 8
    assert listing.total_floors == 8
    assert listing.building_type == "cihla"
    assert listing.condition == "velmi_dobry"
    assert listing.ownership == "osobni"
    assert listing.energy_rating == "C"
    assert listing.has_lift is True
    assert listing.cellar is False
    assert listing.terrace is None               # absent row -> unknown, not False
    assert listing.furnished == "ano"
    assert listing.description.startswith("K prodeji")
    assert listing.raw["remax_ref"] == "ID 259-NP01246"
    # Only this listing's images, full-resolution (no _th350), recommended excluded.
    assert listing.raw["image_urls"] == [
        "https://mlsf.remax-czech.cz/data//zs/440872/3387561.jpg",
        "https://mlsf.remax-czech.cz/data//zs/440872/3387583.jpg",
    ]


def test_parse_detail_content_hash_and_to_row():
    a = parse_detail(DETAIL_HTML, source_url=_DETAIL_URL)
    b = parse_detail(DETAIL_HTML, source_url=_DETAIL_URL)
    assert a.content_hash() == b.content_hash()
    assert len(a.content_hash()) == 64

    row = a.to_row(-11)
    assert row["sreality_id"] == -11
    assert row["category_main"] == "byt"
    assert row["price_czk"] == 9_962_000
    assert row["lat"] == a.lat and row["lon"] == a.lon


def test_parse_detail_najemni_dum_subtype():
    # remax's coarse "Nájemní domy" group is the one live typ value specific
    # enough for a subtype; the row lands where every other portal keys
    # cinzovni_dum — category komercni.
    listing = parse_detail(
        NAJEMNI_DUM_HTML,
        source_url="https://www.remax-czech.cz/reality/detail/100/prodej-najemniho-domu-1250-m2-brno",
    )
    assert listing.category_main == "komercni"
    assert listing.category_type == "prodej"
    assert listing.subtype == "cinzovni_dum"
    assert listing.price_czk == 32_500_000
    assert listing.area_m2 == 1250.0


def test_parse_detail_rent_and_category_from_typ():
    listing = parse_detail(
        RENT_HTML,
        source_url="https://www.remax-czech.cz/reality/detail/100/pronajem-garazoveho-stani-praha-10",
    )
    assert listing.category_main == "ostatni"
    assert listing.category_type == "pronajem"
    assert listing.price_unit == "za mesic"
    assert listing.price_czk == 2_500
    assert "Praha 10" in (listing.locality or "")
