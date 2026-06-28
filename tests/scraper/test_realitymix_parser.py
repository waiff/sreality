"""Hermetic tests for scraper.realitymix_parser against hand-authored fixtures
that mirror the real realitymix.cz markup: div.advert-item index cards, the
"z celkem N nalezených" total, the detail BreadcrumbList JSON-LD (the category
source), the detail-information__data-item spec list, the #print-map
data-gps-lat/-lon + data-address pin, the /profil-realitniho-maklere/…-{id}
broker anchor + data-fk_rk agency id, and the st.realitymix.cz/i/…/nab_ gallery.
"""

from __future__ import annotations

from scraper.realitymix_parser import (
    _category_from_slug,
    _norm_building_type,
    _norm_condition,
    _norm_ownership,
    category_from_breadcrumb,
    index_price,
    parse_detail,
    parse_index,
    resolve_category,
)

_BYT_URL = (
    "https://realitymix.cz/detail/nupaky/"
    "prostorny-byt-2-1-s-balkonem-v-nupakach-8414569.html"
)
_DUM_URL = (
    "https://realitymix.cz/detail/lucany-nad-nisou/"
    "prodej-domy-rodinny-214-m2-jindrichov-lucany-nad-nisou-8541467.html"
)

INDEX_HTML = """
<!DOCTYPE html><html><head><title>Byty prodej</title></head><body>
<div class="results-head">výsledky 1-20 z celkem 8928 nalezených</div>
<div class="w-full advert-item">
  <a href="https://realitymix.cz/detail/nupaky/prostorny-byt-2-1-8414569.html" class="img-link"></a>
  <div class="advert-item__content-data">
    <h2 class="text-lg sm:text-xl font-extrabold">
      <a href="https://realitymix.cz/detail/nupaky/prostorny-byt-2-1-8414569.html">Prodej bytu, 2+1, 66 m²</a>
    </h2>
    <p class="text-sm text-body-light">Luční, Nupaky, okr. Praha-východ</p>
    <div class="text-xl font-extrabold mb-2.5"><span>Cena na vyžádání</span></div>
  </div>
</div>
<div class="w-full advert-item">
  <a href="https://realitymix.cz/detail/lucany-nad-nisou/prodej-domy-8541467.html" class="img-link"></a>
  <div class="advert-item__content-data">
    <h2 class="text-lg font-extrabold">
      <a href="https://realitymix.cz/detail/lucany-nad-nisou/prodej-domy-8541467.html">Prodej domu 214 m²</a>
    </h2>
    <p class="text-sm text-body-light">Jindřichov, Lučany nad Nisou</p>
    <div class="text-xl font-extrabold mb-2.5"><span>5 290 000 Kč</span></div>
  </div>
</div>
</body></html>
"""

BYT_HTML = """
<!DOCTYPE html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[
 {"@type":"ListItem","position":1,"item":{"@id":"https://realitymix.cz/","name":"RealityMix"}},
 {"@type":"ListItem","position":2,"item":{"@id":"https://realitymix.cz/reality/byty","name":"Byty"}},
 {"@type":"ListItem","position":3,"item":{"@id":"https://realitymix.cz/reality/byty/prodej","name":"Prodej"}},
 {"@type":"ListItem","position":4,"item":{"@id":"https://realitymix.cz/reality/byty/2+1/prodej","name":"2+1"}}
]}
</script>
</head><body>
<h1>Prostorný byt 2+1 s balkonem v Nupakách</h1>
<table><tbody>
  <tr class="advert-description__short-props-price"><td>Cena:</td>
    <td>5 290 000 Kč &nbsp;<button>Nabídněte cenu</button></td></tr>
</tbody></table>
<div id="print-map" data-gps-lon="14.604206388889" data-gps-lat="49.989288333333"
     data-address="Luční, Nupaky, okres Praha-východ"></div>
<a href="/profil-realitniho-maklere/jan-novak-1873381">Jan Novák</a>
<img src="https://st.realitymix.cz/i/1269965/makleri/makler_1873381.jpg" alt="Jan Novák">
<div class="gallery">
  <img data-src="https://st.realitymix.cz/i/1269965/8414569/nab_493218621.jpg"
       data-thumb="https://st.realitymix.cz/i/1269965/8414569/nab_493218621_nahled.jpg">
  <img data-src="https://st.realitymix.cz/i/1269965/8414569/nab_493218623.jpg">
</div>
<div class="advert-description__text-inner-inner">Prostorný byt 2+1 s balkonem. Ideální pro bydlení i investici.</div>
<ul class="detail-information">
  <li class="detail-information__data-item"><span>Dispozice bytu:</span><span>2+1</span></li>
  <li class="detail-information__data-item"><span>Číslo podlaží v domě:</span><span>2</span></li>
  <li class="detail-information__data-item"><span>Počet podlaží objektu:</span><span>1</span></li>
  <li class="detail-information__data-item"><span>Celková podlahová plocha:</span><span>66 m²</span></li>
  <li class="detail-information__data-item"><span>Druh objektu:</span><span>cihlová</span></li>
  <li class="detail-information__data-item"><span>Stav objektu:</span><span>velmi dobrý</span></li>
  <li class="detail-information__data-item"><span>Vlastnictví:</span><span>osobní</span></li>
  <li class="detail-information__data-item"><span>Balkon:</span><span>4 m²</span></li>
  <li class="detail-information__data-item"><span>Vybaveno:</span><span>ano</span></li>
  <li class="detail-information__data-item"><span>Energetická náročnost budovy:</span><span>C - Úsporná</span></li>
</ul>
<div data-fk_rk="1269965" data-id="8414569"></div>
</body></html>
"""

