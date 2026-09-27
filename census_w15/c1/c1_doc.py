"""C1: print C1_rules.md (python3 c1_doc.py > ../../C1_rules.md). Numbers come from the data
files through c1_write's helpers; re-run after new arms land."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from c1_write import APPLY, ARMS, BASE, FF, d, f3, fr, ref  # noqa: E402
import json as _json
_H = Path("/home/hejtm/autodedup-artifacts/w15/census/c1/runs")
HARNESS = {}
for _c, _f in (("trial", "trial_bundle_vs_base.json"), ("c17", "c17_bundle_vs_base.json"), ("c18", "c18_bundle_vs_base.json")):
    if (_H / _f).exists():
        _x = _json.loads((_H / _f).read_text())
        HARNESS.setdefault("bundle:c1_code_bundle_settings", "")
        HARNESS["bundle:c1_code_bundle_settings"] += (f"{_c}: cp+{_x['cp_gained']}/−{_x['cp_lost']} groups {_x['base']['groups']}→{_x['arm']['groups']}; ")

L = []
P = L.append

tb, cb = BASE["trial"], BASE["c17"]
c18b = BASE.get("c18") or {}

P("# C1 — rules and layers census (W0, AUTODEDUP simplification sprint)")
P("")
P("Data for the coordinator. Every number is reproducible from `/home/hejtm/autodedup-artifacts/w15/census/c1/` (§9).")
P("Code pointers are `file:line` under `autodedup/` at **origin/main b2454fa1**; the decision code (`decide`, `guards`,")
P("`indistinguishable`, `demonstrate`, `d43`, `cluster`, `repartition`, `features`) is byte-identical at 092ce2ca (A1's")
P("pointers hold). Newer main afd121ae touches only lane/apply/ui (`apply.py` group-verdict newest-wins, `incremental.py`")
P("G4 ruling-change seeds, E919/E920); the lane/apply pointers below are b2454fa1's.")
P("")
P("## 0. The answer in numbers")
P("")
_n_t = len(ARMS["trial"]); _n_17 = len(ARMS["c17"]); _n_18 = len(ARMS["c18"]); _n_r = len(ARMS["trialr"])
P(f"- **Measured**: {_n_t} single-rule arms on the trial export (5,195 adverts; w31 + w6_gold; engine alone: no must-link, no")
P("  operator must-not-link loaded, so the operator's rulings stay out of the input and serve as the yardstick),")
P(f"  {_n_17} arms on cohort 17 (17,897 adverts), {_n_18} on cohort 18 (17,422 adverts, final check), {_n_r} trial arms re-read")
P("  against the label-free reference, and the deletion bundle executed as CODE through the official `harness run`")
P("  (trial + c17). The in-process replay is faithful: clusters, conflicts and reasons identical to `python3 -m")
P("  autodedup.harness run` on the trial (which equals the stored g15 `pairs.jsonl.gz` byte for byte), and the c18 replay")
P("  reproduces every stored c18 w31 reason count and all 3,754 groups.")
P(f"- **Baselines (engine alone)**: trial merge edges {tb.get('merge_pairs')}, groups {tb.get('groups')}, co-pairs {tb.get('copairs')};"
  f" operator-same together {tb.get('label_same_together')}/{tb.get('label_same_total')}, operator-different together"
  f" {tb.get('label_diff_together')}/{tb.get('label_diff_total')}, Browse-merge yardstick {tb.get('yardstick_browse_together')}/"
  f"{tb.get('yardstick_browse_total')}. c17: merge {cb.get('merge_pairs')}, groups {cb.get('groups')}, co-pairs {cb.get('copairs')};"
  f" label-free reference (truth16 builder, `cohort17_read/ref`) certain duplicates together {cb.get('ref_cd_together')}/"
  f"{cb.get('ref_cd_total')} (95.1 %), structural negatives together {cb.get('ref_cn_together')}/{cb.get('ref_cn_total')};"
  f" operator labels on c17: {cb.get('label_same_total')} same, {cb.get('label_diff_total')} different (the rulings live in the trial area).")
if c18b:
    P(f"- c18 (replay, engine alone): merge {c18b.get('merge_pairs')}, groups {c18b.get('groups')}, co-pairs {c18b.get('copairs')};"
      f" CD together {c18b.get('ref_cd_together')}/{c18b.get('ref_cd_total')}, CN together {c18b.get('ref_cn_together')}/{c18b.get('ref_cn_total')}.")
_ta = ARMS["trial"]
_tv = {k: v for k, v in _ta.items() if "invalid" not in v}
_tz = [k for k, v in _tv.items() if v["dmerge"] == 0 and v["dband"] == 0 and v["cp_gained"] == 0 and v["cp_lost"] == 0 and v["dgroups"] == 0]
_tzg = [k for k, v in _tv.items() if v["cp_gained"] == 0 and v["cp_lost"] == 0 and v["dgroups"] == 0]
_nf = sum(1 for k in _tv if k.startswith("fact:")); _nfz = sum(1 for k in _tz if k.startswith("fact:"))
_nd = sum(1 for k in _tv if k.startswith("dial:")); _ndz = sum(1 for k in _tz if k.startswith("dial:"))
P(f"- **{len(_tz)} of {len(_tv)} valid trial arms change not one pair's zone; {len(_tzg)} change no group.** Zero at group grain on the trial:")
P(f"  {_nfz}/{_nf} fact readers, {_ndz}/{_nd} valid D43/D50/floor/repartition dials, E61, E48, E63, the D43 gate, 4/6 set-level cluster")
P("  invariants, the whole repartition repair ensemble, and the category walls (their pairs are caught downstream).")
P("- **The code bundle** (E61 veto, decide-time wall re-check, K-A, E48 strata, E45/E46/E47, E11, E63, D43 gate, D65,")
P("  cluster size/category_type/compat_class/area_spread/disposition/floor-spread limbs, repartition repairs; branch")
P("  `census/w15-c1-bundle` = −88/+7 lines in `decide.py`+`guards.py` before any dead function is removed):")
P("  **trial 0 co-pairs gained / 0 lost (groups 1,134 = 1,134)**; c17 " + d("bundle:c1_code_bundle_settings", ("c17",)).split(" ‖ ")[0]
  + " of 42,924 co-pairs (official harness on the code = its settings/patch twin, exactly); c18 (the twin) " + d("bundle:c1_code_bundle_settings", ("c18",)).split(" ‖ ")[0] + " of 25,088. On c17 and c18 the whole")
P("  delta is the repartition repairs (identical to the repairs-only arm); every other bundle member is exactly 0 on both.")
P("- **What the ladder actually runs on** (merge edges g15 / c17 / c18): D43 promotion " + f3("merge_by:D43_promote")
  + "; K-C " + f3("merge_by:K-C") + "; model cut 1,942 / 7,794 / 7,995; K-B " + f3("merge_by:K-B") + "; K-R " + f3("merge_by:K-R") + ".")
P("  Removing any single certificate or the model cut moves ≤ 177 merge edges on c17 (another rung re-derives the pair);")
P("  removing D43 promotion loses 22,502 co-pairs on c17 (11,699 certain duplicates). **Load-bearing: D43 promotion + the")
P("  D50 demonstration (incl. its E164 recover-missing waiver), the certificates as a group, the D43 cluster relation (incl.")
P("  E157 price + image facts) and the base repartition. Everything else is a duplicate of one of those, a sub-limb")
P("  excuse of one reader, or dormant.**")
P("")
P("## 1. Method")
P("")
P("- `scripts/c1_engine.py`: `harness.run_engine` re-run in-process (fingerprints → blocking → `pair_features` →")
P("  `decide_pair` → `storable` → `relation_for` → `cluster_pairs`, w31 has the E85/E88 deferred path off), features cached")
P("  per cohort; rules without a dial are ablated by a named patch (a wall dropped from `pair_veto` at blocking AND decide;")
P("  one fact name filtered out of `distinguishing_facts` in all three readings; one A-limb of D50 waived with the later")
P("  limbs still read; one set-level invariant neutralised with the later limbs still read). An arm touching nothing")
P("  `_decide_layers` reads re-uses the base layer decisions (exactly equivalent).")
P("- Coupled dials: 126 validator `raise`s make dials depend on dials; an arm switches off every dial the validator names")
P("  as depending on it (`coupled_off` in the arm row) — a rule cannot be ablated without its dependants.")
P("- Yardsticks: (1) operator rulings only (labels_g13: `operator_labels.jsonl` + `operator_merges.jsonl` +")
P("  `must_not_link.jsonl`, latest wins per pair): opSame/opDiff = change in operator-same / operator-different pairs that")
P("  end in one group; Browse = the 363 Browse-merge groups (the `yardstick.py` population). (2) Label-free, engine-free")
P("  reference (the program's truth16 builder, already built for c17/c18 and for the g13 trial export): CD = certain")
P("  duplicates (shared rare order code, ≥4 exact non-stock frames, identical ≥300-char body), CN = structural negatives")
P("  (two printed unit numbers / a stated-area conflict). CD overlaps the certificates by construction (read certificate")
P("  arms' CD with that in mind); CN overlaps D43 facts.")
P("- Notation: `m` merge edges Δ, `g` groups Δ, `cp+a/−b` co-pairs (two adverts in one group) gained/lost vs baseline;")
P("  `trial ‖ c17` (‖ c18 where run); `·` not run. Fires = `g15 / c17 / c18`: g15 and c18 from the STORED runs")
P("  (`score_g15/autodedup-score-36244048665`, `cohort18_runs/w31`), c17 from the w31 replay (no w31 run was stored for")
P("  c17; `cohort17_runs/w30` is w30; walls for c17 are read off the w30 run, whose blocking is identical: 247,900 pairs).")
P("")
P("## 2. The pair ladder, in the order the code runs")
P("")
P("| # | rule (layer) | code | protects against | fires g15 / c17 / c18 | same fact/signal also compared in | ablation Δ trial ‖ c17 | verdict |")
P("|---|---|---|---|---|---|---|---|")
rows = [
    ("W1", "wall category_type (E2) — retrieval", "guards.py:69-71; called blocking.py:202, incremental.py:642, decide.py:708, yardstick.py:286, town_probe.py:175",
     "sale merged with rent", f3("category_type", "wall") + " lookups refused",
     "D43 reader `category_type` indistinguishable.py:2619; cluster invariant guards.py:205-207; apply `category_type_mix` apply.py:568-573; chokepoint toolkit/property_identity; probe keys carry deal type",
     d("wall:category_type") + "; band +112", "MERGE-INTO the one fact comparator (keep as retrieval slot filter, one definition)"),
    ("W2", "wall category_main (E3)", "guards.py:72-73", "flat merged with house (dům↔komerční allowed)", f3("category_main", "wall"),
     "D43 `category_main` indist:2624; cluster `compat_class` guards.py:209-213; apply `category_main_incompatible` apply.py:574-579; chokepoint; cat_group probe keys",
     d("wall:category_main") + "; band +229", "MERGE-INTO (as W1)"),
    ("W3", "wall area >8 % (E5)", "guards.py:74-75, area_relation 49-61", "two sizes merged", f3("area", "wall"),
     "feature area_rel_diff (model), K-B ≤1 %, K-C ≤3 %, K-A ≤2 %, E63 ≤1 %, D43 `area` (gate/cluster 8 %, promote 3 %), `stated_area`/`printed_area`/`headline_area`, D50 A:area, `agreeing_attributes`, ATTR family (exact/1 %/2 %), cluster `area_spread` 8 %, attr_area/town probe bands — 12 places, 7 thresholds",
     d("wall:area"), "MERGE-INTO (retrieval filter only: −15 merges come from cap-slot displacement, not from a decision)"),
    ("W4", "wall disposition", "guards.py:76-79", "2+kk merged with 3+kk", f3("disposition", "wall"),
     "feature dispo_equal; K-C/K-A; D43 `disposition` indist:2646; D50 A:disposition; `agreeing_attributes`; cluster `disposition` spread (off); attr_dispo/town probe keys",
     d("wall:disposition") + "; band +258", "MERGE-INTO"),
    ("W5", "wall floor ≥2 (flats)", "guards.py:80-83", "two flats of one building", f3("floor", "wall"),
     "features floor_diff + same-portal one-floor; K-A; D43 `floor` (camps) + total_floors/prose_floor/subject_floor/storey_word/offered_storey; numeral_conflict (floor); cluster floor spread (off); `agreeing_attributes`",
     d("wall:floor") + "; band +67; all five walls off: " + d("wall:ALL", ("trial",)), "MERGE-INTO (a floor fact is D43's; the ≥2 wall is a second, looser definition)"),
    ("L1", "decide-time wall re-check (7.1)", "decide.py:708-710", "pair reaching decide without retrieval", "0 / 0 / 0",
     "W1-W5 at retrieval", "0 by construction", "DELETE"),
    ("L2", "E61 unit-designator veto", "decide.py:712-720; guards.py:87-115; machine vetoes cluster.py:340-347, harness.py:521,629, incremental.py:1302,1630-1635",
     "developer twin flats (byt č. 3 vs č. 5)", f3("veto:unit_designator_conflict"),
     "D43 reader `unit_designator` indistinguishable.py:2745-2749 — the SAME predicate (one designator per side, differ, same address block); it never fires only because E61 vetoes first",
     d("E61:unit_designator_veto") + "; cluster-only part: " + d("E61:cluster_machine_veto_only", ("trial",)), "DELETE (the D43 reader carries it; the pair becomes band, reviewable, instead of an invisible veto)"),
    ("L3", "auto-reject attr_contradictions ≥3", "decide.py:433-442, 725-727", "look-alikes stating 3 different extra attributes", f3("auto_reject:attr_contradictions"),
     "model feature `attr_contradictions` (features.py:1453) — the rule is a hard cut on a feature the score already weighs; some slots are D43 facts (floor, cellar, accessory)",
     d("auto_reject:attr_contradictions", ("trial",)), "MERGE-INTO the model (measure as a loosening)"),
    ("L4", "auto-reject numeral_conflict", "decide.py:439-441; features.py:1538", "texts stating different floor/rooms/unit numbers", f3("auto_reject:numeral_conflict"),
     "D43 `prose_floor`, `subject_floor`, `unit_code`, `storey_word` read the same prose; model feature `numeral_conflict`",
     d("auto_reject:numeral_conflict", ("trial",)) + "; both auto-rejects off: " + d("auto_reject:BOTH") + " (trialr " + ref("auto_reject:BOTH") + ")",
     "MERGE-INTO D43 readers (a loosening: c17 +50 co-pairs, 24 CD, 0 CN; needs the confirmation discipline)"),
    ("L5", "K-R certificate (E60)", "decide.py:228-241, 266-267; features ref_code index features.py:863", "the broker's own order key", f3("cert_fired:K-R") + " (merged " + f3("merge_by:K-R") + ")",
     "`ref_code_shared` also read by D50 strong_corroboration (demonstrate.py:684), corroboration 'code' (618), unit_grade_warrant (indist:3216), D43 agency_code readers (off), E45 ref arm (off)",
     d("cert:K-R") + " (trialr " + ref("cert:K-R") + ")", "KEEP as proof rung, but note: 0 merge edges on the trial are K-R-only; on c17 25"),
    ("L6", "K-A certificate", "decide.py:143-153, 269-270", "(off: 52.6 % precision)", "0 / 0 / 0",
     "features same_ruian_adm_kod, dispo, area, floor", "0 by construction", "DELETE"),
    ("L7", "K-B certificate (E6/E7) + E84 gap", "decide.py:190-206, 126-140", "a broker's own re-post", f3("cert_fired:K-B") + " (merged " + f3("merge_by:K-B") + ")",
     "same_source/same_broker/containment ≥0.90/area ≤1 %/disjoint windows: one of 19 overlap/clock functions (§7); containment ≥0.90 = E63, E45 repost arm, D43 price-sequential",
     d("cert:K-B") + "; E84 gap off (with its coupled honest clock): " + d("cert:K-B_gap_E84", ("trial",)), "KEEP (proof rung)"),
    ("L8", "E65 K-B photo floor", "decide.py:156-187", "(off)", "0 / 0 / 0", "n_images_min, phash_loose_matches", "0 by construction", "DELETE (2 dials)"),
    ("L9", "K-C certificate", "decide.py:209-225", "the same photo shoot on two portals", f3("cert_fired:K-C") + " (merged " + f3("merge_by:K-C") + ")",
     "tight ≥4 also: E164 recover (≥3), D43 cellar yield (≥4), price-sequential (≥3), promote photo warrant (≥1), D50 B photo (≥1), E63 images (≥4); area ≤3 % = D43 promote area bar",
     d("cert:K-C") + " (trialr " + ref("cert:K-C") + ")", "KEEP (proof rung)"),
    ("L10", "E48 per-stratum cut / propose-only", "decide.py:406-430, 732-734, 745", "a stratum that could not prove its bar", "0 / 0 / 0 propose-only",
     "w31 cells: K-A (dead), `K-C\\|same: 0.0` (never compared: certificates only test `is None`), `model\\|same` = `model\\|cross` = 0.9788; global `t_hi` 1.0 never read",
     d("E48:strata_table->t_hi"), "DELETE (one scalar t_hi = 0.9788)"),
    ("L11", "E45/E46/E47 merge-zone gates", "decide.py:278-399, 445-453", "(off)", "0 / 0 / 0", "unit arms re-read K-B/K-R/interior", "0 by construction", "DELETE (~12 dials)"),
    ("L12", "E11 evidence-family gate (min 1)", "decide.py:729, 738-743, 749-752", "images alone merging", "0 / 0 / 0 `evidence_gate`",
     "families (features.py:1675) stay as reporting; min 1 is vacuous for every certificate/cut pair", "dial cannot be 0 (validator); fires 0", "DELETE"),
    ("L13", "model cut t_hi 0.9788", "decide.py:745-752", "merging on the learned score", "1,942 / 7,794 / 7,995 model merges",
     "D43 promotion re-derives most model merges (only 39 / 177 are model-only)", d("cut:model_merge_authority") + " (trialr " + ref("cut:model_merge_authority") + ")",
     "KEEP (score rung) — but it and D43 promotion are two roads to one outcome (§7)"),
    ("L14", "band floor t_lo 0.1823", "decide.py:753-755", "band (review/promotion pool) growing", "band(model) 1,488 / 10,436 / 8,138",
     "store_floor 0.02 (store), E25 budget", "t_lo→0.02: " + d("zone:t_lo_floor->store_floor"), "KEEP (the review-budget dial; promotions below it are ~0)"),
    ("L15", "E63 context rule (+E64 rail)", "decide.py:456-540; hazard_context.py (401 lines); incremental.py:1655-1708", "(promote band pairs with identical text+price)", "0 / 0 / 0",
     "containment ≥0.90 (K-B), price ratio (D50 A:price), area ≤1 % (K-B)", d("E63:context_rule"), "DELETE (+10 dials, hazard census)"),
    ("L16", "D43 gate (demote a merge on any stated fact)", "decide.py:543-571; indistinguishable GATE reading", "certified merge across a stated fact", f3("D43_gate_demote"),
     "the D43 CLUSTER reading re-reads every fact on the group (d43.py:86-114) — a demoted edge and a refused union give the same groups",
     d("D43:gate"), "DELETE (and with it one of the three readings)"),
    ("L17", "D43 promotion (band → merge when no fact differs)", "decide.py:572-588; indist:3187-3213", "missing the duplicates the score under-rates", f3("merge_by:D43_promote"),
     "the model cut (L13) reaches most of the same pairs", d("D43:promote"), "KEEP (load-bearing)"),
    ("L17a", "  warrant: agree:2 / photo / unit (E131/E191)", "indistinguishable.py:3127-3246", "promoting pairs that state nothing", "agree " + f3("D43_promote:agree:2") + "; photo " + f3("D43_promote:photo") + "; unit " + f3("D43_promote:unit*"),
     "`agreeing_attributes` counts price and floor when both are STATED, not equal (indist:3155,3166); D50 A then demands area+disposition+price+obec agreement and B unit-grade evidence (demonstrate.py:449-477, 627-662)",
     d("D43:warrant->predicate") + "; photo alt off " + d("D43:warrant_photo_alt", ("trial",)) + "; unit off " + d("D43:warrant_unit_evidence", ("trial",)),
     "MERGE-INTO D50 (the warrant blocks 13 / 60 edges and 20 / 0 co-pairs; 4 dials)"),
    ("L18", "D50 demonstration (E157/E158)", "decide.py:639-664; demonstrate.py:295-477, 578-702", "promotion on the mere absence of a fact", "refusals " + f3("D50_refuse:*"),
     "A re-reads area/disposition/price/obec (all D43 facts too) at stricter bars; B re-reads K-C/K-B/K-R evidence", d("D50:demonstration") + " (trialr " + ref("D50:demonstration") + ")",
     "KEEP (load-bearing) — but see L18a-g"),
    ("L18a", "  A:area", "demonstrate.py:178-186, 458-460", "promoting across an unread area", f3("D50_refuse:A:area"), "D43 area (promote 3 %), printed/stated area", d("D50:A:area", ("trial",)), "KEEP"),
    ("L18b", "  A:disposition", "demonstrate.py:187-195, 461-463", "", f3("D50_refuse:A:disposition"), "W4 wall + D43 disposition (both need both stated; A also refuses a MISSING side)", d("D50:A:disposition"), "KEEP (c17 +242 edges); fold its MISSING case into E164"),
    ("L18c", "  A:price (exact / rounding / path / sequential)", "demonstrate.py:295-337", "co-live neighbouring units at close prices", f3("D50_refuse:A:price"), "D43 price (5 %/60 %), E157 cluster price, E63 ratio, features price_last_ratio/ppm2/path", d("D50:A:price", ("trial",)), "KEEP"),
    ("L18d", "  A:obec", "demonstrate.py:196-199, 467-473", "", f3("D50_refuse:A:obec"), "blocking block key; D43 obec/obec_prose/body_obec", d("D50:A:obec"), "KEEP (c17 +32 co-pairs, 0 CD)"),
    ("L18e", "  A:onesided (E162)", "demonstrate.py:578-607", "development twins printing a unit fact on one side", "onesided " + f3("D50_refuse:A:onesided*"), "D43 unit_code/floor/printed_area", d("D50:A:onesided", ("trial",)), "KEEP (small)"),
    ("L18f", "  E164 recover-missing waiver", "decide.py:655-660; demonstrate.py:665-702", "losing re-posts that state nothing", "n/a (a waiver)", "strong_corroboration = K-C/K-R/K-B evidence again", d("D50:A:recover_missing", ("trial",)), "KEEP (load-bearing: −407 co-pairs, 25 opSame if off)"),
    ("L18g", "  B corroboration (+ identical twin)", "demonstrate.py:610-662, 265-292", "promotion with no unit-grade evidence", f3("D50_refuse:B"), "photo/body/code = certificate evidence again", "B off " + d("D50:B:corroboration", ("trial",)) + "; twin off " + d("D50:B:identical_twin"), "KEEP B; twin KEEP (c17 −12 co-pairs, 4 CD)"),
    ("L19", "D65 merge policy (category hold)", "decide.py:589-636, 690", "(empty table since Decision 2)", "0 / 0 / 0", "apply scope row category_types", "0 by construction", "DELETE (+ the 15 `*_hold.json` settings files)"),
    ("L20", "F3/E93/E908 evidence hold (lane only)", "incremental.py:157-235, 1256-1270; incremental_lane.py:286", "deciding on photos not yet processed", "live only: 118 holds in the G1 read (TRIAL_LIVE.md:194)",
     "the batch/harness path has no equivalent (withheld-evidence replay in rt_equivalence.py only)", "n/a offline", "KEEP the behaviour, MOVE into decide_pair as an input (one code path)"),
]
for r in rows:
    P("| " + " | ".join(r) + " |")
P("")
P("## 3. The D43 fact readers (64 names emitted by `distinguishing_facts`, indistinguishable.py:2591-3109)")
P("")
P("Fires are `any/sole` pairs in the gate / promote / cluster readings (sole = the only fact on that pair; a reader with")
P("sole 0 everywhere cannot change any decision alone). Ablation = the fact name filtered out of all three readings.")
P("`FACT_NAMES` (indistinguishable.py:209-258) lists 47 names; 64 are emitted — the registry is stale (17 unlisted).")
P("")
P("| reader | fires trial \\| c17 \\| c18 (gate promote cluster, any/sole) | ablation Δ trial ‖ c17 | verdict |")
P("|---|---|---|---|")
names = sorted(FF["trial"]) if FF["trial"] else []
DEL_READERS, CAND_READERS, MEAS_READERS = [], [], []
for n in names:
    a = ARMS["trial"].get("fact:" + n, {})
    zero_t = a and "invalid" not in a and a["dmerge"] == 0 and a["cp_gained"] == 0 and a["cp_lost"] == 0 and a["dgroups"] == 0
    sole17 = any(FF["c17"].get(n, {}).get(m, [0, 0])[1] > 0 for m in ("gate", "promote", "cluster"))
    sole18 = any(FF["c18"].get(n, {}).get(m, [0, 0])[1] > 0 for m in ("gate", "promote", "cluster")) if FF["c18"] else None
    a17 = ARMS["c17"].get("fact:" + n)
    zero17 = (not sole17) or (a17 and "invalid" not in a17 and a17["cp_gained"] == 0 and a17["cp_lost"] == 0 and a17["dgroups"] == 0)
    if n in ("unit_designator", "category_type", "category_main", "disposition"):
        v = "KEEP (the one definition once E61 / the walls' re-checks / C6-C7 go; ~0 today only because a duplicate fires first)"
    elif zero_t and zero17 and not sole18:
        v = "DELETE (0 trial, 0 c17" + (", no sole fire c18)" if sole18 is False else ")")
        DEL_READERS.append(n)
    elif zero_t and zero17:
        v = "DELETE-candidate (0 trial, 0 c17; sole fire on c18)"
        CAND_READERS.append(n)
    elif zero_t:
        v = "keep-measure (0 trial, moves c17)"
        MEAS_READERS.append(n)
    else:
        v = "KEEP (moves groups)"
    P(f"| {n} | {fr(n)} | {d('fact:' + n)} | {v} |")
P("")
P("## 4. Cluster invariants and closures (cluster.py, repartition.py, d43.py, guards.py:180-245)")
P("")
P("| # | rule | code | protects against | fires g15 / c17 / c18 | also compared in | ablation Δ trial ‖ c17 | verdict |")
P("|---|---|---|---|---|---|---|---|")
crow = [
    ("C1", "must-link closures (E910)", "cluster.py:164-211, 342-349", "engine splitting an operator merge", "g15: 1,251 pairs, 41 closures, 0 dissolved; c17/c18 not loaded offline", "apply operator checks", "n/a (operator input)", "KEEP"),
    ("C2", "closure validity (size/type/category/MNL)", "cluster.py:191-211", "operator rulings contradicting each other", "0 dissolved (g15)", "the set invariants C5-C8 with relation=None", "n/a", "KEEP, re-expressed on the D43 relation once C5-C8 go"),
    ("C3", "must-not-link (operator + E61 machine vetoes)", "guards.py:236-241; cluster.py:345-347", "joining what the operator split", "conflicts " + f3("must_not_link", "cl"), "apply `operator_pair_verdict`/`must_not_link`; lane rulings read", "machine part: " + d("E61:cluster_machine_veto_only"), "KEEP operator part; machine part goes with E61"),
    ("C4", "D43 cluster relation (E132) incl. image facts + E157 price", "d43.py:86-114; guards.py:243-244", "A=B, B=C each clean while A≠C", "conflicts " + f3("d43_distinguishable", "cl"),
     "the same readers as the gate (L16) and promote readings; E157 = D50 A:price at cluster grain", d("C:d43_cluster_invariant") + " (trialr " + ref("C:d43_cluster_invariant") + ")", "KEEP (the one group-grain fact check)"),
    ("C4a", "  E157 cluster price limb", "d43.py:99-112; demonstrate.py:405-423", "estate units at 9.65/9.75/9.85 M chained", "member pairs refused: 38 / 145 / 109", "D50 A:price, D43 price", d("C:E157_cluster_price") + " (trialr " + ref("C:E157_cluster_price") + ")", "KEEP-measure (c17: costs 124 CD, 0 CN caught; CN cannot see price twins)"),
    ("C4b", "  image facts at cluster (interior, floorplan)", "settings d43_cluster_image_facts; indist:3080-3107", "dev units sharing a shoot", "interior/floorplan cluster sole: trial 25/11, c17 158/12", "gate reading drops them (d43_gate_image_facts false) — two answers to one question", d("C:cluster_image_facts") + " (trialr " + ref("C:cluster_image_facts") + ")", "KEEP-measure (c17 costs 102 CD, 0 CN)"),
    ("C5", "size > 256", "guards.py:202-203", "a runaway group", "0 / 0 / 0 (max group 40 / 116 / 32)", "lane component cap 400 (incremental.py:1518-1571)", d("C:max_cluster_size"), "DELETE"),
    ("C6", "category_type across the set", "guards.py:205-207", "sale+rent via a NULL-type bridge", "conflicts " + f3("category_type", "cl"), "D43 `category_type` in C4 reads every member pair (the c17 conflict is a drazba/prodej/NULL auction: C4 refuses it identically)", d("C:category_type_inv"), "DELETE"),
    ("C7", "compat_class across the set", "guards.py:209-213", "flat+house via a NULL bridge", "0 / 0 / 0", "D43 `category_main` in C4", d("C:compat_class_inv"), "DELETE"),
    ("C8", "area_spread 8 % (+E280 printed excuse)", "guards.py:215-221, 129-152", "area chains", "conflicts " + f3("area_spread", "cl"), "D43 `area` at the lenient 8 % in C4 (the extreme pair of the set) with the same E280 excuse", d("C:area_spread"), "DELETE (conflicts are relabelled d43_distinguishable, groups identical)"),
    ("C9", "disposition / floor spread", "guards.py:223-234", "(off)", "0 / 0 / 0", "D43 disposition / floor", "0 by construction", "DELETE (2 dials)"),
    ("C10", "edge rank (certificate first, score, ids)", "cluster.py:103-112; repartition.py:31-49", "arrival-order dependence (E33)", "every run", "CERTIFICATE_WEIGHT 1000 in the objective", "n/a", "KEEP"),
    ("C11", "repartition (E137) greedy + local search", "cluster.py:214-325; repartition.py:73-164", "one bad member vetoing a family", "components cut 54 / 203 / 179", "union-find path (C13)", "→ union-find: " + d("C:repartition(E137)->unionfind", ("trial",)), "KEEP"),
    ("C12", "  repairs: E156 keep-factless, E193 rejoin (strict relation), E253 shed + outer rounds, E262 shed guard, E263 reconcile-first", "repartition.py:166-526; cluster.py:254-281; d43.py:72-80",
     "factless separations after the greedy pass", "n/a (no counter)", "the strict relation re-reads D43 at the PROMOTE bar = a fourth reading",
     "all off: " + d("C:repartition_ALL_repairs") + "; singly: E156 " + d("C:keep_factless(E156)") + "; E193 " + d("C:rejoin_cells(E193)") + "; E253 " + d("C:shed_blockers(E253)") + "; rounds " + d("C:outer_rounds(E253)->1") + "; E262 " + d("C:shed_factless_guard(E262)") + "; E263 " + d("C:reconcile_factless_first(E263)"),
     "DELETE the ensemble (net −5 CD on c17 of 26,841 and −5 on c18 of 15,977, 0 CN, trial 0; the parts interact: E253 alone +215/−194, E262 alone +219/−84 on c17, together ~0)"),
    ("C13", "constrained union-find + E57 bridges", "cluster.py:115-161, 376-451", "(not run: repartition true)", "0 / 0 / 0", "C11", "n/a", "DELETE (3 dials)"),
    ("C14", "E303 K-C house-number price excuse", "d43.py:109-112; indist:1738", "(off, prepared)", "0 / 0 / 0", "", "0 by construction", "DELETE"),
]
for r in crow:
    P("| " + " | ".join(r) + " |")
P("")
P("## 5. Apply refusals (apply.py:637-848, 955-1095; one planner for batch apply and the lane reconcile)")
P("")
tot = APPLY["_live_totals"]
P("Counts are summed over the 7 live applies of the trial (g12, g13 ×2, g14, g14 rentals, g15; `apply_fires.json`).")
P("")
P("| reason | code | protects against | live fires (sum) | also checked in | verdict |")
P("|---|---|---|---|---|---|")
arows = [
    ("category_outside_scope", "apply.py:97, 177", "merging outside the rollout", str(tot.get("out_of_scope_by_reason:category_outside_scope", 0)), "lane rt_scope", "DELETE with the scope row (W6)"),
    ("member_outside_blocks / member_without_location / member_outside_listing_ids", "apply.py:98-100, 178-185", "", "0", "", "DELETE with the scope row (W6)"),
    ("unattached_member", "apply.py:105, 759", "advert without a property", "0", "chokepoint", "KEEP (live-state rail)"),
    ("property_not_active", "apply.py:106, 763, 988", "merging into a retired record", "0", "chokepoint", "KEEP"),
    ("operator_group_verdict", "apply.py:107, 485", "a set the operator ruled not one", str(tot.get("skipped_by_reason:operator_group_verdict", 0)), "NOT read by the clustering (only kind='pair' rulings become must-links, incremental_sql.py:1094-1104)", "MERGE-INTO the clustering (group verdict → pair must-not-links); then delete here"),
    ("operator_pair_verdict / must_not_link", "apply.py:108-109, 487-496", "joining what the operator split", "0", "cluster C3 every lane pass + `changed_since_plan` in-lock", "MERGE-INTO `changed_since_plan`"),
    ("category_type_mix / category_main_incompatible", "apply.py:110-111, 568-579", "sale+rent / flat+house on one survivor", "0", "W1/W2, D43 facts, C6/C7, and the chokepoint itself (CLAUDE.md rule 15)", "DELETE"),
    ("carries_out_of_scope_listings", "apply.py:112, 583-586", "dragging an out-of-scope advert along", str(tot.get("skipped_by_reason:carries_out_of_scope_listings", 0)), "", "DELETE with the scope row (W6)"),
    ("property_spans_groups", "apply.py:115, 785", "fusing two engine groups through one property", str(tot.get("skipped_by_reason:property_spans_groups", 0)), "", "KEEP"),
    ("carries_ungrouped_listings", "apply.py:116, 794", "absorbing an advert the engine never grouped", str(tot.get("skipped_by_reason:carries_ungrouped_listings", 0)), "", "KEEP"),
    ("refused_at_chokepoint_before", "apply.py:117, 805", "retrying a refused set", "0", "", "KEEP"),
    ("restored_outside_engine (E905)", "apply.py:118, 810-813, 1000", "re-merging what the operator undid", "0", "", "KEEP"),
    ("changed_since_plan", "apply.py:120, 974", "state moving between plan and merge", "0", "", "KEEP (absorbs pair verdict/MNL)"),
    ("asset_linked_units", "apply.py:114, 1135", "two asset links on one survivor", "0", "chokepoint raises it", "KEEP"),
    ("deferred_run_cap", "apply.py:827-832", "one run merging too much", str(tot.get("deferred_run_cap", 0)), "lane per-pass cap", "KEEP one cap (lane)"),
    ("ruled_different_after_merge (report only)", "apply.py:123, 741-751", "silently keeping a contradicted merge", str(tot.get("ruled_different_after_merge", 0)), "proposed splits page", "KEEP"),
]
for r in arows:
    P("| " + " | ".join(r) + " |")
P("")
P("## 6. Lane rails and holds (incremental_lane.py, incremental.py, reconcile.py)")
P("")
P("No offline fire counts exist; the trial log (`TRIAL_LIVE.md`) records: storage budget refused twice (15:26, 15:40 CEST 09-26),")
P("seed-vs-lease refused once (16:10), 118 evidence holds (G1 read, :194), 0 reconcile quarantines named.")
P("")
P("| rail | code | protects against | also in | verdict |")
P("|---|---|---|---|---|")
lrows = [
    ("F3/E93 evidence hold (merge → band `evidence_pending`, 48 h horizon)", "incremental.py:157-235, 1256-1270; incremental_lane.py:286", "deciding on photos not yet evidence", "rt_equivalence withheld replay", "KEEP, move into decide_pair (§2 L20)"),
    ("E64 context rail", "incremental.py:1655-1708", "E63 promotions in a growing block", "E63", "DELETE with E63"),
    ("E83/E85/E88 NotImplementedError refusals", "incremental.py:994-1035", "lane diverging from batch", "family.py, development.py, harness.py:492-603", "DELETE with the dials"),
    ("component cap 400", "incremental.py:696, 1518-1571; incremental_lane.py:230", "re-grouping from a partial view", "cluster size 256 (C5)", "KEEP (one cap)"),
    ("pair budget 150,000 (E75)", "incremental.py:695, 1203", "a pass that cannot fit", "", "KEEP"),
    ("pass deadline 1,050 s (E913) + rate budget", "incremental_lane.py:205, 322-330, 1939", "stalled passes", "", "KEEP"),
    ("storage budget 800 MB (E916)", "incremental_lane.py:360, 1639-1695", "an unwatched schema", "", "KEEP (fired 2× on 09-26)"),
    ("retire rail 5 %/24 h", "incremental_lane.py:338, 1367-1411", "a broken scope retiring the store", "", "KEEP"),
    ("enter-scan cap 60/day", "incremental_lane.py:296", "public-schema scan cost", "", "KEEP"),
    ("lease `autodedup_realtime`", "rt_lease.py:1-90; incremental_lane.py:205-212", "two writers", "apply/unapply/seed", "KEEP"),
    ("seed version / bootstrap gate", "incremental.py:77-120", "merging from an unbuilt stream", "reconcile SEED_MISMATCH", "KEEP"),
    ("calibration recut (coverage < 0.85, ≥ 6 h)", "incremental_lane.py:235-240, 1852-1905", "stale photo populations", "", "KEEP"),
    ("reconcile: block_not_fully_read wait, quarantine 3/24 h, 3 unexplained errors, 30 s margin, never split", "reconcile.py:56-73, 146-259", "merging on a partial block; retry storms", "apply planner", "KEEP"),
]
for r in lrows:
    P("| " + " | ".join(r) + " |")
P("")

P("## 7. Where one signal is compared many times (the duplication map)")
P("")
P("| signal | places it is compared (file:line) | count |")
P("|---|---|---|")
dup = [
    ("deal type / category", "guards.py:69-73 (W1/W2); indistinguishable.py:2619-2625 (D43); guards.py:205-213 (C6/C7); apply.py:568-579; cluster.py:191-211 (closure validity); probe keys (fingerprint cat_group, blocking); chokepoint toolkit/property_identity", "7"),
    ("area", "guards.py:49-75 (W3, E5 3-way); features.py:1403 (model); decide.py:145,194,213,466 (K-A ≤2 %, K-B ≤1 %, K-C ≤3 %, E63 ≤1 %); indistinguishable.py:2629-2641 (D43 gate/cluster 8 %, promote 3 %), 2645 stated_area, printed/headline area readers; demonstrate.py:178-186 (D50 A); indistinguishable.py:3149 (agreeing); features.py:408-409 (ATTR family exact/1 %/2 %); guards.py:215-221 (C8 8 %); blocking attr_area + town bands (±25 %)", "12 places, 7 thresholds (1/2/3/8/20-25 %, exact, printed)"),
    ("floor / storeys", "guards.py:80-83 (W5 ≥2); features floor_diff + same-portal one-floor; decide.py:150 (K-A); indistinguishable.py:2652-2690 (floor with camps, total_floors) + prose_floor, subject_floor, storey_word, offered_storey readers; features numeral_conflict (floor); demonstrate onesided floor; guards.py:230-234 (spread, off); 11 excuse dials (floor_camps*, d43_floor_within_camp*, feed*, total_floors_camp, gate slack)", "9 places + 11 excuses"),
    ("unit identity (designators / codes)", "guards.py:87-115 (E61) = indistinguishable.py:2745-2749 (same predicate); text_facts.unit_designators:148, printed_unit_codes:207, reference_codes:112; D43 unit_code/labelled_unit/english_unit_code/slug_unit/printed_designator/position_designator/space_number/plan_space/named_villa; features unit_number_shared, numeral_conflict (unit); demonstrate onesided code", "4 extractors, 10 readers, 1 veto"),
    ("order code", "decide.py:228-241 (K-R); features.py:863 (index); demonstrate.py:618, 684 (B code, E164 code); indistinguishable.py:3216 (unit warrant); agency_code / agency_code_colive / agency_code_plus readers; decide.py:370 (E45 arm, off)", "7"),
    ("text containment", "features.py:1487 (containment_max) AND indistinguishable.py:836-860 (its own shingles + memo: a second implementation); thresholds 0.80 (D50 B), 0.90 (K-B, E45, E63, price-sequential), 0.97 (agency body ceiling), 0.98 (E164), 0.99 (twin, same-source one-text)", "2 implementations, 6 thresholds"),
    ("tight photo matches", "K-C ≥4 (decide.py:209-225); E164 ≥3 (demonstrate.py:690); price-sequential ≥3; cellar yield ≥4; promote photo warrant ≥1 (indist:376); D50 B photo ≥1 (demonstrate.py:614); E63 ≥4 images; E65 floor (off)", "8 places, 4 bars"),
    ("co-live / live windows (clock)", "features.window_end_stamp:1360 + _window:1378; decide.window_gap_days:112, disjoint_windows:126; hazard_context.live_window:59, live_overlap_days:67, disjoint_windows:77; demonstrate.overlap_days_local:202, honest_overlap_local:232, _sequential:212, sequential_for:242; indistinguishable.overlap_days:318, _honest_overlap_days:421, honest_overlap_days:430, _co_live:333, _windows_overlap:341, _never_live_together:435, _live_together:942, _sequential_for_price:810; family._overlap_days:121; structural_truth.overlap_days:147; 6 `_stamp` parsers; 5 `*_honest_clock` dials", "19 functions, 5 clock dials"),
    ("'a development'", "demonstrate.in_development:480 (PROJECT_TERMS either side); demonstrate.development_context:520 (NEW_BUILD_TERMS both sides + unit/price list; mode off/vocab/narrow/template); indistinguishable._development_pair:783 (PROJECT_TERMS both sides); hazard_context census (E63); development.py (E88, off); two vocabularies development.py:66, demonstrate.py:499", "5 definitions, 2 word lists"),
    ("price", "features price_last_ratio/ppm2/path; indistinguishable.py:2677-2730 (5 %/60 %, path, same-source bar) + charge/accessory_price/rental readers; demonstrate.price_demonstrated:295 (exact/rounding/path/sequential); d43.py:99-112 (E157 20 %); decide.py:469 (E63 0.5 %); broker probe decile", "6 places, 5 tolerances"),
    ("'the same fact' readings", "GATE (decide.py:561), PROMOTE (indist:3201), CLUSTER (d43.py:97), strict PROMOTE again at cluster (d43.py:72-80, E193 rejoin)", "4 readings of one reader set"),
]
for r in dup:
    P("| " + " | ".join(r) + " |")
P("")
P("## 8. Top-ten suspects ablated in the worktree, top five confirmed on cohort 17, final check on cohort 18")
P("")
P("Suspects, chosen for never firing, duplicating another rule, or existing for a case the data no longer shows. Each was")
P("ablated on the trial (patch or settings) AND as code in `census/w15-c1-bundle` (S1-S4, S6-S8, S10), run through the")
P("official `harness run` (its result is on the `all` row: the bundle holds S1-S4, S6-S8 and S10 at once).")
P("")
P("| # | suspect | trial ‖ c17 | c18 | code-bundle harness |")
P("|---|---|---|---|---|")
sus = [
    ("S1", "D43 gate reading", "D43:gate"),
    ("S2", "E61 designator veto (+ machine vetoes)", "E61:unit_designator_veto"),
    ("S3", "set-level cluster invariants: area_spread", "C:area_spread"),
    ("S3", "  size 256", "C:max_cluster_size"),
    ("S3", "  category_type", "C:category_type_inv"),
    ("S3", "  compat_class", "C:compat_class_inv"),
    ("S4", "repartition repair ensemble", "C:repartition_ALL_repairs"),
    ("S5", "D43 warrant → predicate", "D43:warrant->predicate"),
    ("S6", "E63 context rule", "E63:context_rule"),
    ("S7", "E48 strata → scalar t_hi", "E48:strata_table->t_hi"),
    ("S9", "K-R certificate", "cert:K-R"),
    ("all", "settings/patch twin of the code bundle", "bundle:c1_code_bundle_settings"),
    ("Z1", "47 trial-zero fact readers dropped together", "bundle:trial_zero_facts"),
    ("Z2", "85 trial-zero dials flipped together", "bundle:trial_zero_dials"),
]
for num, name, arm in sus:
    a18 = ARMS["c18"].get(arm)
    c18s = ("m%+d g%+d cp+%d/−%d" % (a18["dmerge"], a18["dgroups"], a18["cp_gained"], a18["cp_lost"])
            + ((" CD+%d/−%d CN+%d" % (a18["gained_labels"].get("ref_cd", 0), a18["lost_labels"].get("ref_cd", 0), a18["gained_labels"].get("ref_cn", 0))) if "ref_cd" in a18.get("gained_labels", {}) else "")
            if a18 and "invalid" not in a18 else "·")
    P(f"| {num} | {name} | {d(arm, auto18=False)} | {c18s} | {HARNESS.get(arm, '')} |")
P("")
P("S8 (dead by configuration: K-A, E45/E46/E47 + unit arms, E65, E11, D65, E85/E88/E83, union-find + E57 bridges, E303,")
P("E301/E301b/E302, the 5 off readers) and S10 (decide-time wall re-check) fire 0 on g15/c17/c18: Δ = 0 by construction.")
P("")

P("## 9. Dials census (the D43 / D50 / floor / repartition families)")
P("")
_t = ARMS["trial"]
_valid = {k: v for k, v in _t.items() if k.startswith("dial:") and "invalid" not in v}
_zero = [k[5:] for k, v in _valid.items() if v["dmerge"] == 0 and v["dband"] == 0 and v["cp_gained"] == 0 and v["cp_lost"] == 0 and v["dgroups"] == 0]
P(f"- {len(_t) and sum(1 for k in _t if k.startswith('dial:'))} dial arms on the trial (every ON boolean/mode dial of the families, each switched off with its validator dependants);")
P(f"  {len(_valid)} valid; **{len(_zero)} change nothing** (not one pair zone). Invalid: " + ", ".join(sorted(k for k, v in _t.items() if 'invalid' in v)) + ".")
P("- Flipped together on c17 (`bundle:trial_zero_dials`): " + d("bundle:trial_zero_dials", ("c17",)) + ". Bisected by family on c17:")
for g in ("unitcode", "rental", "location", "land", "area", "price", "storey", "other"):
    a = ARMS["c17"].get("dialgroup:" + g)
    if a:
        P(f"  - `{g}`: " + d("dialgroup:" + g, ("c17",)))
P("  → **the 14 `unitcode` + 11 `rental` dials are 0 on the trial AND on c17** (delete-ready); the rest move c17 through a")
P("  handful of readers (`street_prose` alone = the `location` +97; `subject_floor`, `plot_prose_exact`, `storey_word`,")
P("  `extent`, `headline_area` below). Zero dials by name: " + ", ".join(sorted(_zero)) + ".")
P("- The 40 dials that DO move the trial are almost all EXCUSES of one reader (a fact reader + N switches that un-say it):")
P("  floor (floor_camps, floor_camps_reads, d43_total_floors_camp, d43_gate_total_floors_slack, d43_floor_within_camp×4,")
P("  floor_same_source_feed, floor_feed_unknown_closed, d43_floor_same_feed_sequential), price (d43_price_sequential_path,")
P("  _storey_fact, _text_identity, _honest_clock, demonstrate_price_exact/_path_exact/_rounding_aware,")
P("  d43_colive_charge_requires_price_gap), area (d43_printed_area_prevails, d43_headline_vs_column_*), interior")
P("  (d43_interior_requires_no_tight_photo). Largest: d43_total_floors_camp " + d("dial:d43_total_floors_camp", ("trial",))
  + "; d43_price_sequential_path " + d("dial:d43_price_sequential_path", ("trial",)) + ". This is the patchwork shape the")
P("  mandate names: each reader was made too eager, then excused case by case.")
P("")
P("## 10. Ranked deletion list")
P("")
P("Rank = confidence (measured zero, on how many cohorts) × what it removes. Lines are the function bodies that go (AST")
P("count at b2454fa1), not counting tests, docs and settings files.")
P("")
P("| rank | delete | evidence | removes |")
P("|---|---|---|---|")
dl = [
    ("1", "Dead by configuration: E85 family guard, E88 development hold, E83 carrier-aware stock (family.py, development.py, harness.py:492-603 deferred K-B path, incremental.py:994-1035 refusals), K-A, E45/E46/E47 + 5 unit arms, E65, D65 merge policy (+13 `*_hold.json`), union-find + E57 bridges, E303, E301/E301b/E302 prepared dials, the 5 OFF readers (offer_area, agency_code, agency_code_colive, commercial_subtype, named_villa)",
     "fire 0 on g15/c17/c18; Δ 0 by construction", "~1,250 lines (family 335 + development 361 + harness ~110 + decide 200 + cluster 83 + lane ~40 + 5 readers 124), ~60 dials, 13 settings files"),
    ("2", "E63 context rule + E64 rail + hazard census (`hazard_context.py`, the `context` blob on every stored pair row)", "fires 0/0/0; " + d("E63:context_rule"), "~565 lines (decide 79, lane 85, hazard_context 401), 10 dials, one stored column's reason to exist"),
    ("3", "E61 designator veto + machine vetoes in clustering and the lane", d("E61:unit_designator_veto") + "; c18 in §8", "29 + ~25 plumbing lines, 1 dial; the D43 `unit_designator` reader already carries the same predicate"),
    ("4", "D43 gate reading (decide.py:560-571, the GATE branch of every reader, d43_gate / d43_gate_image_facts)", d("D43:gate") + " (merge edges become cluster conflicts; groups identical)", "1 of 4 readings; 2 dials"),
    ("5", "E48 per-stratum cut → one t_hi = 0.9788", d("E48:strata_table->t_hi"), "23 lines + `t_hi_by_stratum` (5 cells, 3 dead) + evaluate.decide_stratum coupling"),
    ("6", "Set-level cluster invariants size / category_type / compat_class / area_spread (+E280 spread excuse) / disposition / floor spread; decide-time wall re-check; E11; apply category_type_mix / category_main_incompatible", "0 trial, 0 c17 (each and together); apply reasons 0 in 7 live applies", "~45 + 12 lines, 5 dials, 2 apply reasons"),
    ("7", "Repartition repair ensemble (E156, E193 rejoin + the strict 4th reading, E253 shed + outer rounds, E262, E263)", d("C:repartition_ALL_repairs"), "296 lines + cluster.py:254-281 + d43.strict, 8 dials"),
    ("8", f"{len(DEL_READERS)} fact readers: " + ", ".join(DEL_READERS) + "; dial groups `unitcode` (14) and `rental` (11)",
     "trial 0 each (measured); c17 0 (measured, or no sole fire = 0 by construction); c18 no sole fire; unitcode dials c18 m+14 cp 0. The 47 trial-zero readers dropped TOGETHER: c17 " + d("bundle:trial_zero_facts", ("c17",)).split(" ‖ ")[0] + ", c18 " + d("bundle:trial_zero_facts", ("c18",)).split(" ‖ ")[0] + " — carried by " + ", ".join(CAND_READERS + MEAS_READERS) + " (kept out of this row)",
     f"{len(DEL_READERS)} of 64 readers, 25 dials (+ their text_facts extractors where no other reader uses them)"),
    ("9", "D43 promotion warrant (agree:2 / photo / unit / body-sequential) → merge into D50", d("D43:warrant->predicate"), "106 lines, 4 dials"),
    ("10", "apply operator_pair_verdict / must_not_link (→ `changed_since_plan`), operator_group_verdict (→ clustering reads group verdicts)", "0 / 0 / 1 fires in 7 live applies", "3 reasons, 1 negatives reader"),
]
for r in dl:
    P("| " + " | ".join(r) + " |")
P("")
P("Not deletions (measured load-bearing or a loosening that needs the confirmation discipline): D43 promotion, D50 (A:area,")
P("A:price, A:disposition, A:obec, B, E164, twin), the certificates as a group, the model cut, the D43 cluster relation with")
P("E157 and image facts, base repartition, the retrieval walls as a slot filter; auto-reject ×2 (a loosening: c17 +50 co-pairs,")
P("24 CD, 0 CN) and K-R-as-certificate (c17 +104/−45) are MERGE-INTO candidates for a measured wave, not W1 deletions.")
P("")
P("## 11. Assumptions validated / refuted")
P("")
P("Validated: A1's ladder order and every A1 count for g15 (zones, reasons, certificates, 92 gate demotions, 406 demonstration")
P("refusals, conflicts 260/3/2, 54 components, 7,971 edges → 1,133 groups) — reproduced byte for byte; 7.1 never fires; E63")
P("0 promotions (also c17/c18); E11 never fires; D65 empty; K-A/E45/E46/E47/E65/E85/E88/E83 off; all D43 promotions come from")
P("model-band pairs (a gate-demoted certificate is never re-promoted: `promoted_from` = model on g15/c17/c18).")
P("")
P("Refuted / corrected:")
P("- A1 §7.5 'E48 … only K-A is propose-only': the table is effectively ONE scalar — `K-C|same: 0.0` is never compared")
P("  (certificates only test `is None`), the two model cells are equal, the global `t_hi` 1.0 is never read.")
P("- A1 §7.11 '59 readers switched on': 64 fact names are emitted, 5 off; `FACT_NAMES` (47) is stale.")
P("- A1 §7.8 'warrant = 2 of 9 attributes stated … step 1 has already ruled out that they differ': `price` and `floor`")
P("  count when merely STATED (indistinguishable.py:3155-3166), so 'agree:2' is ~every priced flat pair; the warrant blocks")
P("  13 trial / 60 c17 edges and 20 / 0 co-pairs.")
P("- A1 §8 'the whole-group check reads leniently as at the gate but with photo facts': there are FOUR readings — the")
P("  E193 rejoin re-reads the relation at the PROMOTE bar (d43.py:72-80).")
P("- 'E61 protects twin flats': true, but not alone — the D43 `unit_designator` reader is the same predicate; E61 off = 0 Δ.")
P("- 'The D43 gate protects certified merges from facts': its 92 / 406 / 319 demotions change no group; the cluster relation")
P("  refuses the same unions.")
P("- 'The repartition repairs recover factless separations': as an ensemble they are ~0 (trial 0, c17 +4/−10, c18 +19/−41); singly they")
P("  move hundreds of co-pairs in opposite directions (E253 +215/−194, E262 +219/−84 on c17) — they mostly undo each other.")
P("- My own first single-invariant patch returned None before the D43 limb; corrected, re-run (C:category_type_inv c17 0).")
P("")
P("## 12. Open questions for the coordinator")
P("")
P("- The retrieval walls: keep as a slot filter with the D43 comparator as the one definition (the floor wall ≥2 vs the")
P("  D43 floor fact with camps are two definitions today; making the filter the fact tightens retrieval — needs a measured arm).")
P("- The D43 cluster relation as a whole: trial off = +441 co-pairs (23 operator-same, 0 of 162 operator-different, 113 CD, 0 CN);")
P("  c17 off = +2,993 (712 CD, 3 CN). E157 cluster price and cluster image facts cost 124 / 102 CD on c17 and catch 0 CN;")
P("  the CN reference cannot see price twins or shared-shoot twins, so only the operator/judge labels can settle them.")
P("- F3 hold lives only in the lane: moving it into `decide_pair` (an evidence-completeness input) is the one-code-path fix;")
P("  it changes the lane's replay-equivalence proof, not its behaviour.")
P("")
P("## 13. Files")
P("")
P("- `c1/scripts/c1_engine.py` (replay + patches), `c1_ablate.py` (arms; `--adhoc`), `c1_read_stored.py` → `c1/stored_fires.json`,")
P("  `c1_read_apply.py` → `c1/apply_fires.json`, `c1_facts.py` → `c1/<cohort>_fact_fires.json`, `c1_table.py` →")
P("  `c1/<cohort>_ablation.{json,md}`, `c1_compare_runs.py` (harness-run vs harness-run), `c1_doc.py` (this file), `chain_rest.sh`.")
P("- `c1/runs/<cohort>_arms.jsonl` (one row per arm, with the gained/lost label breakdown), `c1/runs/<cohort>_arm_pairs/` (full")
P("  gained/lost co-pair lists, c17/c18/trialr), `c1/runs/<cohort>_base_{facts,decisions,clusters,price_limb}`, official runs")
P("  `c1/runs/{trial_w31_official,trial_bundle_official,c17_bundle_official}`.")
P("- Worktrees: `/home/hejtm/dev/sreality/.claude/worktrees/w15-census-c1` (branch census/w15-c1-rules, pristine code the")
P("  replay imports + the census scripts committed), `/home/hejtm/dev/sreality/.claude/worktrees/w15-census-c1-bundle` (branch")
P("  census/w15-c1-bundle, the code deletion bundle + `autodedup/settings/c1_bundle.json`).")
P("")
P("## Appendix A. Every dial arm on the trial (one row per dial; coupled dependants switched off with it)")
P("")
P("| dial (w31 value → ablated) | Δ trial | coupled off |")
P("|---|---|---|")
import json as _j2
_rows = [_j2.loads(x) for x in (Path("/home/hejtm/autodedup-artifacts/w15/census/c1/runs/trial_arms.jsonl").read_text().splitlines()) if x.strip()]
for _r in _rows:
    if not _r["name"].startswith("dial:"):
        continue
    _n = _r["name"][5:]
    _ov = _r.get("over", {}).get(_n)
    _co = _r.get("coupled_off") or ""
    if "invalid" in _r:
        P(f"| {_n} → {_ov} | invalid: {_r['invalid'][:70]} | |")
    else:
        P(f"| {_n} → {_ov} | {d('dial:' + _n, ('trial',))} | {', '.join(_co) if _co else ''} |")
P("")
Path("/home/hejtm/autodedup-artifacts/w15/census/c1/doc_part1.md").write_text("\n".join(L) + "\n")
print("\n".join(L))
