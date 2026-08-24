/* The hydration layer's two invariants.
 *
 * 1. Its cache keys must not collide with the pipeline's, because two live
 *    invalidations sweep by PREFIX: revalidatePipeline fires
 *    invalidateQueries(['pipeline','board']) after every card write, and the
 *    stage editor fires invalidateQueries(['pipeline']) wholesale. If a
 *    decoration key ever sat under either, every drag would refetch every
 *    thumbnail and every broker on the board — the split that makes the board
 *    fast would make it slower than the blocking chain it replaced. This is
 *    standing constraint 1 of the sprint, and it is cheap to violate by
 *    accident, so it is pinned here rather than trusted to review.
 *
 * 2. Keys are a property of the id SET, not of the array that arrived — so a
 *    re-sort or a re-render with the same cards is a cache hit.
 */

import { describe, expect, it } from 'vitest';

import { pipelineKeys } from '@/lib/queries';

import type { ImagePublic } from '@/lib/types';

import { hydrationKeys, idsKey, HYDRATION_NAMESPACE } from './keys';
import { makeHydration } from './CardHydration';
import { photoBuckets, taggedImageUrls } from './useCardHydration';

/* React Query's own prefix rule: a key is invalidated by `prefix` when every
 * element of `prefix` deep-equals the key's element at the same index. */
const matchesPrefix = (key: readonly unknown[], prefix: readonly unknown[]): boolean =>
  prefix.every((p, i) => JSON.stringify(p) === JSON.stringify(key[i]));

describe('hydration key namespace', () => {
  const decorationKeys = [
    hydrationKeys.covers([1, 2, 3]),
    hydrationKeys.brokers([1, 2, 3]),
  ];

  it('is disjoint from every pipeline invalidation prefix', () => {
    const sweeps: ReadonlyArray<readonly unknown[]> = [
      ['pipeline'],            // StageManager's wholesale invalidation
      pipelineKeys.board,      // revalidatePipeline, after every card write
      pipelineKeys.members,
      pipelineKeys.stages,
    ];
    for (const key of decorationKeys) {
      for (const sweep of sweeps) {
        expect(
          matchesPrefix(key, sweep),
          `${JSON.stringify(key)} must not be swept by ${JSON.stringify(sweep)}`,
        ).toBe(false);
      }
    }
  });

  it('roots every decoration under the hydration namespace', () => {
    for (const key of decorationKeys) expect(key[0]).toBe(HYDRATION_NAMESPACE);
    expect(HYDRATION_NAMESPACE).not.toBe('pipeline');
  });

  it('keys on the id SET, so order and duplicates do not re-key', () => {
    expect(hydrationKeys.covers([3, 1, 2])).toEqual(hydrationKeys.covers([1, 2, 3]));
    expect(hydrationKeys.covers([1, 1, 2])).toEqual(hydrationKeys.covers([1, 2]));
    expect(idsKey([10, 2])).toBe('2,10'); // numeric, not lexicographic
  });

  it('separates covers from brokers over the same cohort', () => {
    expect(hydrationKeys.covers([1])).not.toEqual(hydrationKeys.brokers([1]));
  });

  /* W7a. Three decorations over one cohort, three keys. `photos` additionally
     keys on perId, because that is a CLIENT-SIDE retention cap on the same
     server read — the Browse carousel keeps 50, the comparables modal 6. Drop it
     from the key and whichever surface asked first serves the other a silently
     truncated carousel out of cache. */
  it('separates photos from the other decorations, and by perId', () => {
    expect(hydrationKeys.photos([1], 50)).not.toEqual(hydrationKeys.covers([1]));
    expect(hydrationKeys.photos([1], 50)).not.toEqual(hydrationKeys.brokers([1]));
    expect(hydrationKeys.photos([1], 50)).not.toEqual(hydrationKeys.photos([1], 6));
  });

  it('keeps photo keys inside the hydration namespace, away from every sweep', () => {
    expect(hydrationKeys.photos([1, 2], 6)[0]).toBe(HYDRATION_NAMESPACE);
    // Same disjointness the covers/brokers case above pins: a Browse or pipeline
    // invalidation must never reach a carousel.
    expect(hydrationKeys.photos([1], 6)[0]).not.toBe('pipeline');
    expect(hydrationKeys.photos([1], 6)[0]).not.toBe('cards');
  });

  it('makes a re-sorted cohort a cache hit for photos too', () => {
    expect(hydrationKeys.photos([3, 1, 2], 6)).toEqual(
      hydrationKeys.photos([1, 2, 3, 3], 6),
    );
  });
});

