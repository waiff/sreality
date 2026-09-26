"""Feature-family and single-feature ablations over one cohort, each against this cohort's FULL.

    python3 c2_arms.py <cohort> <armset> [armset ...]
    armsets: families, singles, silence, model_off, e11, photo

`model` arms replace the named features' log-odds terms by their cohort mean (mean ablation:
the family carries no pair-to-pair information, the score's level is kept) — every other
consumer (certificates, E11, D43 readers, the demonstration) still reads the feature.
`silence` arms set the features ABSENT everywhere (model, certificates, E11, D43, clustering).
One JSON per arm under OUT/arms_<cohort>/, skipped when present (resumable).
"""
from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np  # noqa: E402

import c2lib  # noqa: E402
from c2lib import ABSENT, FAMILY_OF, FEATURE_ORDER  # noqa: E402

OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c2")

IMG_SUB = {
    "IMG_DHASH": ["phash_tight_matches", "phash_loose_matches", "phash_match_ratio",
                  "seq_monotone_ratio"],
    "IMG_CLIP": ["clip_max_cos", "clip_mean_top3_cos"],
    "IMG_FAMRATIO": ["interior_match_ratio", "exterior_match_ratio", "plan_match_ratio"],
    "IMG_TAG": ["tag_rooms_both", "tag_rooms_private", "tag_tight_matches", "tag_dhash_min",
                "tag_clip_mean", "tag_clip_min", "tag_room_clip_min", "tag_room_clip_min2",
                "tag_lookalike_share", "floorplan_tight_match", "floorplan_conflict"],
    "IMG_META": ["catalog_ratio_max", "n_images_min"],
}
FAMILIES = {fam: [f for f in FEATURE_ORDER if FAMILY_OF[f] == fam and f != "ref_code_shared"]
            for fam in ("ATTR", "PRICE", "TXT", "BRK", "LOC", "IMG", "TIME")}
FAMILIES.update(IMG_SUB)
# W5 arm T/C analogues at feature grain (the W5 arms silenced tags at LOAD time, which also moved
# the anchors and the D43 image facts; these silence the features only).
FAMILIES["IMG_TAG+FAMRATIO"] = IMG_SUB["IMG_TAG"] + IMG_SUB["IMG_FAMRATIO"]
FAMILIES["IMG_CLIPALL"] = IMG_SUB["IMG_TAG"] + IMG_SUB["IMG_FAMRATIO"] + IMG_SUB["IMG_CLIP"]


def run_model_arm(eng, cols, base_cls, features, ops, compensate=True):
    model = c2lib.ablated_model(eng.model, cols, features, compensate)
    drop = c2lib.term_keys_for(eng.model, features)
    scores = c2lib.score_from_terms(eng.model, cols, drop, compensate, n=len(eng.keys))
    cls = c2lib.score_class(eng, scores)
    changed = np.nonzero(cls != base_cls)[0]
    veto = np.array([d.zone == "veto" for d in eng.base.decisions])
    changed = [int(i) for i in changed if not veto[i]]
    _, post = eng.decide_all(model, eng.settings, only=changed, base=eng.base.decisions,
                             scores=scores)
    decisions_changed = sum(1 for a, b in zip(eng.base.decisions, post)
                            if a.zone != b.zone or a.reason != b.reason)
    stored_changed = sum(
        1 for a, b in zip(eng.base.decisions, post)
        if (a.zone in ("merge", "band") or a.score >= eng.settings.store_floor)
        != (b.zone in ("merge", "band") or b.score >= eng.settings.store_floor))
    if decisions_changed == 0 and stored_changed == 0:
        run = c2lib.Run(post, eng.base.clusters, eng.base.member_of, eng.base.conflicts,
                        eng.base.stats)
        res = c2lib.compare(eng, run, ops)
        res["clustered"] = False
    else:
        run = eng.cluster(post, eng.settings)
        res = c2lib.compare(eng, run, ops)
        res["clustered"] = True
    res.update({"class_changed": len(changed), "decisions_changed": decisions_changed,
                "stored_changed": stored_changed,
                "mean_shift_logit": float(sum(cols[k].mean() for k in drop if k in cols)),
                "features": list(features)})
    res["flipped_pairs"] = [[a.lo, a.hi, a.zone, a.reason, b.zone, b.reason]
                            for a, b in zip(eng.base.decisions, post)
                            if a.zone != b.zone][:400]
    return res


