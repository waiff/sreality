"""Media-extraction tests against REAL captured portal HTML.

Every other portal-parser test in this repo is hermetic against a hand-authored
snippet — which is why a 19-day realitymix gallery blackout and a ~63% idnes photo
loss both shipped with CI green. A hand-written fixture can only assert back the
strings the test itself planted, so it is structurally blind to upstream drift:
`test_realitymix_parser.py` planted `https://st.realitymix.cz/...` and asserted those
exact strings, so the portal's `https:` -> `http:` flip was invisible to it.

These fixtures are real anonymized detail pages (scripts/fetch_and_anonymize_fixtures.py),
so they carry the portals' actual URL shapes. Each assertion below fails on the
pre-fix parsers. Regenerate a fixture when a portal changes markup — never hand-edit
a URL in one, or this file degrades into the tautology it exists to replace.
"""

from __future__ import annotations

from pathlib import Path

from selectolax.parser import HTMLParser

from scraper import media
from scraper.idnes_parser import _clean_image_url, _gallery_urls
from scraper.realitymix_parser import _images

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "portal_html"

# The listing each fixture was captured from (the page's own canonical id).
_REALITYMIX_ID = "8662169"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- realitymix


def test_realitymix_extracts_http_scheme_gallery():
    """The regression: realitymix moved this CDN to plain http:// on 2026-07-16 and a
    pinned `https://` in _IMG_RE silently zeroed every new gallery for 19 days."""
    html = _fixture("realitymix_detail.html")
    assert "http://st.realitymix.cz/" in html, "fixture must carry the http:// shape"

    urls = _images(html, _REALITYMIX_ID)

    assert len(urls) == 13
    assert all(u.startswith("https://") for u in urls), "scheme must be normalised"


def test_realitymix_gallery_is_scoped_to_its_own_listing():
    """The page also renders a 'related adverts' rail whose photos live under OTHER
    listing ids — those must never be attributed to this listing."""
    html = _fixture("realitymix_detail.html")

    urls = _images(html, _REALITYMIX_ID)

    assert all(f"/{_REALITYMIX_ID}/" in u for u in urls)
    assert not any("_nahled" in u for u in urls), "thumbnails are not gallery photos"
    assert not any("_detail" in u for u in urls), "related-rail crops are not ours"
    assert not any("/makleri/" in u for u in urls), "broker portrait is not a photo"


def test_realitymix_keeps_photos_when_listing_id_is_unknown():
    """Scoping must never be able to empty a gallery: if no URL carries our id (an
    upstream id-shape change), fall back to every non-denied photo rather than
    returning [] — returning [] is the exact failure this whole fix addresses."""
    html = _fixture("realitymix_detail.html")

    assert _images(html, "999999999") == _images(html)
    assert len(_images(html, None)) == 13


# --------------------------------------------------------------------------- idnes


def test_idnes_extracts_first_party_gallery_host():
    """The regression: iDNES migrated galleries to its own reality.idnes.cz/file
    service, and the `1gr.cz`/`sta-reality` host allow-list silently dropped them."""
    html = _fixture("idnes_detail.html")
    assert "reality.idnes.cz/file/thumbnail" in html, "fixture must carry the new host"

    urls = _gallery_urls(HTMLParser(html))

    first_party = [u for u in urls if "reality.idnes.cz/file/" in u]
    legacy = [u for u in urls if "1gr.cz" in u]
    assert len(first_party) == 6, "these are exactly what the allow-list dropped"
    assert legacy, "the legacy CDN must keep working alongside it"
    assert len(urls) == len(first_party) + len(legacy)


def test_idnes_preserves_the_load_bearing_profile_query():
    """The first-party path is extension-less and the BARE url is a verified 404 — the
    rendition lives entirely in ?profile=. The old parser stripped the query before the
    host check, so the obvious 'just allow the host' fix would store dead links."""
    html = _fixture("idnes_detail.html")

    urls = _gallery_urls(HTMLParser(html))

    for url in (u for u in urls if "reality.idnes.cz/file/" in u):
        assert "?profile=" in url, "stripping this yields a 404"
        assert "gt=" not in url, "the tracking param is dropped"


