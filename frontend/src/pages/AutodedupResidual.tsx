/* AUTODEDUP · Residual — the duplicates the engine did NOT accept.
 *
 * TWO VIEWS OF ONE COHORT. "Po skupinách" (the default) asks about a SET of
 * adverts with the card the operator already knows from /autodedup/groups; "po
 * dvojicích" is the original pair-at-a-time queue, unchanged. They read the same
 * residual pairs at the same display floor — the grouped view just stops asking
 * the same question five times: measured on g4, 7,653 residual pairs touch only
 * 4,158 listings, 2,909 of those sit in two or more pairs, and ~3,180 already
 * belong to a merged group, so "advert X vs each member of group G" was most of
 * the queue. Every residual pair still sits inside exactly one card (E56), so
 * switching view changes how much is asked at once, never what is asked.
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
 *
 * BLIND BY DEFAULT (D6). This is where the gate's pairs are judged, and an
 * agreement number between an operator who has just read "judge: same property
 * 0.93" and the judge that wrote it measures how persuasive the chip is, not
 * whether the judge is right. So every judge artefact on a row — the chip, the
 * verdict words, the key and contradicting evidence — is withheld until the
 * operator has recorded their own verdict on THAT pair, and appears the moment
 * they have. The engine's own score, zone, contributions and "why it wasn't
 * merged" stay visible throughout: they are not the thing being validated.
 */

import { useMemo, useState } from 'react';

import {
  getAutodedupCandidates,
  getAutodedupResidual,
  postAutodedupCandidateSplitVerdict,
  type AutodedupCandidate,
  type AutodedupCandidateFilters,
  type AutodedupCandidateSplitInput,
  type AutodedupResidualFilters,
  type AutodedupResidualRow,
  type AutodedupZone,
} from '@/lib/api';
import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import { EvidenceLegend } from '@/components/autodedup/EvidenceChips';
import PairCard from '@/components/autodedup/PairCard';
import { annotationInput, useVerdictAnnotations } from '@/components/autodedup/VerdictNotes';
import { parseBlockValue } from '@/components/autodedup/BlockSelect';
import {
  GenerationNotice,
  useAutodedupGenerations,
} from '@/components/autodedup/GenerationSelect';
import ValidationStrip, { BlindToggle } from '@/components/autodedup/ValidationStrip';
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
import CandidateCard, {
  candidateDefaultSplit,
  respectLocks,
} from '@/components/autodedup/CandidateCard';
import CandidateDialog from '@/components/autodedup/CandidateDialog';
import {
  EMPTY_SPLIT,
  candidateSplitInput,
  deriveSplit,
  type SplitControls,
  type SplitState,
  type UnitMap,
} from '@/components/autodedup/UnitSplit';
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
  /* WHICH VIEW of the same cohort. In the URL, so a link restores the view the
   * operator was in — '' is the default (groups), 'pairs' the original queue. */
  view: '' | 'pairs';
}

export type ResidualView = 'groups' | 'pairs';

export const viewOf = (raw: string): ResidualView => (raw === 'pairs' ? 'pairs' : 'groups');

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
    /* Two orders: the working one (expected yield) and the seeded sample the D6
     * agreement number is measured on. The shared `GroupFilterState.sort` also
     * carries the groups queue's three, which this view does not serve — the
     * sanitiser below maps anything that is not `random` back to score order. */
    sort: f.sort === 'random' ? 'random' : 'score_desc',
    seed: f.sort === 'random' ? f.seed : null,
  };
}

export const EMPTY_RESIDUAL_FILTERS: ResidualFilterState = {
  ...EMPTY_FILTERS,
  min_score: DEFAULT_MIN_SCORE,
  zone: '',
  source_a: '',
  source_b: '',
  /* PO SKUPINÁCH BY DEFAULT: one card rules many pairs, and the pairs of one
   * generation are not independent questions. The pair view stays one click
   * away, unchanged. */
  view: '',
  /* ON by default — see the header comment. The groups queue defaults it off. */
  blind: '1',
};

