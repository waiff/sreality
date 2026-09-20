/* AUTODEDUP · the filter state every validation queue shares.
 *
 * THE URL IS THE FILTER STATE (lib/useUrlFilters), so a filtered queue is
 * bookmarkable, shareable and survives a reload. This module holds the shape
 * itself — the keys, the defaults, the sanitiser and the drill-down link — so
 * the three queues (groups, residual pairs, candidate groups) cannot each grow
 * their own idea of what "no filter" means.
 *
 * FILTERS ARE KEYS, NEVER PREDICATES. Every value here is a NAME the server
 * validates against its own registry; nothing composes SQL, and an unknown
 * value is the server's 400 rather than a page's problem. What the sanitiser
 * below does is narrower and worth saying out loud: a hand-edited or stale link
 * should show the queue rather than replace it with a red banner, so a value
 * outside the closed vocabulary falls back to the default instead of travelling.
 */

import { ROUTES, withQuery, type RoutePath } from '@/lib/routes';

/* The seeded sample's name. Shared by every queue, and the same default the
 * server uses — a page that sent a different one would silently review a
 * different sample from the one the counter counts. */
export const DEFAULT_SEED = 'v1';
export const SEED_RE = /^[a-z0-9]{1,16}$/;

export interface GroupFilterState {
  generation: string;
  block: string;
  source: string;
  category_main: string;
  category_type: string;
  min_size: string;
  max_size: string;
  min_score: string;
  max_score: string;
  verdict: string;
  shared_photo: string;
  has_judgement: string;
  sort: 'weakest' | 'newest' | 'largest' | 'random';
  /* WHICH random sample. Only meaningful with `sort=random`, and only written to
   * the URL when it is not the default one. */
  seed: string;
  /* '1' = hide every judge artefact until this row carries the operator's own
   * verdict. Default OFF on the groups queue and ON on the residual one, which
   * is where the D6 pairs are judged — so each page reads its own default
   * (`=== '1'` there, `!== '0'` here) rather than sharing a sanitiser that
   * cannot know both. */
  blind: string;
}

export const EMPTY_FILTERS: GroupFilterState = {
  /* NOT a generation name. The empty value means "the newest pass", which the
   * server resolves off `autodedup.clusters`: a constant here (`g1`) is what
   * kept the queue on a superseded pass long after the engine moved on, and it
   * would go stale again the next time the lane writes a generation. */
  generation: '',
  block: '',
  source: '',
  category_main: '',
  category_type: '',
  min_size: '',
  max_size: '',
  min_score: '',
  max_score: '',
  verdict: '',
  shared_photo: '',
  has_judgement: '',
  sort: 'weakest',
  seed: DEFAULT_SEED,
  blind: '0',
};

/* A blank control is a MISSING parameter; so is a typo. `Number('abc')` is NaN,
 * which `request()` happily stringifies into `?min_size=NaN` and the server
 * answers with a 422 nobody can read — a filter that cannot be parsed simply
 * does not constrain. */
export const num = (v: string): number | null => {
  if (v.trim() === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

export const flag = (v: string): 0 | 1 | null => (v === '1' ? 1 : v === '0' ? 0 : null);

/* The evidence page is a DIFFERENT generation's worth of rows unless it is
 * told which one the queue was reading; the drill-down carries it rather than
 * silently falling back to the default. */
export function pairHref(
  lo: number,
  hi: number,
  generation: string,
  /* BLIND TRAVELS WITH THE LINK. A drill-down out of a blind queue that showed
   * the judge's transcript on arrival would undo the blinding in one click —
   * the operator would read the verdict they were not supposed to see yet on
   * exactly the pair they were about to rule on. */
  blind = false,
): RoutePath {
  return withQuery(ROUTES.autodedupPair.build({ lo, hi }), {
    generation: generation || null,
    blind: blind ? '1' : null,
  });
}

/* EVERY enumerated key arriving off the URL is checked here, not only the sort:
 * the server 400s an unknown value, and a hand-edited or stale link should show
 * the queue rather than replace it with a red banner. The two keys the server
 * validates against a closed vocabulary are `sort` and `verdict`; the free ones
 * (generation, block, the numbers) are already "no filter" when unparseable. */
const SORTS: ReadonlyArray<GroupFilterState['sort']> = [
  'weakest',
  'newest',
  'largest',
  'random',
];

export const VERDICTS: readonly string[] = [
  '',
  'unreviewed',
  /* GROUPS ONLY (E58): a ruling whose member set has moved. A pair verdict binds
   * two listings and can never go stale that way, so the pair queues refuse this
   * value rather than silently returning nothing. */
  'changed',
  'same',
  'different',
  'same_building_different_unit',
  'same_project_different_unit',
  'unsure',
];

export function sanitizeGroupFilters<T extends GroupFilterState>(raw: T): T {
  const sort = SORTS.includes(raw.sort) ? raw.sort : 'weakest';
  const verdict = VERDICTS.includes(raw.verdict) ? raw.verdict : '';
  /* The server 400s a seed outside its charset, and a red banner over the queue
   * is the wrong answer to a hand-edited link: an unusable seed means the
   * default sample, exactly as an unusable number means "no filter". */
  const seed = SEED_RE.test(raw.seed) ? raw.seed : DEFAULT_SEED;
  return sort === raw.sort && verdict === raw.verdict && seed === raw.seed
    ? raw
    : { ...raw, sort, verdict, seed };
}
