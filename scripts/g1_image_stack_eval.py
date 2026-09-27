"""G1 image-stack bake-off: the offline evaluator (local, CPU, numpy only).

Reads the manifest (pairs with truth classes, images with dHash / pop / CLIP tag), any
descriptor arms available (the stored CLIP B/32 vectors from the export, and the pod's
SSCD / DINOv2 / DINOv3 vectors, head scores and LightGlue counts) and writes one JSON of
metrics plus a short markdown table. The same code reads the baseline today and the pod's
results tomorrow, so the bars are measured on one instrument.

Truth: pos = pos_rule or pos_merge; the negative classes are read separately (neg_rule,
neg_unit, neg_mnl, neg_fused, neg_sib_hard, neg_sib_rnd). An operator positive beats a C7
or sibling negative; an operator positive against an operator negative is a conflict and
is dropped (counted).

PARTIAL RUNS (2026-09-27): an arm is read only when its own file exists — the pod writes it
once every item was tried; a crash or a deadline leaves `<arm>.shards/` instead. The report
lists `arms_finished` and `arms_unfinished`, every bar names the finished arms it read (or
says it had none), and while any expected arm is unfinished the decision is PARTIAL RUN,
never STOP or ADOPT. `--include-partial` also reads unfinished LightGlue shards, as a
readout only.

Usage:
    python -m scripts.g1_image_stack_eval --manifest manifest.json.gz \\
        --clip clip_b32_stored.npz [--results g1_results/] --out eval.json
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Callable, Iterable

import numpy as np

POS = ("pos_rule", "pos_merge")
NEG = ("neg_rule", "neg_unit", "neg_mnl", "neg_fused", "neg_sib_hard", "neg_sib_rnd")
HARD = ("neg_rule", "neg_unit", "neg_mnl", "neg_fused", "neg_sib_hard")
# The negatives no model chose: operator rulings, must-not-links and C7's hand-read fused
# groups. neg_sib_hard was DRAWN by stored-CLIP cosine >= 0.92, so it is adversarial to
# CLIP by construction and unfair as the yardstick of any arm against CLIP.
LABELLED = ("neg_rule", "neg_unit", "neg_mnl", "neg_fused")
PRIVATE = ("kitchen", "bathroom", "living_room", "bedroom", "toilet")
ROOMS = ("kitchen", "bathroom", "living_room", "bedroom", "toilet", "hallway",
         "exterior_facade", "floor_plan", "balcony_terrace", "garden")
STOCK_POP = 8
ROOM_CAP = 4
UNIT_ROOMS = ("kitchen", "bathroom", "living_room", "floor_plan")
FMR_POINTS = (0.01, 0.05)
LG_VERIFIED = (15, 30, 60)       # F-inlier bars read at pair grain
# v1 head tag ids -> the engine's room names (C6 section 2.5; ids per PROGRAM.md:764).
HEAD_ROOM = {25: "kitchen", 22: "bathroom", 28: "living_room", 46: "floor_plan",
             39: "plan_3d", 3: "exterior_facade", 17: "garage", 48: "technical",
             42: "site_plan", 43: "site_plan", 45: "property_document"}


OPERATOR_NEG = ("neg_rule", "neg_unit", "neg_mnl")


def truth(classes: Iterable[str]) -> str | None:
    """An operator positive overrides C7's reading and the sibling rule (operator rulings
    override); only operator against operator is a conflict, and it is dropped."""
    cs = set(classes)
    pos = bool(cs & set(POS))
    neg = bool(cs & set(NEG))
    if pos and neg:
        return "conflict" if cs & set(OPERATOR_NEG) else "pos"
    if pos:
        return "pos"
    if neg:
        for c in NEG:  # the hardest class it carries names it
            if c in cs:
                return c
    return None


def auc(pos: np.ndarray, neg: np.ndarray) -> float | None:
    """Mann-Whitney AUC; NaN scores rank lowest (no evidence is never evidence)."""
    if len(pos) == 0 or len(neg) == 0:
        return None
    p = np.where(np.isnan(pos), -np.inf, pos)
    n = np.where(np.isnan(neg), -np.inf, neg)
    allv = np.concatenate([p, n])
    order = allv.argsort(kind="mergesort")
    ranks = np.empty(len(allv))
    sorted_v = allv[order]
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and sorted_v[j + 1] == sorted_v[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    rp = ranks[: len(p)].sum()
    return float((rp - len(p) * (len(p) + 1) / 2) / (len(p) * len(n)))


def threshold_at_fmr(neg: np.ndarray, fmr: float) -> float:
    vals = np.sort(np.where(np.isnan(neg), -np.inf, neg))[::-1]
    k = int(math.floor(fmr * len(vals)))
    if k >= len(vals):
        return -np.inf
    # strictly above the (k+1)-th highest negative
    return float(vals[k])


def rate_above(x: np.ndarray, t: float) -> float | None:
    if len(x) == 0:
        return None
    return float(np.mean(np.where(np.isnan(x), -np.inf, x) > t))


def hamming64(a: int, b: int) -> int:
    return ((int(a) ^ int(b)) & 0xFFFFFFFFFFFFFFFF).bit_count()


class Arm:
    """One descriptor: image_id -> unit vector."""

    def __init__(self, name: str, ids: np.ndarray, vecs: np.ndarray) -> None:
        self.name = name
        v = vecs.astype(np.float32)
        n = np.linalg.norm(v, axis=1, keepdims=True)
        n[n == 0] = 1.0
        self.vecs = v / n
        self.pos = {int(i): k for k, i in enumerate(ids)}

    def sims(self, a: list[int], b: list[int]) -> tuple[list[int], list[int], np.ndarray]:
        ia = [i for i in a if i in self.pos]
        ib = [i for i in b if i in self.pos]
        if not ia or not ib:
            return ia, ib, np.zeros((len(ia), len(ib)), np.float32)
        return ia, ib, self.vecs[[self.pos[i] for i in ia]] @ self.vecs[[self.pos[i] for i in ib]].T


def load_arm(name: str, path: str) -> Arm | None:
    if not os.path.exists(path):
        return None
    z = np.load(path, allow_pickle=False)
    ids = z["image_id"] if "image_id" in z.files else z["key"]
    return Arm(name, ids.astype(np.int64), z["vec"])


def greedy_count(sims: np.ndarray, t: float) -> int:
    if sims.size == 0:
        return 0
    rs, cs = np.nonzero(sims >= t)
    order = np.argsort(-sims[rs, cs])
    used_r, used_c, n = set(), set(), 0
    for k in order:
        r, c = int(rs[k]), int(cs[k])
        if r in used_r or c in used_c:
            continue
        used_r.add(r)
        used_c.add(c)
        n += 1
    return n


def room_groups(imgs: list[dict[str, Any]], room_of: dict[int, str]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = defaultdict(list)
    for r in imgs:
        room = room_of.get(int(r["image_id"]))
        if room and len(out[room]) < ROOM_CAP:
            out[room].append(int(r["image_id"]))
    return out


def pair_features(p: dict[str, Any], imgs_a, imgs_b, arms: dict[str, Arm],
                  routers: dict[str, dict[int, str]], copy_t: dict[str, float],
                  lg: dict[str, dict[tuple[int, int], tuple[int, int, int]]]) -> dict[str, float]:
    f: dict[str, float] = {"n_tight": float(p["n_tight"]),
                           "n_tight_all": float(p.get("n_tight_all", p["n_tight"]))}
    a_all = [int(r["image_id"]) for r in imgs_a]
    b_all = [int(r["image_id"]) for r in imgs_b]
    ha = [(int(r["image_id"]), r["phash"]) for r in imgs_a if r.get("phash") is not None]
    hb = [(int(r["image_id"]), r["phash"]) for r in imgs_b if r.get("phash") is not None]
    f["dhash_min"] = float(min((hamming64(x, y) for _, x in ha for _, y in hb), default=64))
    f["dhash_neg_min"] = -f["dhash_min"]
    ph = {int(r["image_id"]): r["phash"] for r in list(imgs_a) + list(imgs_b)
          if r.get("phash") is not None}
    for rname, room_of in routers.items():
        ga, gb = room_groups(imgs_a, room_of), room_groups(imgs_b, room_of)
        for room in set(ga) & set(gb):
            tight = any(hamming64(ph[x], ph[y]) <= 6 for x in ga[room] for y in gb[room]
                        if x in ph and y in ph)
            f[f"tight@{rname}:{room}"] = 1.0 if tight else 0.0
    for name, arm in arms.items():
        ia, ib, s = arm.sims(a_all, b_all)
        f[f"{name}:max_any"] = float(s.max()) if s.size else math.nan
        f[f"{name}:n_copy"] = float(greedy_count(s, copy_t.get(name, 0.9)))
        for rname, room_of in routers.items():
            ga, gb = room_groups(imgs_a, room_of), room_groups(imgs_b, room_of)
            shared = set(ga) & set(gb)
            per_room = {}
            for room in shared:
                _, _, sr = arm.sims(ga[room], gb[room])
                if sr.size:
                    per_room[room] = float(sr.max())
            for room in ROOMS:
                f[f"{name}@{rname}:room:{room}"] = per_room.get(room, math.nan)
            priv = [v for k, v in per_room.items() if k in PRIVATE]
            f[f"{name}@{rname}:private_min"] = min(priv) if priv else math.nan
            f[f"{name}@{rname}:private_max"] = max(priv) if priv else math.nan
            f[f"{name}@{rname}:n_private"] = float(len(priv))
    for ex, table in lg.items():
        best = best_h = -1
        verified = {t: set() for t in LG_VERIFIED}
        room_best: dict[str, int] = {}
        for x in a_all:
            for y in b_all:
                key = (x, y) if x < y else (y, x)
                v = table.get(key)
                if v is None:
                    continue
                _m, fin, hin = v
                best = max(best, fin)
                best_h = max(best_h, hin)
                for t in LG_VERIFIED:
                    if fin >= t:
                        verified[t].add((x, y))
                for rname, room_of in routers.items():
                    ra, rb = room_of.get(x), room_of.get(y)
                    if ra and ra == rb:
                        k = f"{rname}:{ra}"
                        room_best[k] = max(room_best.get(k, -1), fin)
        f[f"lg_{ex}:max_f"] = float(best) if best >= 0 else math.nan
        f[f"lg_{ex}:max_h"] = float(best_h) if best_h >= 0 else math.nan
        for t, pairs in verified.items():
            f[f"lg_{ex}:n_verified{t}"] = float(_one_to_one(pairs))
        for k, v in room_best.items():
            f[f"lg_{ex}@{k}"] = float(v)
        priv = [v for k, v in room_best.items() if k.split(":", 1)[1] in PRIVATE]
        f[f"lg_{ex}:private_max_f"] = float(max(priv)) if priv else math.nan
    return f


def _one_to_one(pairs: set[tuple[int, int]]) -> int:
    ua, ub, n = set(), set(), 0
    for x, y in sorted(pairs):
        if x in ua or y in ub:
            continue
        ua.add(x)
        ub.add(y)
        n += 1
    return n


EXPECTED_ARMS = ("sscd", "dinov2", "dinov3", "synthetic", "lg_aliked", "lg_disk")
SYNTH_FILES = ("synthetic.json", "syn_sscd.npz", "syn_dinov2.npz", "syn_dinov3.npz",
               "syn_clip.npz", "gal_clip.npz")


def _shard_files(results: str, stem: str) -> list[str]:
    d = os.path.join(results, f"{stem}.shards")
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d)
                  if f.endswith(".npz") and ".tmp." not in f)


def arm_status(results: str) -> tuple[list[str], dict[str, str]]:
    """(finished arms, {unfinished arm: what is on disk}) in a pod's results directory.
    FINISHED means the arm's own file exists; shards alone are unfinished work."""
    finished: list[str] = []
    unfinished: dict[str, str] = {}
    for arm in EXPECTED_ARMS + ("lg_superpoint",):
        if arm == "synthetic":
            have = [f for f in SYNTH_FILES if os.path.exists(os.path.join(results, f))]
            if len(have) == len(SYNTH_FILES):
                finished.append(arm)
            else:
                shards = sum(len(_shard_files(results, f[:-4])) for f in SYNTH_FILES
                             if f.endswith(".npz"))
                unfinished[arm] = f"{len(have)}/{len(SYNTH_FILES)} files, {shards} shards"
            continue
        stem = arm if arm.startswith("lg_") else f"emb_{arm}"
        if os.path.exists(os.path.join(results, f"{stem}.npz")):
            finished.append(arm)
            continue
        shards = len(_shard_files(results, stem))
        legacy = os.path.exists(os.path.join(results, f"{stem}.partial.npz"))
        if arm == "lg_superpoint" and not shards and not legacy:
            continue  # licence-gated, never a default phase: absent is not unfinished
        unfinished[arm] = (f"{shards} shards on disk" if shards
                           else "partial file" if legacy else "not started")
    return finished, unfinished


