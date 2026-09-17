/* AUTODEDUP · Residual — the duplicates the engine did NOT accept.
 *
 * THE OTHER HALF OF THE ERROR BUDGET. Groups shows what the engine merged and
 * asks "is this right?"; this page shows what it left apart, ranked by score so
 * the scroll is ordered by expected yield, and asks the same question from the
 * other side. A recall failure is invisible on the groups page by construction.
 *
 * "WHY IT WASN'T MERGED" IS THE POINT. Each row carries the precondition that
 * failed — a zone, a guard veto, an evidence-family gate — in words. That field
 * is what turns a review session into design feedback (§12), so it is rendered
 * as prose next to the verdict buttons and not hidden behind a drawer.
 *
 * A DISPLAY FLOOR, NOT A DECISION. `min_score` defaults to 0.20 because below
 * that the list is noise; it is an operator control, never a claim that 0.20
 * means anything to the engine.
 */

import { useMemo } from 'react';

import {
  getAutodedupResidual,
  type AutodedupResidualFilters,
  type AutodedupResidualRow,
  type AutodedupZone,
} from '@/lib/api';
import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import { EvidenceLegend } from '@/components/autodedup/EvidenceChips';
import PairCard from '@/components/autodedup/PairCard';
import { PAIR_LABELS } from '@/components/autodedup/VerdictButtons';
import { annotationInput, useVerdictAnnotations } from '@/components/autodedup/VerdictNotes';
import { parseBlockValue } from '@/components/autodedup/BlockSelect';
import {
  GenerationNotice,
  useAutodedupGenerations,
} from '@/components/autodedup/GenerationSelect';
import {
  EMPTY_FILTERS,
  FILTER_CONTROL,
  FILTER_LABEL,
  FilterBar,
  ResultCount,
  SOURCES,
  sanitizeGroupFilters,
  pairHref,
  useVerdictOverlay,
  type GroupFilterState,
} from './AutodedupGroups';
import { useInfiniteList, type InfiniteListPage } from '@/lib/useInfiniteList';
import { useUrlFilters } from '@/lib/useUrlFilters';
import { portalLabel } from '@/lib/portals';

const PAGE_SIZE = 20;
const DEFAULT_MIN_SCORE = '0.2';

/* The residual view's own keys, carried in the SAME url state as the shared
 * filters so one link restores the whole bar.
 *
 * TWO SELECTS, NOT ONE. The wire filter is an unordered source PAIR
 * (`bazos+sreality`), and nine portals make 45 of them — a list nobody scans.
 * The operator picks the two portals instead and the pair is composed below, in
 * the one order the server's `least()/greatest()` predicate compares. */
export interface ResidualExtras {
  zone: '' | AutodedupZone;
  source_a: string;
  source_b: string;
}

export type ResidualFilterState = GroupFilterState & ResidualExtras;

/* Both portals or neither: a single portal cannot be expressed as a pair, and
 * sending half of one would filter on a string no row carries. */
export function sourcePair(a: string, b: string): string | null {
  if (!a || !b) return null;
  return [a, b].sort().join('+');
}

