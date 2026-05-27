"""Hermetic tests for scraper.idnes_parser against hand-authored fixtures.

Minimal synthetic HTML mirroring the real reality.idnes.cz markup observed in
the captured fixtures: c-products__item index cards, the b-detail header, the
<dl><dt>/<dd> spec list, and the map config carrying the subject coordinates.
The skip-when-absent sanity checks against the captured fixtures live in
tests/scraper/test_source_parsers/test_real_fixtures.py.
"""

from __future__ import annotations

from scraper.idnes_parser import parse_detail, parse_index

INDEX_HTML = """
<!DOCTYPE html><html><body>
<p class="srch-results__count">Zobrazujeme 1 - 25 z 24 607 inzerátů</p>
<ul>
  <li class="c-products__item">
    <a class="c-products__link" href="https://reality.idnes.cz/detail/prodej/byt/moravska-trebova/6a1777b49f217d2231096bd7/">odkaz</a>
    <p class="c-products__title"><span class="badges__item">Nové</span> prodej bytu 2+kk 63 m²</p>
    <p class="c-products__info">Moravská Třebová - Sušice, okres Svitavy</p>
    <span class="c-products__price">2 130 000 Kč</span>
  </li>
  <li class="c-products__item">
    <a class="c-products__link" href="https://reality.idnes.cz/detail/prodej/byt/tachov/6a16d3869909f5c07d0be027/">odkaz</a>
    <p class="c-products__title">prodej bytu 3+1 75 m²</p>
    <p class="c-products__info">Želivského, Tachov</p>
    <span class="c-products__price">3 800 000 Kč</span>
  </li>
</ul>
<div class="paginator paging">
  <a class="btn paging__item" href="/s/prodej/byty/?page=1">1</a>
  <a class="btn paging__item next" href="/s/prodej/byty/?page=2">Další</a>
</div>
</body></html>
"""

DETAIL_HTML = """
<!DOCTYPE html><html><head>
<meta property="og:image" content="https://sta-reality2.1gr.cz/sta/compile/thumbs/c/3/c/og.jpg">
</head><body>
<div class="b-detail">
  <h1 class="b-detail__title"><span>Prodej bytu 1+kk 27 m²</span></h1>
  <p class="b-detail__info">Alšova, Bílina - Pražské Předměstí, okres Teplice</p>
  <p class="b-detail__price"><strong>1&zwj;&nbsp;190&zwj;&nbsp;000&zwj;&nbsp;Kč</strong></p>
</div>
<div class="b-gallery">
  <img data-src="https://sta-reality2.1gr.cz/sta/compile/thumbs/a/1/2/one.jpg">
  <img data-src="https://sta-reality2.1gr.cz/sta/compile/thumbs/b/3/4/two.webp">
</div>
<div class="b-definition-columns">
  <dl>
    <dt>Lokalita objektu</dt><dd>klidná část</dd>
    <dt>Užitná plocha</dt><dd>27 m<sup>2</sup></dd>
    <dt>Podlaží</dt><dd>přízemí (1. NP = nadzemní podlaží)</dd>
    <dt>Počet podlaží budovy</dt><dd>5</dd>
    <dt>Konstrukce budovy</dt><dd><a href="/s/prodej/byty/?konstrukce=panelova">panelová</a></dd>
    <dt>Stav bytu</dt><dd><a href="/s/prodej/byty/udrzovane/">dobrý stav</a></dd>
    <dt>Vlastnictví</dt><dd><a href="/s/prodej/byty/?vlastnictvi=osobni">osobní</a></dd>
    <dt>Vybavení</dt><dd>částečně zařízený</dd>
    <dt>PENB</dt><dd>G (vyhl. č. 264/2020 Sb.)</dd>
    <dt>Číslo zakázky</dt><dd>IDNES-941053</dd>
    <dt>Cena</dt><dd>1 190 000 Kč</dd>
  </dl>
</div>
<div class="b-desc pt-10 mt-10"><h2>Prodej bytu 1+kk, 27 m²</h2>
  <p>Nabízíme Vám k prodeji byt o dispozici 1+kk ve vyhledávané lokalitě.</p></div>
<script type="application/json" data-maptiler-json>
{ "mtMapOptions": { "container": "app-maps", "center": [13.63911772, 50.49917731], "zoom": 16 } }
</script>
</body></html>
"""