def test_idnes_clean_image_url_drops_only_the_tracking_param():
    base = "https://reality.idnes.cz/file/thumbnail/abc"
    assert _clean_image_url(f"{base}?profile=front_detail_article_big_fit&gt=r") == (
        f"{base}?profile=front_detail_article_big_fit"
    )
    assert _clean_image_url(f"{base}?gt=r") == base
    assert _clean_image_url(base) == base


def test_idnes_denies_non_photo_gallery_anchors():
    """`a[data-fancybox="images"]` is not photo-exclusive. Matterport tours pass
    media.is_image_url, and the lazy-load placeholder would land byte-identical in
    every listing — colliding at CLIP cosine 1.0 and tripping rule #15's auto-merge."""
    html = (
        '<a data-fancybox="images" href="https://my.matterport.com/show/?m=abc">tour</a>'
        '<a data-fancybox="images" href="https://sta-reality2.1gr.cz/ui/image/'
        'no-image-gallery.png">placeholder</a>'
        '<a data-fancybox="images" href="https://sta-reality2.1gr.cz/sta/a/b/real.jpg">ok</a>'
    )

    urls = _gallery_urls(HTMLParser(html))

    assert urls == ["https://sta-reality2.1gr.cz/sta/a/b/real.jpg"]


def test_idnes_video_anchors_pass_through_to_the_shared_media_split():
    """Video is NOT filtered in the parser — scraper.media owns the image/video split
    for all nine portals (rule #21), so a tour reaches listing_videos, not images."""
    html = _fixture("idnes_detail.html")

    urls = _gallery_urls(HTMLParser(html))
    images, videos = media.split_media_rows(urls)

    assert all(media.is_image_url(i["url"]) for i in images)
    assert all(not media.is_image_url(v["url"]) for v in videos)
    assert len(images) + len(videos) == len(urls)


# --------------------------------------------------------------------------- fixtures


def test_remax_extracts_the_server_rendered_description():
    """remax sat at 0.0% description for its entire life (11,091 rows) because
    `.pd-detail-text` / `#popis` match no state of the page — 0/300 stored pages carry
    either. The text IS server-rendered in the first response; the container is only a
    CSS read-more collapse, so no JS execution is involved."""
    from scraper.remax_parser import parse_detail

    html = _fixture("remax_detail.html")
    assert ".pd-detail-text" not in html and 'id="popis"' not in html

    listing = parse_detail(html, source_url="https://www.remax-czech.cz/reality/detail/445483/x")

    assert listing.description is not None
    assert len(listing.description) > 500
    assert listing.description.startswith("Nabízíme k prodeji rodinný dům")
    assert "\n" in listing.description, "<br> paragraphing must survive"


def test_remax_description_is_never_the_og_boilerplate():
    """og:description is REMAX marketing copy, byte-identical on every listing. Using it
    would look like a fix while poisoning all 11,091 rows with one constant string."""
    from scraper.remax_parser import parse_detail

    html = _fixture("remax_detail.html")
    assert "Spolehněte se na jedničku mezi realitkami" in html, "the trap is in the fixture"

    listing = parse_detail(html, source_url="https://www.remax-czech.cz/reality/detail/445483/x")

    assert "Spolehněte se na jedničku" not in (listing.description or "")


def _reextract():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "reextract", Path(__file__).resolve().parents[2] / "scripts" / "reextract.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves types via sys.modules[cls.__module__], so register before exec.
    sys.modules["reextract"] = module
    spec.loader.exec_module(module)
    return module


def test_reextract_field_registry_agrees_with_the_hash_contract():
    """A field in _HASH_FIELDS cannot be repaired snapshot-free. The registry declares
    that per field and the module raises at import on a mismatch, so adding a field to
    _HASH_FIELDS later can never silently downgrade the backfill's guarantee."""
    from scraper.scraped_listing import _HASH_FIELDS

    module = _reextract()

    assert module._FIELDS["media"].hashed is False
    assert module._FIELDS["description"].hashed is True
    for name, spec in module._FIELDS.items():
        assert spec.hashed == (name in _HASH_FIELDS)


