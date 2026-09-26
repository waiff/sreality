#!/bin/bash
export PYTHONPATH=/home/hejtm/dev/sreality/.claude/worktrees/w15-c4-descriptions
A=/home/hejtm/autodedup-artifacts/w14/s15/score_g15/autodedup-score-36244048665/artifact/cohort.jsonl.gz
cd /tmp
for v in w31 w31_nocamps w31_camps0; do
  if [ "$v" = w31 ]; then SF=/home/hejtm/dev/sreality/.claude/worktrees/w15-c4-descriptions/autodedup/settings/w31.json; else SF=/home/hejtm/autodedup-artifacts/w15/census/c4_runs/$v.json; fi
  /usr/bin/time -v python3 -m autodedup.harness run $A --out /home/hejtm/autodedup-artifacts/w15/census/c4_runs/trial_$v --settings $SF --model /home/hejtm/dev/sreality/.claude/worktrees/w15-c4-descriptions/autodedup/models/w6_gold.json > /home/hejtm/autodedup-artifacts/w15/census/c4_runs/trial_$v.log 2>&1
  echo "done $v $(date +%T)" >> /home/hejtm/autodedup-artifacts/w15/census/c4_runs/progress.txt
done
