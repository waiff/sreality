#!/bin/bash
C=/home/hejtm/autodedup-artifacts/w15/census/c6
cd $C
python3 attribution.py /home/hejtm/autodedup-artifacts/w14/s15/score_g15/autodedup-score-36244048665 /home/hejtm/autodedup-artifacts/w14/s15/score_g15/autodedup-score-36244048665/artifact/cohort.jsonl.gz $C/g15 > g15.log 2>&1
python3 attribution.py $C/runs/c17_w31 /home/hejtm/autodedup-artifacts/w14/s15/cohort17_export_36221961445/autodedup-export-36221961445/cohort.jsonl.gz $C/c17 > c17.log 2>&1
python3 attribution.py /home/hejtm/autodedup-artifacts/w14/s15/cohort18_runs/w31 /home/hejtm/autodedup-artifacts/w14/s15/cohort18_export_36237638871/autodedup-export-36237638871/cohort.jsonl.gz $C/c18 > c18.log 2>&1
echo ALLDONE > run_all.done
