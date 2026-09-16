"""Local reader for the W1 cohort artifact — the zero-cost side of the iteration loop.

The export lane writes ONE gzipped JSONL file (`out/cohort.jsonl.gz`), one JSON object per
line, discriminated by `"t"`: a single `meta` record, then one `listing` record per cohort
listing, then one `image` record per image. This module streams that file into small slotted
dataclasses and offers the two primitives every offline wave (blocking, features, model,
clustering, evaluation) needs — `cosine` over the exported CLIP vectors and `hamming64` over
the exported dHashes — so W2 onwards never has to touch the database again.

The artifact carries NO PII by contract (PROGRAM.md E28): brokers appear only as a salted
`broker_key` plus the numeric identity/firm ids, and descriptions are scrubbed of Czech
contact patterns before export. Nothing here re-derives an identity from those fields.

Record order is meta -> listings -> images by contract, but the loader is order-tolerant: it
indexes by id as records arrive and builds the per-block view once the stream ends, so an
image seen before its listing (or a meta record written last) loads identically.

Vectors are cached as `array("f")`, not `list[float]`: 2 KB per image instead of 16 KB —
measured 45 MB vs 345 MB for 21k vectors, i.e. ~90 MB rather than ~740 MB resident over the
D1 cohort, which is the constraint that actually breaks a laptop. `math.sumprod` consumes the
array directly (135k dot products/s here) though a list is 2.7x faster to iterate, so a hot
gallery cross-product should materialise `[list(img.clip_vector()) …]` once per gallery and
call `cosine_norm` with the cached `clip_norm()`s (measured 372k/s that way).
"""

from __future__ import annotations

import base64
import gzip
import json
import math
import statistics
import struct
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

CLIP_DIM: int = 512
CLIP_STRUCT: str = f"<{CLIP_DIM}e"
CLIP_BYTES: int = CLIP_DIM * 2
MASK64: int = (1 << 64) - 1
CATALOG_POP_MIN: int = 8
CATALOG_DF_SWEEP: tuple[int, ...] = (3, 5, 8, 12)

# Listings the exporter could not attribute to a block arrive with `block=""`; they are
# bucketed under one reserved key so they stay visible on every block-grain surface.
UNASSIGNED_BLOCK: str = "(unassigned)"