DUM_HTML = """
<!DOCTYPE html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[
 {"@type":"ListItem","position":1,"item":{"@id":"https://realitymix.cz/","name":"RealityMix"}},
 {"@type":"ListItem","position":2,"item":{"@id":"https://realitymix.cz/reality/domy","name":"Domy"}},
 {"@type":"ListItem","position":3,"item":{"@id":"https://realitymix.cz/reality/domy/prodej","name":"Prodej"}}
]}
</script>
</head><body>
<h1>Prodej rodinného domu 214 m²</h1>
<table><tr class="advert-description__short-props-price"><td>Cena:</td><td>5 290 000 Kč</td></tr></table>
<div id="print-map" data-gps-lon="15.20100" data-gps-lat="50.71500"
     data-address="Jindřichov, Lučany nad Nisou, okres Jablonec nad Nisou"></div>
<ul class="detail-information">
  <li class="detail-information__data-item"><span>Poloha objektu:</span><span>samostatný</span></li>
  <li class="detail-information__data-item"><span>Druh objektu:</span><span>smíšená</span></li>
  <li class="detail-information__data-item"><span>Stav objektu:</span><span>dobrý</span></li>
  <li class="detail-information__data-item"><span>Počet podlaží objektu:</span><span>2</span></li>
  <li class="detail-information__data-item"><span>Plocha parcely:</span><span>3028 m²</span></li>
  <li class="detail-information__data-item"><span>Užitná plocha:</span><span>214 m²</span></li>
  <li class="detail-information__data-item"><span>Ostatní:</span><span>Garáž, Parkoviště</span></li>
</ul>
</body></html>
"""

RENT_NO_PRICE_HTML = """
<!DOCTYPE html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[
 {"@type":"ListItem","position":2,"item":{"@id":"https://realitymix.cz/reality/byty","name":"Byty"}},
 {"@type":"ListItem","position":3,"item":{"@id":"https://realitymix.cz/reality/byty/pronajem","name":"Pronájem"}}
]}
</script>
</head><body>
<h1>Pronájem bytu 2+kk 48 m²</h1>
<table><tr class="advert-description__short-props-price"><td>Cena:</td><td>Cena na vyžádání</td></tr></table>
<ul class="detail-information">
  <li class="detail-information__data-item"><span>Dispozice bytu:</span><span>2+kk</span></li>
  <li class="detail-information__data-item"><span>Celková podlahová plocha:</span><span>48 m²</span></li>
</ul>
</body></html>
"""


def test_parse_index_total_and_items():
    page = parse_index(INDEX_HTML)
    assert page.total == 8928
    assert len(page.items) == 2
    first = page.items[0]
    assert first.source_id_native == "8414569"
    assert first.detail_path.endswith("prostorny-byt-2-1-8414569.html")
    assert "2+1" in (first.title or "")
    assert first.price_text == "Cena na vyžádání"
    assert page.items[1].price_text == "5 290 000 Kč"


def test_parse_index_ids():
    page = parse_index(INDEX_HTML)
    assert {it.source_id_native for it in page.items} == {"8414569", "8541467"}


def test_index_price_parsing():
    assert index_price("5 290 000 Kč") == 5_290_000
    assert index_price("Cena na vyžádání") is None
    assert index_price("Rezervováno") is None
    assert index_price("info v RK") is None
    assert index_price(None) is None
    # Two runs (struck original + current) must NOT concatenate to an overflow.
    assert index_price("12 000 000 Kč 5 290 000 Kč") == 12_000_000


