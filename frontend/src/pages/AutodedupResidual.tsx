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

import { useState } from 'react';

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
import {
  EMPTY_FILTERS,
  FILTER_CONTROL,
  FILTER_LABEL,
  FilterBar,
  pairHref,
  useVerdictOverlay,
  type GroupFilterState,
} from './AutodedupGroups';
import { useInfiniteList, type InfiniteListPage } from '@/lib/useInfiniteList';

const PAGE_SIZE = 20;
const DEFAULT_GENERATION = 'g1';
const DEFAULT_MIN_SCORE = '0.2';

export interface ResidualExtras {
  zone: '' | AutodedupZone;
  source_pair: string;
}

const num = (v: string): number | null => {
  if (v.trim() === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};
const flag = (v: string): 0 | 1 | null => (v === '1' ? 1 : v === '0' ? 0 : null);

export function toResidualQuery(
  f: GroupFilterState,
  extras: ResidualExtras,
  after: string | null,
): AutodedupResidualFilters {
  return {
    generation: f.generation || DEFAULT_GENERATION,
    after,
    limit: PAGE_SIZE,
    block: num(f.block),
    zone: extras.zone || null,
    min_score: num(f.min_score),
    source_pair: extras.source_pair || null,
    has_judgement: flag(f.has_judgement),
    verdict: f.verdict || null,
    sort: 'score_desc',
  };
}

interface ResidualPage extends InfiniteListPage<AutodedupResidualRow> {
  store_ready: boolean;
}

const pairKey = (row: AutodedupResidualRow) => `${row.listing_lo}:${row.listing_hi}`;

export default function AutodedupResidual() {
  const [filters, setFilters] = useState<GroupFilterState>({
    ...EMPTY_FILTERS,
    min_score: DEFAULT_MIN_SCORE,
  });
  const [extras, setExtras] = useState<ResidualExtras>({ zone: '', source_pair: '' });
  const { overlay, submit, pendingKey } = useVerdictOverlay();

  const list = useInfiniteList<AutodedupResidualRow, ResidualPage>({
    queryKey: ['autodedup', 'residual', filters, extras],
    queryFn: async (cursor) => {
      const res = await getAutodedupResidual(
        toResidualQuery(filters, extras, (cursor as string | null) ?? null),
      );
      return {
        rows: res.data?.items ?? [],
        nextCursor: res.data?.next_after ?? undefined,
        store_ready: res.store_ready,
      };
    },
    pageSize: PAGE_SIZE,
    getRowId: pairKey,
  });

  const storeReady = list.firstPage?.store_ready ?? null;
  const rows = list.rows;

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
            value={extras.zone}
            onChange={(e) => setExtras({ ...extras, zone: e.target.value as ResidualExtras['zone'] })}
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
        <label className="block">
          <span className={FILTER_LABEL}>Source pair</span>
          <input
            className={FILTER_CONTROL}
            placeholder="bazos+sreality"
            value={extras.source_pair}
            onChange={(e) => setExtras({ ...extras, source_pair: e.target.value })}
          />
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

      {rows.length > 0 && (
        <ul className="mt-5 space-y-4">
          {rows.map((row, i) => (
            <li key={pairKey(row)}>
              <PairCard
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
                verdict={overlay[pairKey(row)] ?? row.verdict}
                pending={pendingKey === pairKey(row)}
                eager={i < 2}
                labels={PAIR_LABELS}
                evidenceHref={pairHref(row.listing_lo, row.listing_hi, filters.generation)}
                onVerdict={(value) =>
                  submit(pairKey(row), {
                    kind: 'pair',
                    listing_lo: row.listing_lo,
                    listing_hi: row.listing_hi,
                    verdict: value,
                  })
                }
              />
            </li>
          ))}
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