def _num(value: Any) -> float | None:
    """JSON number (or numeric string) -> finite float; absent/null/NaN/Infinity -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
    return number if math.isfinite(number) else None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


@dataclass(slots=True)
class Block:
    key: str
    grain: str | None = None
    code: int | None = None
    label: str | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Block":
        return cls(
            key=str(raw.get("key") or ""),
            grain=_str(raw.get("grain")),
            code=_int(raw.get("code")),
            label=_str(raw.get("label")),
        )


@dataclass(slots=True)
class Meta:
    exported_at: str | None = None
    generator_version: str | None = None
    blocks: list[Block] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Meta":
        blocks = [Block.from_json(b) for b in (raw.get("blocks") or []) if isinstance(b, dict)]
        return cls(
            exported_at=_str(raw.get("exported_at")),
            generator_version=_str(raw.get("generator_version")),
            blocks=blocks,
            counts=dict(raw.get("counts") or {}),
            params=dict(raw.get("params") or {}),
        )

    def block(self, key: str) -> Block | None:
        for block in self.blocks:
            if block.key == key:
                return block
        return None


@dataclass(slots=True)
class Location:
    obec_kod: int | None = None
    obec_name: str | None = None
    cast_obce_kod: int | None = None
    cast_obce_name: str | None = None
    granularity: str | None = None
    granularity_rank: int | None = None
    is_address_grain: bool | None = None
    lat: float | None = None
    lon: float | None = None
    uncertainty_radius_m: float | None = None
    street_key: str | None = None
    house_number: str | None = None
    # E15's K2 probe keys on čp alone; the joined `house_number` form ("12/3" vs "12") makes
    # two adverts for one address look different, so the components travel separately too.
    house_number_cp: str | None = None
    house_number_co: str | None = None
    psc: str | None = None
    ruian_adm_kod: int | None = None
    country_status: str | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any] | None) -> "Location":
        raw = raw or {}
        return cls(
            obec_kod=_int(raw.get("obec_kod")),
            obec_name=_str(raw.get("obec_name")),
            cast_obce_kod=_int(raw.get("cast_obce_kod")),
            cast_obce_name=_str(raw.get("cast_obce_name")),
            granularity=_str(raw.get("granularity")),
            granularity_rank=_int(raw.get("granularity_rank")),
            is_address_grain=_bool(raw.get("is_address_grain")),
            lat=_num(raw.get("lat")),
            lon=_num(raw.get("lon")),
            uncertainty_radius_m=_num(raw.get("uncertainty_radius_m")),
            street_key=_str(raw.get("street_key")),
            house_number=_str(raw.get("house_number")),
            house_number_cp=_str(raw.get("house_number_cp")),
            house_number_co=_str(raw.get("house_number_co")),
            psc=_str(raw.get("psc")),
            ruian_adm_kod=_int(raw.get("ruian_adm_kod")),
            country_status=_str(raw.get("country_status")),
        )

    def has_point(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(slots=True)
class Listing:
    id: int
    block: str
    source: str | None = None
    source_id_native: str | None = None
    source_url: str | None = None
    category_main: str | None = None
    category_type: str | None = None
    subtype: str | None = None
    disposition: str | None = None
    area_m2: float | None = None
    floor: int | None = None
    total_floors: int | None = None
    price: float | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    description: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    inactive_at: str | None = None
    is_active: bool = True
    broker_key: str | None = None
    broker_identity_id: int | None = None
    broker_firm_id: int | None = None
    location: Location = field(default_factory=Location)
    price_history: list[tuple[str, float | None]] = field(default_factory=list)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Listing":
        history: list[tuple[str, float | None]] = []
        for entry in raw.get("price_history") or []:
            if isinstance(entry, (list, tuple)) and entry:
                history.append((str(entry[0]), _num(entry[1]) if len(entry) > 1 else None))
        return cls(
            id=int(raw["id"]),
            block=str(raw.get("block") or ""),
            source=_str(raw.get("source")),
            source_id_native=_str(raw.get("source_id_native")),
            source_url=_str(raw.get("source_url")),
            category_main=_str(raw.get("category_main")),
            category_type=_str(raw.get("category_type")),
            subtype=_str(raw.get("subtype")),
            disposition=_str(raw.get("disposition")),
            area_m2=_num(raw.get("area_m2")),
            floor=_int(raw.get("floor")),
            total_floors=_int(raw.get("total_floors")),
            price=_num(raw.get("price")),
            attrs=dict(raw.get("attrs") or {}),
            description=_str(raw.get("description")),
            first_seen_at=_str(raw.get("first_seen_at")),
            last_seen_at=_str(raw.get("last_seen_at")),
            inactive_at=_str(raw.get("inactive_at")),
            is_active=bool(raw.get("is_active", True)),
            broker_key=_str(raw.get("broker_key")),
            broker_identity_id=_int(raw.get("broker_identity_id")),
            broker_firm_id=_int(raw.get("broker_firm_id")),
            location=Location.from_json(raw.get("location")),
            price_history=history,
        )


@dataclass(slots=True)
class Image:
    listing_id: int
    image_id: int
    seq: int | None = None
    storage_path: str | None = None
    phash: int | None = None
    pop: int | None = None
    clip: str | None = None
    tags: list[tuple[str, float | None]] = field(default_factory=list)
    _clip_vector: "array[float] | None" = field(
        default=None, init=False, repr=False, compare=False
    )
    _clip_norm: float | None = field(default=None, init=False, repr=False, compare=False)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Image":
        tags: list[tuple[str, float | None]] = []
        for entry in raw.get("tags") or []:
            if isinstance(entry, (list, tuple)) and entry:
                tags.append((str(entry[0]), _num(entry[1]) if len(entry) > 1 else None))
        return cls(
            listing_id=int(raw["listing_id"]),
            image_id=int(raw["image_id"]),
            seq=_int(raw.get("seq")),
            storage_path=_str(raw.get("storage_path")),
            phash=_int(raw.get("phash")),
            pop=_int(raw.get("pop")),
            clip=_str(raw.get("clip")),
            tags=tags,
        )

    def clip_vector(self) -> "array[float] | None":
        """Base64 of 512 little-endian IEEE half floats -> float32 array; cached per image.

        float16 is exactly representable in float32, so the compact cache is lossless."""
        if self._clip_vector is not None:
            return self._clip_vector
        if not self.clip:
            return None
        blob = base64.b64decode(self.clip)
        if len(blob) != CLIP_BYTES:
            raise ValueError(
                f"image {self.image_id}: clip payload is {len(blob)} bytes, expected {CLIP_BYTES}"
            )
        self._clip_vector = array("f", struct.unpack(CLIP_STRUCT, blob))
        return self._clip_vector

    def clip_norm(self) -> float | None:
        """Euclidean norm of the CLIP vector, computed once beside it — see `cosine_norm`."""
        if self._clip_norm is not None:
            return self._clip_norm
        vector = self.clip_vector()
        if vector is None:
            return None
        self._clip_norm = math.sqrt(math.sumprod(vector, vector))
        return self._clip_norm

    def drop_clip_cache(self) -> None:
        """Release the decoded vector so a block-at-a-time pass can bound its resident set."""
        self._clip_vector = None
        self._clip_norm = None

    def pop_is_measured(self) -> bool:
        """A phash-bearing image appears on at least its own listing, so `pop == 0` cannot be
        a real population — it means the exporter's population probe did not run."""
        return self.phash is None or (self.pop is not None and self.pop > 0)

    def is_catalog_candidate(self, pop_min: int = CATALOG_POP_MIN) -> bool:
        return self.pop is not None and self.pop >= pop_min


