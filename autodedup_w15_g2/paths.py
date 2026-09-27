"""G2 model-first prototype: every input path in one place (offline, no DB)."""
from __future__ import annotations

from pathlib import Path

ART = Path("/home/hejtm/autodedup-artifacts")
OUT = ART / "w15/global_search/g2"
CACHE = OUT / "cache"
C2_CACHE = ART / "w15/census/c2/cache"
LABELS = ART / "w14/labels_g13_36225845749/autodedup-labels-36225845749"
JUDGE = ART / "w14/judge_trial"
JUDGE_RUNS = {
    "vision": JUDGE / "vision_36220814481/autodedup-judge-36220814481/judgements.jsonl",
    "text": JUDGE / "text_36223717746/autodedup-judge-36223717746/judgements.jsonl",
    "gold": JUDGE / "gold_36225151443/autodedup-judge-36225151443/judgements.jsonl",
}
W6_RUNS = ["35124676256", "35116313682", "35115036274", "35155058503", "35149028995",
           "35202925670", "35203586251"]
W6_LABELFILES = ART / "w6/labelfiles"
C7_JUDGEMENTS = ART / "w15/census/c7/judgements"
COHORTS = {
    "trial": ART / "w14/s15/score_g15/autodedup-score-36244048665/artifact/cohort.jsonl.gz",
    "c17": ART / "w14/s15/cohort17_export_36221961445/autodedup-export-36221961445/cohort.jsonl.gz",
    "c18": ART / "w14/s15/cohort18_export_36237638871/autodedup-export-36237638871/cohort.jsonl.gz",
}
for _n in range(3, 17):
    COHORTS[f"c{_n}"] = ART / f"w14/cohort{_n}/export/cohort.jsonl.gz"
