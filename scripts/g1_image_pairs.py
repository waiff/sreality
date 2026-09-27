"""G1 image-stack bake-off: the offline pair-list builder (local, CPU, no DB, no R2).

Reads the cohort exports (per-image dHash, corpus pop, stored CLIP B/32 vector, CLIP top
tag, R2 storage_path) and the operator rulings, and writes the frozen evaluation set the
GPU pod scores:

  * advert pairs with a truth class:
      pos_rule   operator ruling `same` (newest ruling per pair; the export holds one row
                 per pair already), stratified by how many dHash-identical non-stock
                 frames the two galleries share (h0 = none, h1 = 1-3, h4 = 4+, K-C's bar);
      pos_merge  every pair inside an operator Browse merge group (status live);
      neg_rule   operator ruling `different`;
      neg_unit   operator ruling same_building_ / same_project_different_unit;
      neg_mnl    operator must-not-link;
      neg_fused  a pair inside a C7-read FUSED group (census C7 section 4.1) whose two
                 adverts state a distinguishing unit fact (area, disposition, floor), or
                 are one broker's co-live adverts on one portal (C7's two-units reading:
                 Plumlovska, Komenskeho), plus the A5 show-flat pair (sreality 18831201 x
                 18831387);
      neg_sib    presumed negatives at scale: one broker, one obec, one category, both
                 adverts state a unit fact that differs strongly (disposition, or area by
                 20 %+). `rnd` = uniform sample, `hard` = the ones whose best same-room
                 stored-CLIP cosine is >= 0.92 (the kitchen-to-kitchen trap);
  * the images each pair needs (image_id, listing_id, R2 key, dHash, pop, CLIP tag);
  * the same-room frame pairs to verify geometrically (routed by the CLIP top tag, at
    most `ROOM_CAP` frames per room per side, gallery order) — the pod adds the pairs its
    own descriptors route;
  * the CATALOGUE NEIGHBOURHOOD: for every advert of a labelled pair, up to `NB_SIBS`
    co-live adverts of the same broker, obec and category that STATE a distinguishing
    unit fact against it (two units by the standing test), with only their kitchen,
    bathroom, living-room and floor-plan frames. A frame of the pair that also matches a
    neighbour's frame is catalogue material, not unit evidence (C7's exclusivity guard,
    read with modern descriptors instead of dHash).

Nothing here decides a merge: it freezes the population a measurement reads.

Usage:
    python -m scripts.g1_image_pairs --out /path/to/g1 [--jobs 6]
"""

from __future__ import annotations

import argparse
import base64
import gzip
import itertools
import json
import os
import pickle
import random
import re
import struct
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

ART = "/home/hejtm/autodedup-artifacts/w14"
EXPORTS: dict[str, str] = {
    "trial": f"{ART}/s15/score_g15/autodedup-score-36244048665/artifact/cohort.jsonl.gz",
    "c17": f"{ART}/s15/cohort17_export_36221961445/autodedup-export-36221961445/cohort.jsonl.gz",
    "c18": f"{ART}/s15/cohort18_export_36237638871/autodedup-export-36237638871/cohort.jsonl.gz",
    "region": f"{ART}/town/region/autodedup-export-35622651878/cohort.jsonl.gz",
    **{f"cohort{n}": f"{ART}/cohort{n}/export/cohort.jsonl.gz" for n in range(3, 17)},
}
LABELS = f"{ART}/labels_g13_36225845749/autodedup-labels-36225845749"
C7_JUDGEMENTS = "/home/hejtm/autodedup-artifacts/w15/census/c7/judgements"