@dataclass(slots=True)
class Integrity:
    """What the stream itself said about its own consistency, counted while loading."""

    skipped_types: dict[str, int] = field(default_factory=dict)
    duplicate_listing_ids: int = 0
    duplicate_image_ids: int = 0
    orphan_images: int = 0


@dataclass(slots=True)
class Dataset:
    meta: Meta
    listings: dict[int, Listing]
    images_by_listing: dict[int, list[Image]]
    integrity: Integrity = field(default_factory=Integrity)

    def block(self, key: str) -> list[Listing]:
        """Listings of one block in id order — so every downstream pass is order-reproducible."""
        match = "" if key == UNASSIGNED_BLOCK else key
        return sorted(
            (listing for listing in self.listings.values() if listing.block == match),
            key=lambda listing: listing.id,
        )

    def block_keys(self) -> list[str]:
        """Meta's block order first, then any block seen only in the listing stream."""
        keys = [block.key for block in self.meta.blocks]
        unassigned = False
        for listing in sorted(self.listings.values(), key=lambda lst: lst.id):
            if not listing.block:
                unassigned = True
            elif listing.block not in keys:
                keys.append(listing.block)
        if unassigned:
            keys.append(UNASSIGNED_BLOCK)
        return keys

    def images(self, listing_id: int) -> list[Image]:
        return self.images_by_listing.get(listing_id, [])

    def all_images(self) -> Iterator[Image]:
        for bucket in self.images_by_listing.values():
            yield from bucket

    def clip_payload_errors(self) -> list[tuple[int, int]]:
        """`(image_id, byte length)` for every clip payload that is not `CLIP_BYTES` long."""
        bad: list[tuple[int, int]] = []
        for image in self.all_images():
            if not image.clip:
                continue
            size = len(base64.b64decode(image.clip))
            if size != CLIP_BYTES:
                bad.append((image.image_id, size))
        return bad

    def unmeasured_pop_images(self) -> int:
        return sum(1 for image in self.all_images() if not image.pop_is_measured())

    def summary(self, pop_min: int = CATALOG_POP_MIN) -> dict[str, Any]:
        return {key: self._block_summary(key, pop_min) for key in self.block_keys()}

    def _block_summary(self, key: str, pop_min: int = CATALOG_POP_MIN) -> dict[str, Any]:
        listings = self.block(key)
        n = len(listings)
        block = self.meta.block(key)
        out: dict[str, Any] = {
            "grain": block.grain if block else None,
            "code": block.code if block else None,
            "label": block.label if block else None,
            "n_listings": n,
        }
        if n == 0:
            return out

        def share(predicate: Any) -> float:
            return sum(1 for listing in listings if predicate(listing)) / n

        sources: dict[str, int] = {}
        categories: dict[str, int] = {}
        types: dict[str, int] = {}
        for listing in listings:
            sources[listing.source or "unknown"] = sources.get(listing.source or "unknown", 0) + 1
            cat = listing.category_main or "unknown"
            categories[cat] = categories.get(cat, 0) + 1
            typ = listing.category_type or "unknown"
            types[typ] = types.get(typ, 0) + 1

        per_listing_images = [len(self.images(listing.id)) for listing in listings]
        all_images = [img for listing in listings for img in self.images(listing.id)]
        n_images = len(all_images)
        with_phash = [img for img in all_images if img.phash is not None]
        unmeasured = sum(1 for img in with_phash if not img.pop_is_measured())
        measurable = bool(with_phash) and unmeasured < len(with_phash)
        depth: dict[str, int] = {}
        for listing in listings:
            bucket = str(len(listing.price_history))
            depth[bucket] = depth.get(bucket, 0) + 1

        def catalog_share(threshold: int) -> float | None:
            if with_phash and not measurable:
                return None
            if not n_images:
                return 0.0
            return sum(1 for img in all_images if img.is_catalog_candidate(threshold)) / n_images

        out.update({
            "active_share": share(lambda lst: lst.is_active),
            "sources": dict(sorted(sources.items(), key=lambda kv: (-kv[1], kv[0]))),
            "category_main": dict(sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))),
            "category_type": dict(sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))),
            "with_area": share(lambda lst: lst.area_m2 is not None),
            "with_disposition": share(lambda lst: lst.disposition is not None),
            "with_floor": share(lambda lst: lst.floor is not None),
            "with_broker_key": share(lambda lst: lst.broker_key is not None),
            "with_street_key": share(lambda lst: lst.location.street_key is not None),
            "with_ruian_adm_kod": share(lambda lst: lst.location.ruian_adm_kod is not None),
            "with_point": share(lambda lst: lst.location.has_point()),
            "n_images": n_images,
            "images_per_listing_mean": statistics.fmean(per_listing_images),
            "images_per_listing_median": float(statistics.median(per_listing_images)),
            "with_any_clip": share(
                lambda lst: any(img.clip for img in self.images(lst.id))
            ),
            "with_any_phash": share(
                lambda lst: any(img.phash is not None for img in self.images(lst.id))
            ),
            "catalog_pop_min": pop_min,
            "catalog_pop_unmeasured": unmeasured,
            "catalog_image_share": catalog_share(pop_min),
            "catalog_share_curve": {str(df): catalog_share(df) for df in CATALOG_DF_SWEEP},
            "price_history_depth": dict(sorted(depth.items(), key=lambda kv: int(kv[0]))),
        })
        return out


