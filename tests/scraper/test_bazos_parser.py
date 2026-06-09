"""Hermetic tests for scraper.bazos_parser against hand-authored fixtures.

These use minimal, synthetic HTML that mirrors the documented bazos markup
(div.inzeraty.inzeratyflex index blocks, the detail details-table, the
Google-Maps locality link). The richer skipif tests against the real captured
fixtures live in tests/scraper/test_source_parsers/test_real_fixtures.py-style
checks once the fetch-fixtures workflow has run.
"""

from __future__ import annotations

import re

import pytest

from scraper.bazos_parser import (
    LINK_DISTRUST_RADIUS_KM,
    LINK_TRUST_RADIUS_KM,
    _resolve_coords,
    extract_street,
    parse_detail,
    parse_index,
)
from scraper.geocoding import GeocodeResult, GeocodingError


def _gr(
    lat: float, lng: float, confidence: str, matched_type: str = "regional.street"
) -> GeocodeResult:
    return GeocodeResult(
        lat=lat, lng=lng, confidence=confidence, matched_address="x",
        matched_type=matched_type, bbox=None, raw={},
    )


def _stub_geocoder(mapping):
    """Geocoder stub: first query-substring key that matches wins; a None value
    raises GeocodingError (mimics a no-result query). Never hits the network."""
    def g(query: str) -> GeocodeResult:
        for key, value in mapping.items():
            if key.lower() in query.lower():
                if value is None:
                    raise GeocodingError(f"no result for {query}")
                return value
        raise GeocodingError(f"unmapped: {query}")
    return g


# Letovice town centre, used as the locality anchor across the resolve tests.
_LETOVICE = (49.550, 16.570)

INDEX_HTML = """
<!DOCTYPE html><html><body>
<div class="listadvert">Zobrazeno 1-20 inzerátů z 6 990</div>
<div class="inzeraty inzeratyflex">
  <h2 class="nadpis"><a href="/inzerat/219122924/prodam-byt-2-kk-letovice.php">Prodám byt 2+kk Letovice</a></h2>
  <span class="velikost10">[12.5. 2026]</span>
  <div class="popis">Pěkný byt 2+kk o výměře 65 m² v klidné lokalitě.</div>
  <div class="inzeratycena">5 499 000 Kč</div>
  <div class="inzeratylok">Letovice<br>679 61</div>
  <div class="inzeratyview">123 x</div>
  <img class="obrazek" src="https://www.bazos.cz/img/1/219/219122924.jpg">
</div>
<div class="inzeraty inzeratyflex">
  <h2 class="nadpis"><a href="/inzerat/219122925/prodam-byt-3-1-brno.php">Prodám byt 3+1 Brno</a></h2>
  <span class="velikost10">[11.5. 2026]</span>
  <div class="popis">Prostorný byt 3+1, 82 m2.</div>
  <div class="inzeratycena">Dohodou</div>
  <div class="inzeratylok">Brno<br>602 00</div>
  <div class="inzeratyview">45 x</div>
  <img class="obrazek" src="https://www.bazos.cz/img/1/219/219122925.jpg">
</div>
<div class="strankovani">
  <a href="/prodam/byt/">1</a>
  <a href="/prodam/byt/20/">2</a>
  <a href="/prodam/byt/20/">Další</a>
</div>
</body></html>
"""

