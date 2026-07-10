/* The ONE shared registry of dedup-funnel steps, used by BOTH the /dedup
 * funnel and the /costs dedup grouping so the two tabs can never disagree
 * about what a step is, whether it's free or paid, or which llm_calls
 * feature tag belongs to it. Ordering mirrors resolve_pair (rule #15):
 * cheap deterministic rules first, then the free visual tiers, then paid
 * vision, then the operator queue. */

export type FunnelKind = 'free' | 'paid' | 'manual';

export interface FunnelStepDef {
  id: string;
  /* dedup_pair_audit.stage this step resolves under, if it resolves pairs */
  auditStage?: 'address' | 'phash' | 'attr' | 'visual' | 'operator';
  label: string;
  kind: FunnelKind;
  /* llm_calls.called_for tags whose spend belongs to this step */
  calledFor: string[];
  /* #anchor on /dedup the /costs rows deep-link to */
  anchor: string;
  description: string;
}

export const DEDUP_FUNNEL_STEPS: readonly FunnelStepDef[] = [
  {
    id: 'considered',
    label: 'Pairs evaluated',
    kind: 'free',
    calledFor: [],
    anchor: 'funnel-considered',
    description:
      'Candidate pairs the engine evaluated (re-scans of the same group count each time).',
  },
  {
    id: 'rejected',
    label: 'Rule rejects',
    kind: 'free',
    calledFor: [],
    anchor: 'funnel-rejected',
    description:
      'Hard contradictions — area gap, floor gap ≥ 2, house number, unit markers. No LLM.',
  },
  {
    id: 'address',
    auditStage: 'address',
    label: 'Exact address',
    kind: 'free',
    calledFor: [],
    anchor: 'funnel-address',
    description:
      'Rule B — same street + number + disposition + floor. Retired as an auto-merge (2026-06); ' +
      'now queues as a candidate, so recent rows here are operator/legacy resolutions. No LLM.',
  },
  {
    id: 'phash',
    auditStage: 'phash',
    label: 'pHash fast-path',
    kind: 'free',
    calledFor: [],
    anchor: 'funnel-phash',
    description:
      'Near-identical photo hashes (≥2 pairs, or one distinctive room) resolve with no LLM.',
  },
  {
    id: 'attr',
    auditStage: 'attr',
    label: 'Area + exact price (houses / land)',
    kind: 'free',
    calledFor: [],
    anchor: 'funnel-attr',
    description:
      'Non-apartment candidates whose areas match within 2% and asking prices are identical ' +
      'auto-merge through the floor-plan gate with no paid room compare. Validated 99.6% vs the ' +
      'forensic verdict. No LLM.',
  },
  {
    id: 'clip',
    label: 'CLIP tier',
    kind: 'free',
    calledFor: [],
    anchor: 'funnel-clip',
    description:
      'Self-hosted CLIP tags rooms and routes each compare to Haiku / Sonnet / skip by cosine. Free (runs on Actions).',
  },
  {
    id: 'visual',
    auditStage: 'visual',
    label: 'Forensic visual compare',
    kind: 'paid',
    calledFor: ['compare_listings_visually', 'classify_listing_images'],
    anchor: 'funnel-visual',
    description:
      'Room-by-room vision compare; a High verdict is the only auto-merge gate. Paid vision.',
  },
  {
    id: 'floor_plan',
    label: 'Floor-plan gate',
    kind: 'paid',
    calledFor: ['compare_listing_floor_plans'],
    anchor: 'funnel-floor-plan',
    description:
      'Validates would-be merges plan-to-plan; different_layout is the only new auto-dismiss. Paid vision.',
  },
  {
    id: 'site_plan',
    label: 'Site-plan guard',
    kind: 'paid',
    calledFor: ['compare_listing_site_plans'],
    anchor: 'funnel-site-plan',
    description:
      'Development guard — same-unit check on site plans; different_unit queues for review. Paid vision.',
  },
  {
    id: 'queue',
    label: 'Operator queue',
    kind: 'manual',
    calledFor: [],
    anchor: 'funnel-queue',
    description: 'Pairs no automatic step could resolve, awaiting review on this page.',
  },
  {
    id: 'operator',
    auditStage: 'operator',
    label: 'Operator decisions',
    kind: 'manual',
    calledFor: [],
    anchor: 'funnel-operator',
    description: 'Manual merges/dismissals from the review queue.',
  },
] as const;

/* called_for -> step (for grouping + deep links on /costs) */
export const DEDUP_CALLED_FOR_STEP: Record<string, FunnelStepDef> = Object.fromEntries(
  DEDUP_FUNNEL_STEPS.flatMap((s) => s.calledFor.map((cf) => [cf, s])),
);

export const isDedupCalledFor = (calledFor: string): boolean =>
  calledFor in DEDUP_CALLED_FOR_STEP;

