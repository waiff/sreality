/* AUTODEDUP · Soudce — every pair the judge read, for the operator to check.
 *
 * The judge is a language model (the judge lane: text, vision and the three
 * careful gold votes). It read pairs of adverts and marked each one the same
 * property or not. Its marks train models and never merge anything; the
 * operator's word is the exam of the judge and of the engine alike (E55). One
 * row per pair it marked, and per pair of a sealed random draw it may not have
 * read yet (`GET /autodedup/judgements`).
 *
 * "NAVRŽENO K POSOUZENÍ" is four rules over stored data, each capped at 100:
 * the random sample, the judge against the operator, a sure judge against the
 * engine, an unsure judge. The page asks for no reason by default; the server
 * answers with the suggested pairs while there are any, else with everything,
 * and says which. The sample comes first, then the other suggestions in one
 * seeded order, then what the operator has already ruled.
 *
 * BLIND BY DEFAULT, as on the residual queue: nothing the judge said about a
 * pair — its word, its evidence, the reasons that read it, the filters that
 * sort by it — shows before the operator has answered that pair. The moment
 * they have, the row opens in place; the list never reorders under the hand
 * that answers (the verdict overlay). */

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';

import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import { Segmented } from '@/components/controls';
import { Chip } from '@/components/autodedup/EvidenceChips';
import {
  FILTER_LABEL,
  FilterSelect,
  ResultCount,
  TownSelect,
  counted,
  type FilterOption,
} from '@/components/autodedup/FilterBar';
import LoadMore from '@/components/autodedup/LoadMore';
import Notice, { StoreNotReady } from '@/components/autodedup/Notice';
import PairCard from '@/components/autodedup/PairCard';
import ValidationStrip, { BlindToggle } from '@/components/autodedup/ValidationStrip';
import { annotationInput, useVerdictAnnotations } from '@/components/autodedup/VerdictNotes';
import { DEFAULT_SEED, pairHref } from '@/components/autodedup/filterState';
import { PHOTOS_PER_ADVERT, memberFromListing } from '@/components/autodedup/memberFromListing';
import useVerdictOverlay from '@/components/autodedup/useVerdictOverlay';
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

export interface JudgeFilterState {
  reason: string;
  ruled: string;
  operator: string;
  judge: string;
  tier: string;
  engine: string;
  stratum: string;
  town: string;
  generation: string;
  /* '1' (the default) hides every judge artefact on a row until it is ruled. */
  blind: string;
}

export const JUDGE_FILTER_DEFAULTS: JudgeFilterState = {
  /* '' = let the server choose: the suggested pairs while there are any. */
  reason: '',
  ruled: '',
  operator: '',
  judge: '',
  tier: '',
  engine: '',
  stratum: '',
  town: '',
  generation: '',
  blind: '1',
};

/* "Výběr". The two marked `judge` read the judge on an unruled row, so a blind
 * page neither offers nor sends them. */
const SELECTIONS: ReadonlyArray<{ value: string; label: string; judge?: true }> = [
  { value: 'suggested', label: 'Navrženo k posouzení' },
  { value: 'sample', label: 'Náhodný vzorek pro vás' },
  { value: 'operator', label: 'Soudce říká opak než vy' },
  { value: 'engine', label: 'Soudce si je jistý a engine to vidí jinak', judge: true },
  { value: 'unsure', label: 'Soudce si nebyl jistý', judge: true },
  { value: 'all', label: 'Všechny posouzené páry' },
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
const JUDGE_SAID: ReadonlyArray<FilterOption> = [
  { value: 'same', label: 'Stejná nemovitost' },
  { value: 'different', label: 'Jiná nemovitost' },
  { value: 'abstain', label: 'Nerozhodl' },
  { value: 'none', label: 'Zatím nečetl' },
];
const WHO_READ: ReadonlyArray<FilterOption> = [
  { value: 'vision', label: 'Fotky' },
  { value: 'text', label: 'Text' },
  { value: 'gold', label: 'Pečlivé čtení (3 hlasy)' },
  { value: 'none', label: 'Zatím nikdo' },
];
const AGREEMENTS = ['agrees', 'disagrees'];
const TOWN = /^[oc]:\d{1,12}$/;

/* A hand-edited or shared link shows the list rather than a red banner: a value
 * outside a vocabulary is no filter. And while blind, nothing that reads the
 * judge on an unruled row travels — whatever the link said. */
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
    town: TOWN.test(raw.town) ? raw.town : '',
    stratum: raw.stratum.length <= 200 ? raw.stratum : '',
  };
}

function toQuery(f: JudgeFilterState, after: string | null): JudgementFilters {
  return {
    reason: f.reason || null,
    ruled: f.ruled || null,
    operator: f.operator || null,
    judge: f.judge || null,
    tier: f.tier || null,
    engine: f.engine || null,
    stratum: f.stratum || null,
    town: f.town || null,
    generation: f.generation || null,
    after,
    limit: PAGE_SIZE,
  };
}

