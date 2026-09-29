/* AUTODEDUP · Soudce — every pair the LLM judge read, for the operator to rule
 * (`GET /autodedup/judgements`, PROGRAM.md E922). The judge's marks train
 * models and never merge anything; the operator's word is the exam.
 *
 * BLIND BY DEFAULT (E55): nothing the judge said about a pair — its word, its
 * evidence, the reasons and filters that read it — shows before the operator
 * has said Stejné or Různé on that pair ("Nevím" opens nothing); then the row
 * opens in place, and the list never reorders under the answering hand (the
 * verdict overlay). */

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';

import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import { Chip, EvidenceLegend } from '@/components/autodedup/EvidenceChips';
import {
  AgreementSwitch,
  FILTER_GRID,
  FilterPanel,
  FilterSelect,
  ResetFilters,
  ResultCount,
  type FilterOption,
} from '@/components/autodedup/FilterBar';
import LoadMore from '@/components/autodedup/LoadMore';
import Notice, { StoreNotReady } from '@/components/autodedup/Notice';
import PairCard from '@/components/autodedup/PairCard';
import ValidationStrip, { BlindToggle } from '@/components/autodedup/ValidationStrip';
import { annotationInput, useVerdictAnnotations } from '@/components/autodedup/VerdictNotes';
import { ENGINE_VIEW } from '@/components/autodedup/engineView';
import { DEFAULT_SEED, pairHref } from '@/components/autodedup/filterState';
import { PHOTOS_PER_ADVERT, memberFromListing } from '@/components/autodedup/memberFromListing';
import useVerdictOverlay from '@/components/autodedup/useVerdictOverlay';
import { revealsJudge } from '@/components/autodedup/VerdictButtons';
import {
  getAutodedupJudgements,
  type JudgedPair,
  type JudgementFilters,
  type JudgementReason,
  type JudgementsPage,
} from '@/lib/api';
import { useListingPhotos } from '@/lib/hydration/useCardHydration';
import { mergedAdvertsKeys } from '@/lib/mergedAdverts';
import { fetchListingsForListingIds } from '@/lib/queries';
import type { ListingPublic } from '@/lib/types';
import { useInfiniteList, type InfiniteListPage } from '@/lib/useInfiniteList';
import { useUrlFilters } from '@/lib/useUrlFilters';

const PAGE_SIZE = 25;
/* The payload names adverts by id only; the card's portal arrives with the facts read. */
const NO_FALLBACK = { source: null, is_active: null };

/* '' is no filter — and for `reason`, the server's choice (the suggested pairs
 * while there are any). `blind` is ON unless the link says '0'. */
export const JUDGE_FILTER_DEFAULTS = {
  reason: '', ruled: '', operator: '', judge: '', tier: '', engine: '', generation: '',
  blind: '1',
};
export type JudgeFilterState = typeof JUDGE_FILTER_DEFAULTS;

/* "Výběr". The two marked `judge` read the judge, so a blind page drops them.
 * "Soudce říká opak než vy" is no selection: "Soudce × vy: Neshody" asks it. */
const SELECTIONS: ReadonlyArray<{ value: string; label: string; judge?: true }> = [
  { value: 'suggested', label: 'Navrženo k posouzení' },
  { value: 'sample', label: 'Náhodný vzorek pro vás' },
  { value: 'engine', label: 'Soudce si je jistý a engine to vidí jinak', judge: true },
  { value: 'unsure', label: 'Soudce si nebyl jistý', judge: true },
  { value: 'all', label: 'Všechny dvojice' },
];

/* Why a row is offered, once the operator may see the judge's side of it. */
const REASON_CHIP: Record<JudgementReason, string> = {
  sample: 'Náhodný vzorek',
  operator: 'Soudce říká opak než vy',
  engine: 'Soudce a engine se neshodují',
  unsure: 'Soudce si nebyl jistý',
};

const RULED: ReadonlyArray<FilterOption> = [
  { value: '0', label: 'Čeká na vás' },
  { value: '1', label: 'Rozhodnuto' },
];
/* A pair the judge has not read is "Kdo četl: Zatím nikdo", asked once. */
const JUDGE_SAID: ReadonlyArray<FilterOption> = [
  { value: 'same', label: 'Stejná nemovitost' },
  { value: 'different', label: 'Jiná nemovitost' },
  { value: 'abstain', label: 'Nerozhodl' },
];
const WHO_READ: ReadonlyArray<FilterOption> = [
  { value: 'vision', label: 'Fotky' },
  { value: 'text', label: 'Text' },
  { value: 'gold', label: 'Pečlivé čtení (3 hlasy)' },
  { value: 'none', label: 'Zatím nikdo' },
];
const AGREEMENTS = ['agrees', 'disagrees'];

/* A value outside a vocabulary is no filter; while blind, nothing that reads
 * the judge travels, whatever a link said. */
