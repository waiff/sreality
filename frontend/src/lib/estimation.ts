/* Helpers that turn EstimateForm state + ResolvedInput into a
 * CreateEstimationIn body the API will accept.
 *
 * The API contract requires *exactly one* of `url` or `spec`. When
 * the origin is a URL we send `url` (so the run row records
 * `input_url` for traceability) plus a minimal `spec_overrides` diff
 * containing only the fields the user changed after the scrape.
 * When the origin is "from a listing" we send `spec` directly.
 *
 * The five cohort-search knobs (radius, area band, disposition
 * match, age window, active-only) are NOT included here: the agent
 * chooses them per-iteration. UI submissions always set
 * `mode: 'agent'`.
 */

import { triToBool, type EstimateFormState } from '@/components/EstimateForm';
import type { ResolvedInput } from '@/components/UrlScrapeStep';
import type { CreateEstimationIn, TargetSpecIn } from '@/lib/types';

export function buildEstimationPayload(
  form: EstimateFormState,
  resolved: ResolvedInput,
): CreateEstimationIn {
  const editedSpec: TargetSpecIn = {
    lat: form.lat as number,
    lng: form.lng as number,
    area_m2: form.area_m2,
    disposition: form.disposition,
    floor: form.floor,
    exclude_ids: form.exclude_ids,
  };

  const filters = {
    population: form.population,
    has_balcony: triToBool(form.has_balcony),
    has_lift: triToBool(form.has_lift),
    has_parking: triToBool(form.has_parking),
    /* Migration 022 — granular target attributes thread through to
     * ComparableFilters so the comparable cohort respects them. */
    terrace: triToBool(form.terrace),
    cellar: triToBool(form.cellar),
    garage: triToBool(form.garage),
    furnished: form.furnished,
    ownership: form.ownership,
    category_sub_cb: form.category_sub_cb,
  };

  const yieldFields = {
    purchase_price_czk: form.purchase_price_czk,
    expected_monthly_rent_czk: form.expected_monthly_rent_czk,
  };

  // Rentals route through the agent (Phase 7 deliverable). Sales stay
  // on the deterministic path until a sales_estimator_v1 skill ships
  // — `api/agent.py` is rental-only today (initial prompt, terminator
  // tool schema, and _finalise all assume rent fields).
  const mode = form.estimate_kind === 'rent' ? 'agent' : 'deterministic';

  if (resolved.origin.kind === 'url') {
    const overrides = computeSpecOverrides(resolved.spec, editedSpec);
    return {
      source: 'ui',
      mode,
      provider: form.provider,
      estimate_kind: form.estimate_kind,
      url: resolved.origin.url,
      ...(overrides ? { spec_overrides: overrides } : {}),
      ...yieldFields,
      ...filters,
    };
  }

  return {
    source: 'ui',
    mode,
    provider: form.provider,
    estimate_kind: form.estimate_kind,
    spec: editedSpec,
    ...yieldFields,
    ...filters,
  };
}

function computeSpecOverrides(
  scraped: TargetSpecIn,
  edited: TargetSpecIn,
): Partial<TargetSpecIn> | null {
  const overrides: Partial<TargetSpecIn> = {};
  let dirty = false;
  if (edited.lat !== scraped.lat) {
    overrides.lat = edited.lat;
    dirty = true;
  }
  if (edited.lng !== scraped.lng) {
    overrides.lng = edited.lng;
    dirty = true;
  }
  if (edited.area_m2 !== scraped.area_m2) {
    overrides.area_m2 = edited.area_m2;
    dirty = true;
  }
  if (edited.disposition !== scraped.disposition) {
    overrides.disposition = edited.disposition;
    dirty = true;
  }
  if (edited.floor !== scraped.floor) {
    overrides.floor = edited.floor;
    dirty = true;
  }
  if (!sameNumberArray(edited.exclude_ids, scraped.exclude_ids)) {
    overrides.exclude_ids = edited.exclude_ids;
    dirty = true;
  }
  return dirty ? overrides : null;
}

function sameNumberArray(a: number[], b: number[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}
