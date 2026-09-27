"""C1 census: the engine's own pipeline (harness.run_engine, family/dev guards off) re-run
in-process with features cached, so one rule can be ablated per replay.

Faithful to harness.run_engine for w31 (guard_on False): same fps/blocking/features/decide/
storable/pair_slots/relation_for/cluster_pairs. Ablations are either settings overrides or
named monkeypatches (a rule with no dial). Engine-alone by default: no must-link, no operator
must-not-link (the E61 machine vetoes stay, they are an engine rule), so the operator's rulings
stay OUT of the input and can be used as the yardstick.
"""
from __future__ import annotations

import gzip
import json
import os
import pickle
import sys
import time
from collections import Counter
from dataclasses import replace as dc_replace
from pathlib import Path

WT = os.environ.get("C1_WT", "/home/hejtm/dev/sreality/.claude/worktrees/w15-census-c1")
if WT not in sys.path:
    sys.path.insert(0, WT)

import autodedup.blocking as BLK  # noqa: E402
import autodedup.d43 as D43  # noqa: E402
import autodedup.decide as DEC  # noqa: E402
import autodedup.guards as GRD  # noqa: E402
import autodedup.indistinguishable as IND  # noqa: E402
import autodedup.demonstrate as DEM  # noqa: E402
from autodedup.blocking import generate_pairs  # noqa: E402
from autodedup.cluster import cluster_pairs  # noqa: E402
from autodedup.dataset import load  # noqa: E402
from autodedup.features import FeatureContext, pair_features  # noqa: E402
from autodedup.fingerprint import build_all  # noqa: E402
from autodedup.harness import load_model, load_must_not_link  # noqa: E402
from autodedup.hazard_context import ContextIndex  # noqa: E402
from autodedup.indistinguishable import FEATURE_SLOTS as D43_FEATURE_SLOTS  # noqa: E402
from autodedup.settings import Settings  # noqa: E402
from autodedup.store_score import storable  # noqa: E402

LABELS = Path("/home/hejtm/autodedup-artifacts/w14/labels_g13_36225845749/"
              "autodedup-labels-36225845749")
ORIG_PAIR_VETO = GRD.pair_veto
ORIG_FACTS = IND.distinguishing_facts
ORIG_AUTO_REJECT = DEC.auto_reject_reason
ORIG_CERT_B = DEC.certificate_b
ORIG_CERT_C = DEC.certificate_c
ORIG_CERT_R = DEC.certificate_r
ORIG_DEMO_SHORTFALL = DEM.demonstration_shortfall
ORIG_PRICE_CONFLICT = DEM.price_conflict
ORIG_INVARIANTS = GRD.cluster_invariants_ok

# instrumentation: every fact list distinguishing_facts returns, per (lo,hi,mode)
FACT_LOG: dict | None = None
# instrumentation: every member pair the cluster-grain E157 price limb refused
PRICE_LOG: set | None = None


def _logged_price_conflict(a, b, settings, paths_agree, overlap):
    out = ORIG_PRICE_CONFLICT(a, b, settings, paths_agree, overlap)
    if out and PRICE_LOG is not None:
        PRICE_LOG.add(key(a.id, b.id))
    return out


def key(a, b):
    a, b = int(a), int(b)
    return (a, b) if a < b else (b, a)


def _logged_facts(a, b, feats=None, settings=None, mode=IND.PROMOTE):
    out = ORIG_FACTS(a, b, feats, settings, mode)
    if FACT_LOG is not None:
        FACT_LOG[(key(a.id, b.id), mode)] = tuple(f.name for f in out)
    return out


def install_facts(fn) -> None:
    IND.distinguishing_facts = fn
    DEC.distinguishing_facts = fn
    D43.distinguishing_facts = fn


def reset_patches() -> None:
    BLK.pair_veto = ORIG_PAIR_VETO
    DEC.pair_veto = ORIG_PAIR_VETO
    install_facts(_logged_facts)
    DEC.auto_reject_reason = ORIG_AUTO_REJECT
    DEC.certificate_b = ORIG_CERT_B
    DEC.certificate_c = ORIG_CERT_C
    DEC.certificate_r = ORIG_CERT_R
    DEM.demonstration_shortfall = ORIG_DEMO_SHORTFALL
    DEC.demonstration_shortfall = ORIG_DEMO_SHORTFALL
    DEM.price_conflict = ORIG_PRICE_CONFLICT
    D43.price_conflict = _logged_price_conflict
    GRD.cluster_invariants_ok = ORIG_INVARIANTS
    import autodedup.cluster as CL
    CL.cluster_invariants_ok = ORIG_INVARIANTS


