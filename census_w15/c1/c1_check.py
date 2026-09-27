"""Fidelity check: the in-process replay must equal the official harness run byte-for-byte on
zones/reasons and on clusters.json's clusters+conflicts."""
import json, sys, time
sys.path.insert(0, "/home/hejtm/autodedup-artifacts/w15/census/c1/scripts")
import c1_engine as E
TRIAL = "/home/hejtm/autodedup-artifacts/w14/s15/score_g15/autodedup-score-36244048665/artifact/cohort.jsonl.gz"
WT = E.WT
eng = E.Engine(TRIAL, WT + "/autodedup/settings/w31.json", WT + "/autodedup/models/w6_gold.json",
               "/home/hejtm/autodedup-artifacts/w15/census/c1/cache_trial.pkl")
print("setup", eng.setup_s, flush=True)
res = eng.run(eng.base, log_facts=True)
eng.save_cache()
print("timing", res["timing"], flush=True)
off = json.load(open("/home/hejtm/autodedup-artifacts/w15/census/c1/runs/trial_w31_official/clusters.json"))
mine = {str(k): v for k, v in res["clusters"].clusters.items()}
print("clusters identical:", mine == off["clusters"])
print("conflicts identical:", res["clusters"].conflicts == off["conflicts"])
run = json.load(open("/home/hejtm/autodedup-artifacts/w15/census/c1/runs/trial_w31_official/run.json"))
from collections import Counter
print("reasons identical:", dict(Counter(d.reason for d in res["decisions"])) == run["reasons"])
print("facts logged:", len(res["facts"]))
