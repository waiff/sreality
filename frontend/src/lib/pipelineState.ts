/* URL-backed view state for the deal-pipeline board.
 *
 * The board's Stav / Typ / Lokalita filters were component-local `useState`, so
 * a filtered board could not be linked, bookmarked, or survive a reload — and
 * adding a sort control on top of that would have compounded it (you sort, you
 * navigate to a listing, you come back, and the board is unsorted again).
 *
 * Param names and encodings are Browse's, not new ones: `status`, `cat`,
 * `collections` (Browse's own CSV of collection ids, parsed by its
 * `parseIntList`), and the `districts` / `districts_ctx` / `districts_excl` /
 * `districts_lvl` / `districts_id` CSV family that `districtChipsToCsvParams` +
 * `parseDistrictChips` already define as "the one wire format every
 * location-filterable surface uses". Same meaning, same spelling, everywhere.
 *
 * Every param is omitted at its default so a pristine board is a clean `/pipeline`.
 */

import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  districtChipsToCsvParams,
  parseDistrictChips,
  parseIntList,
  type DistrictChip,
  type ListingStatus,
} from './filters';
import { parseSortParam, sortParamOf } from './cardSort';
import {
  DEFAULT_PIPELINE_SORT,
  PIPELINE_SORT_OPTIONS,
  type PipelineSort,
} from './pipelineSort';

const DISTRICT_PARAMS = [
  'districts',
  'districts_ctx',
  'districts_excl',
  'districts_lvl',
  'districts_id',
] as const;

export interface PipelineViewState {
  status: ListingStatus;
  types: ReadonlySet<string>;
  districts: DistrictChip[];
  collectionIds: number[];
  sort: PipelineSort;
  setStatus: (next: ListingStatus) => void;
  setTypes: (next: string[]) => void;
  setDistricts: (next: DistrictChip[]) => void;
  setCollections: (next: number[]) => void;
  setSort: (next: PipelineSort) => void;
  reset: () => void;
}

export function usePipelineViewState(): PipelineViewState {
  const [searchParams, setSearchParams] = useSearchParams();

  const status = (searchParams.get('status') as ListingStatus | null) ?? 'any';
  const typesParam = searchParams.get('cat');
  const collectionsParam = searchParams.get('collections');
  const sortParam = searchParams.get('sort');

  const types = useMemo(
    () => new Set(typesParam ? typesParam.split(',').filter(Boolean) : []),
    [typesParam],
  );

  const collectionIds = useMemo(() => parseIntList(collectionsParam), [collectionsParam]);

  /* Rebuilt from the five district params rather than from a single joined
   * string, so a chip's admin id / level / exclusion survives a reload — the
   * district chip contract keys on the stable admin ID, not the name. */
  const districtKey = DISTRICT_PARAMS.map((p) => searchParams.get(p) ?? '').join('|');
  const districts = useMemo(
    () =>
      parseDistrictChips(
        searchParams.get('districts'),
        searchParams.get('districts_ctx'),
        searchParams.get('districts_excl'),
        searchParams.get('districts_lvl'),
        searchParams.get('districts_id'),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [districtKey],
  );

  const sort = useMemo(
    () => parseSortParam(sortParam, PIPELINE_SORT_OPTIONS, DEFAULT_PIPELINE_SORT),
    [sortParam],
  );

  /* replace: true — filter/sort tweaks are view state, not navigation steps.
   * Pushing each keystroke would make Back walk the operator through every
   * chip they toggled instead of returning them to where they came from. */
  const patch = useCallback(
    (mutate: (sp: URLSearchParams) => void) => {
      setSearchParams(
        (prev) => {
          const sp = new URLSearchParams(prev);
          mutate(sp);
          return sp;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const setStatus = useCallback(
    (next: ListingStatus) =>
      patch((sp) => (next === 'any' ? sp.delete('status') : sp.set('status', next))),
    [patch],
  );

  const setTypes = useCallback(
    (next: string[]) =>
      patch((sp) => (next.length === 0 ? sp.delete('cat') : sp.set('cat', next.join(',')))),
    [patch],
  );

  const setCollections = useCallback(
    (next: number[]) =>
      patch((sp) =>
        next.length === 0 ? sp.delete('collections') : sp.set('collections', next.join(',')),
      ),
    [patch],
  );

  const setDistricts = useCallback(
    (next: DistrictChip[]) =>
      patch((sp) => {
        // Clear the whole family first: the serializer omits optional params
        // when no chip needs them, so a stale districts_lvl would otherwise
        // survive a change that no longer emits one.
        for (const p of DISTRICT_PARAMS) sp.delete(p);
        for (const [k, v] of Object.entries(districtChipsToCsvParams(next))) {
          sp.set(k, v);
        }
      }),
    [patch],
  );

  const setSort = useCallback(
    (next: PipelineSort) =>
      patch((sp) => {
        const token = sortParamOf(next);
        const isDefault =
          next.field === DEFAULT_PIPELINE_SORT.field &&
          next.direction === DEFAULT_PIPELINE_SORT.direction;
        if (isDefault) sp.delete('sort');
        // Emit the option's own `value`, not sortParamOf's derived token — the
        // manual sort is spelled `manual`, not `board_position`.
        else {
          const opt = PIPELINE_SORT_OPTIONS.find(
            (o) => o.field === next.field && o.direction === next.direction,
          );
          sp.set('sort', opt?.value ?? token);
        }
      }),
    [patch],
  );

  /* One write, one render: Reset is a single intent, not four setter calls.
   * Sort survives — it is how the board is read, not which deals are on it. */
  const reset = useCallback(
    () =>
      patch((sp) => {
        for (const p of ['status', 'cat', 'collections', ...DISTRICT_PARAMS]) sp.delete(p);
      }),
    [patch],
  );

  return {
    status,
    types,
    districts,
    collectionIds,
    sort,
    setStatus,
    setTypes,
    setDistricts,
    setCollections,
    setSort,
    reset,
  };
}