# --- patches (rules without a dial) --------------------------------------------------------

def patch_drop_wall(name: str):
    def veto(a, b, settings=None):
        v = ORIG_PAIR_VETO(a, b, settings)
        if v != name:
            return v
        # the wall is gone: re-read the later walls in pair_veto's order
        cfg = settings or Settings()
        order = ["category_type", "category_main", "area", "disposition", "floor"]
        for later in order[order.index(name) + 1:]:
            if later == "area" and GRD.area_relation(a.area_m2, b.area_m2, cfg) == "reject":
                return "area"
            if later == "disposition":
                is_land = GRD.LAND_CATEGORY in (a.category_main, b.category_main)
                if (not is_land and a.disposition is not None and b.disposition is not None
                        and a.disposition != b.disposition):
                    return "disposition"
            if later == "floor" and (a.category_main == GRD.FLAT_CATEGORY
                                     and b.category_main == GRD.FLAT_CATEGORY
                                     and a.floor is not None and b.floor is not None
                                     and abs(a.floor - b.floor) >= 2):
                return "floor"
            if later == "category_main" and not GRD.category_main_compatible(
                    a.category_main, b.category_main):
                return "category_main"
        return None

    def apply():
        BLK.pair_veto = veto
        DEC.pair_veto = veto
    return apply


def patch_no_walls():
    def apply():
        BLK.pair_veto = lambda a, b, settings=None: None
        DEC.pair_veto = lambda a, b, settings=None: None
    return apply


def patch_drop_facts(names: set[str], modes: set[str] | None = None):
    def fn(a, b, feats=None, settings=None, mode=IND.PROMOTE):
        out = _logged_facts(a, b, feats, settings, mode)
        if modes is not None and mode not in modes:
            return out
        return [f for f in out if f.name not in names]

    def apply():
        install_facts(fn)
    return apply


def patch_auto_reject(keep: str | None):
    def fn(feats, settings=None):
        r = ORIG_AUTO_REJECT(feats, settings)
        if r is None:
            return None
        if keep is None:
            # neither limb: but attr limb evaluated first; check the numeral limb separately
            return None
        if r == keep:
            return r
        # r is the dropped limb; attr is checked first, so if r == attr and keep == numeral,
        # re-read the numeral limb alone
        if keep == "numeral_conflict" and DEC.present_value(feats, "numeral_conflict") == 1.0:
            return "numeral_conflict"
        return None

    def apply():
        DEC.auto_reject_reason = fn
    return apply


def patch_cert(name: str):
    def apply():
        if name == "K-B":
            DEC.certificate_b = lambda *a, **k: False
        elif name == "K-C":
            DEC.certificate_c = lambda *a, **k: False
        elif name == "K-R":
            DEC.certificate_r = lambda *a, **k: False
    return apply


def patch_demo_drop_limb(limb: str):
    """Drop one A-limb of D50: a shortfall naming `limb` is treated as no shortfall...
    but the later limbs still have to hold, so re-evaluate with that limb waived."""
    def fn(a, b, settings, paths_agree, overlap):
        out = ORIG_DEMO_SHORTFALL(a, b, settings, paths_agree, overlap)
        if out is None or out[0] != limb:
            return out
        # waive this limb: evaluate the remaining limbs in order
        s = settings
        if limb == "area":
            if s.demonstrate_require_disposition and not DEM.disposition_demonstrated(a, b):
                kind = DEM.MISSING if (a.disposition is None or b.disposition is None) else DEM.CONTRADICTION
                return ("disposition", kind)
        if limb in ("area", "disposition"):
            if not DEM.price_demonstrated(a, b, s, paths_agree, overlap):
                priced = bool(a.price and b.price and float(a.price) > 0.0 and float(b.price) > 0.0)
                return ("price", DEM.CONTRADICTION if priced else DEM.MISSING)
        if limb in ("area", "disposition", "price"):
            if (s.demonstrate_require_obec and not DEM.obec_demonstrated(a, b)
                    and not (s.demonstrate_obec_one_text_sequential
                             and DEM.one_text_sequential(a, b, s))):
                known = a.location.obec_kod is not None and b.location.obec_kod is not None
                return ("obec", DEM.CONTRADICTION if known else DEM.MISSING)
        if s.demonstrate_onesided and not limb.startswith("onesided"):
            one = DEM.onesided_fact(a, b, s)
            if one is not None:
                return (f"onesided_{one}", DEM.CONTRADICTION)
        return None

    def apply():
        DEM.demonstration_shortfall = fn
        DEC.demonstration_shortfall = fn
    return apply