PHASH_TIGHT = 6          # settings.phash_tight
STOCK_POP = 8            # settings.catalog_df (E9: a frame on >= 8 listings is stock)
PHASH_SAMPLE = 30        # features.PHASH_SAMPLE
ROOM_CAP = 4             # frames per room per side for the routed geometric pairs
SIB_AREA_REL = 0.20
SIB_HARD_COS = 0.92
SIB_RANDOM_N = 1000
SIB_HARD_N = 1000
SIB_SCAN = 12000
PRICE_TOL = 0.005     # d43_price_path_tol
SEED = 20260927
CLIP_STRUCT = "<512e"
A5_SHOWFLAT = (18831201, 18831387)
FLOORPLAN_TAGS = {"floor_plan"}
ROOM_TAGS_IGNORED = {"other", "property_document", "site_plan"}
NB_SIBS = 8              # neighbours per anchor advert
NB_ROOMS = ("kitchen", "bathroom", "living_room", "floor_plan")
NB_PER_ROOM = 2          # frames per room per neighbour
NB_SKIP_CLASSES = {"neg_sib_rnd"}   # anchors: every advert of every other class

_LISTING_PREFIX = '{"t": "listing"'
_IMAGE_PREFIX = '{"t": "image"'
_LID_RE = re.compile(r'"listing_id": (\d+)')


@dataclass
class Advert:
    id: int
    export: str
    source: str
    category_main: str | None
    category_type: str | None
    disposition: str | None
    area_m2: float | None
    floor: int | None
    total_floors: int | None
    price: float | None
    broker_key: str | None
    obec: int | None
    street: str | None
    house_number: str | None
    first_seen: str | None
    last_seen: str | None
    is_active: bool | None
    prices: list[float] = field(default_factory=list)


@dataclass
class Img:
    image_id: int
    listing_id: int
    seq: int
    key: str | None
    phash: int | None
    pop: int | None
    tag: str | None
    tag_conf: float | None
    clip: str | None = field(repr=False, default=None)


def _advert(export: str, r: dict[str, Any]) -> Advert:
    loc = r.get("location") or {}
    return Advert(
        id=int(r["id"]), export=export, source=r.get("source") or "",
        category_main=r.get("category_main"), category_type=r.get("category_type"),
        disposition=r.get("disposition"), area_m2=r.get("area_m2"), floor=r.get("floor"),
        total_floors=r.get("total_floors"), price=r.get("price"),
        broker_key=r.get("broker_key"), obec=loc.get("obec_kod"),
        street=loc.get("street_key"), house_number=loc.get("house_number"),
        first_seen=r.get("first_seen_at"), last_seen=r.get("last_seen_at"),
        is_active=r.get("is_active"),
        prices=[float(p[1]) for p in (r.get("price_history") or []) if p and p[1]]
        or ([float(r["price"])] if r.get("price") else []),
    )


def scan_listings(task: tuple[str, str]) -> tuple[str, list[Advert]]:
    """Pass 1: every listing record of one export (image lines are skipped unparsed)."""
    export, path = task
    out: list[Advert] = []
    with gzip.open(path, "rt") as fh:
        for line in fh:
            if line.startswith(_LISTING_PREFIX):
                out.append(_advert(export, json.loads(line)))
    return export, out


def scan_images(task: tuple[str, str, frozenset[int], bool]) -> tuple[str, list[Img]]:
    """Pass 2: the image records of the wanted listings of one export."""
    export, path, wanted, keep_clip = task
    out: list[Img] = []
    with gzip.open(path, "rt") as fh:
        for line in fh:
            if not line.startswith(_IMAGE_PREFIX):
                continue
            m = _LID_RE.search(line)
            if not m or int(m.group(1)) not in wanted:
                continue
            r = json.loads(line)
            tags = r.get("tags") or []
            tag, conf = (tags[0][0], float(tags[0][1])) if tags else (None, None)
            out.append(Img(
                image_id=int(r["image_id"]), listing_id=int(r["listing_id"]),
                seq=int(r.get("seq") or 0), key=r.get("storage_path"),
                phash=r.get("phash"), pop=r.get("pop"), tag=tag, tag_conf=conf,
                clip=r.get("clip") if keep_clip else None,
            ))
    return export, out


def hamming64(a: int, b: int) -> int:
    return ((int(a) ^ int(b)) & 0xFFFFFFFFFFFFFFFF).bit_count()