def load_lg(results: str, include_partial: bool = False,
            ) -> dict[str, dict[tuple[int, int], tuple[int, int, int]]]:
    """LightGlue tables of the FINISHED extractors; `include_partial` adds the shards (or a
    pre-shard partial file) of an unfinished one, as a readout."""
    out = {}
    for ex in ("disk", "aliked", "superpoint"):
        paths = [os.path.join(results, f"lg_{ex}.npz")]
        if not os.path.exists(paths[0]):
            if not include_partial:
                continue
            paths = _shard_files(results, f"lg_{ex}") + [
                p for p in [os.path.join(results, f"lg_{ex}.partial.npz")] if os.path.exists(p)]
        if not paths:
            continue
        table = {}
        for path in paths:
            z = np.load(path)
            for a, b, m, fi, hi in zip(z["a"], z["b"], z["matches"], z["f_inliers"],
                                       z["h_inliers"]):
                a, b = int(a), int(b)
                table[(a, b) if a < b else (b, a)] = (int(m), int(fi), int(hi))
        out[ex] = table
    return out


def load_head_router(results: str, floor: float = 0.5) -> dict[int, str] | None:
    path = os.path.join(results, "heads_v1.npz")
    if not os.path.exists(path):
        return None
    z = np.load(path)
    rooms = dict(HEAD_ROOM)
    meta = os.path.join(results, "heads_v1.json")
    if os.path.exists(meta):
        # The pod's label-read map (the id map is C6's inference; the labels are the registry's).
        rooms.update({int(k): v for k, v in (json.load(open(meta)).get("rooms") or {}).items()})
    out = {}
    for k, w, s in zip(z["key"], z["winner"], z["winner_score"]):
        if float(s) >= floor:
            out[int(k)] = rooms.get(int(w), f"tag{int(w)}")
    return out