DETAIL_HTML = """
<!DOCTYPE html><html><body>
<div class="drobky"><a href="https://www.bazos.cz/">Hlavní stránka</a> > <a href="https://reality.bazos.cz/">Reality</a> > <a href="https://reality.bazos.cz/prodam/">Prodej</a> > <a href="https://reality.bazos.cz/prodam/byt/">Byty</a> > <b>Inzerát č. 219122924</b></div>
<h1 class="nadpisdetail">Prodám byt 2+kk Letovice</h1>
<span class="velikost10">[12.5. 2026]</span>
<table class="listadvalues">
  <tr><td>Cena:</td><td class="listadvalue">5 499 000 Kč</td></tr>
  <tr><td>Lokalita:</td><td><a href="https://www.google.com/maps/place/49.863882,16.333580/">Letovice 679 61 <span>Zobrazit na mapě</span></a></td></tr>
  <tr><td>Vidělo:</td><td>123 x</td></tr>
  <tr><td>Jméno:</td><td>agent@example.cz</td></tr>
</table>
<div class="popisdetail">Pěkný byt 2+kk o celkové výměře 65 m² v klidné lokalitě.</div>
<img src="https://www.bazos.cz/img/1/924/219122924.jpg">
<img src="https://www.bazos.cz/img/1t/924/219122924.jpg">
<img src="https://www.bazos.cz/img/2t/924/219122924.jpg">
<img src="https://www.bazos.cz/img/3t/924/219122924.jpg">
</body></html>
"""

DOHODOU_DETAIL_HTML = """
<!DOCTYPE html><html><body>
<h1 class="nadpisdetail">Pronájem byt 3+1 Brno</h1>
<table>
  <tr><td>Cena:</td><td>Dohodou</td></tr>
  <tr><td>Lokalita:</td><td>Brno 602 00</td></tr>
</table>
<div class="popisdetail">Byt 3+1 o výměře 82 m2.</div>
</body></html>
"""


def test_parse_index_total_and_items():
    page = parse_index(INDEX_HTML)
    assert page.total == 6990
    assert len(page.items) == 2
    assert page.next_offset == 20

    first = page.items[0]
    assert first.source_id_native == "219122924"
    assert first.detail_path == "/inzerat/219122924/prodam-byt-2-kk-letovice.php"
    assert first.title == "Prodám byt 2+kk Letovice"
    assert first.price_text == "5 499 000 Kč"
    assert "Letovice" in (first.locality_text or "")


def test_next_offset_handles_locality_query_string():
    # A locality-filtered search appends ?hlokalita=…&humkreis=… AFTER the
    # offset path segment; the pager parse must still find the offset, else the
    # walk stops after page 1 and never proves completeness.
    html = """
<!DOCTYPE html><html><body>
<div class="strankovani">
  <a href="/prodam/byt/?hlokalita=Praha&humkreis=10">1</a>
  <a href="/prodam/byt/20/?hlokalita=Praha&humkreis=10">2</a>
  <a href="/prodam/byt/20/?hlokalita=Praha&humkreis=10">Další</a>
</div>
</body></html>
"""
    assert parse_index(html).next_offset == 20


def test_parse_index_ignores_non_listing_divs():
    # The strankovani / header divs must not produce phantom items.
    page = parse_index(INDEX_HTML)
    assert all(item.source_id_native.isdigit() for item in page.items)


def test_parse_detail_full():
    url = "https://reality.bazos.cz/inzerat/219122924/prodam-byt-2-kk-letovice.php"
    listing = parse_detail(
        DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej"
    )

    assert listing.source == "bazos"
    assert listing.source_id_native == "219122924"
    assert listing.source_url == url
    assert listing.category_main == "byt"
    assert listing.category_type == "prodej"
    assert listing.price_czk == 5_499_000
    assert listing.price_unit == "za nemovitost"
    assert listing.area_m2 == 65.0
    assert listing.disposition == "2+kk"
    assert listing.lat == 49.863882
    assert listing.lon == 16.333580
    assert listing.locality == "Letovice"
    assert listing.description.startswith("Pěkný byt")
    assert listing.raw["psc"] == "679 61"
    # cover (/img/1/) + thumbnail strip (/img/1t/,2t,3t) → 3 full-size, deduped
    assert listing.raw["image_urls"] == [
        "https://www.bazos.cz/img/1/924/219122924.jpg",
        "https://www.bazos.cz/img/2/924/219122924.jpg",
        "https://www.bazos.cz/img/3/924/219122924.jpg",
    ]


