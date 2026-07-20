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
export type CheckType = 'recall' | 'precision' | 'review';

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

/** Plain-language meaning of a raw verdict value, for the /model-testing legend + cell tooltips.
 * Each lane's DANGER_VERDICT is the "merge-ward" one (pushes toward combining the two listings);
 * every other verdict keeps them apart or makes no call. */
export const VERDICT_GLOSS: Record<string, string> = {
  High: 'confident it’s the same room → would MERGE',
  Medium: 'unsure → keeps the listings apart',
  Low: 'not the same room → keeps the listings apart',
  same_layout: 'the two floor plans are the same layout → would MERGE',
  different_layout: 'a different floor plan → keeps apart (dismisses the pair)',
  same_unit: 'the same unit within one development → would MERGE',
  different_unit: 'a different unit → keeps apart',
  inconclusive: 'could not tell → makes no call',
  no_2d_plan: 'no 2-D plan to read → makes no call',
};

/** The plain meaning of a verdict, falling back to the raw value for anything unmapped. */
export const verdictGloss = (v: string): string => VERDICT_GLOSS[v] ?? v;

/** Per-lane plain phrasing for the legend's "votes merge when it sees…" / "…otherwise" columns. */
export const LANE_MERGE_GLOSS: Record<Lane, string> = {
  compare: 'a room photo it’s confident matches',
  floor_plan: 'the two floor plans are the same layout',
  site_plan: 'the same unit within one development',
};
export const LANE_KEEP_GLOSS: Record<Lane, string> = {
  compare: 'Medium / Low',
  floor_plan: 'different_layout',
  site_plan: 'different_unit',
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
  is_correct: boolean | null; // null for review rows (no ground truth)
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

export interface ReviewStat {
  n: number;
  mergeVotes: number; // rows where the model emitted a MERGE verdict (is_dangerous)
  pct: number | null; // mergeVotes / n — the would-merge rate on undecided pairs
}

export interface LaneStat {
  recall: CellStat;
  precision: CellStat;
  review: ReviewStat;
}

export interface ModelStat {
  lanes: Record<Lane, LaneStat>;
  totalCostUsd: number;
  callCount: number; // rows carrying a cost — the denominator for $/call
}

/** Summary matrix per model: recall (reproduce the cached verdict), precision (avoid the dangerous
 * verdict on a confirmed-different pair), review (would-merge rate on undecided pairs), and cost
 * (total $ + $/call across the run). */
export const summarize = (rows: readonly BakeoffRow[]): Map<string, ModelStat> => {
  const out = new Map<string, ModelStat>();
  for (const r of rows) {
    let m = out.get(r.model);
    if (!m) {
      m = { lanes: { compare: emptyLane(), floor_plan: emptyLane(), site_plan: emptyLane() }, totalCostUsd: 0, callCount: 0 };
      out.set(r.model, m);
    }
    if (r.cost_usd != null) {
      m.totalCostUsd += r.cost_usd;
      m.callCount += 1;
    }
    const lane = m.lanes[r.lane];
    if (r.check_type === 'review') {
      lane.review.n += 1;
      if (r.is_dangerous) lane.review.mergeVotes += 1;
      lane.review.pct = lane.review.mergeVotes / lane.review.n;
    } else {
      const cell = r.check_type === 'recall' ? lane.recall : lane.precision;
      cell.n += 1;
      if (r.is_correct) cell.correct += 1;
      cell.pct = cell.correct / cell.n;
    }
  }
  return out;
};

/** $/call for the run, or null when the model made no costed calls. */
export const costPerCall = (m: ModelStat | undefined): number | null =>
  m && m.callCount > 0 ? m.totalCostUsd / m.callCount : null;

/** True when this run is a decision-support "review" set (no ground truth) rather than a golden
 * benchmark — the summary then shows would-merge votes instead of recall/precision. */
export const isReviewRun = (rows: readonly BakeoffRow[]): boolean =>
  rows.length > 0 && rows.every((r) => r.check_type === 'review');

const emptyLane = (): LaneStat => ({
  recall: { n: 0, correct: 0, pct: null },
  precision: { n: 0, correct: 0, pct: null },
  review: { n: 0, mergeVotes: 0, pct: null },
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
  /** true if ANY model emitted a MERGE verdict on a pair whose ground truth is DIFFERENT
   * (is_same === false) — an actual false merge. A merge verdict on a same-property pair (e.g.
   * reproducing High on a compare recall pair) is correct, not dangerous, so it does NOT count. */
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
    if (r.is_dangerous && r.is_same === false) g.anyDangerous = true;
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