/* A pair list's name is data: `g2:s3_mf_band` reads "g2 · s3 mf band". */
export function listName(stratum: string): string {
  const cut = stratum.indexOf(':');
  const words = (text: string) => text.replace(/_/g, ' ');
  return cut < 0 ? words(stratum) : `${stratum.slice(0, cut)} · ${words(stratum.slice(cut + 1))}`;
}

/* Blind and unruled, a row says only that it was drawn at random or that it is
 * suggested — the specific reasons would tell which way the judge leaned. */
export function reasonChips(item: JudgedPair, revealed: boolean): string[] {
  if (revealed) return item.reasons.map((r) => REASON_CHIP[r]);
  if (item.reasons.includes('sample')) return [REASON_CHIP.sample];
  return item.reasons.length > 0 ? ['Navrženo k posouzení'] : [];
}

const pairKey = (row: JudgedPair) => `${row.listing_lo}:${row.listing_hi}`;

interface JudgeListPage extends InfiniteListPage<JudgedPair> {
  store_ready: boolean;
  page: JudgementsPage | null;
}

function agreementOptions(facet: Record<string, number> | undefined) {
  return [
    { value: 'disagrees', label: counted('Neshody', facet?.disagrees) },
    { value: '', label: 'Vše' },
    { value: 'agrees', label: counted('Souhlasí', facet?.agrees) },
  ];
}

export default function AutodedupJudge() {
  const [raw, setFilters] = useUrlFilters<JudgeFilterState>(JUDGE_FILTER_DEFAULTS);
  const filters = useMemo(() => sanitizeJudgeFilters(raw), [raw]);
  const blind = filters.blind !== '0';
  const { overlay, submit, pendingKey } = useVerdictOverlay();
  const notes = useVerdictAnnotations();
  const query = useMemo(() => toQuery(filters, null), [filters]);

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
  const lists = Object.keys(facets?.stratum ?? {}).sort();
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
          {page?.generation ? ` (generace ${page.generation})` : ''}
        </details>
      </header>

      <ValidationStrip surface="judge" generation={generation} seed={DEFAULT_SEED} sampleOrder />
      <BlindToggle
        checked={blind}
        onChange={(next) => set({ blind: next ? '1' : '0' })}
      />

      <section
        aria-label="Filtry"
        className="mt-4 space-y-3 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-3"
      >
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
          <span className="flex items-center gap-2">
            <span className={FILTER_LABEL}>Soudce × vy</span>
            <Segmented
              label="Soudce × vy"
              options={agreementOptions(facets?.operator)}
              value={filters.operator}
              onChange={(operator) => set({ operator })}
            />
          </span>
          {!blind && (
            <span className="flex items-center gap-2">
              <span className={FILTER_LABEL}>Soudce × engine</span>
              <Segmented
                label="Soudce × engine"
                options={agreementOptions(facets?.engine)}
                value={filters.engine}
                onChange={(engine) => set({ engine })}
              />
            </span>
          )}
        </div>
        <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
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
          <FilterSelect
            label="Seznam"
            value={filters.stratum}
            onChange={(stratum) => set({ stratum })}
            options={lists.map((name) => ({
              value: name,
              label: listName(name),
              count: facets?.stratum?.[name],
            }))}
          >
            {filters.stratum && !lists.includes(filters.stratum) && (
              <option value={filters.stratum}>{listName(filters.stratum)}</option>
            )}
          </FilterSelect>
          <TownSelect
            value={filters.town}
            towns={page?.towns ?? []}
            onChange={(town) => set({ town })}
          />
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-[0.72rem] text-[var(--color-ink-3)]">
            Navržené páry jsou seřazené: nahoře náhodný vzorek pro vás, pak ostatní. Co už máte
            rozhodnuté, je dole.
          </p>
          <button
            type="button"
            onClick={() => setFilters({ ...JUDGE_FILTER_DEFAULTS, blind: filters.blind })}
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2.5 py-1 text-[0.75rem] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]"
          >
            Zrušit filtry
          </button>
        </div>
      </section>

      {list.error && <ErrorBanner message={list.error.message} />}
      {detailsQ.isError && <ErrorBanner message={`Údaje inzerátů: ${detailsQ.error.message}`} />}
      {list.isLoading && (
        <p className="mt-6 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Načítám páry…
        </p>
      )}
      {list.firstPage?.store_ready === false && <StoreNotReady />}
      {page && rows.length === 0 && (
        <Notice>
          {unfiltered && page.reason === 'all'
            ? 'Soudce zatím nepřečetl žádný pár.'
            : 'Těmto filtrům neodpovídá žádný pár.'}
        </Notice>
      )}

      {rows.length > 0 && <ResultCount shown={rows.length} total={page?.total ?? null} noun="pairs" />}

      <ul className="mt-3 space-y-4">
        {rows.map((item, i) => {
          const key = pairKey(item);
          const stored = overlay[key] ?? item.verdict;
          /* THE ONE GATE (E55): the judge is shown once this pair is ruled — by
           * the server's record or by the answer just given here. */
          const revealed = !blind || item.ruled || overlay[key] != null;
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
                {item.stratum && (
                  <Chip title={item.stratum}>Seznam: {listName(item.stratum)}</Chip>
                )}
                {town && <span className="text-[0.7rem] text-[var(--color-ink-3)]">{town}</span>}
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
