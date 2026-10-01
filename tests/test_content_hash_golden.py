"""Golden content-hash digests (rule 2): the snapshot-on-change decision compares
against hashes already stored in listing_snapshots, so moving the hash code must
leave every digest byte-identical — a drift would append one spurious snapshot per
refetched row. These three values were computed on the pre-chokepoint functions."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from scraper import bazos_parser, hashing
from scraper.scraped_listing import ScrapedListing

FIXTURES = Path(__file__).parent / "fixtures"

SREALITY_DIGEST = "e89b020a3c6a0ff1fc3dc5627fe579279f104b244cfa303cfd76acb25f9a1a16"
SCRAPED_DIGEST = "c5aac3ba854c1fc4ad76b2c55f25723557e9965af7c6b61546fcfe0b9df910a1"
BAZOS_DIGEST = "ac65151af32e249bb5ba6bbd3e0168dd76ca5d939b042b5a248616da0d56ed96"


def _hand_built() -> ScrapedListing:
    return ScrapedListing(
        source="bazos", source_id_native="1",
        source_url="https://reality.bazos.cz/inzerat/1/x.php",
        category_main="byt", category_type="prodej", price_czk=4500000, area_m2=72.5,
        floor=3, has_lift=True, published_at=date(2026, 7, 2),
        description="Byt 2+kk, Praha – Žižkov", locality="Praha 3", district="Praha",
    )


def test_sreality_wire_payload_digest_is_pinned() -> None:
    raw = json.loads((FIXTURES / "sample_listing.json").read_text("utf-8"))
    assert hashing.content_hash(raw) == SREALITY_DIGEST


def test_hand_built_scraped_listing_digest_is_pinned() -> None:
    assert _hand_built().content_hash() == SCRAPED_DIGEST


def test_bazos_parsed_fixture_digest_is_pinned() -> None:
    html = (FIXTURES / "portal_html" / "bazos_detail.html").read_text("utf-8")
    listing = bazos_parser.parse_detail(
        html, source_url="https://reality.bazos.cz/inzerat/219122924/x.php",
        category_main="byt", category_type="prodej",
    )
    assert listing.content_hash() == BAZOS_DIGEST