describe('makeHydration lookup', () => {
  const covers = new Map([[7, 'https://img/7.jpg']]);
  const photos = new Map([
    [
      7,
      [
        { id: 1, sreality_url: 'https://img/a.jpg', storage_path: null,
          clip_fine_tag: 'kuchyne', clip_confidence: 0.9, clip_render_score: null },
        { id: 2, sreality_url: 'https://img/b.jpg', storage_path: null,
          clip_fine_tag: null, clip_confidence: null, clip_render_score: null },
      ] as unknown as ImagePublic[],
    ],
  ]);
  const brokers = new Map([
    [7, { broker_id: 3, display_name: 'A', firm_label: null, email: null,
          phone: null, has_email: false, has_phone: false }],
  ]);

  it('resolves a decoration by listing id', () => {
    const h = makeHydration(covers, brokers);
    expect(h.coverFor(7)).toBe('https://img/7.jpg');
    expect(h.brokerFor(7)?.broker_id).toBe(3);
  });

  it('returns null for a missing entry and for a null listing id', () => {
    const h = makeHydration(covers, brokers);
    // A post-Gate-2 representative can have a null surrogate; it must degrade
    // to "no decoration", never throw and never key on undefined.
    expect(h.coverFor(null)).toBeNull();
    expect(h.coverFor(undefined)).toBeNull();
    expect(h.coverFor(999)).toBeNull();
    expect(h.brokerFor(null)).toBeNull();
    expect(h.brokerFor(999)).toBeNull();
  });

  it('carries the pending flags so a card can reserve space', () => {
    expect(
      makeHydration(covers, brokers, photos, { coversPending: true }).coversPending,
    ).toBe(true);
    expect(
      makeHydration(covers, brokers, photos, { brokersPending: true }).brokersPending,
    ).toBe(true);
    expect(
      makeHydration(covers, brokers, photos, { photosPending: true }).photosPending,
    ).toBe(true);
  });

  /* W7a: photosFor is the one lookup that must NOT answer null. A carousel takes
     an array, and "no photos yet" and "no photos at all" both render its own
     empty state — photosPending is what separates them. The empty answer is a
     stable singleton so a card can memoize its projection on identity and not
     re-project on every render. */
  it('answers photosFor with a stable empty array, never null', () => {
    const h = makeHydration(covers, brokers, photos);
    expect(h.photosFor(7)).toHaveLength(2);
    expect(h.photosFor(999)).toEqual([]);
    expect(h.photosFor(null)).toEqual([]);
    expect(h.photosFor(999)).toBe(h.photosFor(null));
  });

  it('projects raw rows to the carousel shape without losing the CLIP tag', () => {
    const [first] = taggedImageUrls(photos.get(7) ?? []);
    expect(first.tag).toBe('kuchyne');
    expect(first.confidence).toBe(0.9);
    expect(first.url).toContain('a.jpg');
    // The layer itself stays lossless — the comparables modal and its map
    // preview consume these same rows un-projected.
    expect(taggedImageUrls(photos.get(7) ?? [])).toHaveLength(2);
  });
});

/* Browse is an INFINITE list: `rows` accumulates every page loaded so far. One
   cumulative cohort key would therefore change on every append and drag all the
   earlier pages' photos back over the wire with it — O(n²) rows read across n
   pages, ~900 re-read at page 5 to learn about the 178 that are new. Bucketing
   in ARRIVAL order is what keeps each page's key stable once it has landed. */
describe('photoBuckets', () => {
  const page = (from: number) =>
    Array.from({ length: 24 }, (_, i) => from + i);

  it('slices the cohort into page-sized buckets', () => {
    const b = photoBuckets([...page(1), ...page(101)]);
    expect(b).toHaveLength(2);
    expect(b[0]).toHaveLength(24);
    expect(b[1]).toHaveLength(24);
  });

  it('leaves earlier buckets BYTE-IDENTICAL when a page is appended', () => {
    const first = photoBuckets(page(1));
    const afterAppend = photoBuckets([...page(1), ...page(101)]);
    // The stability that makes this O(n): bucket 0's key cannot change, so
    // page 1's photos are never re-read to load page 2.
    expect(afterAppend[0]).toEqual(first[0]);
    expect(hydrationKeys.photos(afterAppend[0], 50)).toEqual(
      hydrationKeys.photos(first[0], 50),
    );
  });

  /* Arrival order, not sorted. Sorting the whole cohort before slicing would
     interleave a later page's ids into the earlier buckets and reshuffle every
     boundary on append — defeating the entire point. Browse's default sort is
     newest-first, so page 2's ids are typically LOWER than page 1's, which is
     exactly the case a sort would scramble. */
  it('keeps arrival order rather than sorting the cohort', () => {
    const b = photoBuckets([...page(101), ...page(1)]);
    expect(b[0][0]).toBe(101);
    expect(b[1][0]).toBe(1);
  });

  it('de-duplicates across pages without disturbing the boundaries', () => {
    const b = photoBuckets([...page(1), ...page(1), ...page(101)]);
    expect(b).toHaveLength(2);
    expect(b[0]).toEqual(page(1));
    expect(b[1]).toEqual(page(101));
  });

  it('has no buckets for an empty cohort, so no query is ever issued', () => {
    expect(photoBuckets([])).toEqual([]);
  });
});