interface ResidualPage extends InfiniteListPage<AutodedupResidualRow> {
  store_ready: boolean;
  /* The pass the server read — see AutodedupGroups' GroupsPage. */
  generation: string | null;
  total: number | null;
}

const pairKey = (row: AutodedupResidualRow) => `${row.listing_lo}:${row.listing_hi}`;

const ZONES: ReadonlyArray<ResidualExtras['zone']> = ['', 'band', 'reject', 'merge'];

/* The shared keys are checked by the shared sanitiser (sort, verdict, seed) and
 * this view's own zone here, so a hand-edited link shows the queue rather than
 * the server's 400 in a red banner.
 *
 * THIS VIEW SERVES TWO ORDERS, not the groups queue's four: the working one
 * (expected yield) and the seeded sample. Anything that is not `random` is the
 * working order, spelled with the shared default (`weakest`) so an unset sort
 * stays out of the URL. */
export function sanitizeResidualFilters(raw: ResidualFilterState): ResidualFilterState {
  const base = sanitizeGroupFilters(raw);
  const zone = ZONES.includes(base.zone) ? base.zone : '';
  /* The GROUPED view serves four orders (the groups queue's own, minus
   * "newest": a candidate card has no clock of its own), the pair view two. A
   * sort the current view does not serve falls back to its default rather than
   * travelling — the server would 400 it, and a red banner is the wrong answer
   * to a link written in the other view. */
  const grouped = viewOf(base.view) === 'groups';
  const allowed: ReadonlyArray<GroupFilterState['sort']> = grouped
    ? ['weakest', 'largest', 'random']
    : ['weakest', 'random'];
  const sort = allowed.includes(base.sort) ? base.sort : 'weakest';
  const view = base.view === 'pairs' ? 'pairs' : '';
  /* ONE url key, TWO vocabularies. A pair carries one of the five verdicts; a
   * CARD carries none of its own — it is reviewed when every pair inside it is —
   * so the grouped view's control is reviewed/unreviewed. The shared sanitiser
   * knows only the five, and would blank `reviewed` on the way in; the same key
   * is kept because switching view with a verdict the other view cannot express
   * has to clear it, not smuggle it. */
  const verdict = grouped
    ? (raw.verdict === 'reviewed' || raw.verdict === 'unreviewed' ? raw.verdict : '')
    : base.verdict;
  return zone === base.zone && sort === base.sort && view === base.view
    && verdict === base.verdict
    ? base
    : { ...base, zone, sort, view, verdict };
}

/* The candidate queue's wire shape. It reads the SAME cohort at the server's own
 * display floor, so there is no `min_score` and no source pair here: a candidate
 * card spans several adverts, and "the pair of portals" is not a question it can
 * answer. The controls this view does not send are not rendered either. */
export function toCandidateQuery(
  f: ResidualFilterState,
  after: string | null,
): AutodedupCandidateFilters {
  const block = parseBlockValue(f.block);
  return {
    generation: f.generation || null,
    after,
    limit: PAGE_SIZE,
    block: block.block,
    block_grain: block.block_grain,
    zone: f.zone || null,
    /* Two values, not the five verdicts: a card is reviewed when every pair
     * inside it carries one. */
    verdict:
      f.verdict === 'reviewed' || f.verdict === 'unreviewed' ? f.verdict : null,
    sort:
      f.sort === 'random' ? 'random' : f.sort === 'largest' ? 'largest' : 'weakest',
    seed: f.sort === 'random' ? f.seed : null,
  };
}

interface CandidatePage extends InfiniteListPage<AutodedupCandidate> {
  store_ready: boolean;
  generation: string | null;
  total: number | null;
}

