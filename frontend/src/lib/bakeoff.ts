/**
 * Model-testing explorer data model — reads the dedup_vision_bakeoff_results_public view
 * (migration 303), the per-pair × per-model × per-lane verdict matrix produced by
 * `scripts/validate_vision_models.py --persist-results` (Session-3 vision bake-off).
 *
 * All aggregation (the recall/precision summary matrix, pair grouping, filtering) is pure
 * and unit-tested in bakeoff.test.ts; the page component only renders.
 */

import { supabase } from './supabase';

export type Lane = 'compare' | 'floor_plan' | 'site_plan';
export type CheckType = 'recall' | 'precision';

export const LANES: readonly Lane[] = ['compare', 'floor_plan', 'site_plan'];
export const LANE_LABEL: Record<Lane, string> = {
  compare: 'Compare (rooms)',
  floor_plan: 'Floor plan',
  site_plan: 'Site plan',
};
/** The verdict on each lane that would WRONGLY merge / fail the guard — the one a model must
 * NOT emit on a confirmed-different pair. Kept in sync with scripts.validate_vision_models._LANES. */
export const DANGER_VERDICT: Record<Lane, string> = {
  compare: 'High',
  floor_plan: 'same_layout',
  site_plan: 'same_unit',
};

export interface BakeoffRow {
  id: number;
  run_label: string;
  set_name: string;
  check_type: CheckType;
  lane: Lane;
  model: string;
  sreality_id_a: number;
  sreality_id_b: number;
  room_type: string | null;
  is_same: boolean | null;
  label_source: string | null;
  category_main: string | null;
  expected_verdict: string | null;
  danger_verdict: string;
  candidate_verdict: string;
  is_correct: boolean;
  is_dangerous: boolean;
  cost_usd: number | null;
  created_at: string;
}

const COLS =
  'id,run_label,set_name,check_type,lane,model,sreality_id_a,sreality_id_b,room_type,' +
  'is_same,label_source,category_main,expected_verdict,danger_verdict,candidate_verdict,' +
  'is_correct,is_dangerous,cost_usd,created_at';

/** All rows for one run_label (the table is small: models × a few hundred pairs). */
export const fetchBakeoffRows = async (runLabel: string): Promise<BakeoffRow[]> => {
  const { data, error } = await supabase
    .from('dedup_vision_bakeoff_results_public')
    .select(COLS)
    .eq('run_label', runLabel)
    .order('sreality_id_a', { ascending: true })
    .order('sreality_id_b', { ascending: true })
    .limit(20000);
  if (error) throw error;
  return (data ?? []) as unknown as BakeoffRow[];
};

/** Distinct run_labels, newest first (by max created_at). */
export const fetchBakeoffRunLabels = async (): Promise<string[]> => {
  const { data, error } = await supabase
    .from('dedup_vision_bakeoff_results_public')
    .select('run_label,created_at')
    .order('created_at', { ascending: false })
    .limit(20000);
  if (error) throw error;
  const seen = new Set<string>();
  const out: string[] = [];
  for (const r of (data ?? []) as { run_label: string }[]) {
    if (!seen.has(r.run_label)) {
      seen.add(r.run_label);
      out.push(r.run_label);
    }
  }
  return out;
};

// --- pure aggregation -------------------------------------------------------

export interface CellStat {
  n: number;
  correct: number;
  pct: number | null; // correct / n, null when n === 0
}

export interface LaneStat {
  recall: CellStat;
  precision: CellStat;
}

/** Summary matrix cell for one (model, lane): recall = share reproducing the cached verdict;
 * precision = share AVOIDING the dangerous verdict on a confirmed-different pair. */
