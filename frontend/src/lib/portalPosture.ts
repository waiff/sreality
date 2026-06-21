import type { PortalKind } from './types';

/* Portal posture is DERIVED, never hand-set (migration 217 retired the manual
 * `stage` label). It is a pure function of three facts already in the registry:
 *   - kind                   — scraper vs on-demand URL parser
 *   - is_enabled             — operator lifecycle (registered but parked)
 *   - supports_complete_walk — THE operational maturity gate the runner trusts
 *     (rule #3/#21): true ⇒ the index walk infers delistings; false ⇒ delistings
 *     are only caught on a gone re-fetch.
 * Live health (the status dot) stays in the per-source scraper_health_checks
 * rollup, so the posture badge reflects stable identity and never flickers. */

export type PortalPosture = 'live' | 'partial' | 'on_demand' | 'disabled';

export function portalPosture(p: {
  kind: PortalKind;
  supports_complete_walk: boolean;
  is_enabled?: boolean;
}): PortalPosture {
  if (p.kind === 'parser') return 'on_demand';
  if (p.is_enabled === false) return 'disabled';
  return p.supports_complete_walk ? 'live' : 'partial';
}

export const PORTAL_POSTURE_LABEL: Record<PortalPosture, string> = {
  live: 'live',
  partial: 'partial walk',
  on_demand: 'on demand',
  disabled: 'disabled',
};

export const PORTAL_POSTURE_BLURB: Record<PortalPosture, string> = {
  live: 'Walks the full index and infers delistings from index absence.',
  partial:
    'Cannot prove a complete index walk, so delistings are caught only on a gone re-fetch — not inferred from index absence.',
  on_demand: 'Parsed on demand from a pasted listing URL — no scheduled crawl.',
  disabled: 'Registered but not currently scraping.',
};
