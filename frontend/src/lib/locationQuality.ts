/* Location quality (location program W1v) — typed wrappers over the
 * admin-gated `/location/*` API. Every call is `jwt: true`: the location
 * tables are service-role-only, so the identity-gated API is the SPA's only
 * path to them. Envelope shape follows toolkit convention:
 * `{ data, metadata }`. */

import { apiGet, apiPost } from './api';

export type MixRow = { value: string | null; n: number };

export type SourceOverview = {
  source: string;
  grain: 'listing';
  totals: {
    active_rows: number;
    street_or_better: number;
    building_or_better: number;
    geo_blockable: number;
    renderable_as_point: number;
    low_precision: number;
    disputed: number;
    with_adm_kod: number;
    with_stavebni_objekt: number;
    with_ulice_kod: number;
    with_parcela: number;
  };
  mixes: Record<string, MixRow[]>;
  pin_histogram: { bucket: string; collision_class: string; n: number }[];
  top_clusters: {
    cell_key: string;
    listing_count: number;
    distinct_streets: number;
    distinct_obec_kods: number;
    classification: string;
    declared_blur_share: number | null;
    nearest_admin_unit: string | null;
  }[];
  current_registry: { label: string; loaded_at: string } | null;
};

export type CorpusSummaryRow = {
  source: string;
  active_rows: number;
  street_or_better: number;
  building_or_better: number;
  geo_blockable: number;
  disputed: number;
  with_adm_kod: number;
};

export type W1vGate = {
  active_rows: number;
  with_ruian_claim: number;
  claim_matches_one_point: number;
  projection_r0: number;
  projection_building_or_better: number;
  primary_pct: number | null;
  fallback_pct: number | null;
  primary_pass: boolean;
  fallback_pass: boolean;
};

export type InspectorClaim = {
  id: number;
  claim_type: string;
  surface: string;
  extraction_method: string;
  extractor_id: string;
  value_text: string | null;
  value_num: number | null;
  licence_class: string;
  claim_confidence: string | null;
  blur_evidence: string;
  first_observed_at: string;
  subject_scoped: boolean | null;
};

export type InspectorCandidate = {
  rank: number;
  score: number;
  target_kind: string;
  granularity: string;
  position_source: string;
  match_confidence: string;
  component_match: Record<string, string> | null;
  distance_to_pin_m: number | null;
  rejected_reason: string | null;
};

export type Inspector = {
  listing_id: number;
  projection: Record<string, unknown> | null;
  claims: InspectorClaim[];
  candidates: InspectorCandidate[];
};

export type SampleMember = {
  listing_id: number;
  source_id_native: string;
  position: number;
  label_street: string | null;
  label_street_nd: boolean;
  label_house_number: string | null;
  label_house_number_nd: boolean;
  label_obec: string | null;
  label_obec_nd: boolean;
  label_okres: string | null;
  label_okres_nd: boolean;
  label_precision_class: string | null;
  label_precision_nd: boolean;
  label_note: string | null;
  labelled_at: string | null;
  is_active: boolean | null;
  source_url: string | null;
};

export type SampleStatus = {
  sample: {
    id: number;
    source: string;
    drawn_at: string;
    method: string;
    n: number;
    note: string | null;
    members: number;
    labelled: number;
  } | null;
  members: SampleMember[];
};

export type ScoreSide = {
  asserted: number;
  matches: number;
  precision_pct: number | null;
  yield_pct: number | null;
  floor_pass?: boolean;
};

export type ScoreBlock = {
  determinable: number;
  new: ScoreSide;
  old?: ScoreSide;
  floor_pct: number;
};

export type SampleScore = {
  source: string;
  grain: 'listing';
  labelled: number;
  street: ScoreBlock;
  obec: ScoreBlock;
  okres: ScoreBlock;
  precision_class: ScoreBlock;
};

export type CorrectionResult = {
  listing_id: number;
  source: string;
  claim_type: string;
  value_text: string;
  inserted: boolean;
  restatement: boolean;
  enqueued: boolean;
  registry_echo: Record<string, unknown> | null;
  resolved: boolean;
  projection: Record<string, unknown> | null;
};

type Envelope<T> = { data: T; metadata?: Record<string, unknown> };

export const LOCATION_SOURCES = [
  'bezrealitky', 'sreality', 'bazos', 'idnes', 'mmreality', 'remax',
  'ceskereality', 'realitymix', 'maxima',
] as const;

export const GRANULARITY_VALUES = [
  'address_point', 'building', 'parcel', 'street_segment', 'street',
  'cast_obce_or_quarter', 'obec', 'okres', 'kraj', 'country', 'unknown',
] as const;

export const fetchCorpusSummary = () =>
  apiGet<Envelope<{ grain: string; sources: CorpusSummaryRow[] }>>(
    '/location/quality/summary', undefined, undefined, true);

export const fetchSourceOverview = (source: string) =>
  apiGet<Envelope<SourceOverview>>(
    `/location/quality/source/${source}`, undefined, undefined, true);

export const fetchW1vGate = () =>
  apiGet<Envelope<W1vGate>>('/location/quality/w1v-gate', undefined, undefined, true);

export const fetchInspector = (listingId: string) => {
  const trimmed = listingId.trim();
  return /^\d+$/.test(trimmed)
    ? apiGet<Envelope<Inspector>>(`/location/listing/${trimmed}`, undefined, undefined, true)
    : Promise.reject(new Error('enter a numeric listing id'));
};

export const fetchInspectorByNative = (source: string, nativeId: string) =>
  apiGet<Envelope<Inspector>>(
    `/location/listing/by-native/${source}/${encodeURIComponent(nativeId.trim())}`,
    undefined, undefined, true);

export const fetchSample = (source: string, unlabelledOnly: boolean) =>
  apiGet<Envelope<SampleStatus>>(
    `/location/sample/${source}`,
    { unlabelled_only: unlabelledOnly, limit: 200 },
    undefined, true);

export const saveMemberLabels = (
  source: string, listingId: number, labels: Record<string, unknown>,
) =>
  apiPost<Envelope<{ saved: boolean }>>(
    `/location/sample/${source}/labels`,
    { listing_id: listingId, labels },
    undefined, true);

export const fetchSampleScore = (source: string) =>
  apiGet<Envelope<SampleScore>>(
    `/location/sample/${source}/score`, undefined, undefined, true);

export const submitCorrection = (input: {
  listing_id: number;
  claim_type: string;
  value_text: string;
  note?: string;
}) =>
  apiPost<Envelope<CorrectionResult>>('/location/corrections', input, undefined, true);
