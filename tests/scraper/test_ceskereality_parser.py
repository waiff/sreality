"""Hermetic tests for scraper.ceskereality_parser against hand-authored fixtures
that mirror the real ceskereality.cz markup: article.i-estate index cards, the
?strana pager, the detail JSON-LD product block, the i-info spec list, the
data-coord-lat/lng pin, the /realitni-makleri/…-{id}/ broker anchor, and the
img.ceskereality.cz/foto gallery.
"""

from __future__ import annotations

from scraper.ceskereality_parser import (
    category_from_url,
    index_price,
    parse_detail,
    parse_index,
)

_DETAIL_URL = (
    "https://www.ceskereality.cz/prodej/byty/byty-1-1/praha/"
    "prodej-bytu-1-1-41-m2-moldavska-3754200.html"
)

INDEX_HTML = """
<!DOCTYPE html><html><head>
<meta name="description" content="Hledáte byty na prodej? Máme tady 8221 bytů, podívejte." />
</head><body>
<div class="g-estates">
  <article class="i-estate ga-tip-region-zobrazeni" id-nemovitosti="3754200">
    <aside class="i-estate__image">
      <a href="/prodej/byty/byty-1-1/praha/prodej-bytu-1-1-41-m2-moldavska-3754200.html"
         class="i-estate__image-link u-img-hover"></a>
    </aside>
    <div class="i-estate__content">
      <div class="i-estate__header">
        <a class="i-estate__title-link" href="/prodej/byty/byty-1-1/praha/prodej-bytu-1-1-41-m2-moldavska-3754200.html">
          <span class="i-estate__header-title">Prodej bytu 1+1 41 m² Praha</span>
        </a>
      </div>
      <div class="i-estate__footer">
        <span class="i-estate__footer-price">
          <span class="i-estate__footer-price-value">6 999 000 Kč</span>
        </span>
      </div>
    </div>
  </article>
  <article class="i-estate" id-nemovitosti="3764546">
    <aside class="i-estate__image">
      <a href="/prodej/byty/byty-4-1/marianske-lazne/prodej-bytu-4-1-103-m2-ceska-3764546.html"
         class="i-estate__image-link"></a>
    </aside>
    <div class="i-estate__content">
      <div class="i-estate__header">
        <a class="i-estate__title-link" href="/prodej/byty/byty-4-1/marianske-lazne/prodej-bytu-4-1-103-m2-ceska-3764546.html">
          <span class="i-estate__header-title">Prodej bytu 4+1 103 m² Mariánské Lázně</span>
        </a>
      </div>
      <div class="i-estate__footer">
        <span class="i-estate__footer-price-value">5 990 000 Kč</span>
      </div>
    </div>
  </article>
</div>
<ul class="pagination">
  <li><a class="pagination-arrow --disabled --previous" href="/prodej/byty/"></a></li>
  <li><a class="pagination-arrow --next" href="/prodej/byty/?strana=2"></a></li>
</ul>
</body></html>
"""