def phash_gallery(images: list[Img], *, stock: bool = False) -> list[Img]:
    """The engine's gallery (E9: stock frames out, first 30); `stock=True` keeps every
    hashed frame, which is what "the two adverts share an identical frame" means."""
    gallery = [i for i in images if i.phash is not None
               and (stock or i.pop is None or i.pop < STOCK_POP)]
    return gallery if stock else gallery[:PHASH_SAMPLE]


def tight_matches(a: list[Img], b: list[Img]) -> list[tuple[int, int, int]]:
    """Greedy one-to-one, closest first (features._phash_matches), tight hits only."""
    cands = []
    for x in a:
        for y in b:
            d = hamming64(x.phash, y.phash)
            if d <= PHASH_TIGHT:
                cands.append((d, min(x.image_id, y.image_id), max(x.image_id, y.image_id),
                              x.image_id, y.image_id))
    cands.sort()
    used_a: set[int] = set()
    used_b: set[int] = set()
    hits = []
    for d, _, _, ia, ib in cands:
        if ia in used_a or ib in used_b:
            continue
        used_a.add(ia)
        used_b.add(ib)
        hits.append((ia, ib, d))
    return hits


def clip_vec(img: Img):
    import numpy as np
    if not img.clip:
        return None
    v = np.frombuffer(base64.b64decode(img.clip), dtype="<f2").astype("float32")
    n = float(np.linalg.norm(v))
    return v / n if n else None


def room_frames(images: list[Img]) -> dict[str, list[Img]]:
    by: dict[str, list[Img]] = defaultdict(list)
    for img in sorted(images, key=lambda i: (i.seq, i.image_id)):
        if img.tag and img.tag not in ROOM_TAGS_IGNORED:
            if len(by[img.tag]) < ROOM_CAP:
                by[img.tag].append(img)
    return by


def routed_frame_pairs(a: list[Img], b: list[Img]) -> list[tuple[int, int, str]]:
    ra, rb = room_frames(a), room_frames(b)
    out = []
    for tag in sorted(set(ra) & set(rb)):
        for x in ra[tag]:
            for y in rb[tag]:
                out.append((x.image_id, y.image_id, tag))
    return out


def best_same_room_cos(a: list[Img], b: list[Img], vecs: dict[int, Any]) -> float | None:
    ra, rb = room_frames(a), room_frames(b)
    best = None
    for tag in set(ra) & set(rb):
        if tag in FLOORPLAN_TAGS:
            continue
        for x in ra[tag]:
            vx = vecs.get(x.image_id)
            if vx is None:
                continue
            for y in rb[tag]:
                vy = vecs.get(y.image_id)
                if vy is None:
                    continue
                c = float(vx @ vy)
                best = c if best is None or c > best else best
    return best


def _rel(a: float | None, b: float | None) -> float | None:
    if not a or not b:
        return None
    return abs(a - b) / max(a, b)


def distinguishing(a: Advert, b: Advert) -> list[str]:
    """The stated unit facts that tell two adverts apart (the standing false-merge test)."""
    out = []
    if a.disposition and b.disposition and a.disposition != b.disposition:
        out.append("disposition")
    rel = _rel(a.area_m2, b.area_m2)
    if rel is not None and rel >= 0.05:
        out.append(f"area{rel:.2f}")
    if a.floor is not None and b.floor is not None and a.floor != b.floor \
            and a.floor < 20 and b.floor < 20:
        out.append("floor")
    return out


