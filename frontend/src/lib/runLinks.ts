import type { EstimationRun } from './types';
import { listingPath } from './listingUrl';
import { ROUTES, withHash, withQuery, type RoutePath } from './routes';

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
): RoutePath {
  if (run.input_sreality_id != null) {
    return withHash(withQuery(listingPath(run.input_sreality_id), { run: run.id }), hash);
  }
  // The standalone page renders an `id="feedback"` anchor (RunPanel) but has no
  // `#estimations` target, so the hash that cannot resolve is dropped rather
  // than left to scroll nowhere. Not a redundancy — deleting it is a behaviour
  // change.
  return withHash(ROUTES.estimationDetail.build({ id: run.id }), hash === '#feedback' ? '#feedback' : '');
}
