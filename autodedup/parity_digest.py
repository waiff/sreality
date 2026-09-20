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
import math
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


def image_population_rows(images: Sequence[Image]) -> list[list[Any]]:
    """The population PER IMAGE, keyed on the hash it was counted for (E94).

    W9g's gate compared one digest per listing and skipped the whole listing's population
    check as soon as ANY image's producer digest had moved. One re-hashed frame therefore
    excused the other fourteen, and a population table decaying image by image ran green —
    the W9f defect in slow motion. The excuse is per IMAGE now, which needs the baseline to
    carry the pair it is excusing: 120 listings x ~15 images is ~54 KB of JSON in the one
    settings row the seed already writes."""
    return [[img.image_id, img.phash, img.pop] for img in images]


def baseline_row(listing: Listing, images: Sequence[Image]) -> dict[str, Any]:
    return {"f": stored_digest(listing, images), "s": sighting_digest(listing),
            "p": producer_digest(images), "c": population_digest(images), "n": len(images),
            "im": image_population_rows(images)}


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
    images_checked = images_excused = legacy_rows = 0
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
        # The population, image by image (E94). A frame whose own hash moved is excused —
        # the population of a hash the calibration never saw is honestly unknown — and every
        # OTHER frame of that listing is still checked.
        want = {int(entry[0]): (entry[1], entry[2]) for entry in (row.get("im") or ())}
        if not want:
            if "im" in row:
                # A new-shape row with an EMPTY gallery: there is no population to compare,
                # which is not the same thing as a baseline that cannot be compared. Counting
                # it as legacy would let a sample of image-less listings trip E94's vacuity
                # rail for the one reason that is not vacuity.
                continue
            legacy_rows += 1
            if live["c"] != row.get("c") and live["p"] == row.get("p"):
                breaches.append(Breach(listing_id, "population",
                                       f"{row.get('c')} != {live['c']} on {live['n']} images "
                                       "with an unmoved producer digest"))
            continue
        moved: list[Any] = []
        for image in gallery:
            entry = want.get(int(image.image_id))
            if entry is None:
                continue  # an image the export never carried: no baseline to breach
            if entry[0] != image.phash:
                images_excused += 1
                continue
            images_checked += 1
            if entry[1] != image.pop:
                moved.append(image.image_id)
        if moved:
            breaches.append(Breach(
                listing_id, "population",
                f"{len(moved)} of {images_checked + images_excused} images changed population "
                f"under an UNMOVED phash (first {moved[0]})"))
    return {
        "listings": len(rows),
        "checked": checked,
        "drifted_skipped": drifted_skipped,
        "absent": absent,
        "producer_moved": producer_moved,
        "sighting_moved": sighting_moved,
        "population_images_checked": images_checked,
        "population_images_excused": images_excused,
        "legacy_baseline_rows": legacy_rows,
        "breaches": len(breaches),
        "breaches_by_kind": _by_kind(breaches),
        "breach_examples": [breach.to_json() for breach in breaches[:MAX_BREACH_EXAMPLES]],
    }


def _by_kind(breaches: Sequence[Breach]) -> dict[str, int]:
    out: dict[str, int] = {}
    for breach in breaches:
        out[breach.kind] = out.get(breach.kind, 0) + 1
    return dict(sorted(out.items()))