def stratum(n_tight: int, n_tight_all: int) -> str:
    """h0: no dHash-identical frame at all (the population the stack exists for);
    hs: identical frames exist but E9 counts every one as stock (re-post trains whose
    photos sit on 8+ listings); h1: 1-3 identical non-stock frames; h4: K-C's bar."""
    if n_tight_all == 0:
        return "h0"
    if n_tight == 0:
        return "hs"
    return "h1" if n_tight < 4 else "h4"


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def load_rulings() -> tuple[dict[tuple[int, int], str], list[dict[str, Any]], set[tuple[int, int]]]:
    verdicts: dict[tuple[int, int], tuple[str, str]] = {}
    for line in open(f"{LABELS}/operator_labels.jsonl"):
        r = json.loads(line)
        key = _pair(int(r["listing_lo"]), int(r["listing_hi"]))
        stamp = r.get("decided_at") or ""
        if key not in verdicts or stamp >= verdicts[key][1]:
            verdicts[key] = (r["verdict"], stamp)
    merges = [json.loads(line) for line in open(f"{LABELS}/operator_merges.jsonl")]
    mnl = {_pair(int(r["listing_lo"]), int(r["listing_hi"]))
           for r in map(json.loads, open(f"{LABELS}/must_not_link.jsonl"))}
    return {k: v for k, (v, _) in verdicts.items()}, merges, mnl


def load_c7_fused() -> list[tuple[str, list[int], str]]:
    out = []
    for cohort in ("trial", "c17", "c18"):
        path = f"{C7_JUDGEMENTS}/{cohort}.json"
        if not os.path.exists(path):
            continue
        for key, val in json.load(open(path)).items():
            if key.startswith("_"):
                continue
            verdict = val[0] if isinstance(val, list) else val
            if verdict == "fused":
                out.append((cohort, [int(x) for x in key.split(",")],
                            val[1] if isinstance(val, list) else ""))
    return out


def _colive(a: Advert, b: Advert) -> bool:
    if not (a.first_seen and b.first_seen):
        return False
    a_end = a.last_seen or "9999"
    b_end = b.last_seen or "9999"
    return a.first_seen <= b_end and b.first_seen <= a_end


def _price_agree(a: Advert, b: Advert, tol: float = PRICE_TOL) -> bool | None:
    if not a.prices or not b.prices:
        return None
    return any(abs(x - y) <= tol * max(x, y) for x in a.prices for y in b.prices)


def fused_negative(a: Advert, b: Advert) -> str | None:
    """Why two members of a C7-read fused group are two units, or None if the pair may
    be one unit seen twice (a cross-portal copy inside the fused group)."""
    facts = distinguishing(a, b)
    if facts:
        return "/".join(facts)
    if a.source == b.source and a.broker_key and a.broker_key == b.broker_key and _colive(a, b):
        return "colive-same-portal"
    return None


def neighbourhood(anchors: Iterable[int], adverts: dict[int, Advert],
                  cap: int = NB_SIBS) -> dict[int, list[list[Any]]]:
    """{anchor: [[neighbour id, "facts"], ...]}: co-live adverts of the anchor's broker,
    obec and category that state a distinguishing unit fact against it."""
    by_key: dict[tuple, list[Advert]] = defaultdict(list)
    for a in adverts.values():
        if a.broker_key and a.obec and a.category_main:
            by_key[(a.broker_key, a.obec, a.category_main, a.category_type)].append(a)
    out: dict[int, list[list[Any]]] = {}
    for aid in sorted(set(anchors)):
        a = adverts.get(aid)
        if a is None or not (a.broker_key and a.obec and a.category_main):
            continue
        group = by_key.get((a.broker_key, a.obec, a.category_main, a.category_type), [])
        sibs = [(c.id, "/".join(f)) for c in group
                if c.id != aid and _colive(a, c) and (f := distinguishing(a, c))]
        if not sibs:
            continue
        sibs.sort()
        random.Random(SEED + aid).shuffle(sibs)
        out[aid] = [[i, f] for i, f in sorted(sibs[:cap])]
    return out


def public_pairs_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """The copy committed to the (public) repo: C7's free-text case notes (addresses,
    prices, broker keys) are cut to `cohort:reason`; nothing the pod reads is lost."""
    def scrub(note: str) -> str:
        if note.startswith(("trial:", "c17:", "c18:", "a5:")):
            return ":".join(note.split(":")[:2])
        return note
    return {**doc, "pairs": [{**p, "notes": [scrub(n) for n in p.get("notes", [])]}
                             for p in doc["pairs"]]}


