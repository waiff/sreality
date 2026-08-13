"""Shared fixtures for the W1 claim-intake tests.

`raw_json` payloads are synthesised from `recon/db-raw-samples.md` §3 — the shapes actually
persisted per portal, including the two sreality shapes and the 80 KB-truncation case. Not
a test module itself (no `test_` prefix), so pytest imports it rather than collecting it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from location_data import contracts
from location_data.claims_intake import LEGACY_COLUMNS, Entry, ListingRow

OBSERVED_AT = datetime(2026, 8, 10, 6, 30, tzinfo=UTC)

_CONTRACTS = {c.source: c for c in contracts.load_all()}


def entries_for(source: str) -> list[Entry]:
    """The real git contract, projected into the shape the extractor reads from the DB.

    Entry ids are assigned in file order, which is exactly what the deploy projection does
    (`bigserial`), so a test can assert on `contract_entry_id` stability.
    """
    contract = _CONTRACTS[source]
    return [
        Entry(
            id=1000 + index,
            source=contract.source,
            contract_id=1,
            contract_version=contract.version,
            entry_id=entry.entry_id,
            surface=entry.surface,
            page_kind=entry.page_kind,
            locator=entry.locator,
            claim_type=entry.claim_type,
            extraction_method=entry.extraction_method,
            subject_scope=entry.subject_scope,
            transform=tuple(entry.transform),
            precision_map=entry.precision_map,
            default_blur_evidence=entry.default_blur_evidence,
            default_licence_class=entry.default_licence_class,
            cardinality=entry.cardinality,
            guards=tuple(entry.guards),
        )
        for index, entry in enumerate(contract.entries)
    ]


def listing(
    source: str,
    raw_json: dict[str, Any],
    *,
    listing_id: int = 1,
    native: str = "n1",
    lat: float | None = None,
    lon: float | None = None,
    in_mapy_inventory: bool = False,
    locality: str | None = None,
    street: str | None = None,
    street_source: str | None = None,
) -> ListingRow:
    """`locality` / `street` / `street_source` are the class-B `listings` columns of
    06 §6.1.3 that the batch query selects alongside `raw_json` — NOT payload keys.

    Every one of `LEGACY_COLUMNS` is always populated, exactly as `_row_from_record`
    populates it, and zipped `strict` for the same reason: a column added to the scan but
    not here would leave the fixtures testing a row shape production never produces.
    """
    return ListingRow(
        listing_id=listing_id,
        source=source,
        source_id_native=native,
        raw_json=raw_json,
        lat=lat,
        lon=lon,
        observed_at=OBSERVED_AT,
        in_mapy_inventory=in_mapy_inventory,
        legacy_columns=dict(zip(
            LEGACY_COLUMNS, (locality, street, street_source), strict=True)),
    )


def claims_by_type(result: Any) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for claim in result.claims:
        grouped.setdefault(claim.claim_type, []).append(claim)
    return grouped


def claim_by_extractor(result: Any, extractor_id: str) -> Any | None:
    for claim in result.claims:
        if claim.extractor_id == extractor_id:
            return claim
    return None


# --------------------------------------------------------------- sreality (db-raw §3.1)

SREALITY_POST_CUTOVER: dict[str, Any] = {
    "advert_description": "Prodej bytu 3+kk …",
    "locality": {
        "zip": 13000,
        "city": "Praha",
        "ward": None,
        "region": "Hlavní město Praha",
        "street": "náměstí Jiřího z Poděbrad",
        "gps_lat": 50.0784977,
        "gps_lon": 14.4501973,
        "quarter": "Praha 3",
        "ward_id": 14967,
        "citypart": "Vinohrady",
        "district": "Praha 3",
        "geometry": {
            "geometry": ["9hETFxX9bEJjg6xKjViOSNSY"],
            "bounding_box": {
                "rightTopLatitude": 50.0786306,
                "rightTopLongitude": 14.4522815,
                "leftBottomLatitude": 50.0771470,
                "leftBottomLongitude": 14.4485223,
            },
            "geometry_type": "linestring",
        },
        "region_id": 10,
        "street_id": 122964,
        "country_id": 112,
        "quarter_id": 89,
        "district_id": 5003,
        "entity_type": "address",
        "housenumber": "1558",
        "streetnumber": "7",
        "inaccuracy_type": "street",
        "municipality_id": 3468,
        "country": "Česká republika",
    },
    # The agency OFFICE — a fully-formed decoy in 11 of 12 mined files [mine-syn §1c].
    "premise": {
        "locality": {"city": "Praha", "street": "Vinohradská", "housenumber": "12",
                     "gps_lat": 50.0781, "gps_lon": 14.4402},
    },
}

# The retired shape: a display string plus a coarse `accuracy` flag, coordinates in
# `map.{lat,lon}`. Permanently lacks zip/housenumber/entity_type/inaccuracy_type.
SREALITY_LEGACY: dict[str, Any] = {
    "text": {"value": "Prodej bytu …"},
    "locality": {"name": "Adresa", "value": "Klatovy, okres Klatovy",
                 "accuracy": "not_address"},
    "map": {"lat": 49.3955, "lon": 13.2951, "zoom": 12},
    "poi": [],
}

# 1588965452: an 80 KB geometry blob truncated raw_json and destroyed the locality object.
SREALITY_TRUNCATED: dict[str, Any] = {
    "advert_description": "Prodej pozemku …",
    "advert_name": "Prodej pozemku 2 000 m²",
}

SREALITY_ZIP_SENTINEL: dict[str, Any] = {
    "locality": {
        "city": "Klatovy", "zip": -1, "gps_lat": 49.3955, "gps_lon": 13.2951,
        "entity_type": "municipality", "inaccuracy_type": "municipality",
        "street_id": -1,
    },
}


# ------------------------------------------------------------ bezrealitky (db-raw §3.2)

BEZREALITKY: dict[str, Any] = {
    "id": "1037096",
    "uri": "praha-liben-davidkova",
    "gps": {"lat": 50.1092, "lng": 14.4749},
    "address": "Davídkova 655/31, Libeň, Praha",
    "addressUserInput": ("Davídkova 655/31, Libeň, Praha, obvod Praha 8, "
                         "Hlavní město Praha, Praha, 180 00, Česko"),
    "street": "Davídkova",
    "houseNumber": "655/31",
    "houseUnit": "",
    "city": "Praha",
    "cityDistrict": "Praha - Libeň",
    "zip": "154 00",
    "ruianId": "22698884",
    "description": "Byt na adrese Komárovská 1964/48 …",
}


# -------------------------------------------------------------- mmreality (db-raw §3.3)

MMREALITY_ACCURATE: dict[str, Any] = {
    "id": "123456",
    "point": {"latitude": 50.0296123456, "longitude": 15.7712123456},
    "accurate": True,
    "street": "Kutnohorská",
    "municipality": "Kolín",
    "municipalityId": 533165,
    "municipalityPart": "Kolín I",
    "district": "Kolín",
    "districtId": 3403,
    "country": "Česká republika",
    "countryCode": "CZ",
    "placement": "centrum obce",
    "slug": "byt-kolin",
    "originalTitle": "Prodej bytu 2+1, Kolín, ul. Kutnohorská",
    "poi": [],
}

MMREALITY_NOT_ACCURATE: dict[str, Any] = {
    "id": "654321",
    "point": {"latitude": 50.1414253647, "longitude": 12.9061627384},
    "accurate": False,
    "municipality": "Bochov",
    "municipalityId": 555029,
    "municipalityPart": None,
    "district": "Karlovy Vary",
    "placement": "okraj obce",
    "poi": [],
}


# ------------------------------------------------------------------ bazos (db-raw §3.4)

BAZOS_LINK: dict[str, Any] = {
    "id": "220059906",
    "psc": "696 81",
    "title": "Prodej bytu 2+1 Hodonín",
    "views": 412,
    "locality_text": "Hodonín",
    "coords": {
        "source": "link",
        "street": "ul. Hurbanova",
        "link_present": True,
        "text_reference": None,
        "street_confidence": None,
        "locality_confidence": None,
        "link_text_distance_km": None,
        "notes": ["no geocoder; used CZ-guarded maps link"],
    },
}

BAZOS_STREET_GEOCODE: dict[str, Any] = {
    "id": "220870847",
    "psc": "50801",
    "locality_text": "Hořice v Podkrkonoší",
    "coords": {
        "source": "street",
        "street": "Nový",
        "street_confidence": "high",
        "locality_confidence": "medium",
        "link_text_distance_km": None,
        "notes": ["geocoded street"],
    },
}

BAZOS_LOCALITY_GEOCODE: dict[str, Any] = {
    "id": "220021475",
    "psc": "37001",
    "locality_text": "České Budějovice",
    "coords": {"source": "locality", "locality_confidence": "low", "notes": []},
}


# ------------------------------------------------- the five slim-dict portals (db-raw §3.4)

IDNES_PAGE: dict[str, Any] = {
    "id": "1234567",
    "title": "Prodej bytu 3+1",
    "locality_text": "Březno - Nechranice, okres Chomutov",
    "idnes_ref": "abc",
    "coords": {"source": "page", "confidence": None, "matched_type": None},
    "params": {},
}

IDNES_UNSTAMPED: dict[str, Any] = {
    "id": "7654321",
    "locality_text": "Brno - střed",
    "coords": {"source": None},
    "params": {},
}

IDNES_CARRY_FORWARD: dict[str, Any] = {
    "id": "5555555",
    "locality_text": "Praha 4",
    "coords": {"source": "carry_forward"},
}

REALITYMIX_GEOCODE: dict[str, Any] = {
    "id": "8375963",
    "locality_text": "Bilovec",
    "coords": {"source": "geocode", "confidence": "medium",
               "matched_type": "regional.street"},
}

REALITYMIX_PAGE: dict[str, Any] = {
    "id": "8460367",
    "locality_text": "Staňkovice, okres Louny",
    "coords": {"source": "page", "confidence": "high", "matched_type": "address"},
}

CESKEREALITY_PAGE: dict[str, Any] = {
    "id": "3849899",
    "title": "Prodej bytu 1+kk 39 m² Na Výrovně 2693, Praha Stodůlky",
    "locality_text": "Praha Stodůlky",
    "coords": {"source": "page"},
}

MAXIMA_PAGE: dict[str, Any] = {
    "id": "d40031686",
    "maxima_ref": "d40031686",
    "locality_text": "Liberec, Liberec XIV-Ruprechtice, Baltská",
    "coords": {"source": "page"},
}

# remax ships NO `coords` key at all, and its `address` is the neighbour card's.
REMAX: dict[str, Any] = {
    "id": "445781",
    "title": "Prodej bytu 2+kk 54 m², Praha 3 - Žižkov",
    "address": "V Horní Stromce, Praha 3, Vinohrady, okres Hlavní město Praha",
    "remax_ref": "445781",
    "params": {"umisteni objektu": "Centrum obce"},
}


# ---------------------------------------- the zero-claim keysets measured on 2026-08-11
#
# Sampled from ACTIVE production rows that carried NO location claim under contract v1.
# Each one is the reason a contract version was bumped, so each is a fixture.

# Post-W0-0d remax: the subject's own header moved to `display_address` and the carousel
# value to `carousel_address`; the v1-readable `address` key is simply gone.
REMAX_DISPLAY_ADDRESS: dict[str, Any] = {
    "id": "446190",
    "title": "Prodej bytu 3+kk 78 m², Praha 3 - Žižkov",
    "price_text": "9 450 000 Kč",
    "display_address": "ulice Roháčova, Praha 3 - Žižkov",
    "carousel_address": "V Horní Stromce, Praha 3, Vinohrady, okres Hlavní město Praha",
    "remax_ref": "446190",
    "params": {"umisteni objektu": "Centrum obce"},
}

# The mixed row: BOTH keys present. The banned one must stay a conflict signal even when
# the subject's own line is right there beside it.
REMAX_BOTH_ADDRESS_KEYS: dict[str, Any] = {
    "id": "445781",
    "title": "Prodej bytu 2+kk 54 m², Praha 3 - Žižkov",
    "display_address": "ulice Roháčova, Praha 3 - Žižkov",
    "address": "V Horní Stromce, Praha 3, Vinohrady, okres Hlavní město Praha",
    "remax_ref": "445781",
    "params": {"umisteni objektu": "Centrum obce"},
}

# ceskereality's silent-parse signature: the key is PRESENT and null (a keyset sample
# cannot tell the two apart), sometimes beside the address-point backfill's marker.
CESKEREALITY_NULL_LOCALITY: dict[str, Any] = {
    "id": "3180041",
    "title": "Prodej bytu 2+1 62 m²",
    "locality_text": None,
    "coord_street_resolved": True,
    "coords": {"source": "geocode", "confidence": "medium"},
    "params": {},
}

REALITYMIX_NULL_LOCALITY: dict[str, Any] = {
    "id": "8590773",
    "locality_text": None,
    "coords": {"source": "geocode", "confidence": "medium",
               "matched_type": "regional.street"},
}


# --------------------------------- the residual zero-claim keyset measured on 2026-08-13
#
# What v2 could NOT reach: `raw_json.locality_text` present and NULL AND `listings.locality`
# NULL too (0 of the 957 ceskereality rows in this cohort have one), so `cr.det.legacy_locality`
# yields nothing either. The slim dict has no street key at any depth — these seven keys are
# the whole payload — so `listings.street` is the only W1-readable signal left, and it is
# readable only where `street_source` says the parser wrote it (06 §6.1.3). Keyset of
# ceskereality 3822640, whose stored street is `Svatoplukova`: ASCII-folded at source, like
# 93% of this portal's streets.
CESKEREALITY_STREET_ONLY: dict[str, Any] = {
    "id": "3822640",
    "title": "Prodej bytu 3+1 74 m²",
    "locality_text": None,
    "broker": {"name": "REALITNÍ KANCELÁŘ"},
    "coords": {"source": "geocode", "confidence": "medium"},
    "image_urls": [],
    "params": {},
}