def test_parse_detail_images_are_fullsize_and_deduped():
    # bazos detail pages carry the full cover plus a thumbnail strip; we must store
    # the full-size variant of every photo and never the same photo twice.
    url = "https://reality.bazos.cz/inzerat/219122924/prodam-byt-2-kk-letovice.php"
    urls = parse_detail(
        DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej"
    ).raw["image_urls"]
    assert all("/img/" in u and "t/" not in u.split("/img/")[1] for u in urls)
    assert len(urls) == len(set(urls))


def test_parse_detail_category_from_breadcrumb_overrides_fallback():
    # The page breadcrumb is authoritative: a "Pronájem" page parses as a rental
    # even when the caller passes the sale fallback (the drain's primary scope).
    # Flipping the breadcrumb LINK segments (prodam -> pronajmu) is what a real
    # rental page looks like — the regression that mis-tagged ~6.6k rentals as
    # sales was the parser reading the localised TEXT, not these href segments.
    html = DETAIL_HTML.replace("/prodam/", "/pronajmu/")
    url = "https://reality.bazos.cz/inzerat/219122924/pronajmu-byt-2-kk-letovice.php"
    listing = parse_detail(
        html, source_url=url, category_main="byt", category_type="prodej"
    )
    assert listing.category_main == "byt"
    assert listing.category_type == "pronajem"   # breadcrumb wins over the fallback
    assert listing.price_unit == "za mesic"      # price unit follows the real type


def test_parse_detail_category_from_real_breadcrumb_markup():
    # Verbatim breadcrumb captured from a live reality.bazos.cz detail page: a
    # plain ">" separator (NOT "»") and the category carried in the link hrefs.
    # Guards against the format the hand-authored "»" fixture missed.
    html = """
<!DOCTYPE html><html><body>
<div class="drobky"><a href="https://www.bazos.cz/" title="Inzerce Bazoš">Hlavní stránka</a>  > <a href="https://reality.bazos.cz/">Reality</a> > <a href="https://reality.bazos.cz/pronajmu/">Pronájem</a> > <a href="https://reality.bazos.cz/pronajmu/byt/">Byty</a> > <b>Inzerát č. 219625164</b></div>
<h1 class="nadpisdetail">Pronájem bytu 3+kk 79,7 m, Pardubice</h1>
<table><tr><td>Cena:</td><td>20 000 Kč</td></tr></table>
<div class="popisdetail">Pronájem bytu 3+kk.</div>
</body></html>
"""
    url = "https://reality.bazos.cz/inzerat/219625164/pronajem-bytu-3kk.php"
    listing = parse_detail(
        html, source_url=url, category_main="byt", category_type="prodej"
    )
    assert (listing.category_main, listing.category_type) == ("byt", "pronajem")
    assert listing.price_unit == "za mesic"


def test_parse_detail_falls_back_when_breadcrumb_missing():
    html = re.sub(r'<div class="drobky">.*?</div>', "", DETAIL_HTML)
    url = "https://reality.bazos.cz/inzerat/219122924/x.php"
    listing = parse_detail(
        html, source_url=url, category_main="byt", category_type="prodej"
    )
    assert (listing.category_main, listing.category_type) == ("byt", "prodej")


# Verbatim breadcrumb shape captured live from reality.bazos.cz fine-section
# detail pages (div.drobky, the section in the LAST link's href). The fine
# section drives BOTH category_main and the portal-agnostic subtype.
def _bazos_section_html(section: str) -> str:
    return (
        '<!DOCTYPE html><html><body>'
        f'<div class="drobky"><a href="https://www.bazos.cz/">Hlavní stránka</a> > '
        f'<a href="https://reality.bazos.cz/">Reality</a> > '
        f'<a href="https://reality.bazos.cz/prodam/">Prodej</a> > '
        f'<a href="https://reality.bazos.cz/prodam/{section}/">X</a> > '
        '<b>Inzerát č. 1</b></div>'
        '<h1 class="nadpisdetail">Detail</h1>'
        '<table><tr><td>Cena:</td><td>1 000 000 Kč</td></tr></table>'
        '<div class="popisdetail">Popis.</div></body></html>'
    )


