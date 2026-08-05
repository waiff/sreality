/* Pure analytics for the /costs LLM-spend dashboard. Everything here is
 * deterministic math over `llm_cost_daily_public` rows so it can be
 * unit-tested without the DB or React. */

export interface LlmCostDailyRow {
  day: string; // 'YYYY-MM-DD'
  called_for: string;
  provider: string;
  model: string;
  calls: number;
  error_calls: number;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
}

/* Color follows the ENTITY (the called_for tag), never its rank — a
 * feature keeps its hue when volumes shift or filters change. The order
 * below is also the chart's stack order; the ochre→slate→brick→teal→
 * plum→sage adjacency was validated for CVD separation against the
 * civic-archive tag tokens (light + dark). Unknown future tags hash
 * onto the spare tokens deterministically by name. */
export const FEATURE_COLOR_TOKENS: ReadonlyArray<readonly [string, string]> = [
  ['compare_listings_visually', '--color-tag-ochre'],
  ['compare_listing_floor_plans', '--color-tag-slate'],
  ['enrich_listing_description', '--color-tag-brick'],
  ['compare_listing_site_plans', '--color-tag-teal'],
  ['score_listing_condition', '--color-tag-plum'],
  ['classify_listing_images', '--color-tag-sage'],
  ['agent_estimation', '--color-tag-sand'],
];

export const OTHER_KEY = 'other';
export const OTHER_COLOR_TOKEN = '--color-ink-3';

const SPARE_TOKENS = ['--color-tag-copper', '--color-tag-sand', '--color-tag-teal'];

const nameHash = (s: string): number => {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h;
};

export function colorTokenFor(feature: string): string {
  if (feature === OTHER_KEY) return OTHER_COLOR_TOKEN;
  const fixed = FEATURE_COLOR_TOKENS.find(([name]) => name === feature);
  if (fixed) return fixed[1];
  return SPARE_TOKENS[nameHash(feature) % SPARE_TOKENS.length];
}

/* Short human labels for the audit tags; unknown tags fall back to the
 * raw tag so a new backend feature appears without a frontend release. */
const FEATURE_LABELS: Record<string, string> = {
  compare_listings_visually: 'Visual compare (dedup)',
  compare_listing_floor_plans: 'Floor-plan gate (dedup)',
  compare_listing_site_plans: 'Site-plan guard (dedup)',
  classify_listing_images: 'Image classify (dedup)',
  enrich_listing_description: 'Description enrichment',
  score_listing_condition: 'Condition scoring',
  agent_estimation: 'Estimation agent',
  summarize_listing: 'Listing summaries',
  summarize_region_dispositions: 'Region annotations',
  discover_condition_markers: 'Condition marker mining',
  parse_url: 'URL parser',
  refine_skill: 'Skill refiner',
  compare_listing_images: 'Image compare (estimation)',
  extract_building_units: 'Building extraction',
  read_floor_plan: 'Floor-plan reader',
  [OTHER_KEY]: 'Other',
};

export const featureLabel = (feature: string): string =>
  FEATURE_LABELS[feature] ?? feature;

const isoDay = (d: Date): string => d.toISOString().slice(0, 10);

const daysAgo = (now: Date, n: number): string => {
  const d = new Date(now);
  d.setUTCDate(d.getUTCDate() - n);
  return isoDay(d);
};

export interface CostKpis {
  today: number;
  last7: number;
  prev7: number;
  last30: number;
  projectedMonth: number; // 7-day average × 30
  calls7: number;
  errors7: number;
}

export function computeKpis(rows: LlmCostDailyRow[], now: Date): CostKpis {
  const today = isoDay(now);
  const d7 = daysAgo(now, 7);
  const d14 = daysAgo(now, 14);
  const d30 = daysAgo(now, 30);
  const k: CostKpis = {
    today: 0, last7: 0, prev7: 0, last30: 0,
    projectedMonth: 0, calls7: 0, errors7: 0,
  };
  for (const r of rows) {
    if (r.day === today) k.today += r.cost_usd;
    if (r.day > d7) {
      k.last7 += r.cost_usd;
      k.calls7 += r.calls;
      k.errors7 += r.error_calls;
    } else if (r.day > d14) {
      k.prev7 += r.cost_usd;
    }
    if (r.day > d30) k.last30 += r.cost_usd;
  }
  k.projectedMonth = (k.last7 / 7) * 30;
  return k;
}

export interface DailySeries {
  /* One row per calendar day (gaps zero-filled): { day, [feature]: cost } */
  data: Array<Record<string, string | number>>;
  /* Features present, in fixed stack/palette order; may end with 'other'. */
  features: string[];
}

/* Fixed stack order: canonical order first, then any top features the
 * canon doesn't know (alphabetical for determinism), then 'other'. */
function orderTopFeatures(
  totals: Map<string, number>,
  maxFeatures: number,
): { features: string[]; top: Set<string> } {
  const top = new Set(
    [...totals.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, maxFeatures)
      .map(([name]) => name),
  );
  const canonical = FEATURE_COLOR_TOKENS.map(([name]) => name).filter((n) => top.has(n));
  const extras = [...top].filter((n) => !canonical.includes(n)).sort();
  const hasOther = [...totals.keys()].some((n) => !top.has(n));
  return { features: [...canonical, ...extras, ...(hasOther ? [OTHER_KEY] : [])], top };
}

