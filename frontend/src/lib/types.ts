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