def test_parse_detail_subtype_from_fine_section_breadcrumb():
    cases = {
        "chata": ("dum", "chata"),
        "kancelar": ("komercni", "kancelar"),
        "sklad": ("komercni", "sklad"),
        "prostory": ("komercni", "obchodni_prostor"),
        "restaurace": ("komercni", "restaurace"),
        "dum": ("dum", None),          # generic house section — no subtype
        "byt": ("byt", None),
    }
    for section, (cm, sub) in cases.items():
        listing = parse_detail(
            _bazos_section_html(section),
            source_url=f"https://reality.bazos.cz/inzerat/1/{section}.php",
            category_main=None, category_type=None,
        )
        assert listing.category_main == cm, section
        assert listing.subtype == sub, section


def test_parse_detail_coords_from_map_link_outside_lokalita_cell():
    """Live bazos renders the "show on map" link OUTSIDE the Lokalita table cell
    (the fixture above happens to have it inside). Coords must still be extracted
    from the page-wide Google-Maps / Mapy.cz link — the bug that left every real
    bazos listing with NULL geom and starved cross-source dedup."""
    html = """
    <!DOCTYPE html><html><body>
    <h1 class="nadpisdetail">Prodám byt 3+kk Brno</h1>
    <table class="listadvalues">
      <tr><td>Cena:</td><td>3 500 000 Kč</td></tr>
      <tr><td>Lokalita:</td><td>Brno 602 00</td></tr>
    </table>
    <div class="popisdetail">Byt 3+kk o výměře 70 m².</div>
    <div class="mapadetail">
      <a href="https://www.google.com/maps/place/49.180806,16.675186/@49.18,16.67,12z">Zobrazit na mapě</a>
    </div>
    </body></html>
    """
    listing = parse_detail(
        html, source_url="https://reality.bazos.cz/inzerat/999/x.php",
        category_main="byt", category_type="prodej",
    )
    assert listing.lat == 49.180806
    assert listing.lon == 16.675186
    assert listing.locality == "Brno"


def test_parse_coords_rejects_out_of_cz_bounds():
    from scraper.bazos_parser import _parse_coords
    # A stray non-Czech decimal pair must never become geom.
    assert _parse_coords("https://x/maps/place/1.234567,2.345678/") == (None, None)
    # A real Czech pair passes through.
    assert _parse_coords("https://x/maps/place/50.087,14.421/") == (50.087, 14.421)


def test_parse_detail_content_hash_stable():
    url = "https://reality.bazos.cz/inzerat/219122924/x.php"
    a = parse_detail(DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej")
    b = parse_detail(DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej")
    assert a.content_hash() == b.content_hash()
    assert len(a.content_hash()) == 64


def test_parsed_listing_bridges_into_ingest_contract():
    # The seam db.ingest_scraped_listing relies on: to_row(pk) must yield a
    # listings-row dict carrying the synthetic PK and the parsed fields.
    url = "https://reality.bazos.cz/inzerat/219122924/x.php"
    listing = parse_detail(DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej")
    row = listing.to_row(-5)
    assert row["sreality_id"] == -5
    assert row["category_main"] == "byt"
    assert row["category_type"] == "prodej"
    assert row["price_czk"] == 5_499_000
    assert row["area_m2"] == 65.0
    assert row["lat"] == 49.863882
    assert row["lon"] == 16.333580


def test_parse_detail_dohodou_price_is_none():
    url = "https://reality.bazos.cz/inzerat/219122925/y.php"
    listing = parse_detail(
        DOHODOU_DETAIL_HTML, source_url=url,
        category_main="byt", category_type="pronajem",
    )
    assert listing.price_czk is None
    assert listing.price_unit == "za mesic"
    assert listing.disposition == "3+1"
    assert listing.area_m2 == 82.0


# --- street extraction ------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("Prodám byt 2+kk Letovice, ulice Dlouhá 12", "ulice Dlouhá 12"),
        ("Pěkný byt na Vinohradské třídě v Praze", "Vinohradské třídě"),
        ("Byt u náměstí Míru 5, Praha 2", "náměstí Míru 5"),
        ("Prodej bytu, Husova 12, Brno", "Husova 12"),
        ("Vinohradská třída 5, Praha", "Vinohradská třída 5"),
        ("Dům na Pražské ulici", "Pražské ulici"),
        ("nábřeží Kapitánů, krásný výhled", "nábřeží Kapitánů"),
    ],
)
def test_extract_street_finds_streets(text, expected):
    assert extract_street(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Prodám byt 2+kk Letovice 679 61",   # PSČ, not a house number
        "Prostorný byt 3+1, 82 m2, Garáž 20",  # stopword noun + number
        "Pozemek 800 v obci",                 # stopword noun + number
        "Pěkný byt v klidné lokalitě",        # no street at all
        "",                                    # empty
    ],
)
def test_extract_street_rejects_non_streets(text):
    assert extract_street(text) is None