DETAIL_HTML = """
<!DOCTYPE html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"individualProduct","additionalType":"Apartment",
 "name":"Prodej bytu 1+1 41 m²",
 "description":"Prodej bytu 1+1, Moldavská ulice – Praha Vršovice. Byt po rekonstrukci.",
 "image":"https://img.ceskereality.cz/foto/79329/de/debe795c2f334c2c122516ebc7488fc7.jpg",
 "offers":{"@type":"OfferForPurchase","priceCurrency":"CZK","price":6999000,
   "areaServed":{"@type":"Place","address":{"@type":"PostalAddress","streetAddress":"Moldavská","addressLocality":"Praha"}},
   "offeredby":{"@type":"RealEstateAgent","name":" Pavel Šandera ","telephone":"737703874",
     "address":{"@type":"PostalAddress","streetAddress":"Kanceláře 9","addressLocality":"Brno"}}}}
</script>
</head><body>
<h1>Prodej bytu 1+1 41 m²</h1>
<a class="btn" href="https://www.google.com/maps/?q=50.06975,14.462591944444">mapa</a>
<input type="text" data-coord-lat="50.06975" data-coord-lng="14.462591944444">
<div class="i-bar-person">
  <div class="i-bar-person__label">Nemovitost nabízí</div>
  <a href="/realitni-makleri/pavel-sandera-12345/" class="i-bar-person__title">Pavel Šandera</a>
  <a href="/realitni-kancelare/sandera-reality/" class="i-bar-person__logo">logo</a>
</div>
<section>
<dl class="g-info">
  <div class="g-info__col">
    <div class="i-info"><span class="i-info__title">Vlastnictví</span><span class="i-info__value"> soukromé </span></div>
    <div class="i-info"><span class="i-info__title">Plocha užitná</span><span class="i-info__value"> 41 m² </span></div>
    <div class="i-info"><span class="i-info__title">Konstrukce</span><span class="i-info__value"> Cihlová </span></div>
    <div class="i-info"><span class="i-info__title">Balkóny</span><span class="i-info__value"> Balkon </span></div>
  </div>
  <div class="g-info__col">
    <div class="i-info"><span class="i-info__title">Stav nemovitosti</span><span class="i-info__value"> Dobrý </span></div>
    <div class="i-info"><span class="i-info__title">Patro</span><span class="i-info__value"> 1. </span></div>
    <div class="i-info"><span class="i-info__title">Energetická náročnost</span><span class="i-info__value"> E - Nehospodárná </span></div>
  </div>
</dl>
</section>
<div class="gallery">
  <img src="https://img.ceskereality.cz/foto/79329/13/134ae6f21767a46282d98690a4c0b5b5.jpg?w=800">
  <img src="https://img.ceskereality.cz/foto/79329/17/1767994deacfeca09fbd49aa1a2973d9.jpg">
  <img src="https://img-cache.ceskereality.cz/nemovitosti/320x320_jpg/79329/x/thumb.jpg">
</div>
</body></html>
"""

# No JSON-LD streetAddress — the street must come from the SEO detail-URL slug.
SLUG_STREET_URL = (
    "https://www.ceskereality.cz/prodej/byty/byty-2-kk/praha/"
    "prodej-bytu-2-kk-48-m2-vodickova-3811111.html"
)
SLUG_STREET_HTML = """
<!DOCTYPE html><html><body>
<h1>Prodej bytu 2+kk 48 m²</h1>
<input data-coord-lat="50.08110" data-coord-lng="14.42030">
<dl class="g-info">
  <div class="i-info"><span class="i-info__title">Plocha užitná</span><span class="i-info__value">48 m²</span></div>
</dl>
</body></html>
"""

RENT_NO_PRICE_HTML = """
<!DOCTYPE html><html><body>
<h1>Pronájem bytu 2+kk 48 m²</h1>
<dl class="g-info">
  <div class="i-info"><span class="i-info__title">Plocha užitná</span><span class="i-info__value">48 m²</span></div>
  <div class="i-info"><span class="i-info__title">Cena</span><span class="i-info__value">Cena dohodou</span></div>
</dl>
</body></html>
"""


def test_parse_index_total_items_and_next_page():
    page = parse_index(INDEX_HTML)
    assert page.total == 8221
    assert len(page.items) == 2
    assert page.next_offset == 2

    first = page.items[0]
    assert first.source_id_native == "3754200"
    assert first.detail_path.endswith("moldavska-3754200.html")
    assert "1+1" in (first.title or "")
    assert first.price_text == "6 999 000 Kč"


def test_parse_index_ids():
    page = parse_index(INDEX_HTML)
    assert {it.source_id_native for it in page.items} == {"3754200", "3764546"}