def _records(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def load(path: str | Path) -> Dataset:
    """Stream the gzipped JSONL cohort artifact into a `Dataset`."""
    meta = Meta()
    listings: dict[int, Listing] = {}
    images_by_listing: dict[int, list[Image]] = {}
    integrity = Integrity()
    seen_images: set[int] = set()
    for record in _records(Path(path)):
        kind = record.get("t")
        if kind == "listing":
            listing = Listing.from_json(record)
            if listing.id in listings:
                integrity.duplicate_listing_ids += 1
            listings[listing.id] = listing
        elif kind == "image":
            image = Image.from_json(record)
            if image.image_id in seen_images:
                integrity.duplicate_image_ids += 1
            seen_images.add(image.image_id)
            images_by_listing.setdefault(image.listing_id, []).append(image)
        elif kind == "meta":
            meta = Meta.from_json(record)
        else:
            name = str(kind)
            integrity.skipped_types[name] = integrity.skipped_types.get(name, 0) + 1
    for bucket in images_by_listing.values():
        bucket.sort(key=lambda img: (img.seq if img.seq is not None else 1 << 30, img.image_id))
    integrity.orphan_images = sum(
        len(bucket) for listing_id, bucket in images_by_listing.items() if listing_id not in listings
    )
    return Dataset(
        meta=meta,
        listings=listings,
        images_by_listing=images_by_listing,
        integrity=integrity,
    )


def norm(vector: Sequence[float]) -> float:
    return math.sqrt(math.sumprod(vector, vector))


def cosine_norm(
    a: Sequence[float], b: Sequence[float], norm_a: float, norm_b: float
) -> float:
    """Cosine with the two norms supplied — the hot path, since a gallery cross-product
    re-uses each vector many times and `Image.clip_norm()` already caches its norm."""
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return math.sumprod(a, b) / (norm_a * norm_b)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, pure stdlib; 0.0 when either side has zero norm."""
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} vs {len(b)}")
    return cosine_norm(a, b, norm(a), norm(b))


def hamming64(a: int, b: int) -> int:
    """Hamming distance over 64-bit dHashes stored as SIGNED bigints — mask, then popcount."""
    return ((a & MASK64) ^ (b & MASK64)).bit_count()
