"""Hermetic tests for scraper.idnes_parser against hand-authored fixtures that
mirror the real reality.idnes.cz markup: c-products__item index cards, the
detail <dl> spec table, the fancybox gallery, and the embedded map config
("center":[lon,lat]).
"""

from __future__ import annotations

from scraper.idnes_parser import parse_detail, parse_index

_DETAIL_URL = (
    "https://reality.idnes.cz/detail/prodej/byt/praha-8-cerneho/"
    "6a18deadbeefdeadbeef0001/"
)

INDEX_HTML = """
<!DOCTYPE html><html><body>
<p class="results-count">Nalezeno 1 234 nemovitostí</p>
<div class="c-products__list grid">
  <div class="c-products__item">
    <article>
      <a class="c-products__link" href="https://reality.idnes.cz/detail/prodej/byt/praha-8-cerneho/6a18deadbeefdeadbeef0001/">
        <h2 class="c-products__title"><span class="text-capitalize">prodej</span> bytu 3+1 69 m²</h2>
        <p class="c-products__info">Černého, Praha 8 - Střížkov</p>
        <p class="c-products__price"><strong>9 790 000 Kč</strong></p>
      </a>
    </article>
  </div>
  <div class="c-products__item c-products__item-advertisment">
    <article><a class="c-products__link" href="https://ads.example.com/x/">Ad</a></article>
  </div>
  <div class="c-products__item">
    <article>
      <a class="c-products__link" href="https://reality.idnes.cz/detail/prodej/byt/brno-stred/6a18deadbeefdeadbeef0002/">
        <h2 class="c-products__title">prodej bytu 2+kk 48 m²</h2>
        <p class="c-products__info">Brno - střed</p>
        <p class="c-products__price"><strong>5 200 000 Kč</strong></p>
      </a>
    </article>
  </div>
</div>
<div class="paginator paging">
  <a class="btn paging__item" href="/s/prodej/byty/">1</a>
  <a class="btn paging__item" href="/s/prodej/byty/?page=2">2</a>
  <a class="btn paging__item next" href="/s/prodej/byty/?page=2">Další</a>
</div>
</body></html>
"""

DETAIL_HTML = """
<!DOCTYPE html><html><body>
<h1>Prodej bytu 3+1 69 m²</h1>
<p class="b-detail__price"><strong>9&zwj;&nbsp;790&zwj;&nbsp;000 Kč</strong> Spočítat hypotéku</p>
<p class="b-detail__info">Černého, Praha 8 - Střížkov</p>
<dl>
  <dt>Číslo zakázky</dt><dd>IDNES-943453</dd>
  <dt>Konstrukce budovy</dt><dd><a href="/s/x?konstrukce=panelova">panelová</a></dd>
  <dt>Stav bytu</dt><dd><a href="/s/x/dobry-stav/">velmi dobrý stav</a></dd>
  <dt>Vlastnictví</dt><dd>osobní</dd>
  <dt>Užitná plocha</dt><dd>69 m<sup>2</sup></dd>
  <dt>Podlaží</dt><dd>2. patro (3. NP)</dd>
  <dt>Počet podlaží budovy</dt><dd>12 podlaží</dd>
  <dt>Vybavení</dt><dd>částečně zařízený</dd>
  <dt>PENB</dt><dd>G (vyhl. č. 148/2007 Sb.)</dd>
  <dt><a href="/s/x?vybaveni=sklep">Sklep</a></dt><dd><span class="icon icon--check"></span></dd>
  <dt><a href="/s/x?vybaveni=balkon">Balkon</a></dt><dd><span class="icon icon--check"></span></dd>
  <dt><a href="/s/x?vybaveni=vytah">Výtah</a></dt><dd><span class="icon icon--check"></span></dd>
  <dt>Parkování</dt><dd>parkování na ulici</dd>
</dl>
<div class="b-desc">Nabízíme k prodeji světlý byt 3+1 o užitné ploše 69 m² v panelovém domě.</div>
<div class="b-gallery carousel">
  <a class="carousel__item" data-fancybox="images" href="https://sta-reality2.1gr.cz/sta/compile/thumbs/3/7/8/img1.jpg?gt=r"><img src="x"></a>
  <a class="carousel__item" data-fancybox="images" href="https://sta-reality2.1gr.cz/sta/compile/thumbs/8/f/8/img2.jpg?gt=r"><img src="x"></a>
</div>
<script>window.cfg = { "mtMapOptions": { "center": [14.484176216, 50.130427866], "zoom": 16 } };</script>
</body></html>
"""