# A rental listing whose price is on request (no digits) + GeoJSON-only coords.
ON_REQUEST_HTML = """
<!DOCTYPE html><html><body>
<div class="b-detail">
  <h1 class="b-detail__title"><span>Pronájem bytu 2+1 68 m²</span></h1>
  <p class="b-detail__info">Veveří, Brno - střed, okres Brno-město</p>
  <p class="b-detail__price"><strong>Informace o ceně u RK</strong></p>
</div>
<div class="b-definition-columns"><dl>
  <dt>Užitná plocha</dt><dd>68 m<sup>2</sup></dd>
  <dt>Podlaží</dt><dd>3. NP</dd>
</dl></div>
<script>var x = { "geometry": { "type": "Point", "coordinates": [16.6068, 49.2002] } };</script>
</body></html>
"""


def test_parse_index_total_items_and_next_page():
    page = parse_index(INDEX_HTML)
    assert page.total == 24607
    assert page.next_page == 2
    assert len(page.items) == 2

    first = page.items[0]
    assert first.source_id_native == "6a1777b49f217d2231096bd7"
    assert first.detail_path.endswith("/6a1777b49f217d2231096bd7/")
    assert "2+kk" in (first.title or "")
    assert first.price_text == "2 130 000 Kč"
    assert "Svitavy" in (first.locality_text or "")


def test_parse_detail_full():
    url = "https://reality.idnes.cz/detail/prodej/byt/bilina-alsova/6a16ab1da57ad6e19a0377e7/"
    listing = parse_detail(
        DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej"
    )

    assert listing.source == "idnes"
    assert listing.source_id_native == "6a16ab1da57ad6e19a0377e7"
    assert listing.source_url == url
    assert listing.category_main == "byt"
    assert listing.category_type == "prodej"
    assert listing.price_czk == 1_190_000
    assert listing.price_unit == "za nemovitost"
    assert listing.area_m2 == 27.0
    assert listing.usable_area == 27.0
    assert listing.disposition == "1+kk"
    assert listing.floor == 0  # přízemí
    assert listing.total_floors == 5
    assert listing.building_type == "panelová"
    assert listing.condition == "dobrý stav"
    assert listing.ownership == "osobni"
    assert listing.furnished == "castecne"
    assert listing.energy_rating == "G"
    assert listing.locality == "Bílina - Pražské Předměstí"
    assert listing.district == "Teplice"
    assert listing.lat == 50.49917731
    assert listing.lon == 13.63911772
    assert listing.description.startswith("Prodej bytu 1+kk")
    assert listing.raw["reference"] == "IDNES-941053"
    assert len(listing.raw["image_urls"]) == 2


def test_parse_detail_content_hash_stable():
    url = "https://reality.idnes.cz/detail/prodej/byt/x/6a16ab1da57ad6e19a0377e7/"
    a = parse_detail(DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej")
    b = parse_detail(DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej")
    assert a.content_hash() == b.content_hash()
    assert len(a.content_hash()) == 64


def test_parsed_listing_bridges_into_ingest_contract():
    url = "https://reality.idnes.cz/detail/prodej/byt/x/6a16ab1da57ad6e19a0377e7/"
    listing = parse_detail(DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej")
    row = listing.to_row(-7)
    assert row["sreality_id"] == -7
    assert row["category_main"] == "byt"
    assert row["price_czk"] == 1_190_000
    assert row["area_m2"] == 27.0
    assert row["lat"] == 50.49917731
    assert row["lon"] == 13.63911772


def test_parse_detail_price_on_request_and_geojson_coords():
    url = "https://reality.idnes.cz/detail/pronajem/byt/brno-veveri/69f8c898e58c5ab7d70f3505/"
    listing = parse_detail(
        ON_REQUEST_HTML, source_url=url,
        category_main="byt", category_type="pronajem",
    )
    assert listing.price_czk is None
    assert listing.price_unit == "za mesic"
    assert listing.disposition == "2+1"
    assert listing.area_m2 == 68.0
    assert listing.floor == 2  # 3. NP -> 2
    # No map config center; coords fall back to the embedded GeoJSON Point.
    assert listing.lat == 49.2002
    assert listing.lon == 16.6068