def patch_invariant_off(name: str):
    """One set-level invariant off; the later limbs (must-not-link, the D43 relation) still
    read. (The first version returned None as soon as the dropped limb fired, skipping the
    later limbs - that was wrong; every C:<invariant> arm is re-run under this one.)"""
    return patch_invariants_off({name})


def patch_invariants_off(names: set[str]):
    """Drop several set-level cluster invariants at once: re-read with each one neutralised."""
    import autodedup.cluster as CL

    def fn(members, settings=None, must_not_link=frozenset(), relation=None, closure_of=None):
        cfg = settings or Settings()
        over = {}
        if "size" in names:
            over["max_cluster_size"] = 10 ** 9
        if "area_spread" in names:
            over["cluster_area_spread"] = 1.0
        if over:
            cfg = Settings.from_dict({**cfg.to_dict(), **over})
        for _ in range(4):
            r = ORIG_INVARIANTS(members, cfg, must_not_link, relation, closure_of)
            if r is None or r not in names:
                return r
            if r in ("category_type", "compat_class"):
                # these two precede must_not_link and d43: re-read the rest by hand
                ids = [fp.listing_id for fp in members]
                for i, left in enumerate(ids):
                    for right in ids[i + 1:]:
                        pair = (left, right) if left < right else (right, left)
                        if pair in must_not_link:
                            return "must_not_link"
                if relation is not None and relation.violating_pair(ids) is not None:
                    return "d43_distinguishable"
                return None
        return None

    def apply():
        GRD.cluster_invariants_ok = fn
        CL.cluster_invariants_ok = fn
    return apply


# --- labels --------------------------------------------------------------------------------

