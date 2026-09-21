"""The ATTRIBUTE contract: one declared producer per (portal, typed column).

Not to be confused with the LOCATION contract in `contracts/portals/*.yaml`. That one is
a governed, hashed, re-minable document about where a listing IS; this one is an ordinary
Python table about what a listing's typed columns are READ FROM. Deliberately not YAML
and deliberately not a seventh top-level key over there: the governed sha covers
everything outside `persistence:`, so one Czech label edit would bump a `contract_version`
and re-mine the whole 10.9M-row location claim corpus (`location_data/contracts.py`
records the 5.1M-row / 2.6 GB precedent), and `location_data` imports `scraper`, never the
reverse. Same shape as the per-portal `PortalConfig` in `scraper/portal.py` (rule 21).

Each cell carries four axes:

  * **producer** — where the value comes from. `structured` (named key(s) in the portal's
    stored payload), `text` (mined from the ad prose), `derived` (computed by the parse
    from something that is not a payload attribute key — the URL, the breadcrumb, the
    SEO title, another column) or `none` (nothing is written).
  * **keys** — for a `structured` cell, the source keys IN PRECEDENCE ORDER, replacing
    the inline `params.get(a) or params.get(b)` chains. Every key must appear in that
    portal's checked-in census (gate A1: no dead read).
  * **absence** — what a MISSING key means: `false` or `unknown`. Not derivable and not a
    helper's decision: remax emits `garaz` as "Ano" or not at all, so absence there is
    unknown, while bezrealitky's `parking` is a real boolean and its `has_parking` has
    always read a missing key as false.
  * **sentinels** — values to read as absent ("neuvedeno", sreality's "- nezadáno").

A cell with nothing behind it carries `gap=` naming the census key W4 will wire, or
`gap=None` where the portal genuinely never states the fact. That marker is the ONE place
"this zero is known" is declared — `scripts/verify_pipeline.py`'s fill matrix reads it
rather than carrying a second flag of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping

from scraper.vocabulary import fold

Producer = Literal["structured", "text", "derived", "none"]
Absence = Literal["false", "unknown"]


@dataclass(frozen=True)
class Cell:
    producer: Producer
    keys: tuple[str, ...] = ()
    absence: Absence = "unknown"
    sentinels: tuple[str, ...] = ()
    gap: str | None = None
    note: str | None = None


def _cell(producer: Producer, *keys: str, absence: Absence = "unknown",
          sentinels: Iterable[str] = (), gap: str | None = None,
          note: str | None = None) -> Cell:
    return Cell(producer, tuple(keys), absence, tuple(sentinels), gap, note)


# sreality paints the sale STATUS over the condition / building-type NAME on a reserved or
# sold advert, and spells "not specified" as a leading dash. Both are absence, not value.
_SREALITY_UNSET = ("- nezadáno", "- vyber", "Rezervováno", "Prodáno")

# `area_m2` on the HTML portals is the one cell whose keys are a SLOT ORDER rather than a
# precedence: `scraper.area.derive_headline_area` takes the portal's užitná, its floor or
# total measure and its parcel as separate arguments and picks between them by category.
# Each `areas_from_params` unpacks `source_values(SOURCE, "area_m2", params)` in exactly
# the order declared below, so the cell is the only declaration of those keys. The shared
# tuples this replaced were a hand-written restatement that named seven keys no parser read
# (laundering them past gate A2) while omitting five it did.

CONTRACT: dict[str, dict[str, Cell]] = {
    # --- sreality: its own flattened JSON estate ---------------------------
    "sreality": {
        "category_main": _cell("structured", "category_main_cb"),
        "category_type": _cell("structured", "category_type_cb"),
        "price_czk": _cell("structured", "price_summary_czk", "price_czk"),
        "price_unit": _cell("structured", "price_summary_unit_cb", "price_unit_cb"),
        "area_m2": _cell("structured", "usable_area", "estate_area"),
        "area_basis": _cell("derived", note="stamped by scraper.area from the two measures"),
        "disposition": _cell("structured", "category_sub_cb", "advert_name"),
        "floor": _cell("structured", "floor_number"),
        "total_floors": _cell("structured", "floors"),
        "has_balcony": _cell("structured", "balcony", "terrace", "loggia"),
        # `parking_lots` is a BOOLEAN in sreality's payload and `parking` the count — the
        # opposite of what the column names suggest. W4 owns that; this records it.
        "has_parking": _cell("structured", "parking_lots", "garage", "parking"),
        "has_lift": _cell("structured", "elevator", sentinels=_SREALITY_UNSET),
        "building_type": _cell("structured", "building_type", sentinels=_SREALITY_UNSET),
        "condition": _cell("structured", "building_condition", sentinels=_SREALITY_UNSET),
        "energy_rating": _cell("structured", "energy_efficiency_rating_cb",
                               sentinels=_SREALITY_UNSET),
        "estate_area": _cell("structured", "estate_area"),
        "usable_area": _cell("structured", "usable_area"),
        "garden_area": _cell("structured", "garden_area"),
        "category_sub_cb": _cell("structured", "category_sub_cb"),
        "subtype": _cell("structured", "category_sub_cb"),
        "furnished": _cell("structured", "furnished", sentinels=_SREALITY_UNSET),
        "terrace": _cell("structured", "terrace"),
        "cellar": _cell("structured", "cellar"),
        "garage": _cell("structured", "garage"),
        "parking_lots": _cell("structured", "parking"),
        "ownership": _cell("structured", "ownership", sentinels=_SREALITY_UNSET),
    },
    # --- bezrealitky: the GraphQL advert -----------------------------------
    "bezrealitky": {
        "category_main": _cell("structured", "estateType"),
        "category_type": _cell("structured", "offerType"),
        "price_czk": _cell("structured", "price"),
        "price_unit": _cell("derived", note="the offerType dialect: celkem / měsíc"),
        "area_m2": _cell("structured", "surface", "surfaceLand"),
        "area_basis": _cell("derived"),
        "disposition": _cell("structured", "disposition"),
        "floor": _cell("structured", "etage"),
        "total_floors": _cell("structured", "totalFloors"),
        "has_balcony": _cell("structured", "balconySurface", "terraceSurface",
                             "loggiaSurface"),
        # The one cell on any portal whose absence has always meant false: both keys are
        # real booleans, and the parser has always read a missing pair as "no parking".
        "has_parking": _cell("structured", "parking", "garage", absence="false"),
        "has_lift": _cell("structured", "lift"),
        "building_type": _cell("structured", "construction", sentinels=("UNDEFINED",)),
        "condition": _cell("structured", "condition", sentinels=("UNDEFINED",)),
        "energy_rating": _cell("structured", "penb"),
        "estate_area": _cell("structured", "surfaceLand"),
        "usable_area": _cell("structured", "surface"),
        "garden_area": _cell("structured", "frontGarden",
                             note="a front yard, not a parcel"),
        "category_sub_cb": _cell("none", gap=None),
        "subtype": _cell("structured", "estateType"),
        "furnished": _cell("structured", "equipped"),
        "terrace": _cell("structured", "terraceSurface"),
        "cellar": _cell("structured", "cellarSurface"),
        "garage": _cell("structured", "garage"),
        "parking_lots": _cell("none", gap=None),
        "ownership": _cell("structured", "ownership",
                           sentinels=("UNDEFINED", "OSTATNI")),
    },
    # --- mmreality: the Vue `:property` estate object ----------------------
    "mmreality": {
        "category_main": _cell("structured", "group"),
        "category_type": _cell("structured", "category"),
        "price_czk": _cell("structured", "price"),
        "price_unit": _cell("derived"),
        "area_m2": _cell("structured", "usableArea", "parcelArea", "gardenArea"),
        "area_basis": _cell("derived"),
        "disposition": _cell("structured", "type", "title"),
        "floor": _cell("structured", "floor"),
        "total_floors": _cell("structured", "overgroundFloors", "undergroundFloors"),
        "has_balcony": _cell("structured", "accessoryGroups",
                             gap="balcony", note="W4: the structured balcony/loggia "
                             "booleans are on 34% of rows and no accessory name has ever "
                             "matched, so this cell is 0/0 on every active row"),
        "has_parking": _cell("structured", "parkingPlaces", "accessoryGroups"),
        "has_lift": _cell("structured", "lift"),
        "building_type": _cell("structured", "construction", sentinels=("neuvedeno",)),
        "condition": _cell("structured", "condition", sentinels=("neuvedeno",)),
        "energy_rating": _cell("structured", "energyClassification"),
        "estate_area": _cell("structured", "parcelArea"),
        "usable_area": _cell("structured", "usableArea"),
        "garden_area": _cell("structured", "gardenArea"),
        "category_sub_cb": _cell("none", gap=None),
        "subtype": _cell("none", gap="type"),
        "furnished": _cell("none", gap="equipment"),
        "terrace": _cell("structured", "accessoryGroups", gap="accessoryGroups"),
        "cellar": _cell("structured", "cellar", "accessoryGroups"),
        "garage": _cell("structured", "accessoryGroups"),
        "parking_lots": _cell("structured", "parkingPlaces"),
        "ownership": _cell("structured", "ownership", sentinels=("neuvedeno",)),
    },
    # --- ceskereality: the `i-info` spec list ------------------------------
    "ceskereality": {
        "category_main": _cell("derived", note="the /{sale}/{category}/ URL segment"),
        "category_type": _cell("derived"),
        "price_czk": _cell("structured", "cena",
                           note="the JSON-LD offer wins where it is present"),
        "price_unit": _cell("derived"),
        "area_m2": _cell("structured", "plocha užitná", "plocha pozemku",
                         note="the (usable, plot) slot order derive_headline_area takes"),
        "area_basis": _cell("derived"),
        "disposition": _cell("derived", note="the h1 title; the portal ships no "
                             "`dispozice` cell on any page of a 1,000-row census"),
        "floor": _cell("structured", "patro"),
        "total_floors": _cell("none", gap=None),
        "has_balcony": _cell("structured", "balkóny"),
        "has_parking": _cell("none", gap="parkování"),
        "has_lift": _cell("none", gap=None),
        "building_type": _cell("structured", "konstrukce"),
        "condition": _cell("structured", "stav nemovitosti"),
        "energy_rating": _cell("structured", "energetická náročnost"),
        "estate_area": _cell("structured", "plocha pozemku"),
        "usable_area": _cell("structured", "plocha užitná"),
        "garden_area": _cell("none", gap=None),
        "category_sub_cb": _cell("none", gap=None),
        "subtype": _cell("none", gap=None),
        "furnished": _cell("none", gap="vybavení pronájem"),
        "terrace": _cell("none", gap="balkóny"),
        "cellar": _cell("none", gap=None),
        "garage": _cell("none", gap="parkování"),
        "parking_lots": _cell("none", gap="parkování"),
        "ownership": _cell("structured", "vlastnictví"),
    },
    # --- idnes: the `<dl>` spec rows ---------------------------------------
    "idnes": {
        "category_main": _cell("derived"),
        "category_type": _cell("derived"),
        "price_czk": _cell("structured", "cena",
                           note="the .b-detail__price element wins where it is present"),
        "price_unit": _cell("derived"),
        "area_m2": _cell("structured", "užitná plocha", "plocha pozemku",
                         note="the (usable, plot) slot order derive_headline_area takes"),
        "area_basis": _cell("derived"),
        "disposition": _cell("derived", note="the h1 title"),
        "floor": _cell("structured", "podlaží"),
        "total_floors": _cell("structured", "počet podlaží budovy"),
        "has_balcony": _cell("structured", "balkon", "lodžie", "terasa"),
        "has_parking": _cell("structured", "parkování", "počet parkovacích míst"),
        "has_lift": _cell("structured", "výtah"),
        "building_type": _cell("structured", "konstrukce budovy"),
        "condition": _cell("structured", "stav bytu", "stav budovy"),
        "energy_rating": _cell("structured", "penb"),
        "estate_area": _cell("structured", "plocha pozemku"),
        "usable_area": _cell("structured", "užitná plocha"),
        "garden_area": _cell("structured", "plocha zahrady"),
        "category_sub_cb": _cell("none", gap=None),
        "subtype": _cell("derived", note="a keyword match over the og:title"),
        "furnished": _cell("structured", "vybavení", "vybavení domu"),
        "terrace": _cell("structured", "terasa"),
        "cellar": _cell("structured", "sklep"),
        "garage": _cell("structured", "dvojgaráž", "parkování"),
        "parking_lots": _cell("structured", "počet parkovacích míst"),
        "ownership": _cell("structured", "vlastnictví"),
    },
    # --- maxima: the `slider_label` spec table -----------------------------
    "maxima": {
        "category_main": _cell("derived", note="the native-id prefix + the title verb"),
        "category_type": _cell("derived"),
        "price_czk": _cell("derived", note="the div.price element"),
        "price_unit": _cell("derived"),
        "area_m2": _cell("structured", "plocha užitná", "plocha podlahová",
                         "plocha pozemku",
                         note="the (usable, floor, plot) slot order"),
        "area_basis": _cell("derived"),
        "disposition": _cell("derived", note="the h3 title"),
        "floor": _cell("structured", "podlaží"),
        "total_floors": _cell("structured", "podlaží"),
        "has_balcony": _cell("structured", "balkón"),
        "has_parking": _cell("structured", "parkovací stání", "garáž"),
        "has_lift": _cell("structured", "výtah"),
        "building_type": _cell("structured", "budova"),
        "condition": _cell("structured", "stav objektu"),
        "energy_rating": _cell("structured", "penb",
                               note="a whole-page `PENB: X` scan is the last resort"),
        "estate_area": _cell("structured", "plocha pozemku"),
        "usable_area": _cell("structured", "plocha užitná"),
        "garden_area": _cell("none", gap=None),
        "category_sub_cb": _cell("none", gap=None),
        "subtype": _cell("none", gap=None,
                         note="operator ruling: `typ domu` is structural, not a subtype"),
        "furnished": _cell("none", gap="vybavení"),
        "terrace": _cell("structured", "terasa"),
        "cellar": _cell("none", gap=None),
        "garage": _cell("structured", "garáž"),
        "parking_lots": _cell("none", gap=None),
        "ownership": _cell("structured", "vlastnictví"),
    },
    # --- realitymix: the `detail-information__data-item` spec list ---------
    "realitymix": {
        "category_main": _cell("derived", note="the breadcrumb, else the URL slug"),
        "category_type": _cell("derived"),
        "price_czk": _cell("derived", note="the short-props price row"),
        "price_unit": _cell("derived"),
        "area_m2": _cell("structured", "užitná plocha", "celková podlahová plocha",
                         "plocha", "plocha parcely",
                         note="the (usable, floor, total, plot) slot order"),
        "area_basis": _cell("derived"),
        "disposition": _cell("structured", "dispozice bytu"),
        "floor": _cell("structured", "číslo podlaží v domě"),
        "total_floors": _cell("structured", "počet podlaží objektu"),
        "has_balcony": _cell("structured", "balkon", "lodžie"),
        "has_parking": _cell("structured", "ostatní"),
        "has_lift": _cell("none", gap=None),
        "building_type": _cell("structured", "druh objektu"),
        "condition": _cell("structured", "stav objektu"),
        "energy_rating": _cell("structured", "energetická náročnost budovy"),
        "estate_area": _cell("structured", "plocha parcely"),
        "usable_area": _cell("structured", "užitná plocha"),
        # The live key is `zahrada`; `areas_from_params` used to read `plocha zahrady`,
        # which realitymix emits on no row, so the column is 0-filled on all 48,757 (W4).
        "garden_area": _cell("none", gap="zahrada"),
        "category_sub_cb": _cell("none", gap=None),
        "subtype": _cell("none", gap=None),
        "furnished": _cell("structured", "vybaveno"),
        "terrace": _cell("structured", "terasa"),
        "cellar": _cell("none", gap="sklep"),
        "garage": _cell("structured", "ostatní"),
        "parking_lots": _cell("none", gap="počet míst k parkování"),
        "ownership": _cell("structured", "vlastnictví"),
    },
    # --- remax: the `pd-detail-info__row` spec rows ------------------------
    "remax": {
        "category_main": _cell("structured", "typ nemovitosti"),
        "category_type": _cell("derived", note="the title verb, else the URL"),
        "price_czk": _cell("derived",
                           note="data-advert-price, else the .pd-table price cell"),
        "price_unit": _cell("derived"),
        "area_m2": _cell("structured", "uzitna plocha", "celkova plocha",
                         "plocha parcely",
                         note="the (usable, total, plot) slot order"),
        "area_basis": _cell("derived"),
        "disposition": _cell("structured", "dispozice"),
        "floor": _cell("structured", "cislo podlazi"),
        "total_floors": _cell("structured", "pocet podlazi v objektu"),
        "has_balcony": _cell("none", gap=None,
                             note="no balcony/loggia key in a 1,000-row census"),
        "has_parking": _cell("structured", "garaz"),
        "has_lift": _cell("structured", "vytah"),
        "building_type": _cell("structured", "druh objektu"),
        "condition": _cell("structured", "stav objektu"),
        "energy_rating": _cell("structured", "energeticka narocnost budovy"),
        "estate_area": _cell("structured", "plocha parcely"),
        "usable_area": _cell("structured", "uzitna plocha"),
        "garden_area": _cell("structured", "plocha zahrady"),
        "category_sub_cb": _cell("none", gap=None),
        "subtype": _cell("structured", "typ nemovitosti"),
        "furnished": _cell("structured", "vybaveno"),
        "terrace": _cell("none", gap=None),
        "cellar": _cell("none", gap=None),
        "garage": _cell("structured", "garaz"),
        "parking_lots": _cell("none", gap="pocet parkovacich mist"),
        "ownership": _cell("structured", "vlastnictvi"),
    },
    # --- bazos: no spec table at all; the ad's own words -------------------
    "bazos": {
        "category_main": _cell("derived", note="the breadcrumb"),
        "category_type": _cell("derived"),
        "price_czk": _cell("structured", "price_text"),
        "price_unit": _cell("derived"),
        "area_m2": _cell("text", note="scraper.area over the title + description"),
        "area_basis": _cell("derived"),
        "disposition": _cell("text"),
        "floor": _cell("text", note="scraper.floor over the same haystack"),
        "total_floors": _cell("text"),
        "has_balcony": _cell("none", gap=None),
        "has_parking": _cell("none", gap=None),
        "has_lift": _cell("none", gap=None),
        "building_type": _cell("none", gap=None),
        "condition": _cell("none", gap=None),
        "energy_rating": _cell("none", gap=None),
        "estate_area": _cell("none", gap=None),
        "usable_area": _cell("none", gap=None),
        "garden_area": _cell("none", gap=None),
        "category_sub_cb": _cell("none", gap=None),
        "subtype": _cell("derived", note="the breadcrumb section slug"),
        "furnished": _cell("none", gap=None),
        "terrace": _cell("none", gap=None),
        "cellar": _cell("none", gap=None),
        "garage": _cell("none", gap=None),
        "parking_lots": _cell("none", gap=None),
        "ownership": _cell("none", gap=None),
    },
}

# Census keys no cell reads, with the reason. The A2 gate refuses a key above the floor
# that is neither mapped nor listed here: the point is to turn "nobody noticed" into
# "someone decided". `no column` is PROGRAM.md §6 — this program adds none.
IGNORED: dict[str, dict[str, str]] = {
    "sreality": {
        "hash_id": "identity", "seo": "identity, not an attribute",
        "advert_code": "the portal's own reference number", "edited": "published_at",
        "locality": "location contract territory (contracts/portals/sreality.yaml)",
        "object_location": "location", "premise": "broker intelligence",
        "user": "broker intelligence",
        "advert_description": "the text lane's substrate",
        "advert_images": "images phase", "videos": "the video media table",
        "panorama": "media", "panorama_data": "media", "matterport_url": "media",
        "keywords": "no column", "labels": "no column", "labels_extended": "no column",
        "stats": "no column", "project": "no column", "protection": "no column",
        "since": "no column", "sale_date": "no column", "ready_date": "no column",
        "rus": "no column", "rus_reply": "no column", "state_cb": "no column",
        "price": "price_czk", "price_summary": "price_czk",
        "price_currency_cb": "CZK only", "price_czk_m2": "computed per-m², not stored",
        "price_note": "no column", "price_summary_old": "no column",
        "price_summary_old_czk": "no column",
        "price_flag_negotiation_cb": "no column",
        "commission": "no column", "refundable_deposit": "no column",
        "tenant_not_pay_commission": "no column", "lease_type_cb": "no column",
        "personal": "no column", "garret": "no column", "low_energy": "no column",
        "object_age": "no column", "object_kind": "no column",
        "object_type": "no column", "surroundings_type": "no column",
        "reconstruction_year": "no column", "underground_floors": "no column",
        "room_count_cb": "no column", "solar_panels": "no column",
        "ftv_panels": "no column", "circuit_breaker_cb": "no column",
        "phase_distributions_cb": "no column",
        "sdn_energy_performance_attachment_url": "no column",
        "energy_performance_certificate": "no column",
        "energy_performance_summary": "no column", "acceptance_year": "no column",
        "annuity": "no column", "cost_of_living": "no column",
        "beginning_date": "no column", "finish_date": "no column",
        "first_tour_date": "no column", "first_tour_date_to": "no column",
        "discount_show": "no column", "exclusively_at_rk": "no column",
        "easy_access": "no column", "electricity_set": "no column",
        "flat_class": "no column", "basin": "no column", "basin_area": "no column",
        "building_area": "no column", "balcony_area": "no column",
        "cellar_area": "no column", "floor_area": "no column",
        "garage_count": "no column", "loggia_area": "no column",
        "terrace_area": "no column",
        # The `*_set` families are multi-value utility lists (heating, water, gas,
        # transport, telecoms, roads) — no column each, and PROGRAM.md §6 adds none.
        "gas_set": "no column", "gully_set": "no column", "heating_set": "no column",
        "heating_element_set": "no column", "heating_source_set": "no column",
        "road_type_set": "no column", "telecommunication_set": "no column",
        "transport_set": "no column", "water_set": "no column",
        "water_heat_source_set": "no column", "well_type_set": "no column",
        "internet_connection_provider": "no column",
        "internet_connection_speed": "no column",
        "internet_connection_type_set": "no column",
    },
    "bezrealitky": {
        "address": "location contract territory", "addressInput": "location",
        "city": "location", "cityDistrict": "location", "regionTree": "location",
        "street": "location", "zip": "location", "houseNumber": "location",
        "gps": "location", "ruianId": "location (the R0 rung, owed to that program)",
        "id": "identity", "uri": "identity", "title": "identity",
        "active": "lifecycle", "isDiscounted": "no column",
        "originalPrice": "no column", "charges": "no column", "currency": "CZK only",
        "timeActivated": "published_at", "timeDeactivated": "lifecycle",
        "mainImage": "images phase", "publicImages": "images phase",
        "image_urls": "images phase", "description": "the text lane's substrate",
    },
    "mmreality": {
        "id": "identity", "slug": "identity", "source_url": "identity",
        "shortTitle": "identity", "originalTitle": "identity",
        "active": "lifecycle", "accurate": "location", "country": "location",
        "countryCode": "location", "countryId": "location", "district": "location",
        "districtId": "location", "municipality": "location",
        "municipalityId": "location", "municipalityPart": "location",
        "location": "location", "point": "location", "street": "location",
        "poi": "amenities are their own OSM mirror (rule 10)",
        "broker": "broker intelligence", "mortgageAdviser": "broker intelligence",
        "participantIds": "broker intelligence",
        "description": "the text lane's substrate", "image": "images phase",
        "images": "images phase", "image_urls": "images phase",
        "totalArea": "no column", "builtUpArea": "no column",
        "cellarArea": "no column", "nonResidentArea": "no column",
        "rooms": "no column", "placement": "no column", "situation": "no column",
        "hasAuction": "no column", "canBeSubjectOfAuction": "no column",
        "marginIncluded": "no column", "pricePerMeter": "no column",
        "pricePeriod": "price_unit is derived from the category", "priceNote": "no column",
        "swimmingPool": "no column", "wheelchairAccess": "no column",
        "equipment": "W4 wires it to furnished",
        "garage": "W4 wires it to garage; today the accessory names are read instead",
        "balcony": "W4 wires it to has_balcony", "loggia": "W4 wires it to has_balcony",
        "terraceArea": "no column", "balconyArea": "no column", "loggiaArea": "no column",
    },
    "ceskereality": {
        "id nemovitosti": "the portal's own reference number",
        "datum vložení": "published_at",
        "příjezdy": "no column", "dopravní spojení": "no column",
        "elektřina": "no column", "kanalizace": "no column", "zdroje vody": "no column",
        "inženýrské sítě": "no column", "způsoby vytápění": "no column",
        "vytápění podrobnosti": "no column", "cena nezahrnuje": "no column",
        "druhy bytů": "no column", "okna": "no column", "zateplení": "no column",
        "wc": "no column", "parkování": "W4 wires it to has_parking / garage",
        "vybavení pronájem": "W4 wires it to furnished",
        "plocha obytná": "no column (a living area, not the užitná measure)",
        "plocha celková": "no column (a whole-building total, not the užitná measure)",
        "plocha zastavěná": "no column (a built-up area, not a headline measure)",
    },
    "idnes": {
        "číslo zakázky": "the portal's own reference number",
        "spočítej stěhování": "a CTA, always json null",
        "spočítej vyklizení": "a CTA, always json null",
        "počet podlaží": "W4 wires it to total_floors on houses (29.7k rows)",
        "datum nastěhování": "no column", "topení": "no column",
        "topné těleso": "no column", "zdroj vytápění": "no column",
        "zdroj ohřevu vody": "no column", "plyn": "no column", "voda": "no column",
        "odpad": "no column", "kanalizace": "no column", "elektřina": "no column",
        "internet": "no column", "televize": "no column", "telefon": "no column",
        "poloha domu": "no column", "lokalita objektu": "no column",
        "lokalita projektu": "no column", "přístupová komunikace": "no column",
        "počet místností": "no column", "výstavba": "no column",
        "rekonstrukce": "no column", "kolaudace": "no column",
        "roční spotřeba energie": "no column", "bezbariérový přístup": "no column",
        "dopravní dostupnost": "no column", "občanská vybavenost": "no column",
        "vratná kauce": "no column", "provize": "no column",
        "typ pronájmu": "no column", "typ komerční nemovitosti": "no column",
        "podlaží umístění": "a duplicate of `podlaží` (W8 territory)",
        "počet podzemních podlaží": "no column",
        "připojení k internetu": "no column",
        "zastavěná plocha": "no column (a built-up area, not a headline measure)",
    },
    "maxima": {
        "id zakázky": "the portal's own reference number", "topení": "no column",
        "voda": "no column", "odpad": "no column", "plyn": "no column",
        "elektřina": "no column", "doprava": "no column", "typ domu": "no column",
        "poloha domu": "no column", "bazén": "no column",
        "lodžie": "W4: has_balcony reads `balkón` only",
        "vybavení": "W4 wires it to furnished",
    },
    "realitymix": {
        "doprava": "no column", "elektřina": "no column", "voda": "no column",
        "odpad": "no column", "plyn": "no column", "topení": "no column",
        "zdroj topení": "no column", "topné těleso": "no column",
        "zdroj teplé vody": "no column", "telekomunikace": "no column",
        "ostatní rozvody": "no column", "inženýrské sítě": "no column",
        "komunikace": "no column", "umístění objektu": "no column",
        "poloha objektu": "no column", "typ domu": "no column",
        "druh pozemku": "no column", "druh prostor": "no column",
        "energetický ukazatel podle vyhlášky": "no column",
        "ukazatel energetické náročnosti budovy": "no column",
        "počet podlaží pod zemí": "no column", "stáří objektu": "no column",
        "číslo jednotky": "no column", "výše vratné kauce": "no column",
        "bezbariérový byt": "no column", "nízkoenergetický": "no column",
        "fotovoltaika": "no column", "typ internetového připojení": "no column",
        "typ pronájmu": "no column", "občanská vybavenost": "no column",
        "popis vybavení": "no column", "sklep": "W4 wires it to cellar",
        "zastavěná plocha": "no column (a built-up area, not a headline measure)",
        "celková plocha": "no column (a whole-building total, not the užitná measure)",
        "zahrada": "W4 wires it to garden_area",
    },
    "remax": {
        "cislo zakazky": "the portal's own reference number",
        "k nastehovani": "no column", "doprava": "no column", "voda": "no column",
        "odpad": "no column", "plyn": "no column", "elektrina": "no column",
        "telekomunikace": "no column", "ostatni rozvody": "no column",
        "inzenyrske site": "no column", "umisteni objektu": "no column",
        "poloha objektu": "no column", "typ domu": "no column",
        "druh pozemku": "no column", "druh prostor": "no column",
        "charakter okolni zastavby": "no column", "obcanska vybavenost": "no column",
        "bezbarierovy byt": "no column", "rok rekonstrukce": "no column",
        "pocet podlazi pod zemi": "no column",
        "umisteni v chranenych lokalitach": "no column",
        "vybaveni kancelari": "no column", "plocha kancelari": "no column",
        "merna vypoctena rocni spotreba energie v kwh/m²/rok": "no column",
        "pocet parkovacich mist": "W4 wires it to parking_lots",
        "zastavena plocha": "no column (a built-up area, not a headline measure)",
    },
    "bazos": {
        "id": "identity", "title": "the text lane's substrate",
        "coords": "location contract territory", "psc": "location",
        "locality_text": "location", "posted_date": "published_at",
        "views": "no column", "image_urls": "images phase",
    },
}

def cell(portal: str, field: str) -> Cell:
    return CONTRACT[portal][field]


def source_value(portal: str, field: str, params: Mapping[str, Any]) -> Any:
    """The first declared source key present in `params`, in contract order.

    The single replacement for the per-portal `params.get(a) or params.get(b)` chains —
    which is why the precedence is reviewable in one table and provable against the
    census. A declared sentinel reads as absent, so the NEXT key gets its turn."""
    declared = CONTRACT[portal][field]
    sentinels = {fold(s) for s in declared.sentinels}
    for key in declared.keys:
        value = params.get(key)
        if value is None:
            continue
        label = label_of(value)
        if sentinels and label is not None and any(
            fold(label).startswith(s) for s in sentinels
        ):
            continue
        return value
    return None


def label_of(value: Any) -> str | None:
    """The LABEL a payload value carries: sreality and mmreality state an enum as a
    `{name, value}` / `{id, name}` object, every HTML portal as the cell's own text."""
    if isinstance(value, Mapping):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return value if isinstance(value, str) else None


def source_label(portal: str, field: str, params: Mapping[str, Any]) -> str | None:
    """`source_value`, unwrapped to the label an enum cell is read from."""
    return label_of(source_value(portal, field, params))


def source_values(portal: str, field: str, params: Mapping[str, Any]) -> tuple[Any, ...]:
    """One value per declared source key, in contract order, None where absent.

    For the cells whose keys are a UNION rather than a precedence — the legacy combined
    booleans `has_balcony` (balcony | loggia | terrace) and `has_parking` (a space | a
    garage | a count). The contract still declares WHICH keys; how they combine stays
    with the parser, because the combination differs per column, not per portal."""
    declared = CONTRACT[portal][field]
    return tuple(
        source_value(portal, field, {key: params.get(key)}) for key in declared.keys
    )


def known_gaps() -> dict[str, str | None]:
    """`"{portal}/{field}" -> the census key W4 will wire` for every cell with nothing
    behind it. The one declaration `verify_pipeline` reads to say which of the live
    zero-fill cells are known."""
    return {
        f"{portal}/{field}": declared.gap
        for portal, cells in CONTRACT.items()
        for field, declared in cells.items()
        if declared.gap is not None or declared.producer == "none"
    }