def load_all_adverts(out_dir: str, jobs: int) -> dict[int, Advert]:
    """Pass 1 over every local export; first export wins (trial, c17, c18 first)."""
    cache = os.path.join(out_dir, "cache_listings.pkl")
    if os.path.exists(cache):
        raw = pickle.load(open(cache, "rb"))
    else:
        raw = {}
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            for export, rows in ex.map(scan_listings, list(EXPORTS.items())):
                raw[export] = [asdict(a) for a in rows]
                print(f"pass1 {export}: {len(rows)} listings", flush=True)
        pickle.dump(raw, open(cache, "wb"))
    adverts: dict[int, Advert] = {}
    for export in EXPORTS:
        for row in raw.get(export, []):
            adverts.setdefault(int(row["id"]), Advert(**row))
    return adverts


def load_images(adverts: dict[int, Advert], wanted: set[int], jobs: int,
                paths: dict[str, str] | None = None) -> dict[int, list[Img]]:
    paths = paths or EXPORTS
    """Pass 2: the image records of the wanted listings, from the export each one lives in."""
    by_export: dict[str, set[int]] = defaultdict(set)
    for lid in wanted:
        if lid in adverts:
            by_export[adverts[lid].export].add(lid)
    images: dict[int, list[Img]] = defaultdict(list)
    tasks = [(e, paths[e], frozenset(ids), True) for e, ids in by_export.items()]
    with ProcessPoolExecutor(max_workers=max(1, min(jobs, len(tasks) or 1))) as ex:
        for export, rows in ex.map(scan_images, tasks):
            for img in rows:
                images[img.listing_id].append(img)
            print(f"pass2 {export}: {len(rows)} images", flush=True)
    for lst in images.values():
        lst.sort(key=lambda i: (i.seq, i.image_id))
    return images