def load_labels(ids: set[int]) -> dict:
    """Latest-wins operator verdict per pair (operator_labels + Browse merges + must-not-link)."""
    events: list[tuple[str, tuple[int, int], str, str]] = []
    for line in (LABELS / "operator_labels.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        k = key(r["listing_lo"], r["listing_hi"])
        v = "same" if r["verdict"] == "same" else "different"
        events.append((r.get("decided_at") or "", k, v, "label:" + r.get("source", "")))
    merges_pairs = set()
    for line in (LABELS / "operator_merges.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        mem = sorted({int(m["listing_id"]) for m in r["members"]})
        for i, a in enumerate(mem):
            for b in mem[i + 1:]:
                events.append((r.get("merged_at") or "", (a, b), "same", "browse_merge"))
                merges_pairs.add((a, b))
    for line in (LABELS / "must_not_link.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        events.append((r.get("created_at") or "", key(r["listing_lo"], r["listing_hi"]),
                       "different", "must_not_link"))
    events.sort()
    latest: dict[tuple[int, int], str] = {}
    for _t, k, v, _s in events:
        latest[k] = v
    inside = {k: v for k, v in latest.items() if k[0] in ids and k[1] in ids}
    merges_inside = {k for k in merges_pairs if k[0] in ids and k[1] in ids
                     and inside.get(k) == "same"}
    return {"verdict": inside, "browse_merge": merges_inside}


def load_ref(path: Path | None, ids: set[int]) -> dict:
    """truth16's label-free reference: certain duplicates (guard-clean) + structural labels."""
    if path is None or not path.exists():
        return {}
    cd, cn = set(), set()
    for line in (path / "certain_duplicates.jsonl").read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            k = key(r["lo"], r["hi"])
            if k[0] in ids and k[1] in ids:
                cd.add(k)
    for line in (path / "structural_labels.jsonl").read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            k = key(r["lo"], r["hi"])
            if k[0] in ids and k[1] in ids and r.get("label") == "different":
                cn.add(k)
    return {"cd": cd, "cn": cn}


# --- the engine ----------------------------------------------------------------------------

class Engine:
    def __init__(self, cohort: str, settings_path: str, model_path: str, cache: str | None,
                 ref: str | None = None, must_not_link: str | None = None) -> None:
        clock = time.perf_counter()
        self.ds = load(Path(cohort))
        self.base = Settings.from_json(Path(settings_path))
        self.model = load_model(model_path)
        self.fps = build_all(self.ds, self.base)
        self.ctx = FeatureContext.build(self.fps, self.base, self.ds)
        self.ctx.index_attrs(self.fps, self.ds.listings)
        self.hazard = ContextIndex.build(self.ds.listings, self.ds.images_by_listing)
        self.cache_path = Path(cache) if cache else None
        self.feats: dict[tuple[int, int], dict] = {}
        if self.cache_path and self.cache_path.exists():
            with open(self.cache_path, "rb") as fh:
                self.feats = pickle.load(fh)
        self.base_pairs, self.base_blocking = generate_pairs(self.fps, self.base)
        self.ids = set(self.ds.listings)
        self.labels = load_labels(self.ids)
        self.ref = load_ref(Path(ref) if ref else None, self.ids)
        self.mnl = (frozenset(p for p in load_must_not_link(must_not_link)
                              if p[0] in self.ids and p[1] in self.ids)
                    if must_not_link else frozenset())
        self.setup_s = time.perf_counter() - clock
        self.dirty = False

    def ensure_features(self, pairs) -> int:
        todo = [k for k in sorted(pairs) if k not in self.feats]
        for lo, hi in todo:
            fa, fb = self.fps[lo], self.fps[hi]
            la, lb = self.ds.listings[lo], self.ds.listings[hi]
            self.feats[(lo, hi)] = pair_features(fa, fb, la, lb, self.ds.images(lo),
                                                 self.ds.images(hi), self.ctx, self.base)
        if todo:
            self.dirty = True
        return len(todo)

    def save_cache(self) -> None:
        if self.cache_path and self.dirty:
            tmp = self.cache_path.with_suffix(".tmp")
            with open(tmp, "wb") as fh:
                pickle.dump(self.feats, fh, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(self.cache_path)
            self.dirty = False

    def layers(self) -> dict:
        """The base settings' `_decide_layers` answer per pair (rule floor, certificates, cut),
        reused by every arm that only touches what runs after it (E63, D43, D50, D65)."""
        if getattr(self, "_layers", None) is None:
            reset_patches()
            self.ensure_features(self.base_pairs)
            self._layers = {}
            for (lo, hi) in sorted(self.base_pairs):
                self._layers[(lo, hi)] = DEC._decide_layers(
                    self.fps[lo], self.fps[hi], self.ds.listings[lo], self.ds.listings[hi],
                    self.feats[(lo, hi)], self.base_pairs[(lo, hi)], self.model, self.base)
        return self._layers

    def run(self, settings: Settings, patch=None, reblock: bool = False,
            must_link: frozenset = frozenset(), mnl: frozenset | None = None,
            log_facts: bool = False, post_only: bool = False,
            machine_vetoes: bool = True) -> dict:
        global FACT_LOG, PRICE_LOG
        reset_patches()
        if patch is not None:
            patch()
        FACT_LOG = {} if log_facts else None
        PRICE_LOG = set() if log_facts else None
        t0 = time.perf_counter()
        if reblock:
            pairs, blocking = generate_pairs(self.fps, settings)
        else:
            pairs, blocking = self.base_pairs, self.base_blocking
        new = self.ensure_features(pairs)
        if new:
            self.save_cache()
        t1 = time.perf_counter()
        decisions = []
        slots = {}
        layers = self.layers() if post_only else None
        if post_only:
            # the layers were cut with no patch; re-install this arm's patch afterwards
            reset_patches()
            if patch is not None:
                patch()
        for (lo, hi) in sorted(pairs):
            feats = self.feats[(lo, hi)]
            la, lb = self.ds.listings[lo], self.ds.listings[hi]
            if post_only:
                d = dc_replace(layers[(lo, hi)])
                if d.zone in ("merge", "band"):
                    d = DEC.apply_context_rule(d, feats, la, lb, settings, self.hazard)
                    d = DEC.apply_d43_rule(d, la, lb, feats, settings)
                    d = DEC.apply_merge_policy(d, la, lb, settings)
            else:
                d = DEC.decide_pair(self.fps[lo], self.fps[hi], la, lb, feats,
                                    pairs[(lo, hi)], self.model, settings, self.hazard)
            decisions.append(d)
            if storable({"zone": d.zone, "score": d.score, "evidence": d.evidence},
                        settings.store_floor):
                slots[(lo, hi)] = {n: feats[n] for n in D43_FEATURE_SLOTS if n in feats}
        t2 = time.perf_counter()
        vetoed = (frozenset((d.lo, d.hi) for d in decisions
                            if d.veto == GRD.UNIT_DESIGNATOR_VETO)
                  if machine_vetoes else frozenset())
        kc = ({(d.lo, d.hi): d.certificate for d in decisions if d.certificate == "K-C"}
              if settings.d43_cluster_price_kc_house_number else None)
        use_mnl = self.mnl if mnl is None else mnl
        clusters = cluster_pairs(decisions, self.ds.listings, self.fps, settings,
                                 frozenset(use_mnl) if settings.operator_must_not_link else frozenset(),
                                 D43.relation_for(settings, self.ds.listings, slots, kc),
                                 must_link=must_link, machine_vetoes=vetoed)
        t3 = time.perf_counter()
        out = {"decisions": decisions, "clusters": clusters, "blocking": blocking,
               "n_pairs": len(pairs), "new_features": new,
               "timing": {"features_s": t1 - t0, "decide_s": t2 - t1, "cluster_s": t3 - t2},
               "facts": FACT_LOG, "price_log": PRICE_LOG}
        FACT_LOG = None
        PRICE_LOG = None
        reset_patches()
        return out


def copairs(clusters) -> set[tuple[int, int]]:
    out = set()
    for members in clusters.clusters.values():
        m = sorted(members)
        for i, a in enumerate(m):
            for b in m[i + 1:]:
                out.add((a, b))
    return out


def summarize(engine: Engine, res: dict) -> dict:
    decisions = res["decisions"]
    zones = Counter(d.zone for d in decisions)
    reasons = Counter(d.reason for d in decisions)
    certs = Counter(d.certificate for d in decisions if d.certificate)
    merge = {(d.lo, d.hi) for d in decisions if d.zone == "merge"}
    cp = copairs(res["clusters"])
    verdict = engine.labels["verdict"]
    same = {k for k, v in verdict.items() if v == "same"}
    diff = {k for k, v in verdict.items() if v == "different"}
    bm = engine.labels["browse_merge"]
    st = res["clusters"].stats
    s = {
        "n_pairs": res["n_pairs"],
        "zones": dict(zones),
        "certificates": dict(certs),
        "merge_pairs": len(merge),
        "groups": st["n_clusters"],
        "clustered": st["n_clustered_listings"],
        "copairs": len(cp),
        "conflicts": st["conflicts_by_invariant"],
        "repartitioned": st["n_components_repartitioned"],
        "label_same_total": len(same),
        "label_same_together": len(same & cp),
        "label_diff_total": len(diff),
        "label_diff_together": len(diff & cp),
        "yardstick_browse_total": len(bm),
        "yardstick_browse_together": len(bm & cp),
        "timing": res["timing"],
        "new_features": res["new_features"],
    }
    if engine.ref:
        s["ref_cd_total"] = len(engine.ref["cd"])
        s["ref_cd_together"] = len(engine.ref["cd"] & cp)
        s["ref_cn_total"] = len(engine.ref["cn"])
        s["ref_cn_together"] = len(engine.ref["cn"] & cp)
    return s, reasons, merge, cp


def diff_against(engine: Engine, base: tuple, arm: tuple) -> dict:
    (_bs, _br, bmerge, bcp) = base
    (_as, _ar, amerge, acp) = arm
    verdict = engine.labels["verdict"]
    gained, lost = acp - bcp, bcp - acp

    def lab(pairs):
        c = Counter(verdict.get(k, "unlabelled") for k in pairs)
        out = dict(c)
        if engine.ref:
            out["ref_cd"] = len(pairs & engine.ref["cd"])
            out["ref_cn"] = len(pairs & engine.ref["cn"])
        return out
    return {
        "merge_pairs_delta": len(amerge) - len(bmerge),
        "merge_gained": len(amerge - bmerge),
        "merge_lost": len(bmerge - amerge),
        "copairs_gained": len(gained),
        "copairs_lost": len(lost),
        "gained_labels": lab(gained),
        "lost_labels": lab(lost),
        "gained_sample": sorted(gained)[:12],
        "lost_sample": sorted(lost)[:12],
    }
