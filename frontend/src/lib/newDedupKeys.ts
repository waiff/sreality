/* The query keys the tag-annotation matrix's TWO pages share. They live here
 * and not on a page because a write from the shared all-tags panel dirties the
 * same caches whichever page opened it — two copies would let the pages
 * disagree about what one click invalidates. */
export const NEW_DEDUP_OVERVIEW_KEY = ['new-dedup', 'labeling', 'overview'];
export const NEW_DEDUP_PROPOSALS_KEY = ['new-dedup', 'labeling', 'proposals'];
export const NEW_DEDUP_TAG_IMAGES_KEY = ['new-dedup', 'labeling', 'tag-images'];
/* Prefix; the live key appends the tag id, because a candidate queue only ever
 * exists FOR one tag. Invalidating the prefix (the all-tags detail panel) tops
 * up whichever tag's readout happens to be mounted. */
export const NEW_DEDUP_CANDIDATES_KEY = ['new-dedup', 'labeling', 'candidates'];
export const newDedupImageTagsKey = (imageId: number) => [
  'new-dedup',
  'labeling',
  'image-tags',
  imageId,
];
export const newDedupPositiveImagesKey = (tagId: number) => [
  'new-dedup',
  'labeling',
  'positive-images',
  tagId,
];

/* The training-set page's keys, shared with the taxonomy page (a routing change
 * there re-reads the heads here). Note `heads` and `locate` are their own roots,
 * so the `all` prefix sweeps the row pages only. */
export const trainingSetKeys = {
  all: ['training-set'] as const,
  heads: ['training-set-heads'] as const,
  locate: (tagId: number | null, imageId: number | null) =>
    ['training-set-locate', tagId, imageId] as const,
  rows: (tagId: number | null, tray: string, offset: number, n: number) =>
    ['training-set', tagId, tray, offset, n] as const,
};