def evaluate(manifest: dict[str, Any], arms: dict[str, Arm], routers: dict[str, dict[int, str]],
             lg: dict[str, Any], copy_t: dict[str, float], include_stock: bool = True) -> dict[str, Any]:
    imgs_by_listing: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in manifest["images"]:
        if include_stock or r.get("pop") is None or r["pop"] < STOCK_POP:
            imgs_by_listing[int(r["listing_id"])].append(r)
    for lst in imgs_by_listing.values():
        lst.sort(key=lambda r: (r.get("seq") or 0, r["image_id"]))

    rows = []
    conflicts = 0
    for p in manifest["pairs"]:
        t = truth(p["classes"])
        if t is None:
            continue
        if t == "conflict":
            conflicts += 1
            continue
        feats = pair_features(p, imgs_by_listing.get(int(p["a"]), []),
                              imgs_by_listing.get(int(p["b"]), []), arms, routers, copy_t, lg)
        rows.append((t, p["stratum"], p, feats))

    keys = sorted({k for _, _, _, f in rows for k in f})
    report: dict[str, Any] = {"n_pairs": len(rows), "conflicts": conflicts,
                              "truth": dict(Counter(t for t, _, _, _ in rows)),
                              "truth_by_stratum": dict(Counter(f"{t}:{s}" for t, s, _, _ in rows)),
                              "features": {}}

    def vec(key, pred):
        return np.array([f.get(key, math.nan) for t, s, p, f in rows if pred(t, s, p)], dtype=np.float64)

    for key in keys:
        entry: dict[str, Any] = {}
        pos_all = vec(key, lambda t, s, p: t == "pos")
        entry["coverage_pos"] = float(np.mean(~np.isnan(pos_all))) if len(pos_all) else None
        for stratum in ("h0", "hs", "h1", "h4", "all"):
            pos = pos_all if stratum == "all" else vec(key, lambda t, s, p, st=stratum: t == "pos" and s == st)
            row = {"n_pos": int(len(pos))}
            for neg_cls in NEG + ("hard",):
                neg = vec(key, (lambda t, s, p: t in HARD) if neg_cls == "hard"
                          else (lambda t, s, p, c=neg_cls: t == c))
                row[f"auc_vs_{neg_cls}"] = auc(pos, neg)
            rnd = vec(key, lambda t, s, p: t == "neg_sib_rnd")
            hard = vec(key, lambda t, s, p: t in HARD)
            for fmr in FMR_POINTS:
                thr = threshold_at_fmr(rnd, fmr)
                row[f"recall@rndFMR{fmr}"] = rate_above(pos, thr)
                row[f"hardFMR@rndFMR{fmr}"] = rate_above(hard, thr)
                thr_h = threshold_at_fmr(hard, fmr)
                row[f"recall@hardFMR{fmr}"] = rate_above(pos, thr_h)
            entry[stratum] = row
        report["features"][key] = entry
    report["rooms"] = room_eval(rows, sorted(arms), sorted(routers), sorted(lg))
    share: dict[str, Any] = {}
    for room in ROOMS:
        grp: dict[str, list[float]] = defaultdict(list)
        for t, _s, _p, f in rows:
            flag = f.get(f"tight@clip:{room}")
            if flag is None:
                continue
            g = "pos" if t == "pos" else ("labelled" if t in LABELLED else t)
            grp[g].append(flag)
        share[room] = {g: {"n": len(v), "identical": float(np.mean(v))} for g, v in grp.items()}
    report["identical_share"] = share
    return report


def room_eval(rows, arm_names: list[str], router_names: list[str], lg_names: list[str]) -> dict[str, Any]:
    """Room grain, where dHash has nothing: the two adverts share a room (same router label)
    and NO frame pair in it is dHash-identical. Positives = same property; negatives =
    hard classes / random siblings. Score = best frame pair in that room."""
    out: dict[str, Any] = {}
    rooms = list(ROOMS) + ["private"]
    for rname in router_names:
        scorers = [(a, lambda f, room, a=a, r=rname: f.get(f"{a}@{r}:room:{room}", math.nan))
                   for a in arm_names]
        scorers += [(f"lg_{e}", lambda f, room, e=e, r=rname: f.get(f"lg_{e}@{r}:{room}", math.nan))
                    for e in lg_names]
        for room in rooms:
            members = PRIVATE if room == "private" else (room,)
            for sname, fn in scorers:
                buckets: dict[str, list[float]] = defaultdict(list)
                for t, s, p, f in rows:
                    for rm in members:
                        flag = f.get(f"tight@{rname}:{rm}")
                        if flag is None or flag > 0:
                            continue
                        grp = "pos" if t == "pos" else ("hard" if t in HARD else "rnd")
                        buckets[grp].append(fn(f, rm))
                        if t in LABELLED:
                            buckets["lab"].append(fn(f, rm))
                pos = np.array(buckets["pos"], float)
                hard = np.array(buckets["hard"], float)
                rnd = np.array(buckets["rnd"], float)
                lab = np.array(buckets["lab"], float)
                if len(pos) == 0:
                    continue
                out[f"{sname}@{rname}:{room}"] = {
                    "n_pos": len(pos), "n_hard": len(hard), "n_rnd": len(rnd), "n_lab": len(lab),
                    "auc_vs_lab": auc(pos, lab),
                    "recall@labFMR0.05": rate_above(pos, threshold_at_fmr(lab, 0.05)) if len(lab) else None,
                    "auc_vs_hard": auc(pos, hard), "auc_vs_rnd": auc(pos, rnd),
                    "recall@hardFMR0.05": rate_above(pos, threshold_at_fmr(hard, 0.05)),
                    "recall@hardFMR0.01": rate_above(pos, threshold_at_fmr(hard, 0.01)),
                    "recall@rndFMR0.01": rate_above(pos, threshold_at_fmr(rnd, 0.01)),
                    "pos_median": float(np.nanmedian(pos)) if np.any(~np.isnan(pos)) else None,
                    "hard_median": float(np.nanmedian(hard)) if np.any(~np.isnan(hard)) else None,
                }
    return out