export function sanitizeJudgeFilters(raw: JudgeFilterState): JudgeFilterState {
  const pick = (value: string, allowed: ReadonlyArray<string>) =>
    allowed.includes(value) ? value : '';
  const blind = raw.blind !== '0';
  const readsJudge = (value: string) => SELECTIONS.some((s) => s.value === value && s.judge);
  const reason = pick(raw.reason, SELECTIONS.map((s) => s.value));
  return {
    ...raw,
    reason: blind && readsJudge(reason) ? '' : reason,
    ruled: pick(raw.ruled, RULED.map((o) => o.value)),
    operator: pick(raw.operator, AGREEMENTS),
    judge: blind ? '' : pick(raw.judge, JUDGE_SAID.map((o) => o.value)),
    tier: blind ? '' : pick(raw.tier, WHO_READ.map((o) => o.value)),
    engine: blind ? '' : pick(raw.engine, AGREEMENTS),
  };
}

/* Every key but `blind` is a server filter; the API drops a blank one. */
function toQuery(f: JudgeFilterState): JudgementFilters {
  const { blind: _blind, ...keys } = f;
  return { ...keys, limit: PAGE_SIZE };
}

/* Blind and unruled, a row says only whether it is in the random sample: any
 * other reason, even unnamed, would tell which way the judge leaned. */
export function reasonChips(item: JudgedPair, revealed: boolean): string[] {
  return item.reasons.filter((r) => revealed || r === 'sample').map((r) => REASON_CHIP[r]);
}

const pairKey = (row: JudgedPair) => `${row.listing_lo}:${row.listing_hi}`;

interface JudgeListPage extends InfiniteListPage<JudgedPair> {
  store_ready: boolean;
  page: JudgementsPage | null;
}