export function buildDailySeries(
  rows: LlmCostDailyRow[],
  now: Date,
  windowDays: number,
  maxFeatures = 6,
): DailySeries {
  const from = daysAgo(now, windowDays - 1);
  const inWindow = rows.filter((r) => r.day >= from);

  const totals = new Map<string, number>();
  for (const r of inWindow) {
    totals.set(r.called_for, (totals.get(r.called_for) ?? 0) + r.cost_usd);
  }
  const { features, top } = orderTopFeatures(totals, maxFeatures);

  const byDay = new Map<string, Record<string, string | number>>();
  for (let i = windowDays - 1; i >= 0; i--) {
    const day = daysAgo(now, i);
    const blank: Record<string, string | number> = { day };
    for (const f of features) blank[f] = 0;
    byDay.set(day, blank);
  }
  for (const r of inWindow) {
    const rec = byDay.get(r.day);
    if (!rec) continue;
    const key = top.has(r.called_for) ? r.called_for : OTHER_KEY;
    rec[key] = ((rec[key] as number) ?? 0) + r.cost_usd;
  }
  return { data: [...byDay.values()], features };
}

/* Hour-grain twin of the daily row, from `llm_cost_hourly_public`
 * (migration 281). `bucket` is the normalized ISO of the UTC hour start. */
export interface LlmCostHourlyRow extends Omit<LlmCostDailyRow, 'day'> {
  bucket: string;
}

const hourStart = (d: Date): Date => {
  const h = new Date(d);
  h.setUTCMinutes(0, 0, 0);
  return h;
};

const hoursAgoIso = (now: Date, n: number): string => {
  const d = hourStart(now);
  d.setUTCHours(d.getUTCHours() - n);
  return d.toISOString();
};

export function buildHourlySeries(
  rows: LlmCostHourlyRow[],
  now: Date,
  windowHours: number,
  maxFeatures = 6,
): DailySeries {
  const from = hoursAgoIso(now, windowHours - 1);
  const inWindow = rows.filter((r) => r.bucket >= from);

  const totals = new Map<string, number>();
  for (const r of inWindow) {
    totals.set(r.called_for, (totals.get(r.called_for) ?? 0) + r.cost_usd);
  }
  const { features, top } = orderTopFeatures(totals, maxFeatures);

  const byHour = new Map<string, Record<string, string | number>>();
  for (let i = windowHours - 1; i >= 0; i--) {
    const bucket = hoursAgoIso(now, i);
    const blank: Record<string, string | number> = { bucket };
    for (const f of features) blank[f] = 0;
    byHour.set(bucket, blank);
  }
  for (const r of inWindow) {
    const rec = byHour.get(r.bucket);
    if (!rec) continue;
    const key = top.has(r.called_for) ? r.called_for : OTHER_KEY;
    rec[key] = ((rec[key] as number) ?? 0) + r.cost_usd;
  }
  return { data: [...byHour.values()], features };
}

export interface FeatureSummary {
  feature: string;
  models: string[];
  calls7: number;
  errors7: number;
  cost7: number;
  avgPerCall7: number | null;
  cost30: number;
  share30: number; // 0..1 of the 30-day total
}

export function summarizeByFeature(rows: LlmCostDailyRow[], now: Date): FeatureSummary[] {
  const d7 = daysAgo(now, 7);
  const d30 = daysAgo(now, 30);
  const acc = new Map<string, FeatureSummary & { modelSet: Set<string> }>();
  let total30 = 0;
  for (const r of rows) {
    if (r.day <= d30) continue;
    let s = acc.get(r.called_for);
    if (!s) {
      s = {
        feature: r.called_for, models: [], modelSet: new Set(),
        calls7: 0, errors7: 0, cost7: 0, avgPerCall7: null, cost30: 0, share30: 0,
      };
      acc.set(r.called_for, s);
    }
    s.modelSet.add(r.model);
    s.cost30 += r.cost_usd;
    total30 += r.cost_usd;
    if (r.day > d7) {
      s.calls7 += r.calls;
      s.errors7 += r.error_calls;
      s.cost7 += r.cost_usd;
    }
  }
  return [...acc.values()]
    .map((s) => ({
      ...s,
      models: [...s.modelSet].sort(),
      avgPerCall7: s.calls7 > 0 ? s.cost7 / s.calls7 : null,
      share30: total30 > 0 ? s.cost30 / total30 : 0,
    }))
    .sort((a, b) => b.cost30 - a.cost30);
}

export interface ModelSummary {
  model: string;
  provider: string;
  calls30: number;
  cost30: number;
  share30: number;
}

export function summarizeByModel(rows: LlmCostDailyRow[], now: Date): ModelSummary[] {
  const d30 = daysAgo(now, 30);
  const acc = new Map<string, ModelSummary>();
  let total = 0;
  for (const r of rows) {
    if (r.day <= d30) continue;
    const key = `${r.provider}/${r.model}`;
    let s = acc.get(key);
    if (!s) {
      s = { model: r.model, provider: r.provider, calls30: 0, cost30: 0, share30: 0 };
      acc.set(key, s);
    }
    s.calls30 += r.calls;
    s.cost30 += r.cost_usd;
    total += r.cost_usd;
  }
  return [...acc.values()]
    .map((s) => ({ ...s, share30: total > 0 ? s.cost30 / total : 0 }))
    .sort((a, b) => b.cost30 - a.cost30);
}