export const summarize = (rows: readonly BakeoffRow[]): Map<string, Record<Lane, LaneStat>> => {
  const out = new Map<string, Record<Lane, LaneStat>>();
  const blank = (): Record<Lane, LaneStat> => ({
    compare: emptyLane(),
    floor_plan: emptyLane(),
    site_plan: emptyLane(),
  });
  for (const r of rows) {
    let m = out.get(r.model);
    if (!m) {
      m = blank();
      out.set(r.model, m);
    }
    const cell = r.check_type === 'recall' ? m[r.lane].recall : m[r.lane].precision;
    cell.n += 1;
    if (r.is_correct) cell.correct += 1;
    cell.pct = cell.correct / cell.n;
  }
  return out;
};

const emptyLane = (): LaneStat => ({
  recall: { n: 0, correct: 0, pct: null },
  precision: { n: 0, correct: 0, pct: null },
});

export interface PairKey {
  a: number;
  b: number;
}

export interface PairGroup {
  a: number;
  b: number;
  is_same: boolean | null;
  label_source: string | null;
  category_main: string | null;
  check_type: CheckType;
  /** rows keyed `${model}|${lane}` for O(1) cell lookup in the detail table. */
  byModelLane: Map<string, BakeoffRow>;
  /** true if the models disagree with each other on any lane (interesting to review). */
  hasDisagreement: boolean;
  /** true if ANY model emitted the dangerous verdict on this pair. */
  anyDangerous: boolean;
}

/** Group rows into one entry per (a,b) pair, newest-wins on the shared pair metadata. */
export const groupPairs = (rows: readonly BakeoffRow[]): PairGroup[] => {
  const map = new Map<string, PairGroup>();
  for (const r of rows) {
    const key = `${r.sreality_id_a}|${r.sreality_id_b}`;
    let g = map.get(key);
    if (!g) {
      g = {
        a: r.sreality_id_a,
        b: r.sreality_id_b,
        is_same: r.is_same,
        label_source: r.label_source,
        category_main: r.category_main,
        check_type: r.check_type,
        byModelLane: new Map(),
        hasDisagreement: false,
        anyDangerous: false,
      };
      map.set(key, g);
    }
    g.byModelLane.set(`${r.model}|${r.lane}`, r);
    if (r.is_dangerous) g.anyDangerous = true;
    // precision pairs carry the ground truth; prefer their metadata if a recall row set it null
    if (g.category_main == null && r.category_main != null) g.category_main = r.category_main;
    if (g.label_source == null && r.label_source != null) g.label_source = r.label_source;
    if (g.is_same == null && r.is_same != null) g.is_same = r.is_same;
  }
  // compute per-lane disagreement across models
  for (const g of map.values()) {
    for (const lane of LANES) {
      const verdicts = new Set<string>();
      for (const [k, row] of g.byModelLane) {
        if (k.endsWith(`|${lane}`)) verdicts.add(row.candidate_verdict);
      }
      if (verdicts.size > 1) {
        g.hasDisagreement = true;
        break;
      }
    }
  }
  return [...map.values()];
};

export interface PairFilter {
  lane: Lane | 'all';
  checkType: CheckType | 'all';
  category: string | 'all';
  disagreementsOnly: boolean;
  dangerousOnly: boolean;
}

export const filterPairs = (pairs: readonly PairGroup[], f: PairFilter): PairGroup[] =>
  pairs.filter((p) => {
    if (f.checkType !== 'all' && p.check_type !== f.checkType) return false;
    if (f.category !== 'all' && (p.category_main ?? 'unknown') !== f.category) return false;
    if (f.disagreementsOnly && !p.hasDisagreement) return false;
    if (f.dangerousOnly && !p.anyDangerous) return false;
    if (f.lane !== 'all') {
      // keep only pairs that were evaluated on this lane by at least one model
      let seen = false;
      for (const k of p.byModelLane.keys()) {
        if (k.endsWith(`|${f.lane}`)) {
          seen = true;
          break;
        }
      }
      if (!seen) return false;
    }
    return true;
  });

export const distinctModels = (rows: readonly BakeoffRow[]): string[] =>
  [...new Set(rows.map((r) => r.model))].sort();

export const distinctCategories = (pairs: readonly PairGroup[]): string[] =>
  [...new Set(pairs.map((p) => p.category_main ?? 'unknown'))].sort();
