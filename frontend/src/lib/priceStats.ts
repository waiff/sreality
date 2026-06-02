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

export const priceStatsKeys = {
  datasets: ['price_stat_datasets'] as const,
  cityMetrics: (id: number) => ['price_stat_city_metrics', id] as const,
  choropleth: (id: number) => ['price_stat_choropleth', id] as const,
  series: (id: number, t: string, e: number) =>
    ['price_stat_series', id, t, e] as const,
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
