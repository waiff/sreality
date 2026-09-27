#!/bin/bash
# sequential: c18 final check -> trialr (trial + label-free ref) -> official c17 code-bundle harness
set -u
C1=/home/hejtm/autodedup-artifacts/w15/census/c1
while ps aux | grep -q "[p]ython3 c1_ablate.py c17"; do sleep 10; done
cd $C1/scripts
python3 c1_ablate.py c18 --adhoc ../adhoc_c18.json --arms "bundle:c1_code_bundle_settings,bundle:trial_zero_facts,bundle:trial_zero_dials,D43:gate,E61:unit_designator_veto,D43:warrant->predicate,C:repartition_ALL_repairs,C:area_spread,E63:context_rule,E48:strata_table->t_hi,C:category_type_inv,C:max_cluster_size" > ../logs/c18_arms.log 2>&1
python3 c1_ablate.py trialr --arms "D43:gate,C:d43_cluster_invariant,C:E157_cluster_price,C:cluster_image_facts,fact:total_floors,fact:floor,fact:interior,fact:price,cut:model_merge_authority,cert:K-R,cert:K-B,cert:K-C,D50:demonstration,auto_reject:BOTH,bundle:c1_code_bundle_settings,bundle:trial_zero_dials,C:repartition_ALL_repairs,D43:warrant->predicate,C:shed_factless_guard(E262),dial:d43_total_floors_camp,dial:d43_price_sequential_path" > ../logs/trialr_arms.log 2>&1
B=/home/hejtm/dev/sreality/.claude/worktrees/w15-census-c1-bundle
C17=/home/hejtm/autodedup-artifacts/w14/s15/cohort17_export_36221961445/autodedup-export-36221961445/cohort.jsonl.gz
cd $B && PYTHONPATH=$B /usr/bin/time -v python3 -m autodedup.harness run $C17 --out $C1/runs/c17_bundle_official --settings autodedup/settings/c1_bundle.json --model autodedup/models/w6_gold.json > $C1/logs/c17_bundle_official.log 2>&1
echo CHAIN_DONE >> $C1/logs/chain.log
