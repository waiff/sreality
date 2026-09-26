"""One precomputed row per listing (PROGRAM.md E14) — the local twin of `autodedup.listing_fp`.

Everything a probe or a feature needs is derived ONCE here: token sets, shingles, the text
SimHash and its bands, the non-catalog hash set, the anchor image bands, the numeric facts.
A pair pass then touches only precomputed structures, which is what keeps ~150k pairs inside
a five-minute single-core budget.

Key derivation lives in small named pure functions (`block_key_of`, `cat_group_of`,
`area_band_of`, `pin_key_of`) because the production index probes must be derivable from
exactly these, not from a re-reading of the design.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from autodedup.dataset import Dataset, Image, Listing
from autodedup.normalize import (
    disposition_norm,
    fact_text_folded,
    fold,
    normalize_folded,
    numeric_facts_folded,
    shingles,
    simhash64,
    simhash_bands,
)
from autodedup.settings import Settings
from autodedup.stock import PLAIN as STOCK_PLAIN, StockIndex
from toolkit.room_taxonomy import ROOM_FAMILIES

_TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "data" / "clip_taxonomy.json"
CROSS_TYPE_GROUP: str = "dum_komercni"
_CROSS_TYPE_MEMBERS: frozenset[str] = frozenset({"dum", "komercni"})


def _collapse() -> dict[str, str]:
    """Fine CLIP anchor -> logical tag, from the tagger's own taxonomy file (never guessed)."""
    try:
        raw = json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover - the engine runs without the data file
        return {}
    collapse = raw.get("collapse")
    return {str(k): str(v) for k, v in collapse.items()} if isinstance(collapse, dict) else {}


_COLLAPSE: dict[str, str] = _collapse()


def logical_tag(tag: str | None) -> str | None:
    """A fine CLIP anchor collapsed to the tagger's logical room (`situation_plan` ->
    `site_plan`); an unknown tag is returned as itself, never invented and never dropped."""
    if not tag:
        return None
    return _COLLAPSE.get(tag, tag)


def tag_family(tag: str | None) -> str:
    """E10's family for a logical tag or a fine anchor; anything unknown is `other`."""
    if not tag:
        return "other"
    family = ROOM_FAMILIES.get(tag)
    if family is not None:
        return family
    return ROOM_FAMILIES.get(_COLLAPSE.get(tag, ""), "other")


def family_scores(image: Image) -> dict[str, float]:
    """Best tag score per family for one image; an unscored tag still counts as present."""
    out: dict[str, float] = {}
    for tag, score in image.tags:
        family = tag_family(tag)
        value = float(score) if score is not None else 0.0
        if value > out.get(family, -1.0):
            out[family] = value
    return out


def dominant_family(image: Image) -> str:
    scores = family_scores(image)
    if not scores:
        return "other"
    return max(sorted(scores), key=lambda family: scores[family])


def block_key_of(obec_kod: int | None, cast_obce_kod: int | None) -> str:
    """`cast_obce_kod` in the split cities, else `obec_kod`; grain-prefixed so a část code can
    never collide numerically with an obec code."""
    if cast_obce_kod is not None:
        return f"c{cast_obce_kod}"
    if obec_kod is not None:
        return f"o{obec_kod}"
    return ""


def cat_group_of(category_main: str | None) -> str | None:
    """E3's sanctioned cross-type folded into one blocking token."""
    if category_main is None:
        return None
    return CROSS_TYPE_GROUP if category_main in _CROSS_TYPE_MEMBERS else category_main


def area_band_of(area_m2: float | None, settings: Settings) -> int | None:
    """`floor(ln(area)/w)` — a range tolerance turned into an equality lookup."""
    if area_m2 is None or area_m2 <= 0.0:
        return None
    return math.floor(math.log(area_m2) / settings.band_width())


def house_number_key_of(
    house_number: str | None, house_number_cp: str | None, house_number_co: str | None
) -> str | None:
    """E15's K2 component, provenance-tagged: `cp:12` can never collide with `co:12`, and a
    bare number of unknown provenance keeps K2 silent rather than guessing."""
    if house_number_cp:
        return f"cp:{house_number_cp}"
    if house_number_co:
        return f"co:{house_number_co}"
    if house_number and "/" in house_number:
        return f"cp:{house_number.split('/', 1)[0]}"
    return None


def pin_key_of(lat: float | None, lon: float | None) -> str | None:
    """Exact-pin identity at 6 decimal places (~11 cm) — a feature key, never a probe key (E16)."""
    if lat is None or lon is None:
        return None
    return f"{lat:.6f},{lon:.6f}"


