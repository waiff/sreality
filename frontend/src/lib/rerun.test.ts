/* A re-run keeps the subject's property out of its own cohort (decision 13):
 * a spec re-run resolves no subject, so it carries the parent's. */

import { describe, expect, it } from 'vitest';

import { buildRerunPayload } from './rerun';
import type { EstimationRun, TargetSpecIn } from './types';

const SPEC: TargetSpecIn = { lat: 50.1, lng: 14.4, area_m2: 60, disposition: '2+kk', floor: 3 };

const run = (over: Partial<EstimationRun>): EstimationRun =>
  ({
    id: 7,
    input_url: null,
    input_listing_id: null,
    input_spec: null,
    estimate_kind: 'rent',
    input_purchase_price_czk: null,
    special_instructions: null,
    contextual_text: null,
    ...over,
  }) as EstimationRun;

describe('buildRerunPayload', () => {
  it('an adjusted re-run of a watchdog kickoff keeps its exclusion', () => {
    const parent = run({ input_listing_id: 555, input_spec: { ...SPEC, exclude_listing_ids: [555] } });
    const body = buildRerunPayload(parent, { spec: { ...SPEC, area_m2: 62 } });
    expect(body.spec).toEqual({ ...SPEC, area_m2: 62, exclude_listing_ids: [555] });
    expect(body.parent_run_id).toBe(7);
  });

  it('an adjusted re-run of a URL-launched run excludes its resolved subject', () => {
    const parent = run({ input_url: 'https://www.sreality.cz/detail/x/1', input_listing_id: 901, input_spec: SPEC });
    expect(buildRerunPayload(parent, { spec: SPEC }).spec?.exclude_listing_ids).toEqual([901]);
    expect(buildRerunPayload(parent).url).toBe('https://www.sreality.cz/detail/x/1');
  });

  it('a same-inputs spec re-run keeps the stored exclusion; a subjectless spec carries none', () => {
    const kickoff = run({ input_listing_id: 555, input_spec: { ...SPEC, exclude_listing_ids: [555] } });
    expect(buildRerunPayload(kickoff).spec?.exclude_listing_ids).toEqual([555]);
    expect(buildRerunPayload(run({ input_spec: SPEC })).spec).toEqual(SPEC);
  });
});
