/* Location quality — typed wrappers over the admin-gated `/location/*` API.
 * Every call is `jwt: true`: the location tables are service-role-only, so the
 * identity-gated API is the SPA's only path to them. Envelope shape follows
 * toolkit convention: `{ data, metadata }`.
 *
 * W2-b cut the page over to `listing_location` (the location program's one
 * answer table) and deleted the frozen-labelled-sample family with its two
 * tables — the samples existed to score old-vs-new precision, and "new" is the
 * only system now. */

import { apiGet, apiPost } from './api';

export type MixRow = { value: string | null; n: number };

export type SourceOverview = {
  source: string;
  grain: 'listing';
  totals: {
    active_rows: number;
    street_or_better: number;
    building_or_better: number;
    /* Rule 25's invariant, read per portal: every active Czech listing has a town. */
    with_obec_kod: number;
    with_adm_kod: number;
    with_ulice_kod: number;
    disputed: number;
    /* Rows still carrying an older registry label — drain lag, not data loss. */
    stale_registry: number;
  };
  mixes: Record<string, MixRow[]>;
  current_registry: { label: string; loaded_at: string } | null;
};

export type CorpusSummaryRow = {
  source: string;
  active_rows: number;
  street_or_better: number;
  building_or_better: number;
  with_obec_kod: number;
  disputed: number;
  with_adm_kod: number;
};

export type InspectorClaim = {
  id: number;
  claim_type: string;
  surface: string;
  extraction_method: string;
  value_text: string | null;
  value_num: number | null;
  licence_class: string;
  claim_confidence: string | null;
  blur_evidence: string;
  first_observed_at: string;
  subject_scoped: boolean | null;
};

export type Inspector = {
  listing_id: number;
  projection: Record<string, unknown> | null;
  claims: InspectorClaim[];
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

export const fetchCorpusSummary = () =>
  apiGet<Envelope<{ grain: string; sources: CorpusSummaryRow[] }>>(
    '/location/quality/summary', undefined, undefined, true);

export const fetchSourceOverview = (source: string) =>
  apiGet<Envelope<SourceOverview>>(
    `/location/quality/source/${source}`, undefined, undefined, true);

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

export const submitCorrection = (input: {
  listing_id: number;
  claim_type: string;
  value_text: string;
  note?: string;
}) =>
  apiPost<Envelope<CorrectionResult>>('/location/corrections', input, undefined, true);