@dataclass(slots=True)
class Fingerprint:
    listing_id: int
    source: str | None
    block_key: str
    obec_kod: int | None
    cast_obce_kod: int | None
    cat_group: str | None
    category_main: str | None
    category_type: str | None
    area_m2: float | None
    area_band: int | None
    disposition: str | None
    floor: int | None
    total_floors: int | None
    broker_key: str | None
    broker_identity_id: int | None
    street_key: str | None
    house_number: str | None  # provenance-tagged ("cp:12"), never a bare number — E15/K2
    ruian_adm_kod: int | None
    psc: str | None
    lat: float | None
    lon: float | None
    granularity_rank: int | None
    is_address_grain: bool | None
    pin_key: str | None
    price: float | None
    price_events: list[tuple[str, float]] = field(default_factory=list)
    desc_norm: str = ""
    desc_tokens: list[str] = field(default_factory=list)
    desc_shingles: set[int] = field(default_factory=set)
    desc_simhash: int | None = None
    desc_bands: list[tuple[int, int]] = field(default_factory=list)
    numerals: set[tuple[str, float]] = field(default_factory=set)
    image_hashes: set[int] = field(default_factory=set)
    all_hashes: set[int] = field(default_factory=set)
    catalog_ratio: float | None = None
    anchor_bands: list[tuple[int, int, int]] = field(default_factory=list)
    room_frames: tuple[tuple[str, int], ...] = ()
    image_ids: list[int] = field(default_factory=list)
    interior_image_ids: list[int] = field(default_factory=list)
    exterior_image_ids: list[int] = field(default_factory=list)
    plan_image_ids: list[int] = field(default_factory=list)
    n_images: int = 0
    country_status: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    inactive_at: str | None = None
    is_active: bool = True
    has_text: bool = False


def _anchor_sort_key(image: Image) -> tuple[float, int, int]:
    interior = family_scores(image).get("interior", -1.0)
    return (-interior, image.seq if image.seq is not None else 1 << 30, image.image_id)


def build_fingerprint(
    listing: Listing,
    images: Sequence[Image],
    settings: Settings,
    stock: "StockIndex | None" = None,
    shared: frozenset[int] = frozenset(),
) -> Fingerprint:
    """One listing plus its gallery -> the row every probe and feature reads.

    `stock` is E83's carrier-aware catalogue index; omitting it is plain E9 (every populous
    frame is stock), which is the conservative reading and what g6 ships."""
    loc = listing.location
    description = listing.description or ""
    has_text = len(description) >= settings.text_min_chars
    folded = fold(description)  # deaccent is ~40% of this pass; both forms share the one fold
    desc_norm = normalize_folded(folded)
    desc_tokens = desc_norm.split()
    simhash = simhash64(desc_tokens) if has_text and desc_tokens else None
    bands = (simhash_bands(simhash, settings.simhash_bands, settings.band_bits)
             if simhash is not None else [])

    hashed = [img for img in images if img.phash is not None]
    # E9 can only subtract what was measured: `pop == 0`/None means the exporter's population
    # probe did not run, and publishing catalog_ratio=0.0 there would disarm K-C's stock guard.
    pop_measured = all(img.pop_is_measured() for img in hashed)
    test = stock or STOCK_PLAIN
    catalog = [img for img in hashed if test.is_stock(img, settings.catalog_df)]
    non_catalog = [img for img in hashed if not test.is_stock(img, settings.catalog_df)]
    anchors = sorted(non_catalog, key=_anchor_sort_key)[: settings.anchor_images]
    anchor_bands: list[tuple[int, int, int]] = []
    for image in anchors:
        phash = int(image.phash) if image.phash is not None else 0
        for band_no, band_val in simhash_bands(phash, settings.simhash_bands, settings.band_bits):
            anchor_bands.append((band_no, band_val, phash))

    # C7: the frames the photo-override rung may count — non-stock, in a proof room, and (when
    # the own-frame bar is set) carried by at most that many adverts corpus-wide.
    room_frames: list[tuple[str, int]] = []
    if settings.photo_override_frames > 0:
        own = settings.photo_override_own_pop_max
        for image in non_catalog:
            room = image.room_tag()
            if room is None or image.phash is None:
                continue
            if "*" not in settings.photo_override_rooms and room not in settings.photo_override_rooms:
                continue
            if own > 0 and (image.pop is None or image.pop > own):
                continue
            if int(image.phash) in shared:
                continue
            room_frames.append((room, int(image.phash)))

    by_family: dict[str, list[int]] = {"interior": [], "exterior": [], "plan": []}
    for image in images:
        family = dominant_family(image)
        if family in by_family:
            by_family[family].append(image.image_id)

    return Fingerprint(
        listing_id=listing.id,
        source=listing.source,
        block_key=block_key_of(loc.obec_kod, loc.cast_obce_kod),
        obec_kod=loc.obec_kod,
        cast_obce_kod=loc.cast_obce_kod,
        cat_group=cat_group_of(listing.category_main),
        category_main=listing.category_main,
        category_type=listing.category_type,
        area_m2=listing.area_m2,
        area_band=area_band_of(listing.area_m2, settings),
        disposition=disposition_norm(listing.disposition),
        floor=listing.floor,
        total_floors=listing.total_floors,
        broker_key=listing.broker_key,
        broker_identity_id=listing.broker_identity_id,
        street_key=loc.street_key,
        house_number=house_number_key_of(loc.house_number, loc.house_number_cp,
                                         loc.house_number_co),
        ruian_adm_kod=loc.ruian_adm_kod,
        psc=loc.psc,
        lat=loc.lat,
        lon=loc.lon,
        granularity_rank=loc.granularity_rank,
        is_address_grain=loc.is_address_grain,
        pin_key=pin_key_of(loc.lat, loc.lon),
        price=listing.price,
        price_events=[(when, price) for when, price in listing.price_history if price is not None],
        desc_norm=desc_norm,
        desc_tokens=desc_tokens,
        desc_shingles=shingles(desc_tokens) if desc_tokens else set(),
        desc_simhash=simhash,
        desc_bands=bands,
        numerals=numeric_facts_folded(fact_text_folded(folded)),
        image_hashes={int(img.phash) for img in non_catalog if img.phash is not None},
        all_hashes={int(img.phash) for img in hashed if img.phash is not None},
        catalog_ratio=(len(catalog) / len(hashed)) if (hashed and pop_measured) else None,
        anchor_bands=anchor_bands,
        room_frames=tuple(room_frames),
        image_ids=[img.image_id for img in images],
        interior_image_ids=by_family["interior"],
        exterior_image_ids=by_family["exterior"],
        plan_image_ids=by_family["plan"],
        n_images=len(images),
        country_status=loc.country_status,
        first_seen_at=listing.first_seen_at,
        last_seen_at=listing.last_seen_at,
        inactive_at=listing.inactive_at,
        is_active=listing.is_active,
        has_text=has_text,
    )


