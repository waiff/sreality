"""Turn a stored dedup decision's `detail` factor dict into an AUDITABLE breakdown —
one rung per signal (pHash / cosine / forensic verdict / floor-plan / address) carrying
the measured value, the bar it was judged against, whether it was met, and which
operator Settings knob governs it (for a deep-link).

Pure + stateless: a function of the `detail` dict ONLY, so it renders identically for a
terminal decision (`dedup_pair_audit.detail`) and a queued candidate
(`property_identity_candidates.markers_matched`) — and works on historical rows, since
it reads only what the engine already records. The threshold SEMANTICS live here once;
the frontend is a dumb renderer of the rungs.

`settings_keys` reference real `toolkit.dedup_settings` registry keys (validated by a
test), so a rung links to the exact knob the operator can change. pHash min-pairs /
Hamming are CODE constants (not in the registry), so their rung carries no link and says
so — honest about what is and isn't tunable.
"""

from __future__ import annotations

from typing import Any

# Forensic verdicts that mean "merge" (the rest are non-merge). Mirrors the engine's
# verdict_is_merge — kept as a local set so this module is import-light + pure.
_MERGE_VERDICTS = {"High"}

# A floor-plan / site-plan reason can OVERRIDE an otherwise-merging signal, so its rung is
# rendered in addition to the pHash/verdict rung that got the pair to the gate.
_PLAN_REASONS = {
    "floor_plan_different_layout",
    "floor_plan_review",
    "floor_plan_pending",
    "site_plan_different_unit",
}


def _num(v: Any) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build_audit_breakdown(detail: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The ordered rungs for one decision's factor `detail`. Each rung:
    {key, label, value, threshold?, comparator?, status: 'met'|'unmet'|'info',
     settings_keys: [str], note?}. Empty list when there's nothing to show."""
    d = detail or {}
    rungs: list[dict[str, Any]] = []
    reason = d.get("reason") if isinstance(d.get("reason"), str) else None

    # --- Address (exact street + house number + disposition + floor) ---
    if d.get("stage") == "address" or reason == "address_exact":
        bits: list[str] = []
        if isinstance(d.get("street_key"), str):
            bits.append(d["street_key"])
        if d.get("house_number"):
            bits.append(f"č. {d['house_number']}")
        if d.get("floor") is not None:
            bits.append(f"podlaží {d['floor']}")
        rungs.append({
            "key": "address",
            "label": "Shoda adresy",
            "value": " · ".join(bits) if bits else "shoda",
            "status": "met",
            "settings_keys": [],
            "note": "Stejná ulice + číslo + dispozice + podlaží (tolerance plochy 5 %).",
        })

    # --- pHash near-identical photo count ---
    pairs = _num(d.get("phash_pairs"))
    if pairs is not None and (d.get("stage") == "phash" or reason == "image_phash"
                              or d.get("phash_distinctive") or pairs > 0):
        minp = _num(d.get("phash_min_pairs"))
        ham = _num(d.get("phash_threshold"))
        distinctive = bool(d.get("phash_distinctive"))
        met = distinctive or (minp is not None and pairs >= minp)
        note = f"Hammingova vzdálenost páru ≤ {int(ham)}." if ham is not None else None
        if distinctive:
            note = ((note + " ") if note else "") + \
                "Jedna shoda v rozlišující místnosti (kuchyně/koupelna) stačí."
        rungs.append({
            "key": "phash",
            "label": "Shodné fotky (pHash)",
            "value": int(pairs),
            "threshold": int(minp) if minp is not None else None,
            "comparator": "≥",
            "status": "met" if met else "unmet",
            # min-pairs + Hamming are fixed code constants, not operator settings.
            "settings_keys": [],
            "note": ((note + " ") if note else "") + "Pevný práh (v kódu)."
                    if note else "Pevný práh (v kódu).",
        })

    # --- CLIP cosine (routes the forensic model; never gates the merge alone) ---
    cos = _num(d.get("cosine"))
    if cos is not None:
        rungs.append({
            "key": "cosine",
            "label": "CLIP kosinus (směr. modelu)",
            "value": round(float(cos), 4),
            "status": "info",
            "settings_keys": ["dedup_cosine_haiku_min", "dedup_cosine_sonnet_min"],
            "note": "≥ Haiku-práh → levný Haiku; ≥ Sonnet-práh → Sonnet; níže → "
                    "místnost přeskočena (nikdy nesloučí jen podle kosinu).",
        })

    # --- Forensic visual verdict (the auto-merge / auto-dismiss gate) ---
    verdict = d.get("verdict") if isinstance(d.get("verdict"), str) else None
    if verdict and reason not in ("site_plan_different_unit",):
        if verdict in _MERGE_VERDICTS:
            status, skeys = "met", ["llm_visual_match_model"]
        elif verdict == "Low":
            status = "unmet"
            skeys = ["dedup_forensics_autodismiss_enabled", "llm_visual_match_model"]
        else:
            status, skeys = "info", ["llm_visual_match_model"]
        rungs.append({
            "key": "verdict",
            "label": "Forenzní verdikt (vize)",
            "value": verdict,
            "status": status,
            "settings_keys": skeys,
            "note": "Sloučí pouze 'High'; jisté 'Low' v rozlišující místnosti zamítne."
                    if status != "info" else
                    "Nejednoznačný verdikt — pár jde do fronty k ruční kontrole.",
        })

    # --- Floor-plan validation gate (can override even a High) ---
    if reason in ("floor_plan_different_layout", "floor_plan_review", "floor_plan_pending"):
        if reason == "floor_plan_different_layout":
            value, status = "různé dispozice", "unmet"
            note = "Validace půdorysu zamítla sloučení: dispozice se neshodují."
        elif reason == "floor_plan_review":
            value, status = "nejednoznačný 2D plán", "info"
            note = ("Obě strany mají použitelný 2D půdorys, ale porovnání je nejednoznačné "
                    "— odesláno k ruční kontrole. (Chybějící / jen 3D vizualizace už blokem "
                    "není — sloučení proběhne podle fotek.)")
        else:
            value, status = "čeká na ověření", "info"
            note = "Verdikt půdorysu zatím nedostupný — odloženo na další běh."
        rungs.append({
            "key": "floor_plan",
            "label": "Validace půdorysu",
            "value": value,
            "status": status,
            "settings_keys": [
                "llm_floor_plan_match_model",
                "dedup_floor_plan_inconclusive_to_review",
            ],
            "note": note,
        })

    # --- Site-plan development guard ---
    if reason == "site_plan_different_unit":
        rungs.append({
            "key": "site_plan",
            "label": "Situační plán",
            "value": "jiná jednotka",
            "status": "unmet",
            "settings_keys": ["llm_site_plan_match_model"],
            "note": "Plány zvýrazňují různé jednotky téhož projektu — do fronty.",
        })

    return rungs


def referenced_settings_keys() -> set[str]:
    """Every app_settings key any rung deep-links to — for a registry-coverage test."""
    keys: set[str] = set()
    for sample in (
        {"stage": "phash", "reason": "image_phash", "phash_pairs": 2,
         "phash_min_pairs": 2, "phash_threshold": 6, "cosine": 0.9, "verdict": "High"},
        {"verdict": "Low", "reason": "visual_different"},
        {"reason": "floor_plan_different_layout"},
        {"reason": "site_plan_different_unit", "verdict": "different_unit"},
    ):
        for rung in build_audit_breakdown(sample):
            keys.update(rung.get("settings_keys") or [])
    return keys