def test_parse_detail_full():
    listing = parse_detail(
        DETAIL_HTML, source_url=_DETAIL_URL,
        category_main="byt", category_type="prodej",
    )
    assert listing.source == "ceskereality"
    assert listing.source_id_native == "3754200"
    assert listing.source_url == _DETAIL_URL
    assert listing.category_main == "byt"
    assert listing.category_type == "prodej"
    assert listing.price_czk == 6_999_000
    assert listing.price_unit == "za nemovitost"
    assert listing.area_m2 == 41.0
    assert listing.usable_area == 41.0
    assert listing.disposition == "1+1"
    assert listing.lat == 50.06975
    assert listing.lon == 14.462591944444
    assert listing.locality == "Moldavská, Praha"
    # Street from JSON-LD areaServed (NOT the broker office "Kanceláře 9"/Brno).
    assert listing.street == "Moldavská"
    assert listing.house_number is None
    assert listing.floor == 1
    assert listing.building_type == "cihla"
    assert listing.condition == "dobry"
    assert listing.ownership == "osobni"
    assert listing.energy_rating == "E"
    assert listing.has_balcony is True
    assert listing.description.startswith("Prodej bytu 1+1")
    # Broker: stable profile id + agency slug from the contact anchors, name +
    # phone from JSON-LD (idnes-shaped raw["broker"] block for resolve_brokers).
    assert listing.raw["broker"] == {
        "broker_id": "12345",
        "name": "Pavel Šandera",
        "phone": "737703874",
        "agency_slug": "sandera-reality",
    }
    assert len(listing.raw["image_urls"]) == 2          # img-cache thumb excluded
    assert listing.raw["image_urls"][0].endswith("134ae6f21767a46282d98690a4c0b5b5.jpg")
    assert listing.raw["coords"]["source"] == "page"


def test_parse_detail_street_from_slug_when_no_jsonld_address():
    listing = parse_detail(
        SLUG_STREET_HTML, source_url=SLUG_STREET_URL,
        category_main="byt", category_type="prodej",
    )
    # The SEO slug's street segment (ASCII-folded), capitalized for display.
    assert listing.street == "Vodickova"
    assert listing.disposition == "2+kk"
    assert listing.area_m2 == 48.0


def test_parse_detail_content_hash_and_bridges_to_ingest():
    a = parse_detail(DETAIL_HTML, source_url=_DETAIL_URL, category_main="byt", category_type="prodej")
    b = parse_detail(DETAIL_HTML, source_url=_DETAIL_URL, category_main="byt", category_type="prodej")
    assert a.content_hash() == b.content_hash()
    assert len(a.content_hash()) == 64

    row = a.to_row(-7)
    assert row["sreality_id"] == -7
    assert row["category_main"] == "byt"
    assert row["price_czk"] == 6_999_000
    assert row["area_m2"] == 41.0
    assert row["lat"] == 50.06975
    assert row["lon"] == 14.462591944444
    assert row["street"] == "Moldavská"


def test_category_from_detail_url():
    assert category_from_url(
        "https://www.ceskereality.cz/prodej/byty/byty-1-1/praha/x-3754200.html"
    ) == ("byt", "prodej")
    assert category_from_url(
        "https://www.ceskereality.cz/pronajem/komercni-prostory/brno/y-12.html"
    ) == ("komercni", "pronajem")
    assert category_from_url(
        "https://www.ceskereality.cz/prodej/chaty-chalupy/x/z-9.html"
    ) == ("dum", "prodej")
    # Full coverage: houses + land (the categories the original config omitted).
    assert category_from_url(
        "https://www.ceskereality.cz/prodej/rodinne-domy/rodinne-domy/drnovice/d-3700657.html"
    ) == ("dum", "prodej")
    assert category_from_url(
        "https://www.ceskereality.cz/pronajem/pozemky/stavebni-parcely/chyne/p-3227497.html"
    ) == ("pozemek", "pronajem")


def test_index_price_parsing():
    assert index_price("6 999 000 Kč") == 6_999_000
    assert index_price("Cena dohodou") is None
    assert index_price(None) is None


def test_price_takes_first_run_and_clamps_to_int():
    # Two numbers in the price text must NOT concatenate (it overflows the
    # price_czk integer column); take the first run only.
    assert index_price("12 000 000 Kč 6 999 000 Kč") == 12_000_000
    assert index_price("9 999 999 999 Kč") is None


def test_parse_detail_price_on_request_is_none_for_rent():
    listing = parse_detail(
        RENT_NO_PRICE_HTML,
        source_url="https://www.ceskereality.cz/pronajem/byty/x/y-2.html",
        category_main="byt", category_type="pronajem",
    )
    assert listing.price_czk is None
    assert listing.price_unit == "za mesic"
    assert listing.area_m2 == 48.0
    assert listing.disposition == "2+kk"
    assert listing.lat is None and listing.lon is None   # no coords, no geocoder
    assert listing.raw["broker"] is None                 # no broker block on the page