def test_reextract_recovers_remax_description_from_a_stored_page():
    """The backfill substrate is stored portal_raw_pages HTML — assert the wired
    extractor actually yields text on a real page, or the run would report a clean
    'recovered 0' and look like success."""
    module = _reextract()

    text = module._FIELDS["description"].extractors["remax"](_fixture("remax_detail.html"), "")

    assert text and len(text) > 500


def test_reextract_registry_recovers_media_for_every_wired_portal():
    """scripts/reextract.py replays these same extractors over stored HTML. If a
    registered portal's extractor returns nothing on a real page, the backfill would
    silently 'recover' zero rows and report success — the failure shape being fixed."""
    module = _reextract()

    cases = {
        "realitymix": ("realitymix_detail.html", _REALITYMIX_ID),
        "idnes": ("idnes_detail.html", ""),
    }
    extractors = module._FIELDS["media"].extractors
    assert set(extractors) == set(cases), "wire a fixture for every portal"

    for source, (fixture, native) in cases.items():
        urls = extractors[source](_fixture(fixture), native)
        assert urls, f"{source} extractor returned nothing on real HTML"


def test_fixtures_are_anonymized_but_urls_survive():
    """scripts/fetch_and_anonymize_fixtures.py used to corrupt exactly the data these
    tests need: _PHONE_RE matches any 9 consecutive digits (a realitymix photo id) and
    _STREET_NUM_RE matches any `N/M` pair (an idnes CDN shard path). URLs are masked
    during scrubbing now — assert both halves hold."""
    for name in ("realitymix_detail.html", "idnes_detail.html"):
        html = _fixture(name)
        assert "ANONYMIZED FIXTURE" in html
        assert "+420 XXX XXX XXX" in html, "PII scrubbing still runs"
        assert "nab_+420" not in html, "photo ids must not be scrubbed as phone numbers"
        assert "/thumbs/XXX/YY/" not in html, "CDN paths must not be scrubbed as streets"


def test_remax_broker_block_matches_the_resolver_contract():
    """resolve_brokers reads raw_json->'broker'->>'broker_id' as the identity key. The id
    is the `uzivatele/{id}` photo DIRECTORY — the two filename numbers are per-asset and
    change on photo re-upload, so only the directory is 1:1 with the agent."""
    from scraper.remax_parser import parse_detail

    listing = parse_detail(
        _fixture("remax_detail.html"),
        source_url="https://www.remax-czech.cz/reality/detail/445483/x",
    )

    broker = listing.raw["broker"]
    assert broker["broker_id"] == "90001"
    assert broker["name"]
    assert "@" in broker["email"]
    assert broker["agency_slug"] == "re-max-diamond"
    assert "phone" not in broker, "broker_phone is an intentional zero on every portal"


def test_remax_broker_profile_link_is_absolute():
    """The profile href is absolute — a `/reality/`-prefixed relative selector matches
    nothing, which is how the agency slug would silently come back empty."""
    html = _fixture("remax_detail.html")

    assert 'href="https://www.remax-czech.cz/reality/' in html
    assert 'href="/reality/re-max' not in html


def test_remax_is_wired_end_to_end_for_broker_attribution():
    """Three registries had to agree or nothing was ever attributed: the ingest
    enqueue allow-list, the resolver's source list, and the per-source SQL in
    _attribute(). They are now ONE config row (toolkit.broker_sources) that all
    three derive from — this asserts the derivation actually reaches all three."""
    import importlib.util
    import sys

    from scraper.db import BROKER_ATTRIBUTED_SOURCES
    from toolkit.broker_sources import BROKER_SOURCES

    assert "remax" in BROKER_ATTRIBUTED_SOURCES

    spec = importlib.util.spec_from_file_location(
        "resolve_brokers", Path(__file__).resolve().parents[2] / "scripts" / "resolve_brokers.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["resolve_brokers"] = module
    spec.loader.exec_module(module)

    assert "remax" in module._BROKER_SOURCES
    # identity upsert + email contact + listing link — remax publishes no phone.
    (remax,) = [c for c in BROKER_SOURCES if c.source == "remax"]
    assert len(remax.statements()) == 3
    assert sum("l.source = 'remax'" in s for s in module._ATTRIBUTION_SQL) == 3
