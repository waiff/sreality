"""What a listing's facts ARE, as three digests — the permanent parity gate's unit (E91).

`rt_parity` compares the live lane's facts with the export artifact's field by field, and that
is the instrument. This is the GATE: the same comparison compressed to three short digests per
listing, small enough that the seed can write a baseline for a whole sample into one
`autodedup.settings` row and every scheduled pass can re-check a slice of it in one fact read.

Three digests and not one, because the three move for three different reasons:

* `stored` — the listing's own columns, its location, its attrs, its price history and its
  gallery's identity and ORDER. Under rule #2 these move only when the row's content hash
  moves, so a difference on a listing with no newer snapshot is a LANE defect and refuses.
* `sighting` — `last_seen_at`, `is_active` and `inactive_at`. These move on the SCRAPER's clock
  and append no snapshot at all: rule #4 bumps `last_seen_at` on every index sighting and rule
  #3 flips `is_active` without one. Measured on the live cohort, 78 of 200 sampled listings had
  a newer `last_seen_at` and 74 of those carried no newer snapshot — a gate that read them as
  stored facts would refuse every re-seed. Reported, never refused; they are the corpus moving
  under a frozen calibration, exactly like the producers.
* `producer` — `phash`, the CLIP vector, the CLIP tags. The pHash and CLIP jobs re-run over the
  corpus on their own cadence, so these move under a frozen calibration without any listing
  changing. Reported, never refused.
* `population` — the frozen corpus-wide pHash population E9 subtracts catalogue photos with.
  It is frozen WITH the calibration (E70), so a difference is a defect — unless this image's
  `phash` itself moved, in which case the population of a hash the calibration never saw is
  honestly unknown and the producer explains it.

W9f shipped with the population table EMPTY and no writer: every image read `pop = 0`, every
`catalog_ratio` went absent, and K-C — the certificate that rests on exact photo matches — was
structurally unreachable for a whole live pass. That is what this gate exists to make loud.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, fields as dataclass_fields
from typing import Any, Mapping, Sequence

from autodedup.dataset import Image, Listing

# A listing's `block` is the COHORT's draw label — the lane deliberately has none (a real-time
# arrival belongs to no draw) and it reaches no feature; `location` is spelled out field by
# field below instead of compared as an object.
_LISTING_SKIP: frozenset[str] = frozenset({"location", "block"})


def listing_view(listing: Listing) -> dict[str, Any]:
    """One flat `field -> value` map per listing: the record's own fields, then `location.*`,
    then the two collection shapes spelled out so a diff can name WHAT moved rather than print
    two blobs."""
    view: dict[str, Any] = {
        field.name: getattr(listing, field.name)
        for field in dataclass_fields(listing)
        if field.name not in _LISTING_SKIP
    }
    view["attrs"] = dict(listing.attrs or {})
    view["attrs_keys"] = sorted(view["attrs"])
    view["price_history"] = [list(entry) for entry in (listing.price_history or [])]
    view["price_history_len"] = len(listing.price_history or [])
    for field in dataclass_fields(listing.location):
        view[f"location.{field.name}"] = getattr(listing.location, field.name)
    return view


def image_view(image: Image) -> dict[str, Any]:
    view: dict[str, Any] = {
        name: getattr(image, name)
        for name in ("seq", "storage_path", "phash", "pop", "clip")
    }
    view["tags"] = [list(tag) for tag in (image.tags or [])]
    view["tags_len"] = len(image.tags or [])
    view["clip_present"] = image.clip is not None
    return view


# What a portal SIGHTING moves without appending a snapshot (rules #3 and #4). They are facts
# and they do reach features (`both_active`, the live-window clock), but no lane can read them
# the same way an artifact cut last week did.
SIGHTING_FIELDS: tuple[str, ...] = ("last_seen_at", "is_active", "inactive_at")


def _digest(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def stored_digest(listing: Listing, images: Sequence[Image]) -> str:
    """Everything a portal wrote that only a CONTENT change moves: the listing's columns (minus
    the sighting clock), its location, attrs and price path, and its gallery's identity and
    order."""
    view = {name: value for name, value in listing_view(listing).items()
            if name not in SIGHTING_FIELDS}
    return _digest([
        view,
        [[img.image_id, img.seq, img.storage_path] for img in images],
    ])


def sighting_digest(listing: Listing) -> str:
    """The scraper's clock, which moves with no snapshot behind it."""
    view = listing_view(listing)
    return _digest([view.get(name) for name in SIGHTING_FIELDS])


def producer_digest(images: Sequence[Image]) -> str:
    """What the pHash and CLIP jobs derived — free to move under a frozen calibration."""
    return _digest([[img.image_id, img.phash, _digest(img.clip) if img.clip else None,
                     [list(tag) for tag in (img.tags or [])]] for img in images])


def population_digest(images: Sequence[Image]) -> str:
    """The frozen corpus-wide population, per image, keyed on the hash it was counted for."""
    return _digest([[img.image_id, img.phash, img.pop] for img in images])