export default function AutodedupResidual() {
  /* One url state for the whole bar — the shared filters and this view's own. */
  const [urlFilters, setFilters] = useUrlFilters<ResidualFilterState>(EMPTY_RESIDUAL_FILTERS);
  const filters = useMemo(() => sanitizeResidualFilters(urlFilters), [urlFilters]);
  const view = viewOf(filters.view);
  const grouped = view === 'groups';
  const { overlay, submit, pendingKey } = useVerdictOverlay();
  /* The candidate save is its own endpoint, so it gets its own overlay — one
   * shared overlay keyed by two different kinds of key would flip a pair badge
   * from a card's save. Everything the operator sees is the same hook. */
  const candidates = useVerdictOverlay<AutodedupCandidateSplitInput>(
    postAutodedupCandidateSplitVerdict,
  );
  const notes = useVerdictAnnotations();
  const candidateNotes = useVerdictAnnotations();
  const { latest } = useAutodedupGenerations();
  const [openCandidate, setOpenCandidate] = useState<string | null>(null);
  /* The letters the operator has moved, per card. Page level for the reason the
   * groups queue keeps them there: the card and the dialog are two views of ONE
   * decision, and a dialog that started blank would throw away the letters the
   * card already carries. */
  const [candidateSplits, setCandidateSplits] = useState<Record<string, SplitState>>({});

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
    /* The view that is NOT on screen asks nothing: two queues over one cohort
     * would double every page load for a list nobody is looking at. */
    enabled: !grouped,
  });

  const cards = useInfiniteList<AutodedupCandidate, CandidatePage>({
    queryKey: ['autodedup', 'candidates', filters],
    queryFn: async (cursor) => {
      const res = await getAutodedupCandidates(
        toCandidateQuery(filters, (cursor as string | null) ?? null),
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
    getRowId: (row) => row.candidate_key,
    enabled: grouped,
  });

  const active = grouped ? cards : list;
  const storeReady = active.firstPage?.store_ready ?? null;
  const rows = list.rows;
  const cardRows = cards.rows;
  const total = active.firstPage?.total ?? null;
  const generation = filters.generation || active.firstPage?.generation || null;
  const halfPair = Boolean(filters.source_a) !== Boolean(filters.source_b);
  /* Default ON here — so only the explicit '0' turns the judge back on, and a
   * link written before this key existed still arrives blind. */
  const blind = filters.blind !== '0';

  /* THE STORED RULING, READ BACK (E50) — the same derivation the groups card
   * makes, then reconciled with the locks: the operator may have split a merged
   * group on the Groups page, and this card holds that group as one unit. */
  const storedSplits = useMemo(() => {
    const out: Record<string, SplitState | null> = {};
    for (const row of cardRows) {
      const derived = deriveSplit(row.members, row.member_verdicts);
      out[row.candidate_key] = derived ? respectLocks(derived, row.units) : null;
    }
    return out;
  }, [cardRows]);

  const candidateOf = (key: string): AutodedupCandidate | undefined =>
    cardRows.find((row) => row.candidate_key === key);

  const candidateControls = (card: AutodedupCandidate): SplitControls => {
    const key = card.candidate_key;
    const stored = storedSplits[key] ?? null;
    /* Untouched, the card edits the STORED ruling if there is one, else the
     * opening assignment: one letter per unit, because the engine did not merge
     * these (all-A would be putting its answer in the operator's mouth). */
    const base =
      candidateSplits[key] ?? stored ?? candidateDefaultSplit(card.units) ?? EMPTY_SPLIT;
    const edit = (patch: (current: SplitState) => SplitState) =>
      setCandidateSplits((all) => ({
        ...all,
        [key]: patch(all[key] ?? stored ?? candidateDefaultSplit(card.units)),
      }));
    return {
      state: base,
      setUnit: (listingId, unit) =>
        edit((current) => ({ ...current, units: { ...current.units, [listingId]: unit } })),
      save: (members, confirmRetract = false) =>
        candidates.submitSplit(
          key,
          candidateSplitInput(
            key,
            /* The pass the CARD came from, never the queue's: a ruling is stored
             * against one clustering. */
            card.generation,
            members,
            base,
            confirmRetract,
            candidateNotes.annotationOf(`candidate:${key}`, null),
          ),
        ),
      pending: candidates.pendingKey === key,
      annotation: candidateNotes.annotationOf(`candidate:${key}`, null),
      setAnnotation: (next) => candidateNotes.setAnnotation(`candidate:${key}`, next),
      error: candidates.splitErrors[key],
      receipt: candidates.splitResults[key],
      stored,
    };
  };

  /* A shortcut sets the LETTERS, which is all a split now says: same letter,
   * same unit; different letters, different units (D39). */
  const setCandidateUnits = (key: string, units: UnitMap) =>
    setCandidateSplits((all) => ({
      ...all,
      [key]: { ...(all[key] ?? storedSplits[key] ?? EMPTY_SPLIT), units },
    }));

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

      {/* ONE COHORT, TWO VIEWS. The switch is a view, not a filter: it changes
        * how much is asked at once, never which pairs are asked about. */}
      <div
        role="group"
        aria-label="Zobrazení"
        className="mt-3 inline-flex rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-0.5"
      >
        {([
          ['', 'po skupinách'],
          ['pairs', 'po dvojicích'],
        ] as const).map(([value, label]) => {
          const on = (filters.view || '') === value;
          return (
            <button
              key={label}
              type="button"
              aria-pressed={on}
              onClick={() => setFilters({ ...filters, view: value })}
              className={[
                'rounded-[var(--radius-xs)] px-3 py-1 text-[0.72rem] transition-colors',
                on
                  ? 'bg-[var(--color-paper-3)] text-[var(--color-ink)]'
                  : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink)]',
              ].join(' ')}
            >
              {label}
            </button>
          );
        })}
      </div>

      <EvidenceLegend />

      <FilterBar
        value={filters}
        onChange={setFilters}
        showSource={false}
        showCategory={false}
        /* The five-verdict select is about a PAIR. The grouped view has its own
          * two-value control below — a card is reviewed when every pair in it is. */
        showVerdict={!grouped}
      >
        {grouped && (
          <label className="block">
            <span className={FILTER_LABEL}>Stav</span>
            <select
              className={FILTER_CONTROL}
              value={filters.verdict}
              onChange={(e) => setFilters({ ...filters, verdict: e.target.value })}
            >
              <option value="">vše</option>
              <option value="unreviewed">nezkontrolované</option>
              <option value="reviewed">zkontrolované</option>
            </select>
          </label>
        )}
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
        {/* THE DISPLAY FLOOR IS THE PAIR VIEW'S. The cards are packed server-side
          * at one floor — the cohort a card comes from cannot move under a
          * control — so this is not offered in the grouped view rather than
          * offered and ignored. */}
        {!grouped && (
        <label className="block">
          <span className={FILTER_LABEL}>Score ≥</span>
          <input
            className={FILTER_CONTROL}
            inputMode="decimal"
            value={filters.min_score}
            onChange={(e) => setFilters({ ...filters, min_score: e.target.value })}
          />
        </label>
        )}
        {/* Two portals, not a typed pair string: the wire filter is an unordered
          * pair and the 45 of them are not a list anyone reads. A CARD spans
          * several adverts, so a portal pair is not a question it can answer. */}
        {!grouped && (
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
        )}
        {!grouped && (
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
        )}
        {/* "has a judge verdict" is a JUDGE ARTEFACT — a filter that tells the
          * operator which rows the machine has an opinion about. It belongs to
          * the pair view, where it predates blind mode; the grouped view, which
          * is blind by default and carries no judge field at all, does not
          * offer a way to sort by what the judge has done. */}
        {!grouped && (
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
        )}
        <label className="block">
          <span className={FILTER_LABEL}>Sort</span>
          <select
            className={FILTER_CONTROL}
            value={filters.sort}
            onChange={(e) =>
              setFilters({ ...filters, sort: e.target.value as ResidualFilterState['sort'] })
            }
          >
            <option value="weakest">
              {grouped ? 'nejslabší dvojice nahoře' : 'nejvyšší skóre nahoře'}
            </option>
            {grouped && <option value="largest">největší skupiny nahoře</option>}
            <option value="random">náhodný vzorek</option>
          </select>
        </label>
      </FilterBar>

      <GenerationNotice
        generation={generation}
        latest={latest}
        onLatest={() => setFilters({ ...filters, generation: '' })}
      />

      {/* The counter counts the grain the operator is working at: cards in the
        * grouped view, pairs in the pair view. The two are never added up — a
        * card is reviewed only when every pair inside it is. */}
      <ValidationStrip
        surface={grouped ? 'candidates' : 'residual'}
        generation={generation}
        seed={filters.seed}
        sampleOrder={filters.sort === 'random'}
        minScore={grouped ? null : toResidualQuery(filters, null).min_score}
      />

      <BlindToggle
        checked={blind}
        onChange={(next) => setFilters({ ...filters, blind: next ? '1' : '0' })}
      />

      {/* Said out loud rather than filtered silently: half a pair is not a
        * filter the server can apply, and a control that quietly does nothing is
        * the defect this bar was rebuilt to remove. */}
      {halfPair && !grouped && (
        <p className="mt-2 text-[0.72rem] text-[var(--color-ink-3)]">
          Vyberte oba portály — dvojice se filtruje jen jako pár (A + B).
        </p>
      )}

      {active.error && <ErrorBanner message={active.error.message} />}

      {active.isLoading && (
        <p className="mt-6 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> {grouped ? 'Načítám skupiny kandidátů…' : 'Loading residual pairs…'}
        </p>
      )}

      {storeReady === false && (
        <p className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 text-sm text-[var(--color-ink-2)]">
          Schema not migrated yet — the program's store does not exist in this database, so there is
          nothing to review.
        </p>
      )}

      {storeReady !== false && !active.isLoading && !active.isError
        && (grouped ? cardRows : rows).length === 0 && (
        <p className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 text-sm text-[var(--color-ink-2)]">
          {grouped
            ? 'Žádná skupina kandidátů neodpovídá těmto filtrům.'
            : 'No pair above this score matches these filters.'}
        </p>
      )}

      {grouped && cardRows.length > 0 && (
        <ResultCount shown={cardRows.length} total={total} noun="groups" />
      )}

      {grouped && cardRows.length > 0 && (
        <ul className="mt-3 space-y-4">
          {cardRows.map((card, i) => (
            <CandidateCard
              key={card.candidate_key}
              candidate={card}
              split={candidateControls(card)}
              eager={i < 2}
              verdict={candidates.overlay[card.candidate_key] ?? null}
              onOpen={() => setOpenCandidate(card.candidate_key)}
              onShortcut={(units) => setCandidateUnits(card.candidate_key, units)}
            />
          ))}
        </ul>
      )}

      {!grouped && rows.length > 0 && (
        <ResultCount shown={rows.length} total={total} noun="pairs" />
      )}

      {!grouped && rows.length > 0 && (
        <ul className="mt-3 space-y-4">
          {rows.map((row, i) => {
            const key = pairKey(row);
            const stored = overlay[key] ?? row.verdict;
            /* THE ONE GATE. Every judge artefact this card renders — the chip,
              * the verdict words, the key and contradicting evidence — hangs off
              * this single prop, so blinding is one condition rather than four
              * places that each have to remember. */
            const judgement = blind && stored == null ? null : row.judgement;
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
                judgement={judgement}
                blind={blind && stored == null}
                verdict={stored}
                pending={pendingKey === key}
                eager={i < 2}
                evidenceHref={pairHref(row.listing_lo, row.listing_hi, generation ?? '', blind)}
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

      {active.hasNextPage && (
        <div className="mt-4">
          <button
            type="button"
            onClick={active.fetchNextPage}
            disabled={active.isFetchingNextPage}
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-3 py-1.5 text-sm text-[var(--color-ink-2)] hover:text-[var(--color-ink)] disabled:opacity-50"
          >
            {active.isFetchingNextPage ? 'Loading…' : 'Load more'}
          </button>
        </div>
      )}

      {openCandidate != null && candidateOf(openCandidate) && (
        <CandidateDialog
          candidateKey={openCandidate}
          generation={candidateOf(openCandidate)!.generation}
          split={candidateControls(candidateOf(openCandidate)!)}
          /* The drawer is the same review, so it blinds with the queue and
            * un-blinds on the same condition: this card has been ruled on. */
          blind={
            blind
            && candidates.overlay[openCandidate] == null
            && !(candidateOf(openCandidate)!.reviewed)
          }
          onClose={() => setOpenCandidate(null)}
        />
      )}
    </div>
  );
}
