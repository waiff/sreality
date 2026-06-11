import type { EstimationRun } from './types';

/* The one place that decides where an estimation run "lives" in the UI.
 *
 * Linked runs (input_sreality_id set — the subject is a listing we have in
 * the DB) live on their listing's page: the estimations section there
 * selects the run via ?run= and #estimations scrolls it into view. Orphan
 * runs (pasted URLs of listings we don't have) keep the standalone
 * /estimation/:id fallback surface. Every cross-link to a run — the
 * estimations list, the post-create navigation, re-run redirects, and the
 * /estimation/:id route itself — routes through this helper so the two
 * surfaces can never disagree. */
export function runSurfaceUrl(
  run: Pick<EstimationRun, 'id' | 'input_sreality_id'>,
  hash: '#estimations' | '#feedback' = '#estimations',
): string {
  if (run.input_sreality_id != null) {
    return `/listing/${run.input_sreality_id}?run=${run.id}${hash}`;
  }
  return `/estimation/${run.id}${hash === '#feedback' ? '#feedback' : ''}`;
}