@dataclass(slots=True, frozen=True)
class Floors:
    """What the gate needs to have SEEN before a pass may read it as a pass (E94).

    W9g's gate counted breaches and nothing else, so it passed vacuously in two ways that were
    both demonstrated: give every baseline listing a newer snapshot and every stored fact can
    then be broken with `checked` 0, `breaches` 0, `ok` true; delete every baseline listing and
    `absent` 120 passes just as quietly. Drift is monotone in export age — 4 of 200 at three
    days — so the gate self-weakened as the export aged, which is exactly backwards. Four
    floors, all data, and below any of them the gate REFUSES instead of passing."""

    tolerance: int = 0
    # An absolute floor on what a pass must have compared. At the shipped 25-listing slice, 15
    # means the gate refuses once 40% of the slice is drift or absence — against a measured 2%
    # of drift at three days of export age, so this fires long before vacuity does.
    min_checked: int = 15
    # And a floor as a SHARE, because the absolute one alone is defeated from both ends: a
    # baseline smaller than 15 could never meet it, and a 120-listing seed sample could meet
    # it with 105 of its listings drifted or absent. Whichever floor is higher governs.
    min_checked_share: float = 0.6
    # The EXPORT's age, not the seed's: what is frozen is the cohort's statistics, and they go
    # on ageing however recently the generation was cut from them.
    max_age_days: float = 14.0
    # The share of phash-bearing images whose hash the frozen population cannot measure.
    # Measured 4.0% (115 of 2,847) at three days; a ceiling of 15% is roughly where that rate
    # reaches the age rail, and it is a number to re-cut once a second export gives two points.
    max_unknown_pop_share: float = 0.15

    def required(self, offered: int) -> int:
        """How many of the `offered` baseline listings this gate must have COMPARED.

        Zero on both knobs switches the rail off — an operator settings row, never a dispatch
        argument (W9e/R6) — and is how a fixture whose `public` holds no cohort at all admits
        that it is not testing the gate. Everywhere else an EMPTY baseline needs at least one
        comparison, which is the vacuity W9g's gate could not see."""
        if self.min_checked <= 0 and self.min_checked_share <= 0:
            return 0
        by_share = math.ceil(self.min_checked_share * max(0, offered))
        if offered < self.min_checked:
            return max(1, by_share)
        return max(self.min_checked, by_share)


def verdict(report: Mapping[str, Any], *, generation: str, floors: Floors,
            what: str) -> str | None:
    """The gate's whole verdict: the breach count AND the four floors, in one sentence or None."""
    breached = refusal(report, generation=generation, tolerance=floors.tolerance, what=what)
    if breached:
        return breached
    checked = int(report.get("checked") or 0)
    offered = int(report.get("listings") or 0)
    required = floors.required(offered)
    if checked < required:
        return (
            f"PARITY GATE: {what} — the gate checked {checked} of "
            f"{offered} baseline listings in generation {generation!r}, below "
            f"the floor of {required} (rt_parity_min_checked {floors.min_checked}): "
            f"{report.get('drifted_skipped')} drifted since the export, "
            f"{report.get('absent')} are absent live. A gate that checks nothing cannot pass "
            "(E94) — re-seed the generation from a fresh export. Nothing was written and no "
            "cursor moved.")
    age = report.get("age_days")
    if age is not None and float(age) > floors.max_age_days:
        return (
            f"PARITY GATE: {what} — generation {generation!r} was cut from an export "
            f"{float(age):.1f} days old, over the {floors.max_age_days} day "
            f"rt_calibration_max_age_days rail. A frozen population can only UNDERCOUNT, and "
            "an undercount is the anti-conservative direction: a frame that spread since the "
            "export is still read as rare, so E9 does not subtract it. Re-export and re-seed "
            "(E95). Nothing was written and no cursor moved.")
    share = report.get("unknown_pop_share")
    if share is not None and float(share) > floors.max_unknown_pop_share:
        return (
            f"PARITY GATE: {what} — {float(share):.1%} of the phash-bearing images this pass "
            f"read carry a hash generation {generation!r}'s frozen population has never "
            f"measured, over the {floors.max_unknown_pop_share:.0%} ceiling "
            "(rt_parity_max_unknown_pop_share). Those images cannot be subtracted as catalogue "
            "photos and cannot certify K-C (E94/E96). Re-export and re-seed. Nothing was "
            "written and no cursor moved.")
    excused = int(report.get("population_images_excused") or 0)
    legacy = int(report.get("legacy_baseline_rows") or 0)
    # A sample of image-less listings is not a vacuous gate; a sample whose every image was
    # excused, or whose baseline predates the per-image map, is.
    if (checked and int(report.get("population_images_checked") or 0) <= 0
            and (excused or legacy)):
        return (
            f"PARITY GATE: {what} — not one image of the {checked} checked listings in "
            f"generation {generation!r} had its population compared "
            f"({report.get('population_images_excused')} excused as re-hashed, "
            f"{report.get('legacy_baseline_rows')} baseline rows carry no per-image map). "
            "A producer move excuses ONE image, never a listing and never a sample (E94). "
            "Re-seed the generation. Nothing was written and no cursor moved.")
    return None


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
