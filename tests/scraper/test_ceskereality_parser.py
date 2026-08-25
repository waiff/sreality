"""Hermetic tests for scraper.ceskereality_parser against hand-authored fixtures
that mirror the real ceskereality.cz markup: article.i-estate index cards, the
?strana pager, the detail JSON-LD product block, the i-info spec list, the
data-coord-lat/lng pin, the /realitni-makleri/…-{id}/ broker anchor, and the
img.ceskereality.cz/foto gallery.
"""

from __future__ import annotations

from scraper.ceskereality_parser import (
    _norm_building_type,
    _norm_condition,
    _norm_ownership,
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
    assert listing.area_basis == "usable"
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


PUBLISHED_HTML = """
<!DOCTYPE html><html><body>
<h1>Prodej bytu 2+kk 48 m²</h1>
<input data-coord-lat="50.08110" data-coord-lng="14.42030">
<dl class="g-info">
  <div class="i-info"><span class="i-info__title">Datum vložení</span><span class="i-info__value"> 10. února 2020 </span></div>
  <div class="i-info"><span class="i-info__title">Plocha užitná</span><span class="i-info__value">48 m²</span></div>
</dl>
</body></html>
"""


def test_parse_detail_published_at_from_datum_vlozeni():
    # "Datum vložení" is ceskereality's insertion date, Czech long-form
    # month name ("10. února 2020").
    from datetime import date

    listing = parse_detail(
        PUBLISHED_HTML, source_url=_DETAIL_URL,
        category_main="byt", category_type="prodej",
    )
    assert listing.published_at == date(2020, 2, 10)


def test_parse_detail_published_at_none_when_absent():
    # DETAIL_HTML carries no "Datum vložení" row.
    listing = parse_detail(
        DETAIL_HTML, source_url=_DETAIL_URL, category_main="byt", category_type="prodej",
    )
    assert listing.published_at is None


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


def test_enum_normalization_aligned_to_sreality_vocabulary():
    # Divergent ceskereality labels map onto sreality's canonical values so a
    # cross-portal filter agrees; already-matching values pass through.
    assert _norm_condition("Bezvadný") == "velmi_dobry"
    assert _norm_condition("K rekonstrukci") == "pred_rekonstrukci"
    assert _norm_condition("Rozestavěný") == "ve_vystavbe"
    assert _norm_condition("Dobrý") == "dobry"
    assert _norm_condition("Po rekonstrukci") == "po_rekonstrukci"
    assert _norm_building_type("Zděná") == "cihla"
    assert _norm_building_type("Cihlová") == "cihla"
    assert _norm_building_type("Panelová") == "panel"
    assert _norm_building_type("Jiná") == "jina"          # no sreality equiv -> left as-is
    assert _norm_ownership("Státní, obecní, jiné") == "statni"
    assert _norm_ownership("soukromé") == "osobni"
    assert _norm_ownership("Družstevní") == "druzstevni"


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


def test_title_street_and_okres(monkeypatch):
    # W0 item 0j: the <title> states the accented street (", ulice Písecká,")
    # and the okres — live-verified shape 2026-08-10. Street beats the
    # ASCII-folded slug; okres fills the hitherto always-NULL district.
    from scraper.ceskereality_parser import _TITLE_OKRES_RE, _TITLE_STREET_RE

    title = ("Pronájem komerčního pozemku, 1 085 m², Strakonice, "
             "ulice Písecká, okres Strakonice - ČESKÉREALITY.cz inzerce realit")
    assert _TITLE_STREET_RE.search(title).group(1).strip() == "Písecká"
    assert _TITLE_OKRES_RE.search(title).group(1).strip() == "okres Strakonice"
    # No street segment in a street-less title, and the site-name suffix
    # ("- ČESKÉREALITY.cz ...") never leaks into the okres capture.
    bare = "Prodej stavební parcely, 1 715 m², Žihle, okres Plzeň-sever - ČESKÉREALITY.cz"
    assert _TITLE_STREET_RE.search(bare) is None
    assert _TITLE_OKRES_RE.search(bare).group(1).strip() == "okres Plzeň-sever"


# Two DIFFERENT labelled measures on one page: the discriminating case for the
# portal-label -> typed-slot mapping. Without it, swapping the kwargs in
# parse_detail's derive_headline_area call is a silent wrong value under a
# confident wrong label, and the whole suite stays green.

def test_uzitna_beats_bare_plocha_and_says_so():
    cell = '<div class="i-info"><span class="i-info__title">{}</span>' \
           '<span class="i-info__value"> {} </span></div>'
    html = DETAIL_HTML.replace(
        cell.format("Plocha užitná", "41 m²"),
        cell.format("Plocha užitná", "41 m²") + cell.format("Plocha", "58 m²"),
    )
    listing = parse_detail(
        html, source_url=_DETAIL_URL, category_main="byt", category_type="prodej",
    )
    assert (listing.area_m2, listing.area_basis) == (41.0, "usable")


def test_bare_plocha_alone_is_a_total_not_an_uzitna():
    # The pre-collapse the resolver exists to prevent: ceskereality's usable_area
    # column has always folded "Plocha užitná" / "Plocha" into ONE string, so a page
    # carrying only the bare "Plocha" used to reach area_m2 stamped as an interior
    # užitná. Separate slots, separate labels.
    cell = '<div class="i-info"><span class="i-info__title">{}</span>' \
           '<span class="i-info__value"> {} </span></div>'
    html = DETAIL_HTML.replace(
        cell.format("Plocha užitná", "41 m²"), cell.format("Plocha", "58 m²"),
    )
    listing = parse_detail(
        html, source_url=_DETAIL_URL, category_main="byt", category_type="prodej",
    )
    assert (listing.area_m2, listing.area_basis) == (58.0, "total")


def _with_cena(cell_text: str) -> str:
    spec = '<div class="i-info"><span class="i-info__title">Cena</span>' \
           '<span class="i-info__value"> {} </span></div>'
    return DETAIL_HTML.replace(
        '<dl class="g-info">', '<dl class="g-info">' + spec.format(cell_text),
    )


def test_jsonld_offer_is_vetoed_by_a_per_area_cena_cell():
    # The JSON-LD offer is a bare NUMBER, and ceskereality puts the RATE there
    # verbatim: production carries `"price":100` beside `100 Kč za m²/měsíc`.
    # W1's parse-time rail only guarded the fallback path, so the primary path
    # walked 1,254+ per-m² rows straight into price_czk. Measured 2026-08-24.
    listing = parse_detail(
        _with_cena("100 Kč za m²/měsíc"), source_url=_DETAIL_URL,
        category_main="komercni", category_type="pronajem",
    )
    assert listing.price_czk is None


def test_jsonld_offer_survives_an_ordinary_cena_cell():
    listing = parse_detail(
        _with_cena("6 999 000 Kč"), source_url=_DETAIL_URL,
        category_main="byt", category_type="prodej",
    )
    assert listing.price_czk == 6_999_000


def test_jsonld_offer_survives_a_per_area_note_beside_the_total():
    listing = parse_detail(
        _with_cena("6 999 000 Kč (170 707 Kč/m²)"), source_url=_DETAIL_URL,
        category_main="byt", category_type="prodej",
    )
    assert listing.price_czk == 6_999_000


def test_jsonld_offer_survives_when_the_page_has_no_cena_cell_at_all():
    listing = parse_detail(
        DETAIL_HTML, source_url=_DETAIL_URL,
        category_main="byt", category_type="prodej",
    )
    assert listing.price_czk == 6_999_000