export const stepByAuditStage = (stage: string): FunnelStepDef | undefined =>
  DEDUP_FUNNEL_STEPS.find((s) => s.auditStage === stage);

/* Category buckets the breakdowns render (no subcategories, per spec). */
export const CATEGORY_MAIN_ORDER = ['byt', 'dum', 'komercni', 'pozemek', 'ostatni'] as const;
export const CATEGORY_MAIN_LABELS: Record<string, string> = {
  byt: 'Byty',
  dum: 'Domy',
  komercni: 'Komerční',
  pozemek: 'Pozemky',
  ostatni: 'Ostatní',
};
export const CATEGORY_TYPE_ORDER = ['prodej', 'pronajem', 'ostatni'] as const;
export const CATEGORY_TYPE_LABELS: Record<string, string> = {
  prodej: 'Prodej',
  pronajem: 'Pronájem',
  ostatni: 'Ostatní',
};

export const categoryMainBucket = (raw: string | null | undefined): string =>
  raw && (CATEGORY_MAIN_ORDER as readonly string[]).includes(raw) ? raw : 'ostatni';

/* ---- Row types (the migration-282 views) ------------------------------- */

export interface DedupResolutionRow {
  source: string; // 'engine' | 'operator'
  stage: string; // 'address' | 'phash' | 'visual' | 'operator' | …
  outcome: string; // 'merged' | 'dismissed'
  category_main: string;
  category_type: string;
  pairs_7d: number;
  pairs_30d: number;
  properties_7d: number;
  properties_30d: number;
  listings_7d: number;
  listings_30d: number;
}

export interface DedupEngineFlowRow {
  eligible_market: number | null;
  flagged_location_market: number | null;
  flagged_disposition_market: number | null;
  runs_7d: number; runs_30d: number;
  pairs_considered_7d: number; pairs_considered_30d: number;
  rejected_7d: number; rejected_30d: number;
  queued_7d: number; queued_30d: number;
  clip_cosine_calls_7d: number; clip_cosine_calls_30d: number;
  routed_haiku_7d: number; routed_haiku_30d: number;
  routed_sonnet_7d: number; routed_sonnet_30d: number;
  floor_plan_deferred_7d: number; floor_plan_deferred_30d: number;
  clip_deferred_7d: number; clip_deferred_30d: number;
  skipped_unresolved_7d: number; skipped_unresolved_30d: number;
  vision_calls_7d: number; vision_calls_30d: number;
  vision_errors_7d: number; vision_errors_30d: number;
}

export interface DedupQueueRow {
  tier: string;
  category_main: string;
  category_type: string;
  pairs: number;
}

export interface DedupCostByCategoryRow {
  called_for: string;
  category_main: string;
  category_type: string;
  calls_7d: number;
  calls_30d: number;
  cost_7d: number;
  cost_30d: number;
  listings_7d: number;
  listings_30d: number;
}

export type FunnelWindow = 7 | 30;

/* ---- Category matrix (byty/domy/komerční/pozemky/ostatní × typ) -------- */

export interface CategoryCell {
  pairs?: number;
  properties?: number;
  cost?: number;
  calls?: number;
  listings?: number;
}

export type CategoryMatrix = Record<string, Record<string, CategoryCell>>;

const emptyMatrix = (): CategoryMatrix => {
  const m: CategoryMatrix = {};
  for (const cm of CATEGORY_MAIN_ORDER) {
    m[cm] = {};
    for (const ct of CATEGORY_TYPE_ORDER) m[cm][ct] = {};
  }
  return m;
};

const addCell = (m: CategoryMatrix, cm: string, ct: string, add: CategoryCell) => {
  const row = m[categoryMainBucket(cm)];
  const cell = row[ct in row ? ct : 'ostatni'] ?? (row[ct] = {});
  for (const k of ['pairs', 'properties', 'cost', 'calls', 'listings'] as const) {
    if (add[k] != null) cell[k] = (cell[k] ?? 0) + (add[k] as number);
  }
  return m;
};

export const matrixHasData = (m: CategoryMatrix): boolean =>
  Object.values(m).some((row) =>
    Object.values(row).some((c) => Object.values(c).some((v) => (v ?? 0) > 0)),
  );

/* ---- Funnel assembly ---------------------------------------------------- */

export interface FunnelStepView {
  def: FunnelStepDef;
  /* terminal resolutions (distinct pairs), when the step resolves pairs */
  merged: number;
  dismissed: number;
  properties: number;
  listings: number;
  /* work volume (evaluations; re-scans count each time) */
  evaluations: number | null;
  /* paid-lane spend from dedup_llm_cost_by_category (rolling window) */
  cost: number;
  calls: number;
  /* extra per-step figures, rendered as small hint chips */
  extras: Array<{ label: string; value: number }>;
  categories: CategoryMatrix;
}

