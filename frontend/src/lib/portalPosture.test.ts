import { describe, expect, it } from 'vitest';
import { portalPosture, PORTAL_POSTURE_LABEL } from './portalPosture';

describe('portalPosture', () => {
  it('an on-demand parser is on_demand regardless of supports_complete_walk', () => {
    expect(portalPosture({ kind: 'parser', supports_complete_walk: false })).toBe('on_demand');
    expect(portalPosture({ kind: 'parser', supports_complete_walk: true })).toBe('on_demand');
  });

  it('a disabled scraper is disabled', () => {
    expect(
      portalPosture({ kind: 'scraper', supports_complete_walk: true, is_enabled: false }),
    ).toBe('disabled');
  });

  it('an enabled complete-walk scraper is live (e.g. bazos, bezrealitky)', () => {
    expect(
      portalPosture({ kind: 'scraper', supports_complete_walk: true, is_enabled: true }),
    ).toBe('live');
    // is_enabled defaults to true (the Health payload only lists enabled portals)
    expect(portalPosture({ kind: 'scraper', supports_complete_walk: true })).toBe('live');
  });

  it('an enabled partial-walk scraper is partial (e.g. remax, maxima)', () => {
    expect(portalPosture({ kind: 'scraper', supports_complete_walk: false })).toBe('partial');
  });

  it('every posture has a label', () => {
    for (const p of ['live', 'partial', 'on_demand', 'disabled'] as const) {
      expect(PORTAL_POSTURE_LABEL[p]).toBeTruthy();
    }
  });
});