def select_pairs(adverts: dict[int, Advert], jobs: int) -> list[dict[str, Any]]:
    """The frozen advert-pair population with its truth classes (ids only).

    Operator rulings, merges and must-not-links are taken WHOLE, whether or not a local
    export holds the adverts: the runner's by-id export fills the gaps. The C7 fused split
    and the sibling sample read the local exports' stated facts and CLIP vectors."""
    verdicts, merges, mnl = load_rulings()
    fused = load_c7_fused()
    pairs: dict[tuple[int, int], dict[str, Any]] = {}

    def add(a: int, b: int, cls: str, note: str = "") -> None:
        key = _pair(a, b)
        if key[0] == key[1]:
            return
        row = pairs.setdefault(key, {"a": key[0], "b": key[1], "classes": [], "notes": []})
        if cls not in row["classes"]:
            row["classes"].append(cls)
        if note and note not in row["notes"]:
            row["notes"].append(note)

    for (a, b), v in verdicts.items():
        if v == "same":
            add(a, b, "pos_rule")
        elif v == "different":
            add(a, b, "neg_rule")
        else:
            add(a, b, "neg_unit", v)
    for g in merges:
        if g.get("status") not in (None, "live"):
            continue
        ids = sorted({int(m["listing_id"]) for m in g.get("members") or []})
        for a, b in itertools.combinations(ids, 2):
            add(a, b, "pos_merge", str(g.get("merge_group_id")))
    for a, b in mnl:
        add(a, b, "neg_mnl")
    for cohort, ids, note in fused:
        for a, b in itertools.combinations(sorted(set(ids)), 2):
            aa, bb = adverts.get(a), adverts.get(b)
            if aa is None or bb is None:
                continue
            why = fused_negative(aa, bb)
            if why:
                add(a, b, "neg_fused", f"{cohort}:{why}:{note[:60]}")
    add(*A5_SHOWFLAT, "neg_fused", "a5:showflat floors 2 vs 5")

    by_broker: dict[tuple, list[Advert]] = defaultdict(list)
    for a in adverts.values():
        if a.broker_key and a.obec and a.category_main:
            by_broker[(a.broker_key, a.obec, a.category_main, a.category_type)].append(a)
    ruled = set(pairs)
    sib_pool: list[tuple[int, int, str]] = []
    for group in by_broker.values():
        if len(group) < 2 or len(group) > 60:
            continue
        for x, y in itertools.combinations(group, 2):
            key = _pair(x.id, y.id)
            if key in ruled:
                continue
            why = None
            if x.category_main == "byt" and x.disposition and y.disposition \
                    and x.disposition != y.disposition:
                why = f"disposition {x.disposition}/{y.disposition}"
            else:
                rel = _rel(x.area_m2, y.area_m2)
                if rel is not None and rel >= SIB_AREA_REL and min(x.area_m2, y.area_m2) >= 20:
                    why = f"area {x.area_m2:g}/{y.area_m2:g}"
            if why:
                sib_pool.append((key[0], key[1], why + (" colive" if _colive(x, y) else "")))
    sib_pool.sort()
    rng = random.Random(SEED)
    rng.shuffle(sib_pool)
    sib_scan = sib_pool[:SIB_SCAN]
    images = load_images(adverts, {i for a, b, _ in sib_scan for i in (a, b)}, jobs)
    vecs = {img.image_id: v for lst in images.values() for img in lst
            if (v := clip_vec(img)) is not None}
    rows = []
    for a, b, why in sib_scan:
        if images.get(a) and images.get(b):
            rows.append((a, b, why, best_same_room_cos(images[a], images[b], vecs)))
    hard = [r for r in rows if r[3] is not None and r[3] >= SIB_HARD_COS][:SIB_HARD_N]
    hard_keys = {(r[0], r[1]) for r in hard}
    rnd = [r for r in rows if (r[0], r[1]) not in hard_keys][:SIB_RANDOM_N]
    for a, b, why, cos in hard:
        add(a, b, "neg_sib_hard", f"{why} cos={cos:.3f}")
    for a, b, why, cos in rnd:
        add(a, b, "neg_sib_rnd", f"{why} cos={'' if cos is None else f'{cos:.3f}'}")
    print(f"select: {len(pairs)} pairs; sibling pool {len(sib_pool)}, scanned {len(rows)}, "
          f"hard {len(hard)}, random {len(rnd)}", flush=True)
    return [pairs[k] for k in sorted(pairs)]


def neighbour_frames(images: list[Img]) -> list[Img]:
    """A neighbour contributes only the rooms a catalogue can repeat, NB_PER_ROOM each."""
    taken: Counter = Counter()
    out = []
    for img in sorted(images, key=lambda i: (i.seq, i.image_id)):
        if img.tag in NB_ROOMS and taken[img.tag] < NB_PER_ROOM:
            taken[img.tag] += 1
            out.append(img)
    return out