def markdown(report: dict[str, Any], picks: list[str]) -> str:
    lines = [f"pairs {report['n_pairs']} (conflicts dropped {report['conflicts']}); truth "
             f"{json.dumps(report['truth'])}", "",
             "| feature | stratum | n_pos | cover | AUC hard | AUC fused | AUC unit | AUC sib_hard | "
             "AUC sib_rnd | recall @1% rnd FMR | hard FMR there | recall @5% hard FMR |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]

    def fmt(x):
        return "-" if x is None else f"{x:.3f}"

    for key in picks:
        e = report["features"].get(key)
        if not e:
            continue
        for st in ("h0", "hs", "h1", "h4", "all"):
            r = e[st]
            lines.append(f"| {key} | {st} | {r['n_pos']} | {fmt(e['coverage_pos'])} | "
                         f"{fmt(r['auc_vs_hard'])} | {fmt(r['auc_vs_neg_fused'])} | "
                         f"{fmt(r['auc_vs_neg_unit'])} | {fmt(r['auc_vs_neg_sib_hard'])} | "
                         f"{fmt(r['auc_vs_neg_sib_rnd'])} | {fmt(r['recall@rndFMR0.01'])} | "
                         f"{fmt(r['hardFMR@rndFMR0.01'])} | {fmt(r['recall@hardFMR0.05'])} |")
    return "\n".join(lines)


def rooms_markdown(rooms: dict[str, Any]) -> str:
    def fmt(x):
        return "-" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))

    lines = ["room grain, no dHash-identical frame in the room (score = best frame pair):", "",
             "| scorer@router:room | n_pos | n_lab | n_hard | n_rnd | AUC labelled | recall @5% labelled FMR | "
             "AUC hard | AUC rnd | recall @5% hard FMR | recall @1% hard FMR | recall @1% rnd FMR | "
             "pos median | hard median |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for k, v in rooms.items():
        lines.append(f"| {k} | {v['n_pos']} | {v['n_lab']} | {v['n_hard']} | {v['n_rnd']} | "
                     f"{fmt(v['auc_vs_lab'])} | {fmt(v['recall@labFMR0.05'])} | {fmt(v['auc_vs_hard'])} | "
                     f"{fmt(v['auc_vs_rnd'])} | {fmt(v['recall@hardFMR0.05'])} | "
                     f"{fmt(v['recall@hardFMR0.01'])} | {fmt(v['recall@rndFMR0.01'])} | "
                     f"{fmt(v['pos_median'])} | {fmt(v['hard_median'])} |")
    return "\n".join(lines)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


class DhashArm:
    """dHash as a retrieval arm: similarity = 64 - Hamming distance, over the same frames."""

    name = "dhash"

    def __init__(self, phash: dict[int, int]) -> None:
        self.pos = {i: k for k, i in enumerate(phash)}
        self.bits = np.array([int(v) & 0xFFFFFFFFFFFFFFFF for v in phash.values()], dtype=np.uint64)

    def sims(self, a: list[int], b: list[int]) -> tuple[list[int], list[int], np.ndarray]:
        ia = [i for i in a if i in self.pos]
        ib = [i for i in b if i in self.pos]
        if not ia or not ib:
            return ia, ib, np.zeros((len(ia), len(ib)), np.float32)
        x = self.bits[[self.pos[i] for i in ia]][:, None] ^ self.bits[[self.pos[i] for i in ib]][None, :]
        return ia, ib, (64 - np.bitwise_count(x)).astype(np.float32)


def positive_links(manifest: dict[str, Any]) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    """Listing -> listings known to be the same unit / known to be a different one."""
    pos: dict[int, set[int]] = defaultdict(set)
    neg: dict[int, set[int]] = defaultdict(set)
    for p in manifest["pairs"]:
        t = truth(p["classes"])
        a, b = int(p["a"]), int(p["b"])
        if t == "pos":
            pos[a].add(b)
            pos[b].add(a)
        elif t in HARD:
            neg[a].add(b)
            neg[b].add(a)
    return pos, neg


def _no_identical(ga: list[int], gb: list[int], ph: dict[int, int]) -> bool:
    return not any(hamming64(ph[x], ph[y]) <= 6 for x in ga for y in gb if x in ph and y in ph)


def retrieval_eval(manifest: dict[str, Any], arms: dict[str, Any], router: dict[int, str],
                   rooms: Iterable[str] = UNIT_ROOMS, ks: tuple[int, ...] = (1, 5, 10)) -> dict[str, Any]:
    """Frame-level retrieval, advert grain. A query is (A, room): A's frames of that room
    against EVERY frame of that room in the manifest (the labelled pairs' adverts AND the
    catalogue neighbourhood, i.e. the stated-different siblings, as distractors), A's own
    frames excluded. Relevant = frames of any advert ruled/merged the same unit as A.
    `interesting` keeps only queries where no frame of the room is dHash-identical (<= 6
    bits) between A and any relevant advert: the population dHash cannot serve.
    R@k = the first relevant frame ranks <= k; P@k = share of the top k frames that are
    relevant; neg_above = a labelled different-unit advert's frame outranks every
    relevant one."""
    pos_links, neg_links = positive_links(manifest)
    ph = {int(r["image_id"]): int(r["phash"]) for r in manifest["images"] if r.get("phash") is not None}
    listing_of = {int(r["image_id"]): int(r["listing_id"]) for r in manifest["images"]}
    frames: dict[str, dict[int, list[int]]] = {r: defaultdict(list) for r in rooms}
    for r in sorted(manifest["images"], key=lambda r: (r.get("seq") or 0, r["image_id"])):
        room = router.get(int(r["image_id"]))
        if room in frames:
            frames[room][int(r["listing_id"])].append(int(r["image_id"]))
    out: dict[str, Any] = {}
    for name, arm in arms.items():
        for room in rooms:
            by_listing = frames[room]
            gallery = [i for lst in by_listing.values() for i in lst]
            if not gallery:
                continue
            g_lid = np.array([listing_of[i] for i in gallery])
            stats = {pop: defaultdict(list) for pop in ("all", "interesting")}
            for a in sorted(pos_links):
                qa = by_listing.get(a, [])
                rel_l = {b for b in pos_links[a] if by_listing.get(b)}
                if not qa or not rel_l:
                    continue
                ia, ig, s = arm.sims(qa, gallery)
                if not ia:
                    continue
                own = g_lid == a
                s = np.where(own[None, :], -np.inf, s)
                best = s.max(axis=0)          # advert-grain: the best of A's frames per gallery frame
                order = np.argsort(-best, kind="stable")
                rel_mask = np.isin(g_lid, list(rel_l))
                if not rel_mask.any():
                    continue
                first = int(np.argmax(rel_mask[order])) + 1
                neg_mask = np.isin(g_lid, list(neg_links.get(a, ())))
                neg_above = bool(neg_mask.any() and best[neg_mask].max() > best[rel_mask].max())
                rel_frames = [i for b in rel_l for i in by_listing[b]]
                pops = ["all"] + (["interesting"] if _no_identical(qa, rel_frames, ph) else [])
                for pop in pops:
                    st = stats[pop]
                    st["rank"].append(first)
                    for k in ks:
                        st[f"R@{k}"].append(first <= k)
                        st[f"P@{k}"].append(float(rel_mask[order[:k]].mean()))
                    if neg_mask.any():
                        st["neg_above"].append(neg_above)
            for pop, st in stats.items():
                if not st["rank"]:
                    continue
                row = {"n": len(st["rank"]), "gallery": len(gallery),
                       "MRR": float(np.mean([1.0 / r for r in st["rank"]]))}
                for k in ks:
                    row[f"R@{k}"] = float(np.mean(st[f"R@{k}"]))
                    row[f"P@{k}"] = float(np.mean(st[f"P@{k}"]))
                if st["neg_above"]:
                    row["neg_above"] = float(np.mean(st["neg_above"]))
                    row["n_with_neg"] = len(st["neg_above"])
                out[f"{name}:{room}:{pop}"] = row
    return out


def room_scorers(arms: dict[str, Any], lg: dict[str, Any]) -> dict[str, Callable[..., Any]]:
    """name -> sims(a_ids, b_ids) for every descriptor arm and every LightGlue table
    (F-inliers; NaN where the pair was never matched)."""
    scorers: dict[str, Callable[..., Any]] = {name: arm.sims for name, arm in arms.items()}
    for ex, table in lg.items():
        def lg_sims(a: list[int], b: list[int], table=table):
            s = np.full((len(a), len(b)), np.nan, np.float32)
            for i, x in enumerate(a):
                for j, y in enumerate(b):
                    v = table.get((x, y) if x < y else (y, x))
                    if v is not None:
                        s[i, j] = v[1]
            return list(a), list(b), s
        scorers[f"lg_{ex}"] = lg_sims
    return scorers


def pair_room_scores(manifest: dict[str, Any], fn: Callable[..., Any], router: dict[int, str],
                     room: str) -> dict[tuple[int, int], tuple[float, float, bool]]:
    """(a, b) -> (s, cat, has_nb) for every labelled pair sharing `room`: s = the best
    A-B frame similarity, cat = the best similarity of that pair's two frames to a
    same-room frame of a stated-different co-live sibling (-inf when none)."""
    listing_imgs: dict[int, list[int]] = defaultdict(list)
    for r in sorted(manifest["images"], key=lambda r: (r.get("seq") or 0, r["image_id"])):
        if not r.get("nb"):
            listing_imgs[int(r["listing_id"])].append(int(r["image_id"]))
    nb_frames: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for anchor, sibs in (manifest.get("neighbours") or {}).items():
        for sib, _facts, ids in sibs:
            nb_frames[int(anchor)].extend((int(sib), int(i)) for i in ids
                                          if router.get(int(i)) == room)
    # A neighbour that is also a pair member has its frames in `listing_imgs` (never
    # flagged nb), so the anchors naming it get those frames too.
    named = {(int(a), int(sib)) for a, sibs in (manifest.get("neighbours") or {}).items()
             for sib, _f, _i in sibs}
    for a, sib in named:
        if not any(n == sib for n, _ in nb_frames[a]):
            nb_frames[a].extend((sib, i) for i in listing_imgs.get(sib, [])
                                if router.get(i) == room)
    out = {}
    for p in manifest["pairs"]:
        a, b = int(p["a"]), int(p["b"])
        ga = [i for i in listing_imgs.get(a, []) if router.get(i) == room][:ROOM_CAP]
        gb = [i for i in listing_imgs.get(b, []) if router.get(i) == room][:ROOM_CAP]
        if not ga or not gb:
            continue
        ia, ib, s = fn(ga, gb)
        if not ia or not ib or s.size == 0 or np.all(np.isnan(s)):
            continue
        r_, c_ = np.unravel_index(np.nanargmax(s), s.shape)
        sv, xa, xb = float(s[r_, c_]), ia[r_], ib[c_]
        cat = -np.inf
        witnesses = 0
        for x, anchor in ((xa, a), (xb, b)):
            # The witness is a THIRD advert: the pair's own partner never testifies.
            nbf = [i for n, i in nb_frames.get(anchor, []) if n not in (a, b)]
            witnesses += len(nbf)
            if nbf:
                _, _, cs = fn([x], nbf)
                if cs.size and not np.all(np.isnan(cs)):
                    cat = max(cat, float(np.nanmax(cs)))
        out[(a, b)] = (sv, cat, witnesses > 0)
    return out


def operating_point(y: np.ndarray, sv: np.ndarray, cat: np.ndarray | None,
                    fmr: float) -> dict[str, Any] | None:
    """The cut t that maximises recall of the rule `s > t and not (catalogue > t)` while
    the rule's own false-match rate on the negatives stays <= fmr. `cat=None` reads the
    match alone. The veto is therefore credited with the negatives it removes BEFORE the
    cut is chosen, which is how the engine would run it."""
    if not y.any() or y.all():
        return None
    c = np.full_like(sv, -np.inf) if cat is None else cat
    best = None
    for t in np.unique(sv)[::-1]:
        hit = (sv > t) & ~(c > t)
        f = float(hit[~y].mean())
        if f > fmr:
            continue
        r = float(hit[y].mean())
        if best is None or r > best["recall"]:
            best = {"threshold": float(t), "recall": r, "fmr": f,
                    "pos_hit": int(hit[y].sum()), "neg_hit": int(hit[~y].sum())}
    return best


def catalogue_eval(manifest: dict[str, Any], scores: dict[str, dict[str, Any]],
                   fmrs: tuple[float, ...] = (0.05, 0.01)) -> dict[str, Any]:
    """The catalogue veto, measured. The best A-B pair of frames is CATALOGUE when one of
    them is at least that similar to a same-room frame of a THIRD advert: a stated-different
    co-live sibling of A or B. Read at one false-match budget per arm (5 % / 1 % of the
    hard negatives), once for the match alone and once for match-and-not-catalogue."""
    tr = {(int(p["a"]), int(p["b"])): truth(p["classes"]) for p in manifest["pairs"]}
    out: dict[str, Any] = {}
    for key, table in scores.items():
        recs = [(tr[k] == "pos", sv, cat, nb, tr[k]) for k, (sv, cat, nb) in table.items()
                if tr.get(k) == "pos" or tr.get(k) in HARD]
        if not recs:
            continue
        y = np.array([r[0] for r in recs])
        sv = np.array([r[1] for r in recs], float)
        cat = np.array([r[2] for r in recs], float)
        lab = np.array([r[4] != "neg_sib_hard" for r in recs])
        row: dict[str, Any] = {"n_pos": int(y.sum()), "n_hard": int((~y).sum()),
                               "n_labelled_hard": int((~y & lab).sum()),
                               "nb_coverage_pos": float(np.mean([r[3] for r in recs if r[0]])) if y.any() else None,
                               "nb_coverage_hard": float(np.mean([r[3] for r in recs if not r[0]])) if (~y).any() else None}
        for fmr in fmrs:
            raw = operating_point(y, sv, None, fmr)
            veto = operating_point(y, sv, cat, fmr)
            if raw is None:
                continue
            t = raw["threshold"]
            m = sv > t
            flagged = m & (cat > t)
            pf, nf = int((flagged & y).sum()), int((flagged & ~y).sum())
            row[f"fmr{fmr}"] = {
                "raw": raw, "with_veto": veto,
                "flagged_at_raw_cut": {"pos": pf, "hard": nf,
                                       "catalogue_precision": nf / (nf + pf) if nf + pf else None,
                                       "catalogue_recall": nf / int((m & ~y).sum()) if (m & ~y).any() else None,
                                       "cost": pf / int((m & y).sum()) if (m & y).any() else None},
                "recall_gain": (veto["recall"] - raw["recall"]) if veto else None,
            }
        out[key] = row
    return out


# PRE-REGISTERED 2026-09-27, before any pod result exists. The arm each proof reads is
# the first one present in its list; the threshold is that arm's own 1 %-hard-FMR cut.
K_ARMS = ("lg_aliked", "lg_disk", "dinov3", "dinov2", "clip")
P_ARMS = ("sscd", "lg_aliked", "lg_disk", "clip")
COMBO_FMR = 0.01
MISMATCH_FNR = 0.05


def layered_proof(manifest: dict[str, Any], scores: dict[str, dict[str, Any]],
                  router_name: str) -> dict[str, Any]:
    """The operator's layered proof as a truth table: K = a kitchen match that is not
    catalogue, P = a floor-plan match that is not catalogue, $ = price paths agree."""
    tr = {(int(p["a"]), int(p["b"])): (truth(p["classes"]), p) for p in manifest["pairs"]}
    ops: dict[str, Any] = {}

    def proof(arms: tuple[str, ...], room: str) -> tuple[str | None, dict[tuple[int, int], bool]]:
        for arm in arms:
            table = scores.get(f"{arm}@{router_name}:{room}")
            if not table:
                continue
            keys = [k for k in table if tr.get(k, (None,))[0] == "pos" or tr.get(k, (None,))[0] in HARD]
            y = np.array([tr[k][0] == "pos" for k in keys])
            sv = np.array([table[k][0] for k in keys], float)
            cat = np.array([table[k][1] for k in keys], float)
            op = operating_point(y, sv, cat, COMBO_FMR)
            if op is None:
                continue
            thr = op["threshold"]
            ops[room] = {"arm": arm, **op}
            return arm, {k: (sv_ > thr and not cat_ > thr) for k, (sv_, cat_, _nb) in table.items()}
        return None, {}

    k_arm, kmap = proof(K_ARMS, "kitchen")
    p_arm, pmap = proof(P_ARMS, "floor_plan")
    # X = the plans DISAGREE: both adverts carry a plan and the best plan pair scores
    # under the cut that only MISMATCH_FNR of true pairs with two plans fall below. Two
    # units of different size have different layouts, so a mismatch is the veto the
    # operator asked for (a floor plan as the mitigation of a kitchen match).
    xmap: dict[tuple[int, int], bool] = {}
    for arm in P_ARMS:
        table = scores.get(f"{arm}@{router_name}:floor_plan")
        if not table:
            continue
        pos_s = np.array([v[0] for k, v in table.items() if tr.get(k, (None,))[0] == "pos"], float)
        pos_s = pos_s[~np.isnan(pos_s)]
        if len(pos_s) == 0:
            continue
        t_mis = float(np.quantile(pos_s, MISMATCH_FNR))
        ops["plan_mismatch"] = {"arm": arm, "threshold": t_mis, "fnr": MISMATCH_FNR}
        xmap = {k: bool(v[0] < t_mis) for k, v in table.items()}
        break
    cells: dict[str, Counter] = defaultdict(Counter)
    fused_pass: dict[str, list[str]] = defaultdict(list)
    for key, (t, p) in tr.items():
        if t is None or t == "conflict":
            continue
        k, pl, pr = kmap.get(key, False), pmap.get(key, False), p.get("price_agree")
        grp = "pos" if t == "pos" else ("hard" if t in HARD else "rnd")
        x = xmap.get(key, False)
        rules = {"K": k, "P": pl, "K&P": k and pl, "K&$": k and pr is True,
                 "P&$": pl and pr is True, "K&P&$": k and pl and pr is True,
                 "(K|P)&$": (k or pl) and pr is True,
                 "K&!X": k and not x, "K&!X&$": k and not x and pr is True}
        for name, hit in rules.items():
            if hit:
                cells[name][grp] += 1
                if t == "pos":
                    cells[name][f"pos_{p.get('stratum')}"] += 1
                if "neg_fused" in p["classes"] and t != "pos":
                    fused_pass[name].append(f"{key[0]}x{key[1]}")
    n_pos = sum(1 for t, _ in tr.values() if t == "pos")
    out = {"k_arm": k_arm, "p_arm": p_arm, "router": router_name, "n_pos": n_pos,
           "operating_points": ops, "rules": {}}
    for name, c in cells.items():
        hit_pos, hit_hard = c.get("pos", 0), c.get("hard", 0)
        out["rules"][name] = {**dict(c), "recall": hit_pos / n_pos if n_pos else None,
                              "precision_vs_hard": hit_pos / (hit_pos + hit_hard) if hit_pos + hit_hard else None,
                              "hard_ci": wilson(hit_hard, hit_pos + hit_hard),
                              "fused_passing": fused_pass.get(name, [])}
    return out


NEW_ARMS = ("dinov3", "dinov2", "sscd", "lg_aliked", "lg_disk")


def _best(rooms: dict[str, Any], room: str, metric: str, prefixes: tuple[str, ...]) -> tuple[str | None, float | None]:
    best, where = None, None
    for key, row in rooms.items():
        scorer, _, rm = key.partition("@")
        if rm.split(":", 1)[-1] != room or scorer not in prefixes:
            continue
        v = row.get(metric)
        if v is not None and (best is None or v > best):
            best, where = v, key
    return where, best


def verdict(report: dict[str, Any]) -> dict[str, Any]:
    """The pre-registered bars (G1_image_stack.md section 6), scored mechanically, on the
    FINISHED arms only when the report says which those are (`arms_finished`)."""
    rooms = report.get("rooms", {})
    out: dict[str, Any] = {}
    finished = report.get("arms_finished")
    gated = finished is not None
    new_arms = tuple(a for a in NEW_ARMS if not gated or a in finished)
    lg_arms = tuple(a for a in ("lg_aliked", "lg_disk") if a in new_arms)
    desc_arms = tuple(a for a in ("dinov3", "dinov2", "sscd") if a in new_arms)
    syn = (report.get("synthetic") or {}).get("per_arm", {})
    if gated and "synthetic" not in finished:
        syn = {}
    s1 = (syn.get("sscd") or {}).get("recall@1_where_dhash_misses")
    c1 = (syn.get("clip") or {}).get("recall@1_where_dhash_misses")
    out["B1_copy"] = {"sscd": s1, "clip": c1,
                      "pass": None if s1 is None else bool(s1 >= 0.95 and (c1 is None or s1 >= c1 + 0.10))}
    b2 = {}
    # Amended 2026-09-27 before any GPU number was read (GLOBAL_SEARCH review): a single room has
    # ~60-125 positives, a 95 % half-width of ~0.09 against a +0.10 margin, so a per-room AND would
    # STOP on noise about half the time. STOP rests on the pooled private rooms (n ~ 430, +-0.045);
    # kitchen and bathroom are readouts with their interval, never the gate.
    for room, margin in (("kitchen", 0.10), ("bathroom", 0.10), ("private", 0.08)):
        where, best = _best(rooms, room, "recall@labFMR0.05", new_arms)
        base = (rooms.get(f"clip@clip:{room}") or {}).get("recall@labFMR0.05")
        n_pos = (rooms.get(where) or {}).get("n_pos") if where else None
        half = (1.96 * math.sqrt(best * (1 - best) / n_pos)
                if best is not None and n_pos else None)
        b2[room] = {"best": where, "recall": best, "clip": base, "margin": margin, "n_pos": n_pos,
                    "ci95_half_width": half,
                    "pass": None if best is None or base is None else bool(best >= base + margin)}
    kitchen_ok = b2["kitchen"]["recall"] is not None and b2["kitchen"]["recall"] >= 0.75
    out["B2_same_room"] = {**b2, "kitchen_floor_0.75": kitchen_ok,
                           "gate": "private (pooled); kitchen, bathroom and the 0.75 floor are PARTIAL readouts",
                           "per_room_partial": [r for r in ("kitchen", "bathroom") if b2[r]["pass"] is False]
                           + ([] if kitchen_ok else ["kitchen_floor_0.75"]),
                           "pass": b2["private"]["pass"]}
    lg_where, lg_auc = _best(rooms, "private", "auc_vs_hard", lg_arms)
    _, lg_rec = _best(rooms, "private", "recall@hardFMR0.01", lg_arms)
    _, d_rec = _best(rooms, "private", "recall@hardFMR0.01", desc_arms)
    out["B3_geometry"] = {"lg": lg_where, "auc_vs_hard": lg_auc, "lg_recall": lg_rec,
                          "descriptor_recall": d_rec,
                          "pass": None if lg_auc is None or lg_rec is None or d_rec is None
                          else bool(lg_auc >= 0.93 and lg_rec >= d_rec + 0.05)}
    lay = report.get("layered") or {}
    cat = (report.get("catalogue") or {}).get(f"{lay.get('k_arm')}@{lay.get('router')}:kitchen", {})
    c1 = cat.get("fmr0.01") or {}
    fl = c1.get("flagged_at_raw_cut") or {}
    gain = c1.get("recall_gain")
    out["B4_catalogue"] = {"arm": lay.get("k_arm"), "recall_raw": (c1.get("raw") or {}).get("recall"),
                           "recall_with_veto": (c1.get("with_veto") or {}).get("recall"),
                           "recall_gain": gain, **{k: fl.get(k) for k in (
                               "catalogue_precision", "catalogue_recall", "cost")},
                           "pass": None if gain is None or fl.get("catalogue_precision") is None
                           else bool(fl["catalogue_precision"] >= 0.80 and gain >= 0.05
                                     and (fl.get("cost") or 0) <= 0.10)}
    rules = lay.get("rules", {})
    kp = rules.get("K&P&$") or {}
    k_only, p_only = rules.get("K") or {}, rules.get("P") or {}
    stronger = (kp.get("precision_vs_hard") is not None
                and kp["precision_vs_hard"] >= max(k_only.get("precision_vs_hard") or 0,
                                                    p_only.get("precision_vs_hard") or 0))
    out["B5_layered"] = {"K&P&$": {k: kp.get(k) for k in ("pos", "hard", "precision_vs_hard", "recall")},
                         "K": {k: k_only.get(k) for k in ("pos", "hard", "precision_vs_hard", "recall")},
                         "P": {k: p_only.get(k) for k in ("pos", "hard", "precision_vs_hard", "recall")},
                         "fused_passing": kp.get("fused_passing"), "stronger_than_either": stronger,
                         "pass": None if not kp else bool((kp.get("precision_vs_hard") or 0) >= 0.99
                                                          and (kp.get("pos") or 0) >= 30
                                                          and not kp.get("fused_passing") and stronger)}
    kx = rules.get("K&!X&$") or {}
    out["B5b_plan_veto"] = {"K&!X&$": {k: kx.get(k) for k in ("pos", "hard", "precision_vs_hard", "recall")},
                            "fused_passing": kx.get("fused_passing"),
                            "pass": None if not kx else bool(
                                (kx.get("precision_vs_hard") or 0) >= 0.99
                                and not kx.get("fused_passing")
                                and (kx.get("recall") or 0) >= 0.5 * (k_only.get("recall") or 0))}
    ret = report.get("retrieval", {})
    best_r, best_k = None, None
    for arm in new_arms:
        row = ret.get(f"{arm}:kitchen:interesting")
        if row and (best_r is None or row["R@5"] > best_r["R@5"]):
            best_r, best_k = row, arm
    base_r = ret.get("clip:kitchen:interesting")
    out["B6_retrieval"] = {"arm": best_k, "R@5": best_r and best_r["R@5"],
                           "neg_above": best_r and best_r.get("neg_above"),
                           "clip_R@5": base_r and base_r["R@5"],
                           "pass": None if not best_r or not base_r else bool(
                               best_r["R@5"] >= base_r["R@5"] + 0.10
                               and (best_r.get("neg_above") or 0) <= 0.10)}
    if gated:
        # Each bar names the finished arms it could read; one that had none says so
        # instead of reporting a reading it never made.
        needs = {"B1_copy": ("synthetic",), "B3_geometry": ("lg_aliked", "lg_disk")}
        for bar, row in out.items():
            want = needs.get(bar, NEW_ARMS)
            read = [a for a in want if a in finished]
            row["arms_read"] = read
            if not read:
                row["pass"] = None
                row["not_reported"] = f"no finished arm among {', '.join(want)}"
    b = {k: v.get("pass") for k, v in out.items()}
    unfinished = report.get("arms_unfinished") or {}
    if not report.get("arms_new") and not unfinished:
        decision = "NO NEW ARM: incumbent reading only (the bars need the pod's results)"
    elif unfinished:
        decision = (f"PARTIAL RUN: bars read on the finished arms only "
                    f"({', '.join(finished or []) or 'none'}); unfinished: "
                    + "; ".join(f"{k} ({v})" for k, v in sorted(unfinished.items()))
                    + ". No STOP or ADOPT until a resume finishes them")
    elif b["B2_same_room"] is False:
        decision = "STOP: the stack does not find the room dHash cannot; keep dHash/CLIP evidence"
    elif b["B2_same_room"] and b["B4_catalogue"] and b["B5_layered"]:
        decision = ("ADOPT: build the image proof on the stack (engine arms next: K-C on "
                    "descriptor+geometry, catalogue veto, layered proof)")
    else:
        decision = "PARTIAL: read the failing bars; no engine arm until they are understood"
    out["decision"] = decision
    out["partial"] = bool(unfinished)
    # A pixel verdict is never an engine adoption: the lab arm MF+G1 vs MF must also gain.
    out["engine_adoption_bar"] = ("M1 +2 on c17 AND c18, or >= 10 % of MF's lost band co-pairs recovered, "
                                  "at 0 fused on the strict read; features admissible only if the lane "
                                  "computes them from stored data for every scored pair")
    out["sscd_replaces_dhash_for_copies"] = b["B1_copy"]
    out["lightglue_needed"] = b["B3_geometry"]
    return out


def synthetic_eval(results: str, phash: dict[int, int] | None = None) -> dict[str, Any] | None:
    """Copy retrieval: each transformed copy queries the gallery (its original + distractors)."""
    path = os.path.join(results, "synthetic.json")
    if not os.path.exists(path):
        return None
    syn = json.load(open(path))
    rows = syn["rows"]
    gallery = syn["plan"]["gallery"]
    out: dict[str, Any] = {"n_queries": len(rows), "gallery": len(gallery),
                           "dhash_hit_le6": float(np.mean([r["dhash_hamming"] <= 6 for r in rows]))
                           if rows else None, "per_arm": {}}
    by_t = defaultdict(list)
    for r in rows:
        by_t[r["transform"]].append(r["dhash_hamming"] <= 6)
    out["dhash_hit_by_transform"] = {t: float(np.mean(v)) for t, v in by_t.items()}
    for arm, gal_file in (("sscd", "emb_sscd.npz"), ("dinov2", "emb_dinov2.npz"),
                          ("dinov3", "emb_dinov3.npz"), ("clip", "gal_clip.npz")):
        q = os.path.join(results, f"syn_{arm}.npz")
        g = os.path.join(results, gal_file)
        if not (os.path.exists(q) and os.path.exists(g)):
            continue
        qz, gz = np.load(q), np.load(g)
        gpos = {int(k): i for i, k in enumerate(gz["key"])}
        keep = [gpos[i] for i in gallery if i in gpos]
        gids = np.array([int(gz["key"][i]) for i in keep])
        gv = gz["vec"][keep].astype(np.float32)
        gv /= np.linalg.norm(gv, axis=1, keepdims=True) + 1e-9
        qv = qz["vec"].astype(np.float32)
        qv /= np.linalg.norm(qv, axis=1, keepdims=True) + 1e-9
        qids = [str(k) for k in qz["key"]]
        src = {r["synth_id"]: r for r in rows}
        sims = qv @ gv.T
        top = np.argsort(-sims, axis=1)[:, :5]
        hit1, hit5, by_tr, miss_dhash = [], [], defaultdict(list), []
        pos_cos, neg_cos = [], []
        for i, sid in enumerate(qids):
            r = src.get(sid)
            if r is None:
                continue
            target = int(r["image_id"])
            ranked = [int(x) for x in gids[top[i]]]

            def same_photo(x: int) -> bool:
                if x == target:
                    return True
                if phash and x in phash and target in phash:
                    return hamming64(phash[x], phash[target]) <= 2
                return False

            h1, h5 = same_photo(ranked[0]), any(same_photo(x) for x in ranked)
            hit1.append(h1)
            hit5.append(h5)
            by_tr[r["transform"]].append(h1)
            if r["dhash_hamming"] > 6:
                miss_dhash.append(h1)
            ti = np.where(gids == target)[0]
            if len(ti):
                pos_cos.append(float(sims[i, ti[0]]))
                neg_cos.append(float(np.max(np.delete(sims[i], ti[0]))))
        out["per_arm"][arm] = {
            "recall@1": float(np.mean(hit1)) if hit1 else None,
            "recall@5": float(np.mean(hit5)) if hit5 else None,
            "recall@1_where_dhash_misses": float(np.mean(miss_dhash)) if miss_dhash else None,
            "n_dhash_misses": len(miss_dhash),
            "recall@1_by_transform": {t: float(np.mean(v)) for t, v in by_tr.items()},
            "auc_copy_vs_best_distractor": auc(np.array(pos_cos), np.array(neg_cos)),
        }
    return out


def extra_markdown(report: dict[str, Any]) -> str:
    def fmt(x):
        return "-" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))

    lines = ["", "frame-level retrieval, advert grain (gallery = every same-room frame, siblings "
             "included; interesting = no dHash-identical frame in the room):", "",
             "| arm:room:population | n | gallery | R@1 | R@5 | R@10 | P@5 | MRR | neg above pos |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for k, v in sorted(report.get("retrieval", {}).items()):
        lines.append(f"| {k} | {v['n']} | {v['gallery']} | {fmt(v['R@1'])} | {fmt(v['R@5'])} | "
                     f"{fmt(v['R@10'])} | {fmt(v['P@5'])} | {fmt(v['MRR'])} | {fmt(v.get('neg_above'))} |")
    lines += ["", "catalogue veto (witness = a THIRD advert: a stated-different co-live sibling). "
              "Operating points at 1 % of the hard negatives, match alone vs match-and-not-catalogue:", "",
              "| arm@router:room | n_pos | n_hard | witness cover pos/hard | recall raw | recall with veto | "
              "gain | flagged at raw cut pos/hard | catalogue precision | cost |",
              "|---|---:|---:|---|---:|---:|---:|---|---:|---:|"]
    for k, v in sorted(report.get("catalogue", {}).items()):
        c = v.get("fmr0.01") or {}
        fl = c.get("flagged_at_raw_cut") or {}
        lines.append(f"| {k} | {v['n_pos']} | {v['n_hard']} | {fmt(v['nb_coverage_pos'])}/"
                     f"{fmt(v['nb_coverage_hard'])} | {fmt((c.get('raw') or {}).get('recall'))} | "
                     f"{fmt((c.get('with_veto') or {}).get('recall'))} | {fmt(c.get('recall_gain'))} | "
                     f"{fl.get('pos')}/{fl.get('hard')} | {fmt(fl.get('catalogue_precision'))} | "
                     f"{fmt(fl.get('cost'))} |")
    lines += ["", "share of pairs whose galleries share a dHash-identical frame (<= 6 bits) in the room:", "",
              "| room | " + " | ".join(("pos", "labelled", "neg_sib_hard", "neg_sib_rnd")) + " |",
              "|---|---:|---:|---:|---:|"]
    for room, g in (report.get("identical_share") or {}).items():
        lines.append(f"| {room} | " + " | ".join(
            f"{fmt((g.get(k) or {}).get('identical'))} (n={(g.get(k) or {}).get('n', 0)})"
            for k in ("pos", "labelled", "neg_sib_hard", "neg_sib_rnd")) + " |")
    lay = report.get("layered") or {}
    lines += ["", f"layered proof (K = kitchen by {lay.get('k_arm')}, P = floor plan by "
              f"{lay.get('p_arm')}, both non-catalogue at the arm's 1 % hard-FMR cut; $ = price "
              f"paths agree; router {lay.get('router')}; {lay.get('n_pos')} positives):", "",
              "| rule | pos | hard | rnd | recall | precision vs hard | fused passing |",
              "|---|---:|---:|---:|---:|---:|---|"]
    for name, r in (lay.get("rules") or {}).items():
        lines.append(f"| {name} | {r.get('pos', 0)} | {r.get('hard', 0)} | {r.get('rnd', 0)} | "
                     f"{fmt(r.get('recall'))} | {fmt(r.get('precision_vs_hard'))} | "
                     f"{', '.join(r.get('fused_passing') or []) or '-'} |")
    lines += ["", "verdict (pre-registered bars):", "", "```",
              json.dumps(report.get("verdict"), indent=1, default=str), "```"]
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--clip", default="", help="Stored CLIP B/32 vectors (npz image_id, vec).")
    ap.add_argument("--results", default="", help="The pod's g1_results directory.")
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine-stock", action="store_true",
                    help="Drop E9 stock frames (pop >= 8) as the engine does; default keeps them.")
    ap.add_argument("--include-partial", action="store_true",
                    help="Also read unfinished LightGlue shards (a readout: the verdict stays PARTIAL).")
    args = ap.parse_args(list(argv) if argv is not None else None)

    with gzip.open(args.manifest, "rt") as fh:
        manifest = json.load(fh)
    arms: dict[str, Arm] = {}
    if args.clip:
        a = load_arm("clip", args.clip)
        if a:
            arms["clip"] = a
    routers: dict[str, dict[int, str]] = {
        "clip": {int(r["image_id"]): r["tag"] for r in manifest["images"] if r.get("tag")}}
    lg: dict[str, Any] = {}
    status: tuple[list[str], dict[str, str]] | None = None
    if args.results:
        status = arm_status(args.results)
        for name in ("sscd", "dinov2", "dinov3"):
            a = load_arm(name, os.path.join(args.results, f"emb_{name}.npz"))
            if a:
                arms[name] = a
        head = load_head_router(args.results)
        if head:
            routers["head"] = head
            # The pod's own routing: the head winner where one clears the floor, else CLIP.
            routers["hc"] = {**routers["clip"], **head}
        lg = load_lg(args.results, include_partial=args.include_partial)
    copy_t = {"clip": 0.97, "sscd": 0.75, "dinov2": 0.9, "dinov3": 0.9}
    report = evaluate(manifest, arms, routers, lg, copy_t, include_stock=not args.engine_stock)
    report["include_stock"] = not args.engine_stock
    report["arms"] = sorted(arms)
    report["routers"] = sorted(routers)
    report["lightglue"] = sorted(lg)
    if args.results:
        report["synthetic"] = synthetic_eval(
            args.results, {int(r["image_id"]): int(r["phash"]) for r in manifest["images"]
                           if r.get("phash") is not None})
    phash = {int(r["image_id"]): int(r["phash"]) for r in manifest["images"]
             if r.get("phash") is not None}
    unit_router = "hc" if "hc" in routers else "clip"
    scorers = room_scorers({**arms, "dhash": DhashArm(phash)}, lg)
    scores = {f"{name}@{unit_router}:{room}": pair_room_scores(manifest, fn, routers[unit_router], room)
              for name, fn in scorers.items() for room in UNIT_ROOMS}
    if unit_router != "clip" and "clip" in arms:
        # The incumbent read the incumbent's way, for the baseline rows.
        for room in UNIT_ROOMS:
            scores[f"clip@clip:{room}"] = pair_room_scores(manifest, arms["clip"].sims,
                                                           routers["clip"], room)
    report["catalogue"] = catalogue_eval(manifest, scores)
    report["layered"] = layered_proof(manifest, scores, unit_router)
    report["retrieval"] = retrieval_eval(manifest, {**arms, "dhash": DhashArm(phash)},
                                         routers[unit_router])
    if unit_router != "clip" and "clip" in arms:
        for k, v in retrieval_eval(manifest, {"clip": arms["clip"]}, routers["clip"]).items():
            report["retrieval"][f"{k}@clip-router"] = v
    report["arms_new"] = sorted(set(arms) - {"clip"}) + [f"lg_{e}" for e in sorted(lg)]
    if status is not None:
        report["arms_finished"], report["arms_unfinished"] = status
        report["partial"] = bool(status[1])
    report["verdict"] = verdict(report)
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    picks = ["n_tight", "n_tight_all", "dhash_neg_min"]
    for name in sorted(arms):
        picks += [f"{name}:max_any", f"{name}:n_copy"]
        for rname in sorted(routers):
            picks += [f"{name}@{rname}:private_min", f"{name}@{rname}:private_max",
                      f"{name}@{rname}:room:kitchen", f"{name}@{rname}:room:bathroom",
                      f"{name}@{rname}:room:floor_plan"]
    for ex in sorted(lg):
        picks += [f"lg_{ex}:max_f", f"lg_{ex}:n_verified30", f"lg_{ex}:private_max_f"]
    md = markdown(report, picks) + "\n\n" + rooms_markdown(report["rooms"]) + extra_markdown(report)
    if report.get("partial"):
        md = (f"**PARTIAL RUN** — finished: {', '.join(report['arms_finished']) or 'none'}; "
              "unfinished: " + "; ".join(f"{k} ({v})" for k, v in
                                         sorted(report["arms_unfinished"].items()))
              + ". Bars below read the finished arms only.\n\n" + md)
    with open(os.path.splitext(args.out)[0] + ".md", "w") as fh:
        fh.write(md + "\n")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