def baseline_row(listing: Listing, images: Sequence[Image]) -> dict[str, Any]:
    return {"f": stored_digest(listing, images), "s": sighting_digest(listing),
            "p": producer_digest(images), "c": population_digest(images), "n": len(images)}


def baseline(listings: Mapping[int, Listing],
             images: Mapping[int, Sequence[Image]]) -> dict[str, dict[str, Any]]:
    """The artifact side of the gate, keyed by listing id AS TEXT — it travels as JSON."""
    return {str(i): baseline_row(listings[i], list(images.get(i, ())))
            for i in sorted(listings)}


def stratified_sample(
    listings: Mapping[int, Listing], present: set[int], n: int, seed: int
) -> list[int]:
    """`n` ids present on BOTH sides, drawn proportionally per source so a portal that is 3% of
    the cohort is not absent from a 200-row sample by luck. Deterministic in `seed`."""
    by_source: dict[str, list[int]] = {}
    for listing_id in sorted(present):
        listing = listings.get(listing_id)
        if listing is None:
            continue
        by_source.setdefault(listing.source or "(none)", []).append(listing_id)
    total = sum(len(ids) for ids in by_source.values())
    if total <= n:
        return sorted(present & set(listings))
    rng = random.Random(seed)
    picked: list[int] = []
    for source in sorted(by_source):
        ids = by_source[source]
        # At least one of every portal, then that portal's share of the rest.
        share = max(1, round(n * len(ids) / total))
        picked.extend(rng.sample(ids, min(share, len(ids))))
    if len(picked) > n:
        picked = rng.sample(picked, n)
    return sorted(set(picked))


@dataclass(slots=True, frozen=True)
class Breach:
    listing_id: int
    kind: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"listing_id": self.listing_id, "kind": self.kind, "detail": self.detail}


MAX_BREACH_EXAMPLES: int = 10


def compare(
    rows: Mapping[str, Mapping[str, Any]],
    listings: Mapping[int, Listing],
    images: Mapping[int, Sequence[Image]],
    drifted: set[int] | frozenset[int] = frozenset(),
) -> dict[str, Any]:
    """The live side against the baseline. A listing whose CONTENT changed since the export is
    genuine drift and is counted, never judged."""
    breaches: list[Breach] = []
    checked = producer_moved = drifted_skipped = absent = sighting_moved = 0
    for key, row in sorted(rows.items(), key=lambda kv: int(kv[0])):
        listing_id = int(key)
        listing = listings.get(listing_id)
        if listing is None:
            absent += 1
            continue
        gallery = list(images.get(listing_id, ()))
        if listing_id in drifted:
            drifted_skipped += 1
            continue
        checked += 1
        live = baseline_row(listing, gallery)
        if live["s"] != row.get("s"):
            sighting_moved += 1
        if live["f"] != row.get("f"):
            breaches.append(Breach(listing_id, "stored",
                                   f"{row.get('f')} != {live['f']} "
                                   f"(images {row.get('n')} -> {live['n']})"))
        if live["p"] != row.get("p"):
            producer_moved += 1
            continue  # a moved hash or vector explains any population difference under it
        if live["c"] != row.get("c"):
            breaches.append(Breach(listing_id, "population",
                                   f"{row.get('c')} != {live['c']} on {live['n']} images with "
                                   "an unmoved producer digest"))
    return {
        "listings": len(rows),
        "checked": checked,
        "drifted_skipped": drifted_skipped,
        "absent": absent,
        "producer_moved": producer_moved,
        "sighting_moved": sighting_moved,
        "breaches": len(breaches),
        "breaches_by_kind": _by_kind(breaches),
        "breach_examples": [breach.to_json() for breach in breaches[:MAX_BREACH_EXAMPLES]],
    }


def _by_kind(breaches: Sequence[Breach]) -> dict[str, int]:
    out: dict[str, int] = {}
    for breach in breaches:
        out[breach.kind] = out.get(breach.kind, 0) + 1
    return dict(sorted(out.items()))


def refusal(report: Mapping[str, Any], *, generation: str, tolerance: int,
            what: str) -> str | None:
    """The sentence a lane stops on, or None. Loud on purpose: it names the generation, the
    tolerance it broke and the one recipe that fixes it."""
    if int(report.get("breaches") or 0) <= tolerance:
        return None
    examples = ", ".join(
        f"{row['listing_id']}:{row['kind']}" for row in report.get("breach_examples", ())[:5])
    return (
        f"PARITY GATE: {what} — {report['breaches']} of {report['checked']} sampled listings "
        f"in generation {generation!r} do not carry the facts the export cut the calibration "
        f"from (tolerance {tolerance}; by kind {report.get('breaches_by_kind')}; {examples}). "
        "The live lane must read the export's facts or its decisions are not the batch "
        "engine's (E91). Nothing was written and no cursor moved. Re-run `--mode rt_parity` "
        "for the field-by-field report, then re-seed the generation "
        "(`-f mode=rt_seed -f args=...,reseed=true`) once the facts agree."
    )