def test_category_from_breadcrumb():
    assert category_from_breadcrumb(BYT_HTML) == ("byt", "prodej")
    assert category_from_breadcrumb(DUM_HTML) == ("dum", "prodej")
    assert category_from_breadcrumb(RENT_NO_PRICE_HTML) == ("byt", "pronajem")


def test_parse_detail_byt_full():
    listing = parse_detail(BYT_HTML, source_url=_BYT_URL)
    assert listing.source == "realitymix"
    assert listing.source_id_native == "8414569"
    assert listing.source_url == _BYT_URL
    assert listing.category_main == "byt"
    assert listing.category_type == "prodej"
    assert listing.price_czk == 5_290_000
    assert listing.price_unit == "za nemovitost"
    assert listing.area_m2 == 66.0
    assert listing.disposition == "2+1"
    assert listing.lat == 49.989288333333
    assert listing.lon == 14.604206388889
    # Street = the first segment of #print-map data-address, past the don't-fabricate
    # guard (Nupaky / okres Praha-východ are NOT mistaken for the street).
    assert listing.street == "Luční"
    assert listing.district == "Praha-východ"
    assert listing.floor == 2
    assert listing.total_floors == 1
    assert listing.building_type == "cihla"
    assert listing.condition == "velmi_dobry"
    assert listing.ownership == "osobni"
    assert listing.furnished == "ano"
    assert listing.has_balcony is True
    assert listing.energy_rating == "C"
    assert listing.description.startswith("Prostorný byt 2+1")
    # Broker: stable profile id (per-broker key) + agency id, identity-only.
    assert listing.raw["broker"] == {"broker_id": "1873381", "agency_id": "1269965"}
    # Full-size photos only — the _nahled thumbnail is excluded.
    assert listing.raw["image_urls"] == [
        "https://st.realitymix.cz/i/1269965/8414569/nab_493218621.jpg",
        "https://st.realitymix.cz/i/1269965/8414569/nab_493218623.jpg",
    ]
    assert listing.raw["coords"]["source"] == "page"


def test_parse_detail_dum_area_estate_and_garage():
    listing = parse_detail(DUM_HTML, source_url=_DUM_URL)
    assert listing.category_main == "dum"
    assert listing.category_type == "prodej"
    assert listing.area_m2 == 214.0           # "Užitná plocha" when no flat-area row
    assert listing.usable_area == 214.0
    assert listing.estate_area == 3028.0      # "Plocha parcely"
    assert listing.building_type == "smisena"
    assert listing.condition == "dobry"
    assert listing.total_floors == 2
    assert listing.garage is True             # from the "Ostatní: Garáž, Parkoviště" row
    assert listing.has_parking is True
    # data-address first segment "Jindřichov" is a místní část, not a street ->
    # dropped by the morphology gate (don't fabricate a street from a settlement).
    assert listing.street is None
    assert listing.district == "Jablonec nad Nisou"


def test_parse_detail_price_on_request_is_none_for_rent():
    listing = parse_detail(RENT_NO_PRICE_HTML, source_url="https://realitymix.cz/detail/x/y-2.html")
    assert listing.price_czk is None
    assert listing.price_unit == "za mesic"
    assert listing.area_m2 == 48.0
    assert listing.disposition == "2+kk"
    assert listing.lat is None and listing.lon is None   # no coords on the page
    assert listing.raw["broker"] is None                 # no broker block on the page


# A truncated BreadcrumbList (only the home crumb) — realitymix serves this for
# some atypical listings (e.g. room rentals); category must fall back to the slug.
TRUNCATED_BREADCRUMB_HTML = """
<!DOCTYPE html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[
 {"@type":"ListItem","position":1,"item":{"@id":"https://realitymix.cz/","name":"RealityMix"}}
]}
</script>
</head><body>
<h1>Pronájem pokoje 24 m²</h1>
<div id="print-map" data-gps-lon="15.90" data-gps-lat="50.20" data-address="Praskačka, okres Hradec Králové"></div>
<ul class="detail-information">
  <li class="detail-information__data-item"><span>Celková podlahová plocha:</span><span>24 m²</span></li>
</ul>
</body></html>
"""