RENT_DOHODOU_HTML = """
<!DOCTYPE html><html><body>
<h1>Pronájem bytu 2+kk 48 m²</h1>
<p class="b-detail__price">Info o ceně</p>
<p class="b-detail__info">Brno - střed</p>
<dl>
  <dt>Užitná plocha</dt><dd>48 m<sup>2</sup></dd>
</dl>
</body></html>
"""


def test_parse_index_total_items_and_next_page():
    page = parse_index(INDEX_HTML)
    assert page.total == 1234
    assert len(page.items) == 2          # the advertisment card is skipped
    assert page.next_offset == 2

    first = page.items[0]
    assert first.source_id_native == "6a18deadbeefdeadbeef0001"
    assert first.detail_path.endswith("6a18deadbeefdeadbeef0001/")
    assert "3+1" in (first.title or "")
    assert first.price_text == "9 790 000 Kč"
    assert "Praha 8" in (first.locality_text or "")


def test_parse_index_only_listing_ids():
    page = parse_index(INDEX_HTML)
    assert all(len(it.source_id_native) >= 16 for it in page.items)
    assert {it.source_id_native for it in page.items} == {
        "6a18deadbeefdeadbeef0001", "6a18deadbeefdeadbeef0002",
    }


def test_parse_detail_full():
    listing = parse_detail(
        DETAIL_HTML, source_url=_DETAIL_URL,
        category_main="byt", category_type="prodej",
    )
    assert listing.source == "idnes"
    assert listing.source_id_native == "6a18deadbeefdeadbeef0001"
    assert listing.source_url == _DETAIL_URL
    assert listing.category_main == "byt"
    assert listing.category_type == "prodej"
    assert listing.price_czk == 9_790_000
    assert listing.price_unit == "za nemovitost"
    assert listing.area_m2 == 69.0
    assert listing.usable_area == 69.0
    assert listing.disposition == "3+1"
    assert listing.lat == 50.130427866
    assert listing.lon == 14.484176216
    assert "Praha 8" in (listing.locality or "")
    assert listing.floor == 2
    assert listing.total_floors == 12
    assert listing.building_type == "panel"
    assert listing.condition == "velmi_dobry"
    assert listing.ownership == "osobni"
    assert listing.furnished == "castecne"
    assert listing.energy_rating == "G"
    assert listing.has_balcony is True
    assert listing.has_lift is True
    assert listing.cellar is True
    assert listing.has_parking is True
    assert listing.terrace is None      # absent row -> unknown, not guessed False
    assert listing.description.startswith("Nabízíme")
    assert listing.raw["idnes_ref"] == "IDNES-943453"
    assert len(listing.raw["image_urls"]) == 2
    assert listing.raw["image_urls"][0].endswith("img1.jpg")  # ?gt=r stripped
    assert listing.raw["coords"]["source"] == "page"


def test_parse_detail_content_hash_stable_and_bridges_to_ingest():
    a = parse_detail(DETAIL_HTML, source_url=_DETAIL_URL, category_main="byt", category_type="prodej")
    b = parse_detail(DETAIL_HTML, source_url=_DETAIL_URL, category_main="byt", category_type="prodej")
    assert a.content_hash() == b.content_hash()
    assert len(a.content_hash()) == 64

    row = a.to_row(-7)
    assert row["sreality_id"] == -7
    assert row["category_main"] == "byt"
    assert row["price_czk"] == 9_790_000
    assert row["area_m2"] == 69.0
    assert row["lat"] == 50.130427866
    assert row["lon"] == 14.484176216


def test_parse_detail_price_on_request_is_none_for_rent():
    listing = parse_detail(
        RENT_DOHODOU_HTML, source_url="https://reality.idnes.cz/detail/pronajem/byt/brno/6a18deadbeefdeadbeef0002/",
        category_main="byt", category_type="pronajem",
    )
    assert listing.price_czk is None
    assert listing.price_unit == "za mesic"
    assert listing.area_m2 == 48.0
    assert listing.disposition == "2+kk"
    assert listing.lat is None and listing.lon is None   # no map config, no geocoder