const num = (v: string): number | null => {
  if (v.trim() === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};
const flag = (v: string): 0 | 1 | null => (v === '1' ? 1 : v === '0' ? 0 : null);

export function toResidualQuery(
  f: ResidualFilterState,
  after: string | null,
): AutodedupResidualFilters {
  const block = parseBlockValue(f.block);
  return {
    /* Omitted, never defaulted to a generation name: this scroll asks which
     * scored pairs a clustering did NOT join, and against a superseded pass it
     * answers about a clustering nobody is validating. */
    generation: f.generation || null,
    after,
    limit: PAGE_SIZE,
    block: block.block,
    block_grain: block.block_grain,
    zone: f.zone || null,
    /* A CLEARED FLOOR IS ZERO, NOT AN ABSENT PARAMETER. `min_score` is the one
     * filter the server defaults to a non-empty value (0.20), so omitting it
     * says "0.20", not "no floor" — an emptied field would have rendered empty,
     * shared a url that read empty, and shown the default queue anyway. A value
     * that is not a number is a different thing: it constrains nothing, and the
     * server's own default stands. */
    min_score: f.min_score.trim() === '' ? 0 : num(f.min_score),
    source_pair: sourcePair(f.source_a, f.source_b),
    has_judgement: flag(f.has_judgement),
    verdict: f.verdict || null,
    sort: 'score_desc',
  };
}

export const EMPTY_RESIDUAL_FILTERS: ResidualFilterState = {
  ...EMPTY_FILTERS,
  min_score: DEFAULT_MIN_SCORE,
  zone: '',
  source_a: '',
  source_b: '',
};

interface ResidualPage extends InfiniteListPage<AutodedupResidualRow> {
  store_ready: boolean;
  /* The pass the server read — see AutodedupGroups' GroupsPage. */
  generation: string | null;
  total: number | null;
}

const pairKey = (row: AutodedupResidualRow) => `${row.listing_lo}:${row.listing_hi}`;

const ZONES: ReadonlyArray<ResidualExtras['zone']> = ['', 'band', 'reject', 'merge'];

/* The shared keys are checked by the shared sanitiser (sort, verdict) and this
 * view's own zone here, so a hand-edited link shows the queue rather than the
 * server's 400 in a red banner. */
export function sanitizeResidualFilters(raw: ResidualFilterState): ResidualFilterState {
  const base = sanitizeGroupFilters(raw);
  return ZONES.includes(base.zone) ? base : { ...base, zone: '' };
}

export default function AutodedupResidual() {
  /* One url state for the whole bar — the shared filters and this view's own. */
  const [urlFilters, setFilters] = useUrlFilters<ResidualFilterState>(EMPTY_RESIDUAL_FILTERS);
  const filters = useMemo(() => sanitizeResidualFilters(urlFilters), [urlFilters]);
  const { overlay, submit, pendingKey } = useVerdictOverlay();
  const notes = useVerdictAnnotations();
  const { latest } = useAutodedupGenerations();

  const list = useInfiniteList<AutodedupResidualRow, ResidualPage>({
    queryKey: ['autodedup', 'residual', filters],
    queryFn: async (cursor) => {
      const res = await getAutodedupResidual(
        toResidualQuery(filters, (cursor as string | null) ?? null),
      );
      return {
        rows: res.data?.items ?? [],
        nextCursor: res.data?.next_after ?? undefined,
        store_ready: res.store_ready,
        generation: res.data?.generation ?? null,
        total: res.data?.total ?? null,
      };
    },
    pageSize: PAGE_SIZE,
    getRowId: pairKey,
  });

  const storeReady = list.firstPage?.store_ready ?? null;
  const rows = list.rows;
  const total = list.firstPage?.total ?? null;
  const generation = filters.generation || list.firstPage?.generation || null;
  const halfPair = Boolean(filters.source_a) !== Boolean(filters.source_b);

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">AUTODEDUP · Residual</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[52rem]">
          Pairs the engine scored but did <strong>not</strong> join — ranked by score, so the top of
          the list is where a missed duplicate is most likely. Each row says which precondition
          stopped the merge. Marking one "a duplicate" changes nothing in the live database; it
          records the miss so the next calibration can answer for it.
        </p>
      </header>

      <EvidenceLegend />

      <FilterBar value={filters} onChange={setFilters} showSource={false} showCategory={false}>
        <label className="block">
          <span className={FILTER_LABEL}>Zone</span>
          <select
            className={FILTER_CONTROL}
            value={filters.zone}
            onChange={(e) =>
              setFilters({ ...filters, zone: e.target.value as ResidualExtras['zone'] })
            }
          >
            <option value="">vše</option>
            <option value="band">band</option>
            <option value="reject">reject</option>
            <option value="merge">merge</option>
          </select>
        </label>
        <label className="block">
          <span className={FILTER_LABEL}>Score ≥</span>
          <input
            className={FILTER_CONTROL}
            inputMode="decimal"
            value={filters.min_score}
            onChange={(e) => setFilters({ ...filters, min_score: e.target.value })}
          />
        </label>
        {/* Two portals, not a typed pair string: the wire filter is an unordered
          * pair and the 45 of them are not a list anyone reads. */}
        <label className="block">
          <span className={FILTER_LABEL}>Portál A</span>
          <select
            className={FILTER_CONTROL}
            value={filters.source_a}
            onChange={(e) => setFilters({ ...filters, source_a: e.target.value })}
          >
            <option value="">vše</option>
            {SOURCES.map((s) => (
              <option key={s} value={s}>
                {portalLabel(s) ?? s}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className={FILTER_LABEL}>Portál B</span>
          <select
            className={FILTER_CONTROL}
            value={filters.source_b}
            onChange={(e) => setFilters({ ...filters, source_b: e.target.value })}
          >
            <option value="">vše</option>
            {SOURCES.map((s) => (
              <option key={s} value={s}>
                {portalLabel(s) ?? s}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className={FILTER_LABEL}>Judged</span>
          <select
            className={FILTER_CONTROL}
            value={filters.has_judgement}
            onChange={(e) => setFilters({ ...filters, has_judgement: e.target.value })}
          >
            <option value="">vše</option>
            <option value="1">s verdiktem soudce</option>
            <option value="0">bez verdiktu</option>
          </select>
        </label>
      </FilterBar>

      <GenerationNotice
        generation={generation}
        latest={latest}
        onLatest={() => setFilters({ ...filters, generation: '' })}
      />

      {/* Said out loud rather than filtered silently: half a pair is not a
        * filter the server can apply, and a control that quietly does nothing is
        * the defect this bar was rebuilt to remove. */}
      {halfPair && (
        <p className="mt-2 text-[0.72rem] text-[var(--color-ink-3)]">
          Vyberte oba portály — dvojice se filtruje jen jako pár (A + B).
        </p>
      )}

      {list.error && <ErrorBanner message={list.error.message} />}

      {list.isLoading && (
        <p className="mt-6 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Loading residual pairs…
        </p>
      )}

      {storeReady === false && (
        <p className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 text-sm text-[var(--color-ink-2)]">
          Schema not migrated yet — the program's store does not exist in this database, so there is
          nothing to review.
        </p>
      )}

      {storeReady !== false && !list.isLoading && !list.isError && rows.length === 0 && (
        <p className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 text-sm text-[var(--color-ink-2)]">
          No pair above this score matches these filters.
        </p>
      )}

      {rows.length > 0 && <ResultCount shown={rows.length} total={total} noun="pairs" />}

      {rows.length > 0 && (
        <ul className="mt-3 space-y-4">
          {rows.map((row, i) => {
            const key = pairKey(row);
            const stored = overlay[key] ?? row.verdict;
            return (
            <li key={key}>
              <PairCard
                /* A row is a QUEUE ENTRY: thumbnail-sized covers, so the diff
                  * table, the reason and the four answers are all above the fold.
                  * The hero photos live on the pair page, one click away. */
                dense
                lo={row.lo}
                hi={row.hi}
                score={row.score}
                zone={row.zone}
                certificate={row.certificate}
                families={row.family_names}
                guardVeto={row.guard_veto}
                whyNotMerged={row.why_not_merged}
                contributions={row.contributions}
                judgement={row.judgement}
                verdict={stored}
                pending={pendingKey === key}
                eager={i < 2}
                labels={PAIR_LABELS}
                evidenceHref={pairHref(row.listing_lo, row.listing_hi, generation ?? '')}
                annotation={notes.annotationOf(key, stored)}
                onAnnotationChange={(next) => notes.setAnnotation(key, next)}
                annotationDirty={notes.isDirty(key, stored)}
                onSaveAnnotation={() =>
                  stored &&
                  submit(key, {
                    kind: 'pair',
                    listing_lo: row.listing_lo,
                    listing_hi: row.listing_hi,
                    verdict: stored.verdict,
                    ...annotationInput(notes.annotationOf(key, stored)),
                  })
                }
                onVerdict={(value, annotation) =>
                  submit(key, {
                    kind: 'pair',
                    listing_lo: row.listing_lo,
                    listing_hi: row.listing_hi,
                    verdict: value,
                    ...annotation,
                  })
                }
              />
            </li>
            );
          })}
        </ul>
      )}

      {list.hasNextPage && (
        <div className="mt-4">
          <button
            type="button"
            onClick={list.fetchNextPage}
            disabled={list.isFetchingNextPage}
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-3 py-1.5 text-sm text-[var(--color-ink-2)] hover:text-[var(--color-ink)] disabled:opacity-50"
          >
            {list.isFetchingNextPage ? 'Loading…' : 'Load more'}
          </button>
        </div>
      )}
    </div>
  );
}