def build_all(ds: Dataset, settings: Settings) -> dict[int, Fingerprint]:
    """Fingerprints for the whole cohort, in listing-id order so every pass is reproducible."""
    stock = StockIndex.of_dataset(ds, settings)
    shared = (colive_shared_frames(ds, settings) if settings.photo_override_frames > 0
              and settings.photo_override_exclusive else frozenset())
    return {
        listing_id: build_fingerprint(
            ds.listings[listing_id], ds.images(listing_id), settings, stock, shared
        )
        for listing_id in sorted(ds.listings)
    }


def colive_shared_frames(ds: Dataset, settings: Settings, min_days: float = 1.0) -> frozenset[int]:
    """C7: frames two adverts of ONE portal carried while both were live (a project's template
    shoot, not one unit's photographs); carriers read inside the cohort, by exact dHash or, with
    `photo_override_exclusive_bits`, by every hash within that many bits (numpy, experiment only)."""
    from autodedup.indistinguishable import honest_overlap_days as overlap_days

    carriers: dict[int, list[Listing]] = {}
    for listing_id, images in ds.images_by_listing.items():
        listing = ds.listings.get(listing_id)
        if listing is None:
            continue
        for image in images:
            if image.phash is not None:
                carriers.setdefault(int(image.phash), []).append(listing)
    bits = settings.photo_override_exclusive_bits
    if bits > 0:
        import numpy as np

        rooms = settings.photo_override_rooms
        queries = sorted({int(im.phash) for ims in ds.images_by_listing.values() for im in ims
                          if im.phash is not None and im.room_tag() is not None
                          and ("*" in rooms or im.room_tag() in rooms)})
        keys = list(carriers)
        table = np.array([k & 0xFFFFFFFFFFFFFFFF for k in keys], dtype=np.uint64)
        widened: dict[int, list[Listing]] = {}
        for start in range(0, len(queries), 256):
            chunk = queries[start:start + 256]
            q = np.array([k & 0xFFFFFFFFFFFFFFFF for k in chunk], dtype=np.uint64)[:, None]
            near = np.bitwise_count(np.bitwise_xor(table[None, :], q)) <= bits
            for row, phash in enumerate(chunk):
                idx = np.nonzero(near[row])[0]
                widened[phash] = [x for i in idx for x in carriers[keys[i]]]
        carriers = widened
    out: set[int] = set()
    for phash, listings in carriers.items():
        if len(listings) < 2:
            continue
        by_source: dict[str | None, list[Listing]] = {}
        for listing in {x.id: x for x in listings}.values():
            by_source.setdefault(listing.source, []).append(listing)
        for group in by_source.values():
            if any((overlap_days(a, b) or 0.0) > min_days
                   for i, a in enumerate(group) for b in group[i + 1:]):
                out.add(phash)
                break
    return frozenset(out)
