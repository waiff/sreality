/* AUTODEDUP · every query key under the one `['autodedup']` root, so the
 * rulings page's wholesale sweep and each reader agree on what the namespace
 * holds. Parameters whose shape a page owns are typed `unknown`: lib/ never
 * imports from pages/. */

const R = 'autodedup' as const;

export const autodedupKeys = {
  all: [R] as const,
  stats: [R, 'stats'] as const,
  iterations: [R, 'iterations'] as const,
  generations: [R, 'generations'] as const,
  blocks: (generation: string | null) => [R, 'blocks', generation] as const,
  rulings: (filterKey: string, after: string | null) => [R, 'rulings', filterKey, after] as const,
  proposedSplits: [R, 'proposed-splits'] as const,
  proposedSplitsPage: (after: number | null) => [R, 'proposed-splits', after] as const,
  categorySplits: [R, 'category-splits'] as const,
  categorySplitsPage: (propertyIds: readonly number[]) => [R, 'category-splits', propertyIds] as const,
  residual: (filters: unknown) => [R, 'residual', filters] as const,
  candidates: (filters: unknown) => [R, 'candidates', filters] as const,
  candidate: (candidateKey: string, generation: string) =>
    [R, 'candidate', candidateKey, generation] as const,
  pair: (lo: number | null, hi: number | null, generation: string | null) =>
    [R, 'pair', lo, hi, generation] as const,
  judgements: (query: unknown) => [R, 'judgements', query] as const,
  groups: (filters: unknown) => [R, 'groups', filters] as const,
  group: (clusterKey: number, generation: string) => [R, 'group', clusterKey, generation] as const,
  validationProgress: [R, 'validation-progress'] as const,
  validationProgressFor: (
    surface: string,
    generation: string | null,
    seed: string,
    minScore: number | null,
  ) => [R, 'validation-progress', surface, generation, seed, minScore] as const,
};