def assemble(pairs: list[dict[str, Any]], adverts: dict[int, Advert],
             images: dict[int, list[Img]],
             neighbours: dict[Any, list[list[Any]]] | None = None,
             ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Per-pair frame facts, the routed frame pairs, the catalogue neighbourhood and the
    image list: the pod's manifest.

    Pure over (pairs, adverts, images, neighbours), so the local build and the runner's
    by-id build are one code path."""
    out_pairs = []
    frame_pairs: set[tuple[int, int, str]] = set()
    need: set[int] = set()
    missing = Counter()
    for row in pairs:
        a, b = int(row["a"]), int(row["b"])
        ia, ib = images.get(a, []), images.get(b, [])
        if not ia or not ib or a not in adverts or b not in adverts:
            for cls in row["classes"]:
                missing[cls] += 1
            continue
        hits = tight_matches(phash_gallery(ia), phash_gallery(ib))
        n_tight = len(hits)
        hits_all = tight_matches(phash_gallery(ia, stock=True), phash_gallery(ib, stock=True))
        routed = routed_frame_pairs(ia, ib)
        frame_pairs.update(routed)
        aa, bb = adverts[a], adverts[b]
        out_pairs.append({
            "a": a, "b": b, "classes": row["classes"], "notes": row["notes"][:3],
            "export_a": aa.export, "export_b": bb.export, "src_a": aa.source, "src_b": bb.source,
            "cat_a": f"{aa.category_main}/{aa.category_type}",
            "cat_b": f"{bb.category_main}/{bb.category_type}",
            "n_img_a": len(ia), "n_img_b": len(ib), "n_tight": n_tight,
            "n_tight_all": len(hits_all),
            "stock_share_a": round(sum(1 for i in ia if (i.pop or 0) >= STOCK_POP) / len(ia), 3),
            "stock_share_b": round(sum(1 for i in ib if (i.pop or 0) >= STOCK_POP) / len(ib), 3),
            "stratum": stratum(n_tight, len(hits_all)),
            "tight": [[x, y, d] for x, y, d in hits_all], "n_routed": len(routed),
            "facts": distinguishing(aa, bb), "colive": _colive(aa, bb),
            "same_source": aa.source == bb.source, "price_agree": _price_agree(aa, bb),
        })
        need.update(i.image_id for i in ia)
        need.update(i.image_id for i in ib)
    anchors = {i for p in out_pairs for i in (p["a"], p["b"])}
    nb_out: dict[str, list[list[Any]]] = {}
    nb_only: set[int] = set()
    for key, sibs in (neighbours or {}).items():
        anchor = int(key)
        if anchor not in anchors:
            continue
        rows = []
        for sib, facts in sibs:
            frames = neighbour_frames(images.get(int(sib), []))
            if frames:
                rows.append([int(sib), facts, [f.image_id for f in frames]])
                nb_only.update(f.image_id for f in frames if f.image_id not in need)
        if rows:
            nb_out[str(anchor)] = rows
    image_rows = sorted(
        ({"image_id": img.image_id, "listing_id": img.listing_id, "seq": img.seq, "key": img.key,
          "phash": img.phash, "pop": img.pop, "tag": img.tag, "tag_conf": img.tag_conf,
          "nb": img.image_id in nb_only}
         for lst in images.values() for img in lst
         if img.image_id in need or img.image_id in nb_only),
        key=lambda r: r["image_id"])
    manifest = {
        "format": 3,
        "built_by": "scripts/g1_image_pairs.py",
        "params": {"phash_tight": PHASH_TIGHT, "stock_pop": STOCK_POP, "phash_sample": PHASH_SAMPLE,
                   "room_cap": ROOM_CAP, "sib_area_rel": SIB_AREA_REL,
                   "sib_hard_cos": SIB_HARD_COS, "seed": SEED, "price_tol": PRICE_TOL,
                   "nb_sibs": NB_SIBS, "nb_rooms": list(NB_ROOMS), "nb_per_room": NB_PER_ROOM},
        "pairs": out_pairs,
        "images": image_rows,
        "frame_pairs": [[x, y, t] for x, y, t in sorted(frame_pairs)],
        "neighbours": nb_out,
    }
    counts = summarize(out_pairs, [r for r in image_rows if not r["nb"]], frame_pairs, missing)
    counts["nb_anchors"] = len(nb_out)
    counts["nb_adverts"] = len({s for rows in nb_out.values() for s, _, _ in rows})
    counts["nb_images"] = len(nb_only)
    counts["nb_images_by_tag"] = dict(Counter(r["tag"] for r in image_rows if r["nb"]).most_common())
    counts["images_total_manifest"] = len(image_rows)
    return manifest, counts


def write_clip(path: str, images: dict[int, list[Img]], need: set[int]) -> int:
    import numpy as np
    ids, vecs = [], []
    for lst in images.values():
        for img in lst:
            if img.image_id in need and (v := clip_vec(img)) is not None:
                ids.append(img.image_id)
                vecs.append(v)
    mat = np.stack(vecs).astype(np.float16) if vecs else np.zeros((0, 512), np.float16)
    np.savez_compressed(path, image_id=np.array(ids, dtype=np.int64), vec=mat)
    return len(ids)


def summarize(out_pairs, image_rows, frame_pairs, missing) -> dict[str, Any]:
    by_cls: Counter = Counter()
    by_cls_stratum: Counter = Counter()
    adverts_by_cls: dict[str, set[int]] = defaultdict(set)
    for p in out_pairs:
        for cls in p["classes"]:
            by_cls[cls] += 1
            by_cls_stratum[f"{cls}:{p['stratum']}"] += 1
            adverts_by_cls[cls].update((p["a"], p["b"]))
    return {
        "pairs_total": len(out_pairs),
        "pairs_by_class": dict(by_cls),
        "pairs_by_class_stratum": dict(sorted(by_cls_stratum.items())),
        "adverts_by_class": {k: len(v) for k, v in adverts_by_cls.items()},
        "adverts_total": len({i for p in out_pairs for i in (p["a"], p["b"])}),
        "pairs_missing_images_by_class": dict(missing),
        "pairs_by_export_of_a": dict(Counter(p["export_a"] for p in out_pairs)),
        "images_needed": len(image_rows),
        "images_with_r2_key": sum(1 for r in image_rows if r["key"]),
        "images_by_clip_tag": dict(Counter(r["tag"] for r in image_rows).most_common()),
        "routed_frame_pairs": len(frame_pairs),
        "routed_frame_pairs_by_tag": dict(Counter(t for _, _, t in frame_pairs).most_common()),
    }


def write_manifest(path: str, manifest: dict[str, Any]) -> None:
    with gzip.open(path, "wt") as fh:
        json.dump(manifest, fh, separators=(",", ":"))


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="Output directory.")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--pairs", default="",
                    help="Commit target for the frozen pair list (pairs.json.gz).")
    args = ap.parse_args(list(argv) if argv is not None else None)
    os.makedirs(args.out, exist_ok=True)
    adverts = load_all_adverts(args.out, args.jobs)
    pairs = select_pairs(adverts, args.jobs)
    anchors = {i for p in pairs if set(p["classes"]) - NB_SKIP_CLASSES for i in (p["a"], p["b"])}
    neighbours = neighbourhood(anchors, adverts)
    pair_ids = {i for p in pairs for i in (p["a"], p["b"])}
    nb_ids = {int(s) for rows in neighbours.values() for s, _ in rows}
    pairs_doc = {"format": 2, "built_by": "scripts/g1_image_pairs.py", "seed": SEED,
                 "pairs": pairs,
                 "neighbours": {str(k): v for k, v in neighbours.items()},
                 "listing_ids": sorted(pair_ids | nb_ids)}
    with gzip.open(os.path.join(args.out, "pairs.json.gz"), "wt") as fh:
        json.dump(pairs_doc, fh, separators=(",", ":"))
    if args.pairs:
        with gzip.open(args.pairs, "wt") as fh:
            json.dump(public_pairs_doc(pairs_doc), fh, separators=(",", ":"))
    images = load_images(adverts, set(pairs_doc["listing_ids"]), args.jobs)
    manifest, counts = assemble(pairs, adverts, images, pairs_doc["neighbours"])
    write_manifest(os.path.join(args.out, "manifest_local.json.gz"), manifest)
    need = {r["image_id"] for r in manifest["images"]}
    counts["clip_vectors_written"] = write_clip(
        os.path.join(args.out, "clip_b32_stored.npz"), images, need)
    counts["pairs_selected"] = len(pairs)
    counts["pair_listing_ids"] = len(pair_ids)
    counts["nb_listing_ids_selected"] = len(nb_ids - pair_ids)
    counts["listing_ids_selected"] = len(pairs_doc["listing_ids"])
    counts["listing_ids_in_local_exports"] = sum(1 for i in pairs_doc["listing_ids"] if i in adverts)
    json.dump(counts, open(os.path.join(args.out, "counts_local.json"), "w"), indent=1)
    print(json.dumps(counts, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
