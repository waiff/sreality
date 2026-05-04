/* Wire shapes mirroring the public views from migration 008. Only the columns
 * the UI actually reads are typed here; expand as Parts B–E need more. */

export type Disposition =
  | '1+kk' | '1+1'
  | '2+kk' | '2+1'
  | '3+kk' | '3+1'
  | '4+kk' | '4+1'
  | '5+kk' | '5+1';

export interface ListingPublic {
  sreality_id: number;
  first_seen_at: string;
  last_seen_at: string;
  is_active: boolean;
  category_main: number | null;
  category_type: number | null;
  price_czk: number | null;
  price_unit: string | null;
  area_m2: number | null;
  disposition: Disposition | null;
  locality: string | null;
  district: string | null;
  locality_district_id: number | null;
  locality_region_id: number | null;
  lat: number | null;
  lng: number | null;
  floor: number | null;
  total_floors: number | null;
  has_balcony: boolean | null;
  has_parking: boolean | null;
  has_lift: boolean | null;
  building_type: string | null;
  condition: string | null;
  energy_rating: string | null;
}

export interface ListingSnapshotPublic {
  id: number;
  sreality_id: number;
  scraped_at: string;
  price_czk: number | null;
}

export interface ListingFreshnessCheckPublic {
  id: number;
  sreality_id: number;
  checked_at: string;
  outcome: string;
}

export interface ListingFetchFailurePublic {
  sreality_id: number;
  attempts: number;
  first_failure_at: string;
  last_failure_at: string;
  given_up: boolean;
}

export interface ImagePublic {
  id: number;
  sreality_id: number;
  sequence: number | null;
  sreality_url: string;
  storage_path: string | null;
}

/* Region page payloads — shape mirrors migration 012 RPCs. */

export interface PercentileTriple {
  p25: number;
  p50: number;
  p75: number;
}

export interface RegionDispositionRow {
  disposition: string;
  n: number;
  median_price: number | null;
  median_ppm2: number | null;
  median_area: number | null;
}

export interface RegionStats {
  total_active: number;
  total_ever: number;
  last_new_first_seen: string | null;
  price: PercentileTriple | null;
  ppm2: PercentileTriple | null;
  dispositions: RegionDispositionRow[];
  tom_median_days: number | null;
  tom_n: number;
}

export interface ActiveByDayRow {
  day: string;
  active: number;
  new: number;
}

/* Health dashboard payload — shape mirrors migration 013 health_summary RPC. */

export interface HealthDayCount {
  day: string;
  n: number;
}

export interface HealthSnapBucket {
  bucket: '1' | '2' | '3' | '4+' | string;
  n: number;
}

export interface HealthFreshnessRow {
  outcome: string;
  n: number;
}

export interface HealthFailureRow {
  sreality_id: number;
  attempts: number;
  first_failure_at: string;
  last_failure_at: string | null;
  given_up: boolean;
}

export interface HealthSummary {
  last_scrape_at: string | null;
  active_now: number;
  active_7d_ago: number;
  flipped_inactive_7d: number;
  new_per_day_14d: HealthDayCount[];
  flipped_per_day_7d: HealthDayCount[];
  snapshot_density: HealthSnapBucket[];
  freshness_24h: HealthFreshnessRow[];
  failures_given_up: number;
  failures_total: number;
  failures_top10: HealthFailureRow[];
}
