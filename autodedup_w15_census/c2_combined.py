"""Composed deletion arms (the C2 ranked list applied together), full re-decide + re-cluster,
each against the cohort's FULL. Spec: OUT/combined_spec.json = {arm: {model_ablate: [...],
settings: {...}, patches: [...], disable_readers: [...], const_score: null|float}}.

    python3 c2_combined.py <cohort> [<cohort> ...]
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
from autodedup import decide as decide_mod  # noqa: E402
from autodedup.features import evidence_families  # noqa: E402
from autodedup.settings import Settings  # noqa: E402

OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c2")
def _refusal_every_limb(la, lb, feats, settings):
    """E164 as its docstring states it: every D50 limb is read; only MISSING ones are waived."""
    from autodedup import demonstrate as D
    from autodedup.indistinguishable import overlap_days, price_paths_agree

    if not settings.demonstrate_identity:
        return None
    paths_agree = price_paths_agree(la, lb, settings.d43_price_path_tol)
    overlap = overlap_days(la, lb)
    gaps = []
    printed = settings.demonstrate_area_printed_decides
    if not D.area_demonstrated(la, lb, printed):
        gaps.append(("area", D._area_kind(la, lb, printed)))
    if settings.demonstrate_require_disposition and not D.disposition_demonstrated(la, lb):
        gaps.append(("disposition", D.MISSING if (la.disposition is None or lb.disposition is None)
                     else D.CONTRADICTION))
    if not D.price_demonstrated(la, lb, settings, paths_agree, overlap):
        priced = bool(la.price and lb.price and float(la.price) > 0 and float(lb.price) > 0)
        gaps.append(("price", D.CONTRADICTION if priced else D.MISSING))
    if (settings.demonstrate_require_obec and not D.obec_demonstrated(la, lb)
            and not (settings.demonstrate_obec_one_text_sequential
                     and D.one_text_sequential(la, lb, settings))):
        known = la.location.obec_kod is not None and lb.location.obec_kod is not None
        gaps.append(("obec", D.CONTRADICTION if known else D.MISSING))
    if settings.demonstrate_onesided:
        one = D.onesided_fact(la, lb, settings)
        if one is not None:
            gaps.append((f"onesided_{one}", D.CONTRADICTION))
    strong = None
    for gap, kind in gaps:
        if kind == D.MISSING and settings.demonstrate_recover_missing:
            if strong is None:
                strong = D.strong_corroboration(la, lb, feats, settings) is not None
            if strong:
                continue
        return f"A:{gap}"
    if D.corroboration_warrant(la, lb, feats, settings) is None:
        return "B"
    return None


PATCHES = {
    "e164_every_limb": ("demonstration_refusal", _refusal_every_limb),
    "e11_off": ("evidence_families", lambda feats: evidence_families(feats) or {"NONE"}),
    "d50_pair_off": ("demonstration_refusal", lambda *a, **k: None),
}


def const_model(model, value: float):
    out = copy.deepcopy(model)
    for name in out.weights:
        out.weights[name] = 0.0
    for name in out.presence_weights:
        out.presence_weights[name] = 0.0
    out.interactions = []
    out.calibration = None
    out.intercept = float(np.log(value / (1.0 - value)))
    return out


def main() -> None:
    spec = json.loads((OUT / "combined_spec.json").read_text())
    for cohort in sys.argv[1:]:
        out_dir = OUT / f"combined_{cohort}"
        out_dir.mkdir(parents=True, exist_ok=True)
        eng = c2lib.Engine.build(cohort, OUT / "cache")
        eng.baseline()
        ops = c2lib.load_operator(eng)
        cols = c2lib.terms(eng.model, eng.V, eng.P)
        for name, arm in spec.items():
            path = out_dir / f"{name}.json"
            if path.is_file():
                continue
            clock = time.perf_counter()
            settings = Settings.from_dict({**eng.settings.to_dict(), **arm.get("settings", {})})
            model = eng.model
            if arm.get("model_ablate"):
                model = c2lib.ablated_model(model, cols, arm["model_ablate"])
            if arm.get("const_score") is not None:
                model = const_model(model, float(arm["const_score"]))
            originals = {}
            for patch in arm.get("patches", []):
                attr, fn = PATCHES[patch]
                originals[attr] = getattr(decide_mod, attr)
                setattr(decide_mod, attr, fn)
            c2lib.DISABLED.clear()
            c2lib.DISABLED.update(arm.get("disable_readers", []))
            silenced = set(arm.get("silence", []))

            def transform(f, silenced=silenced):
                g = dict(f)
                for n in silenced:
                    g[n] = c2lib.ABSENT
                return g

            tf = transform if silenced else None
            try:
                _, post = eng.decide_all(model, settings, transform=tf)
                run = eng.cluster(post, settings, transform=tf)
            finally:
                for attr, fn in originals.items():
                    setattr(decide_mod, attr, fn)
                c2lib.DISABLED.clear()
            res = c2lib.compare(eng, run, ops)
            res["arm"] = arm
            res["seconds"] = time.perf_counter() - clock
            res["flipped_pairs"] = [[a.lo, a.hi, a.zone, a.reason, b.zone, b.reason]
                                    for a, b in zip(eng.base.decisions, post)
                                    if a.zone != b.zone][:500]
            path.write_text(json.dumps(res, indent=1))
            print(f"[{cohort}] {name}: merge {res['merge_base']}->{res['merge']} flips "
                  f"{res['merge_flips']} groups {res['groups_base']}->{res['groups']} diff "
                  f"{res['groups_differing']} cp +{res['copairs_gained']}/-{res['copairs_lost']} "
                  f"opS {res['op_same_together_base']}->{res['op_same_together']} "
                  f"opD {res['op_diff_together_base']}->{res['op_diff_together']} "
                  f"{res['seconds']:.0f}s", flush=True)
        del eng


if __name__ == "__main__":
    main()