def test_extract_street_none_on_none():
    assert extract_street(None) is None


# --- coordinate resolution / cross-check ------------------------------------

def test_resolve_street_geocode_wins_over_link():
    geocoder = _stub_geocoder({
        "Dlouhá": _gr(49.560, 16.560, "high", "regional.address"),
        "Letovice": _gr(*_LETOVICE, "low", "regional.municipality"),
    })
    lat, lon, prov = _resolve_coords(
        link_lat=49.555, link_lon=16.575,
        street="ulice Dlouhá 12", locality="Letovice", psc="679 61",
        geocoder=geocoder,
    )
    assert (lat, lon) == (49.560, 16.560)
    assert prov["source"] == "street"
    assert prov["street_confidence"] == "high"


def test_resolve_link_consistent_with_text_is_used():
    geocoder = _stub_geocoder({"Letovice": _gr(*_LETOVICE, "low", "regional.municipality")})
    lat, lon, prov = _resolve_coords(
        link_lat=49.555, link_lon=16.575,  # ~0.66 km from text reference
        street=None, locality="Letovice", psc="679 61", geocoder=geocoder,
    )
    assert (lat, lon) == (49.555, 16.575)
    assert prov["source"] == "link"
    assert prov["text_reference"] == "locality"
    assert prov["link_text_distance_km"] < LINK_TRUST_RADIUS_KM


def test_resolve_link_far_from_text_is_rejected_for_locality():
    geocoder = _stub_geocoder({"Letovice": _gr(*_LETOVICE, "low", "regional.municipality")})
    lat, lon, prov = _resolve_coords(
        link_lat=50.080, link_lon=14.420,  # Prague: ~165 km from Letovice
        street=None, locality="Letovice", psc="679 61", geocoder=geocoder,
    )
    assert (lat, lon) == _LETOVICE
    assert prov["source"] == "locality"
    assert prov["link_text_distance_km"] > LINK_DISTRUST_RADIUS_KM
    assert any("distrusted" in n for n in prov["notes"])


def test_resolve_link_in_loose_band_is_accepted_with_note():
    geocoder = _stub_geocoder({"Letovice": _gr(*_LETOVICE, "low", "regional.municipality")})
    lat, lon, prov = _resolve_coords(
        link_lat=49.577, link_lon=16.570,  # ~3 km: between TRUST and DISTRUST
        street=None, locality="Letovice", psc=None, geocoder=geocoder,
    )
    assert (lat, lon) == (49.577, 16.570)
    assert prov["source"] == "link"
    assert LINK_TRUST_RADIUS_KM < prov["link_text_distance_km"] < LINK_DISTRUST_RADIUS_KM
    assert any("loose" in n for n in prov["notes"])