def run_silence_arm(eng, features, ops):
    """Features ABSENT everywhere. Exact shortcut: silencing can remove a certificate or an
    auto-reject but never create one, so a model reject whose new score class is unchanged stays
    a reject; everything else (non-rejects, auto-rejects the family feeds, class changes) is
    re-decided in full."""
    names = set(features)

    def transform(f):
        g = dict(f)
        for n in names:
            g[n] = ABSENT
        return g

    P2 = eng.P.copy()
    for n in names:
        P2[:, c2lib.FIDX[n]] = False
    cols2 = c2lib.terms(eng.model, eng.V, P2)
    scores = c2lib.score_from_terms(eng.model, cols2, n=len(eng.keys))
    cols = c2lib.terms(eng.model, eng.V, eng.P)
    base_cls = c2lib.score_class(eng, c2lib.score_from_terms(eng.model, cols, n=len(eng.keys)))
    cls = c2lib.score_class(eng, scores)
    auto_inputs = bool(names & {"attr_contradictions", "numeral_conflict"})
    only = [i for i, d in enumerate(eng.base.decisions)
            if d.zone != "reject" or cls[i] != base_cls[i]
            or (auto_inputs and d.reason.startswith("auto_reject"))
            or not d.reason.startswith(("model", "auto_reject"))]
    _, post = eng.decide_all(eng.model, eng.settings, transform=transform, only=only,
                             base=eng.base.decisions, scores=scores)
    run = eng.cluster(post, eng.settings, transform=transform)
    res = c2lib.compare(eng, run, ops)
    res["features"] = sorted(names)
    res["redecided"] = len(only)
    res["decisions_changed"] = sum(1 for a, b in zip(eng.base.decisions, post)
                                   if a.zone != b.zone or a.reason != b.reason)
    res["flipped_pairs"] = [[a.lo, a.hi, a.zone, a.reason, b.zone, b.reason]
                            for a, b in zip(eng.base.decisions, post)
                            if a.zone != b.zone][:400]
    return res


def run_settings_arm(eng, patch, ops, only=None):
    settings = eng.settings.__class__.from_dict({**eng.settings.to_dict(), **patch})
    _, post = eng.decide_all(eng.model, settings, only=only,
                             base=eng.base.decisions if only is not None else None)
    run = eng.cluster(post, settings)
    res = c2lib.compare(eng, run, ops)
    res["patch"] = patch
    res["decisions_changed"] = sum(1 for a, b in zip(eng.base.decisions, post)
                                   if a.zone != b.zone or a.reason != b.reason)
    res["flipped_pairs"] = [[a.lo, a.hi, a.zone, a.reason, b.zone, b.reason]
                            for a, b in zip(eng.base.decisions, post)
                            if a.zone != b.zone][:400]
    return res


def _attr_only(feats, settings=None):
    limit = settings.max_attr_contradictions
    value, present = feats.get("attr_contradictions", ABSENT)
    return "attr_contradictions" if present and value >= limit else None


def run_patch_arm(eng, attr, replacement, ops, only):
    from autodedup import decide as decide_mod
    original = getattr(decide_mod, attr)
    setattr(decide_mod, attr, replacement)
    try:
        _, post = eng.decide_all(eng.model, eng.settings, only=only, base=eng.base.decisions)
    finally:
        setattr(decide_mod, attr, original)
    run = eng.cluster(post, eng.settings)
    res = c2lib.compare(eng, run, ops)
    res["patched"] = attr
    res["redecided"] = len(only)
    res["decisions_changed"] = sum(1 for a, b in zip(eng.base.decisions, post)
                                   if a.zone != b.zone or a.reason != b.reason)
    res["flipped_pairs"] = [[a.lo, a.hi, a.zone, a.reason, b.zone, b.reason]
                            for a, b in zip(eng.base.decisions, post)
                            if a.zone != b.zone][:400]
    return res


def run_const_arm(eng, value, ops):
    """The learned score replaced by one constant: what the ladder does without the model."""
    model = copy.deepcopy(eng.model)
    for name in model.weights:
        model.weights[name] = 0.0
    for name in model.presence_weights:
        model.presence_weights[name] = 0.0
    model.interactions = []
    model.calibration = None
    model.intercept = float(np.log(value / (1.0 - value)))
    _, post = eng.decide_all(model, eng.settings)
    run = eng.cluster(post, eng.settings)
    res = c2lib.compare(eng, run, ops)
    res["constant_score"] = value
    res["decisions_changed"] = sum(1 for a, b in zip(eng.base.decisions, post)
                                   if a.zone != b.zone or a.reason != b.reason)
    res["merge_reasons"] = {}
    for d in post:
        if d.zone == "merge":
            r = f"cert:{d.certificate}" if d.certificate else d.reason.split(":")[0]
            res["merge_reasons"][r] = res["merge_reasons"].get(r, 0) + 1
    return res