# A listing realitymix renders WITHOUT a #print-map (no coords / no data-address):
# the street is recovered from the slug's "-ul-{street}-{id}.html" tail.
NO_MAP_SLUG_STREET_URL = (
    "https://realitymix.cz/detail/ostrava/pronajem-bytu-2-kk-74-m-ostrava-ul-lidicka-8429105.html"
)
NO_MAP_HTML = """
<!DOCTYPE html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[
 {"@type":"ListItem","position":2,"item":{"@id":"https://realitymix.cz/reality/byty","name":"Byty"}},
 {"@type":"ListItem","position":3,"item":{"@id":"https://realitymix.cz/reality/byty/pronajem","name":"Pronájem"}}
]}
</script>
</head><body>
<h1>Pronájem bytu 2+kk 74 m²</h1>
<ul class="detail-information">
  <li class="detail-information__data-item"><span>Dispozice bytu:</span><span>2+kk</span></li>
  <li class="detail-information__data-item"><span>Celková podlahová plocha:</span><span>74 m²</span></li>
</ul>
</body></html>
"""


def test_category_from_slug_fallback():
    assert _category_from_slug("https://realitymix.cz/detail/x/pronajem-pokoje-24-m-8560665.html") == ("byt", "pronajem")
    assert _category_from_slug("https://realitymix.cz/detail/x/prodej-domy-rodinny-214-m2-9.html") == ("dum", "prodej")
    assert _category_from_slug("https://realitymix.cz/detail/x/prodej-pozemku-800-m2-9.html") == ("pozemek", "prodej")
    assert _category_from_slug("https://realitymix.cz/detail/x/pronajem-komercni-prostory-9.html") == ("komercni", "pronajem")


def test_resolve_category_falls_back_when_breadcrumb_truncated():
    # Breadcrumb alone yields nothing (only the home crumb); the slug fills both.
    assert category_from_breadcrumb(TRUNCATED_BREADCRUMB_HTML) == (None, None)
    listing = parse_detail(
        TRUNCATED_BREADCRUMB_HTML,
        source_url="https://realitymix.cz/detail/praskacka/pronajem-pokoje-24-m-8560665.html",
    )
    assert listing.category_main == "byt"
    assert listing.category_type == "pronajem"
    assert resolve_category(BYT_HTML, _BYT_URL) == ("byt", "prodej")   # breadcrumb still wins when present


def test_street_recovered_from_slug_when_no_map():
    listing = parse_detail(NO_MAP_HTML, source_url=NO_MAP_SLUG_STREET_URL)
    assert listing.lat is None and listing.lon is None   # no #print-map on the page
    assert listing.street == "Lidicka"                   # mined from the -ul-lidicka- slug
    # No data-address -> locality is rebuilt from the URL town (+ slug street) so the
    # detail-drain can geocode it; also the display label.
    assert listing.locality == "Lidicka, Ostrava"
    assert listing.disposition == "2+kk"
    assert listing.area_m2 == 74.0


def test_fallback_locality_built_from_url():
    from scraper.realitymix_parser import _fallback_locality, _town_from_url
    assert _town_from_url("https://realitymix.cz/detail/velke-mezirici/pronajem-1.html") == "Velke Mezirici"
    assert _fallback_locality(
        "https://realitymix.cz/detail/ostrava/x-ul-lidicka-8429105.html", "Lidicka"
    ) == "Lidicka, Ostrava"
    assert _fallback_locality("https://realitymix.cz/detail/ostrava/x-1.html", None) == "Ostrava"
    assert _fallback_locality("", None) is None


def test_mapbearing_listing_keeps_rich_data_address_locality():
    # A page WITH #print-map keeps the full data-address, not the URL fallback.
    listing = parse_detail(BYT_HTML, source_url=_BYT_URL)
    assert listing.locality == "Luční, Nupaky, okres Praha-východ"


def test_enum_normalization_aligned_to_sreality_vocabulary():
    assert _norm_condition("velmi dobrý") == "velmi_dobry"
    assert _norm_condition("Dobrý") == "dobry"
    assert _norm_condition("Bezvadný") == "velmi_dobry"
    assert _norm_condition("K rekonstrukci") == "pred_rekonstrukci"
    assert _norm_condition("Novostavba") == "novostavba"
    assert _norm_building_type("cihlová") == "cihla"
    assert _norm_building_type("Smíšená") == "smisena"
    assert _norm_building_type("Panelová") == "panel"
    assert _norm_ownership("osobní") == "osobni"
    assert _norm_ownership("Družstevní") == "druzstevni"


def test_content_hash_and_to_row_bridge_to_ingest():
    a = parse_detail(BYT_HTML, source_url=_BYT_URL)
    b = parse_detail(BYT_HTML, source_url=_BYT_URL)
    assert a.content_hash() == b.content_hash()
    assert len(a.content_hash()) == 64
    row = a.to_row(-7)
    assert row["sreality_id"] == -7
    assert row["category_main"] == "byt"
    assert row["price_czk"] == 5_290_000
    assert row["area_m2"] == 66.0
    assert row["lat"] == 49.989288333333
    assert row["street"] == "Luční"