export default function AutodedupJudge() {
  const [raw, setFilters] = useUrlFilters<JudgeFilterState>(JUDGE_FILTER_DEFAULTS);
  const filters = useMemo(() => sanitizeJudgeFilters(raw), [raw]);
  const blind = filters.blind !== '0';
  const { overlay, submit, pendingKey } = useVerdictOverlay();
  const notes = useVerdictAnnotations();
  const query = useMemo(() => toQuery(filters), [filters]);

  const list = useInfiniteList<JudgedPair, JudgeListPage>({
    queryKey: ['autodedup', 'judgements', query],
    queryFn: async (cursor) => {
      const res = await getAutodedupJudgements({ ...query, after: (cursor as string) ?? null });
      return {
        rows: res.data?.items ?? [],
        nextCursor: res.data?.next_after ?? undefined,
        store_ready: res.store_ready,
        page: res.data,
      };
    },
    pageSize: PAGE_SIZE,
    getRowId: pairKey,
  });
  const page = list.firstPage?.page ?? null;
  const facets = page?.facets;
  const rows = list.rows;
  const generation = filters.generation || page?.generation || null;

  /* Every advert on the page in one facts read and one photo read. */
  const ids = useMemo(
    () => [...new Set(rows.flatMap((r) => [r.listing_lo, r.listing_hi]))].sort((a, b) => a - b),
    [rows],
  );
  const detailsQ = useQuery<Map<number, ListingPublic>, Error>({
    queryKey: mergedAdvertsKeys.listings(ids),
    queryFn: () => fetchListingsForListingIds(ids),
    enabled: ids.length > 0,
    staleTime: 60_000,
  });
  const { photos } = useListingPhotos(ids, PHOTOS_PER_ADVERT);
  const member = (id: number) =>
    memberFromListing(id, NO_FALLBACK, detailsQ.data?.get(id), photos.get(id) ?? []);

  const set = (patch: Partial<JudgeFilterState>) => setFilters({ ...filters, ...patch });
  const selection = filters.reason || page?.reason || 'suggested';
  const unfiltered = Object.entries(filters).every(
    ([key, value]) => key === 'blind' || value === '',
  );

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">AUTODEDUP · Soudce</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[56rem]">
          Soudce je jazykový model. Přečetl dvojice inzerátů a u každé řekl, zda jde o stejnou
          nemovitost. Jeho značky slouží jen k učení modelů a nikdy nic neslučují. Zkouškou je
          vždy vaše rozhodnutí.
        </p>
        <details className="mt-1 text-[0.72rem] text-[var(--color-ink-3)]">
          <summary className="w-fit cursor-pointer">(i) co je engine</summary>
          engine = program, který dnes skládá skupiny
        </details>
      </header>

      <ValidationStrip surface="judge" generation={generation} seed={DEFAULT_SEED} sampleOrder />
      <BlindToggle
        checked={blind}
        onChange={(next) => set({ blind: next ? '1' : '0' })}
      />

      <EvidenceLegend />

      <FilterPanel>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
          <AgreementSwitch
            label="Soudce × vy"
            facet={facets?.operator}
            value={filters.operator}
            onChange={(operator) => set({ operator })}
          />
          {!blind && (
            <AgreementSwitch
              label="Soudce × engine"
              facet={facets?.engine}
              value={filters.engine}
              onChange={(engine) => set({ engine })}
            />
          )}
        </div>
        <div className={FILTER_GRID}>
          <FilterSelect
            label="Výběr"
            value={selection}
            onChange={(reason) => set({ reason })}
            allLabel={null}
            options={SELECTIONS.filter((s) => !(blind && s.judge)).map((s) => ({
              value: s.value,
              label: s.label,
              count: s.value === 'all' ? (selection === 'all' ? page?.total : null)
                : facets?.reason?.[s.value] ?? null,
            }))}
          />
          <FilterSelect
            label="Vaše rozhodnutí"
            value={filters.ruled}
            onChange={(ruled) => set({ ruled })}
            options={RULED.map((o) => ({ ...o, count: facets?.ruled?.[o.value] }))}
          />
          {!blind && (
            <FilterSelect
              label="Soudce řekl"
              value={filters.judge}
              onChange={(judge) => set({ judge })}
              options={JUDGE_SAID.map((o) => ({ ...o, count: facets?.judge?.[o.value] }))}
            />
          )}
          {!blind && (
            <FilterSelect
              label="Kdo četl"
              value={filters.tier}
              onChange={(tier) => set({ tier })}
              options={WHO_READ.map((o) => ({ ...o, count: facets?.tier?.[o.value] }))}
            />
          )}
          <ResetFilters
            onClick={() => setFilters({ ...JUDGE_FILTER_DEFAULTS, blind: filters.blind })}
          />
        </div>
        <p className="text-[0.72rem] text-[var(--color-ink-3)]">
          Navržené dvojice jsou seřazené: nahoře náhodný vzorek pro vás, pak ostatní. Co už máte
          rozhodnuté (Stejné nebo Různé), je dole.
        </p>
      </FilterPanel>

      {list.error && <ErrorBanner message={list.error.message} />}
      {detailsQ.isError && <ErrorBanner message={`Údaje inzerátů: ${detailsQ.error.message}`} />}
      {list.isLoading && (
        <p className="mt-6 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Načítám dvojice…
        </p>
      )}
      {list.firstPage?.store_ready === false && <StoreNotReady />}
      {page && rows.length === 0 && (
        <Notice>
          {unfiltered && page.reason === 'all'
            ? 'Soudce zatím nepřečetl žádnou dvojici.'
            : 'Těmto filtrům neodpovídá žádná dvojice.'}
        </Notice>
      )}

      {rows.length > 0 && <ResultCount shown={rows.length} total={page?.total ?? null} noun="pairs" />}

      <ul className="mt-3 space-y-4">
        {rows.map((item, i) => {
          const key = pairKey(item);
          const stored = overlay[key] ?? item.verdict;
          const revealed = !blind || revealsJudge(stored);
          const town = [item.obec_name, item.cast_obce_name].filter(Boolean).join(' · ');
          return (
            <li key={key}>
              <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
                {reasonChips(item, revealed).map((label) => (
                  <Chip key={label} tone="warn">
                    {label}
                  </Chip>
                ))}
                {revealed && !item.judgement && <Chip>soudce zatím nečetl</Chip>}
                {town && <span className="text-[0.7rem] text-[var(--color-ink-3)]">{town}</span>}
                <span className="text-[0.7rem] text-[var(--color-ink-3)]">
                  Engine: {ENGINE_VIEW[item.engine_view]}
                </span>
              </div>
              <PairCard
                dense
                lo={member(item.listing_lo)}
                hi={member(item.listing_hi)}
                score={item.score}
                zone={item.zone}
                certificate={item.certificate}
                guardVeto={item.guard_veto}
                whyNotMerged={item.why_not_merged}
                judgement={revealed ? item.judgement : null}
                blind={!revealed}
                verdict={stored}
                pending={pendingKey === key}
                eager={i < 2}
                evidenceHref={pairHref(item.listing_lo, item.listing_hi, generation ?? '', blind)}
                annotation={notes.annotationOf(key, stored)}
                onAnnotationChange={(next) => notes.setAnnotation(key, next)}
                annotationDirty={notes.isDirty(key, stored)}
                onSaveAnnotation={() =>
                  stored &&
                  submit(key, {
                    kind: 'pair',
                    listing_lo: item.listing_lo,
                    listing_hi: item.listing_hi,
                    verdict: stored.verdict,
                    ...annotationInput(notes.annotationOf(key, stored)),
                  })
                }
                onVerdict={(value, annotation) =>
                  submit(key, {
                    kind: 'pair',
                    listing_lo: item.listing_lo,
                    listing_hi: item.listing_hi,
                    verdict: value,
                    ...annotation,
                  })
                }
              />
            </li>
          );
        })}
      </ul>

      <LoadMore list={list} />
    </div>
  );
}