def main() -> None:
    cohort = sys.argv[1]
    armsets = sys.argv[2:]
    arm_dir = OUT / f"arms_{cohort}"
    arm_dir.mkdir(parents=True, exist_ok=True)
    eng = c2lib.Engine.build(cohort, OUT / "cache")
    clock = time.perf_counter()
    eng.baseline()
    print(f"[{cohort}] FULL in {time.perf_counter()-clock:.0f}s", flush=True)
    ops = c2lib.load_operator(eng)
    cols = c2lib.terms(eng.model, eng.V, eng.P)
    base_scores = np.array([d.score for d in eng.base.decisions])
    base_cls = c2lib.score_class(eng, c2lib.score_from_terms(eng.model, cols, n=len(eng.keys)))
    (arm_dir / "FULL.json").write_text(json.dumps(c2lib.compare(eng, eng.base, ops), indent=1))

    arms: list[tuple[str, callable]] = []
    for armset in armsets:
        if armset == "families":
            for fam, feats in FAMILIES.items():
                arms.append((f"model_{fam}", lambda f=feats: run_model_arm(eng, cols, base_cls, f, ops)))
                arms.append((f"zero_{fam}", lambda f=feats: run_model_arm(eng, cols, base_cls, f, ops, False)))
        elif armset == "families_model":
            for fam, feats in FAMILIES.items():
                arms.append((f"model_{fam}", lambda f=feats: run_model_arm(eng, cols, base_cls, f, ops)))
        elif armset == "singles":
            for name in eng.model.feature_order:
                arms.append((f"single_{name}", lambda f=[name]: run_model_arm(eng, cols, base_cls, f, ops)))
        elif armset == "silence":
            for fam, feats in FAMILIES.items():
                arms.append((f"silence_{fam}", lambda f=feats: run_silence_arm(eng, f, ops)))
            arms.append(("silence_ref_code_shared", lambda: run_silence_arm(eng, ["ref_code_shared"], ops)))
        elif armset == "photo":
            for name in ("phash_loose_matches", "tag_rooms_both", "tag_tight_matches",
                         "tag_room_clip_min", "tag_room_clip_min2", "floorplan_tight_match",
                         "floorplan_conflict", "clip_max_cos", "clip_mean_top3_cos",
                         "phash_tight_matches", "interior_match_ratio"):
                arms.append((f"silence1_{name}", lambda f=[name]: run_silence_arm(eng, f, ops)))
        elif armset == "model_off":
            arms.append(("const_reject", lambda: run_const_arm(eng, 0.01, ops)))
            arms.append(("const_band", lambda: run_const_arm(eng, 0.5, ops)))
        elif armset == "rules":
            vetoed = [i for i, d in enumerate(eng.base.decisions) if d.zone == "veto"]
            arms.append(("e61_veto_off", lambda: run_settings_arm(
                eng, {"unit_designator_veto": False}, ops, only=vetoed)))
            auto = [i for i, d in enumerate(eng.base.decisions) if d.reason.startswith("auto_reject")]
            arms.append(("auto_reject_attr_off", lambda: run_settings_arm(
                eng, {"max_attr_contradictions": 99.0}, ops, only=auto)))
            arms.append(("auto_reject_off", lambda: run_patch_arm(
                eng, "auto_reject_reason", lambda feats, settings=None: None, ops, auto)))
            arms.append(("auto_reject_numeral_off", lambda: run_patch_arm(
                eng, "auto_reject_reason", _attr_only, ops, auto)))
        elif armset == "e11":
            from autodedup.features import evidence_families as _ef
            arms.append(("e11_off", lambda: run_patch_arm(
                eng, "evidence_families", lambda feats: _ef(feats) or {"NONE"}, ops,
                list(range(len(eng.keys))))))
            arms.append(("context_rule_off", lambda: run_settings_arm(eng, {"context_rule_enabled": False}, ops)))
        else:
            raise SystemExit(f"unknown armset {armset}")
    for name, fn in arms:
        path = arm_dir / f"{name}.json"
        if path.is_file():
            continue
        clock = time.perf_counter()
        res = fn()
        res["seconds"] = time.perf_counter() - clock
        path.write_text(json.dumps(res, indent=1))
        print(f"[{cohort}] {name}: merge {res['merge_base']}->{res['merge']} flips {res['merge_flips']} "
              f"groups {res['groups_base']}->{res['groups']} diff {res['groups_differing']} "
              f"op_merge {res.get('op_merge_pairs_together_base')}->{res.get('op_merge_pairs_together')} "
              f"op_diff {res.get('op_diff_together_base')}->{res.get('op_diff_together')} "
              f"{res['seconds']:.0f}s", flush=True)


if __name__ == "__main__":
    main()