const pick = <T extends object>(row: T, base: string, w: FunnelWindow): number =>
  Number((row as Record<string, unknown>)[`${base}_${w}d`] ?? 0);

export function assembleFunnel(
  resolutions: DedupResolutionRow[],
  flow: DedupEngineFlowRow | null,
  queue: DedupQueueRow[],
  costByCat: DedupCostByCategoryRow[],
  w: FunnelWindow,
): FunnelStepView[] {
  const f = (base: string): number => (flow ? pick(flow, base, w) : 0);

  const resFor = (step: FunnelStepDef) =>
    resolutions.filter((r) =>
      step.id === 'operator' ? r.source === 'operator' : r.source === 'engine' && r.stage === step.auditStage,
    );
  const costFor = (step: FunnelStepDef) =>
    costByCat.filter((c) => step.calledFor.includes(c.called_for));

  return DEDUP_FUNNEL_STEPS.map((def) => {
    const view: FunnelStepView = {
      def, merged: 0, dismissed: 0, properties: 0, listings: 0,
      evaluations: null, cost: 0, calls: 0, extras: [], categories: emptyMatrix(),
    };

    if (def.auditStage || def.id === 'operator') {
      for (const r of resFor(def)) {
        const pairs = pick(r, 'pairs', w);
        if (r.outcome === 'merged') view.merged += pairs;
        else if (r.outcome === 'dismissed') view.dismissed += pairs;
        view.properties += pick(r, 'properties', w);
        view.listings += pick(r, 'listings', w);
        addCell(view.categories, r.category_main, r.category_type, {
          pairs, properties: pick(r, 'properties', w),
        });
      }
    }

    for (const c of costFor(def)) {
      view.cost += pick(c, 'cost', w);
      view.calls += pick(c, 'calls', w);
      addCell(view.categories, c.category_main, c.category_type, {
        cost: pick(c, 'cost', w), calls: pick(c, 'calls', w), listings: pick(c, 'listings', w),
      });
    }

    switch (def.id) {
      case 'considered':
        view.evaluations = f('pairs_considered');
        view.extras.push({ label: 'engine runs', value: f('runs') });
        break;
      case 'rejected':
        view.evaluations = f('rejected');
        break;
      case 'clip':
        view.evaluations = f('clip_cosine_calls');
        view.extras.push(
          { label: '→ Haiku', value: f('routed_haiku') },
          { label: '→ Sonnet', value: f('routed_sonnet') },
          { label: 'deferred (tags pending)', value: f('clip_deferred') },
        );
        break;
      case 'visual':
        view.extras.push(
          { label: 'vision calls', value: f('vision_calls') },
          { label: 'vision errors', value: f('vision_errors') },
        );
        break;
      case 'floor_plan':
        view.extras.push({ label: 'deferred (budget)', value: f('floor_plan_deferred') });
        break;
      case 'queue': {
        view.evaluations = f('queued');
        let open = 0;
        for (const q of queue) {
          open += q.pairs;
          addCell(view.categories, q.category_main, q.category_type, { pairs: q.pairs });
        }
        view.extras.push({ label: 'open now', value: open });
        break;
      }
      default:
        break;
    }

    return view;
  });
}

/* Free-vs-paid capture summary for the funnel header strip. */
export interface FunnelCapture {
  freeResolved: number;
  paidResolved: number;
  manualResolved: number;
  paidCost: number;
}

export function summarizeCapture(steps: FunnelStepView[]): FunnelCapture {
  const out: FunnelCapture = { freeResolved: 0, paidResolved: 0, manualResolved: 0, paidCost: 0 };
  for (const s of steps) {
    const resolved = s.merged + s.dismissed;
    if (s.def.kind === 'free') out.freeResolved += resolved;
    else if (s.def.kind === 'paid') out.paidResolved += resolved;
    else out.manualResolved += resolved;
    out.paidCost += s.cost;
  }
  return out;
}

/* Pivot the cost-by-category rows into the matrix the /costs card and the
 * funnel's paid steps both render — one code path, identical numbers. */
export function pivotCostMatrix(
  rows: DedupCostByCategoryRow[],
  w: FunnelWindow,
): { matrix: CategoryMatrix; total: CategoryCell } {
  const matrix = emptyMatrix();
  const total: CategoryCell = { cost: 0, calls: 0, listings: 0 };
  for (const r of rows) {
    const cell: CategoryCell = {
      cost: pick(r, 'cost', w),
      calls: pick(r, 'calls', w),
      listings: pick(r, 'listings', w),
    };
    addCell(matrix, r.category_main, r.category_type, cell);
    total.cost = (total.cost ?? 0) + (cell.cost ?? 0);
    total.calls = (total.calls ?? 0) + (cell.calls ?? 0);
    total.listings = (total.listings ?? 0) + (cell.listings ?? 0);
  }
  return { matrix, total };
}
