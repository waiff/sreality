/* Price-stats datasets (ceny-nemovitosti): public-view fetchers + types.
 * Reads only the *_public views (anon SELECT); writes go through the API
 * (src/lib/api.ts). Mirrors the fetch* conventions in src/lib/queries.ts. */
import { supabase } from './supabase';

export interface PriceStatDataset {
  id: number;
  slug: string;
  name: string;
  description: string | null;
  category_main_cb: number;
  building_condition: string | null;
  building_type: string | null;
  ownership: string | null;
  usable_area_from: number | null;
  usable_area_to: number | null;
  distance: number;
  start_ym?: string | null;
  end_ym?: string | null;
  obec_ids?: number[] | null;
  min_population?: number | null;
  max_population?: number | null;
  periodicity?: 'monthly' | 'quarterly' | 'semiannual' | 'annual';
}

export interface PriceStatCityMetric {
  dataset_id: number;
  entity_type: string;
  entity_id: number;
  locality_name: string;
  obec_id: number | null;
  window_years: number;
  sale_latest_price: number | null;
  sale_latest_ym: string | null;
  sale_cagr_pct: number | null;
  sale_months: number | null;
  sale_min_active: number | null;
  rent_latest_price: number | null;
  rent_latest_ym: string | null;
  rent_cagr_pct: number | null;
  rent_months: number | null;
  rent_min_active: number | null;
  gross_yield_pct: number | null;
  computed_at: string;
}

export interface PriceStatPolygon {
  dataset_id: number;
  obec_id: number;
  obec_name: string;
  geojson: string;
  sale_cagr_pct: number | null;
  rent_cagr_pct: number | null;
  gross_yield_pct: number | null;
  sale_latest_price: number | null;
  rent_latest_price: number | null;
  sale_min_active: number | null;
  rent_min_active: number | null;
}

export interface PriceStatObservation {
  category_type_cb: number; // 1 prodej, 2 pronajem
  year: number;
  month: number;
  price: number | null;
  active_count: number | null;
  new_count: number | null;
  deleted_count: number | null;
}

export interface PriceStatRun {
  dataset_id: number;
  run_id: number;
  status: 'running' | 'success' | 'failed';
  cities_total: number;
  cities_done: number;
  observations: number;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export const fetchLatestRun = async (datasetId: number): Promise<PriceStatRun | null> => {
  const { data, error } = await supabase
    .from('price_stat_runs_public')
    .select('*')
    .eq('dataset_id', datasetId)
    .maybeSingle();
  if (error) throw error;
  return (data as PriceStatRun | null) ?? null;
};

export const priceStatsKeys = {
  datasets: ['price_stat_datasets'] as const,
  latestRun: (id: number) => ['price_stat_latest_run', id] as const,
  cityMetrics: (id: number) => ['price_stat_city_metrics', id] as const,
  choropleth: (id: number) => ['price_stat_choropleth', id] as const,
  growth: (id: number, from: string | null, to: string | null) =>
    ['price_stat_growth', id, from, to] as const,
  obecTree: ['price_stat_obce_tree'] as const,
  series: (id: number, t: string, e: number) =>
    ['price_stat_series', id, t, e] as const,
  obecSeries: (id: number, from: string | null, to: string | null) =>
    ['price_stat_obec_series', id, from, to] as const,
};

export const fetchDatasets = async (): Promise<PriceStatDataset[]> => {
  const { data, error } = await supabase
    .from('price_stat_datasets_public')
    .select('*')
    .order('name');
  if (error) throw error;
  return (data ?? []) as unknown as PriceStatDataset[];
};

export const fetchCityMetrics = async (
  datasetId: number,
): Promise<PriceStatCityMetric[]> => {
  const { data, error } = await supabase
    .from('price_stat_city_metrics_public')
    .select('*')
    .eq('dataset_id', datasetId)
    .order('locality_name')
    .range(0, 9999);
  if (error) throw error;
  return (data ?? []) as unknown as PriceStatCityMetric[];
};

export const fetchChoropleth = async (
  datasetId: number,
): Promise<PriceStatPolygon[]> => {
  const { data, error } = await supabase
    .from('price_stat_choropleth_public')
    .select('*')
    .eq('dataset_id', datasetId)
    .range(0, 9999);
  if (error) throw error;
  return (data ?? []) as unknown as PriceStatPolygon[];
};

/* Live per-obec growth for any [from,to] window via the price_stat_growth RPC
 * (computed from observations server-side; no re-scrape). Drives the revamped
 * Datasets page + the Browse overlay. from/to are 'YYYY-MM' or null (open). */
export interface PriceStatGrowthRow {
  obec_id: number;
  locality_name: string;
  geojson: string;
  sale_latest_price: number | null;
  sale_cagr_pct: number | null;
  sale_min_active: number | null;
  rent_latest_price: number | null;
  rent_cagr_pct: number | null;
  rent_min_active: number | null;
  gross_yield_pct: number | null;
  yield_change_pp_pa: number | null;
}

export const fetchGrowth = async (
  datasetId: number,
  from: string | null,
  to: string | null,
): Promise<PriceStatGrowthRow[]> => {
  const { data, error } = await supabase.rpc('price_stat_growth', {
    p_dataset_id: datasetId,
    p_from: from,
    p_to: to,
  });
  if (error) throw error;
  return (data ?? []) as PriceStatGrowthRow[];
};

/* Per-obec monthly sale + rent price for the map hover-chart
 * (price_stat_series RPC). The frontend derives the metric's variable. */
export interface PriceStatSeriesRow {
  obec_id: number;
  year: number;
  month: number;
  sale_price: number | null;
  rent_price: number | null;
}

export const fetchSeries = async (
  datasetId: number,
  from: string | null,
  to: string | null,
): Promise<PriceStatSeriesRow[]> => {
  const { data, error } = await supabase.rpc('price_stat_series', {
    p_dataset_id: datasetId,
    p_from: from,
    p_to: to,
  });
  if (error) throw error;
  return (data ?? []) as PriceStatSeriesRow[];
};

/* The kraj→okres→obec tree for the city picker (no geometry). */
export interface ObecNode {
  id: number;
  level: 'kraj' | 'okres' | 'obec';
  name: string;
  parent_id: number | null;
  population: number | null;
  sreality_id: number | null;
}

export const fetchObecTree = async (): Promise<ObecNode[]> => {
  const { data, error } = await supabase
    .from('price_stat_obce_picker_public')
    .select('id,level,name,parent_id,population,sreality_id')
    .order('name')
    .range(0, 9999);
  if (error) throw error;
  return (data ?? []) as unknown as ObecNode[];
};

export const fetchCitySeries = async (
  datasetId: number,
  entityType: string,
  entityId: number,
): Promise<PriceStatObservation[]> => {
  const { data, error } = await supabase
    .from('price_stat_observations_public')
    .select(
      'category_type_cb,year,month,price,active_count,new_count,deleted_count',
    )
    .eq('dataset_id', datasetId)
    .eq('entity_type', entityType)
    .eq('entity_id', entityId)
    .order('year')
    .order('month')
    .range(0, 9999);
  if (error) throw error;
  return (data ?? []) as unknown as PriceStatObservation[];
};