def test_resolve_locality_fallback_when_no_street_and_no_link():
    geocoder = _stub_geocoder({"Letovice": _gr(*_LETOVICE, "low", "regional.municipality")})
    lat, lon, prov = _resolve_coords(
        link_lat=None, link_lon=None,
        street=None, locality="Letovice", psc=None, geocoder=geocoder,
    )
    assert (lat, lon) == _LETOVICE
    assert prov["source"] == "locality"


def test_resolve_geocoder_raises_falls_back_to_link():
    geocoder = _stub_geocoder({})  # every query raises GeocodingError
    lat, lon, prov = _resolve_coords(
        link_lat=49.560, link_lon=16.580,
        street="ulice Dlouhá", locality="Letovice", psc=None, geocoder=geocoder,
    )
    assert (lat, lon) == (49.560, 16.580)
    assert prov["source"] == "link"


def test_resolve_geocoder_raises_no_link_returns_none():
    geocoder = _stub_geocoder({})  # every query raises GeocodingError
    lat, lon, prov = _resolve_coords(
        link_lat=None, link_lon=None,
        street="ulice Dlouhá", locality="Letovice", psc=None, geocoder=geocoder,
    )
    assert (lat, lon) == (None, None)
    assert prov["source"] is None


def test_resolve_no_geocoder_uses_cz_guarded_link():
    lat, lon, prov = _resolve_coords(
        link_lat=49.560, link_lon=16.580,
        street="ulice Dlouhá", locality="Letovice", psc=None, geocoder=None,
    )
    assert (lat, lon) == (49.560, 16.580)
    assert prov["source"] == "link"
    assert any("no geocoder" in n for n in prov["notes"])


def test_resolve_low_confidence_street_not_promoted_to_primary():
    # A low-confidence street geocode (Mapy fell back to the municipality) must
    # NOT be accepted as the precise primary; it serves only as a coarse anchor.
    geocoder = _stub_geocoder({
        "Dlouhá": _gr(*_LETOVICE, "low", "regional.municipality"),
    })
    lat, lon, prov = _resolve_coords(
        link_lat=49.555, link_lon=16.575,  # close to the low-conf text anchor
        street="ulice Dlouhá", locality="Letovice", psc=None, geocoder=geocoder,
    )
    assert prov["source"] == "link"
    assert (lat, lon) == (49.555, 16.575)


def test_parse_detail_street_geocode_overrides_coarse_link():
    html = """
    <!DOCTYPE html><html><body>
    <h1 class="nadpisdetail">Prodám byt 2+kk Letovice</h1>
    <table class="listadvalues">
      <tr><td>Cena:</td><td>5 499 000 Kč</td></tr>
      <tr><td>Lokalita:</td><td><a href="https://www.google.com/maps/place/49.500000,16.500000/">Letovice 679 61</a></td></tr>
    </table>
    <div class="popisdetail">Pěkný byt v ulici Tyršova 12, klidná lokalita.</div>
    </body></html>
    """
    geocoder = _stub_geocoder({
        "Tyršova": _gr(49.560, 16.565, "high", "regional.address"),
        "Letovice": _gr(*_LETOVICE, "low", "regional.municipality"),
    })
    listing = parse_detail(
        html, source_url="https://reality.bazos.cz/inzerat/999/x.php",
        category_main="byt", category_type="prodej", geocoder=geocoder,
    )
    assert (listing.lat, listing.lon) == (49.560, 16.565)
    assert listing.raw["coords"]["source"] == "street"
    # locative "v ulici" is what real descriptions write; geocoding tolerates it.
    assert listing.raw["coords"]["street"] == "ulici Tyršova 12"


def test_parse_detail_records_coord_provenance():
    listing = parse_detail(
        DETAIL_HTML,
        source_url="https://reality.bazos.cz/inzerat/219122924/x.php",
        category_main="byt", category_type="prodej",  # no geocoder injected
    )
    coords = listing.raw["coords"]
    assert coords["source"] == "link"
    assert coords["link_present"] is True
    assert (listing.lat, listing.lon) == (49.863882, 16.333580)
