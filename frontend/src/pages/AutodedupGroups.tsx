/* AUTODEDUP · Groups — the proposed clusters, weakest edge first.
 *
 * WHAT THE OPERATOR IS DOING HERE. The engine has proposed that these adverts
 * are the same real-world property. Nothing has been applied: the whole trial is
 * shadow mode (D4), so this page collects an opinion and writes it into the
 * program's own schema. That is said on the page, not left to be inferred —
 * "Confirm" must never read as "merge now".
 *
 * WEAKEST EDGE FIRST IS THE DEFAULT SORT, because a cluster is only as right as
 * its worst link: a five-member group joined by one 0.52 edge is where the false
 * merges live, and a newest-first queue would bury it under confident ones.
 *
 * FILTERS ARE KEYS, NEVER PREDICATES. Every control below sends a NAME the
 * server validates against its own registry; nothing here composes SQL, and an
 * unknown value is the server's 400 rather than this page's problem.
 *
 * A GROUP IS NOT ALWAYS ONE ANSWER. The engine proposes a set, and the operator
 * regularly finds two of its adverts are one flat, a third is the house next
 * door and a fourth is a different unit of the same development. The four
 * whole-group buttons cannot say that, so every member carries a UNIT LABEL:
 * members sharing a letter are one property, members in different letters are
 * the chosen relation — and every pair across letters becomes a permanent
 * must-not-link, which is why the relation is named rather than assumed.
 *
 * "NOT YET" IS A REAL ANSWER. Before migration 528 and before the first score
 * run there is nothing to show, and the page says so in words. A zero would read
 * as "the engine found no duplicates", which is the one wrong answer.
 */

import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import {
  getAutodedupGroup,
  getAutodedupGroups,
  postAutodedupSplitVerdict,
  postAutodedupVerdict,
  type AutodedupGroup,
  type AutodedupGroupFilters,
  type AutodedupJudgementRow,
  type AutodedupMemberDetail,
  type AutodedupSplitInput,
  type AutodedupSplitRelation,
  type AutodedupSplitResult,
  type AutodedupVerdictInput,
  type AutodedupVerdictRow,
  type AutodedupVerdictValue,
} from '@/lib/api';
import { ROUTES, withQuery, type RoutePath } from '@/lib/routes';
import { useUrlFilters } from '@/lib/useUrlFilters';
import { imageSrc } from '@/lib/imageUrl';
import { pushToast } from '@/lib/toast';
import { fmtCount } from '@/lib/format';
import Dialog from '@/components/Dialog';
import ErrorBanner from '@/components/ErrorBanner';
import ImageCarousel from '@/components/ImageCarousel';
import Spinner from '@/components/Spinner';
import EvidenceChips, {
  Chip,
  EvidenceLegend,
  fmtScore,
} from '@/components/autodedup/EvidenceChips';
import BlockSelect, { parseBlockValue } from '@/components/autodedup/BlockSelect';
import ListingMini, { memberAttrs, memberListingPath } from '@/components/autodedup/ListingMini';
import VerdictButtons, { GROUP_LABELS } from '@/components/autodedup/VerdictButtons';
import { JudgeChip } from '@/components/autodedup/PairCard';
import { useInfiniteList, type InfiniteListPage } from '@/lib/useInfiniteList';

const NOT_YET = 'not yet';
const PAGE_SIZE = 20;
const DEFAULT_GENERATION = 'g1';
/* Four members fit one row at every width the shell allows; the rest are
 * counted rather than cropped, so the card never lies about the group size. */
const VISIBLE_MEMBERS = 4;

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
  sort: 'weakest' | 'newest' | 'largest';
}

export const EMPTY_FILTERS: GroupFilterState = {
  generation: DEFAULT_GENERATION,
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
};

/* A blank control is a MISSING parameter; so is a typo. `Number('abc')` is NaN,
 * which `request()` happily stringifies into `?min_size=NaN` and the server
 * answers with a 422 nobody can read — a filter that cannot be parsed simply
 * does not constrain. */
const num = (v: string): number | null => {
  if (v.trim() === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};
const flag = (v: string): 0 | 1 | null => (v === '1' ? 1 : v === '0' ? 0 : null);

/* The wire shape. An empty control is a MISSING parameter, never an empty
 * string: `lib/api`'s request() drops both, and spelling it here keeps the
 * query key stable so a cleared filter refetches the same page it started on. */
export function toQuery(f: GroupFilterState, after: string | null): AutodedupGroupFilters {
  /* ONE url key, TWO wire parameters: a block is a code and a grain (migration
   * 529), and the code alone names two different blocks — a town and a quarter
   * that happen to share a number. */
  const block = parseBlockValue(f.block);
  return {
    generation: f.generation || DEFAULT_GENERATION,
    after,
    limit: PAGE_SIZE,
    block: block.block,
    block_grain: block.block_grain,
    source: f.source || null,
    category_main: f.category_main || null,
    category_type: f.category_type || null,
    min_size: num(f.min_size),
    max_size: num(f.max_size),
    min_score: num(f.min_score),
    max_score: num(f.max_score),
    verdict: f.verdict || null,
    shared_photo: flag(f.shared_photo),
    has_judgement: flag(f.has_judgement),
    sort: f.sort,
  };
}

/* The evidence page is a DIFFERENT generation's worth of rows unless it is
 * told which one the queue was reading; the drill-down carries it rather than
 * silently falling back to the default. */
export function pairHref(lo: number, hi: number, generation: string): RoutePath {
  return withQuery(ROUTES.autodedupPair.build({ lo, hi }), {
    generation: generation || null,
  });
}

export const FILTER_LABEL = 'text-[0.6rem] tracking-[0.12em] uppercase text-[var(--color-ink-3)]';
export const FILTER_CONTROL =
  'mt-0.5 w-full rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.75rem] text-[var(--color-ink)]';

/* The portal vocabulary is the backend's `listings.source` enum; the page only
 * offers the nine that exist rather than a free-text box, so a typo can never
 * silently return an empty queue. */
export const SOURCES = [
  'sreality',
  'bazos',
  'bezrealitky',
  'idnes',
  'mmreality',
  'remax',
  'ceskereality',
  'realitymix',
  'maxima',
];

export function FilterBar<T extends GroupFilterState>({
  value,
  onChange,
  children,
  /* A control this surface does not SEND must not be rendered: an inert select
   * that silently returns the same list is worse than no select at all. The
   * residual view filters by a source PAIR, not a single portal, and its route
   * takes no category keys — so it drops both groups of controls.  */
  showSource = true,
  showCategory = true,
}: {
  /* Generic over the filter state so a surface with extra keys of its own (the
   * residual view's zone + portal pair) keeps them through every edit made
   * here — a non-generic bar would spread them away on the first keystroke. */
  value: T;
  onChange: (next: T) => void;
  children?: ReactNode;
  showSource?: boolean;
  showCategory?: boolean;
}) {
  const set = <K extends keyof T>(key: K, v: T[K]) => onChange({ ...value, [key]: v });
  return (
    <div className="mt-4 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-3">
      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <label className="block">
          <span className={FILTER_LABEL}>Generation</span>
          <input
            className={FILTER_CONTROL}
            value={value.generation}
            onChange={(e) => set('generation', e.target.value)}
          />
        </label>
        <BlockSelect
          value={value.block}
          onChange={(next) => set('block', next)}
          generation={value.generation || DEFAULT_GENERATION}
          labelClassName={FILTER_LABEL}
          controlClassName={FILTER_CONTROL}
        />
        {showSource && (
          <label className="block">
            <span className={FILTER_LABEL}>Portal</span>
            <select
              className={FILTER_CONTROL}
              value={value.source}
              onChange={(e) => set('source', e.target.value)}
            >
              <option value="">vše</option>
              {SOURCES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        )}
        {showCategory && (
          <label className="block">
            <span className={FILTER_LABEL}>Druh</span>
            <select
              className={FILTER_CONTROL}
              value={value.category_main}
              onChange={(e) => set('category_main', e.target.value)}
            >
              <option value="">vše</option>
              <option value="byt">byt</option>
              <option value="dum">dům</option>
              <option value="pozemek">pozemek</option>
              <option value="komercni">komerční</option>
              <option value="ostatni">ostatní</option>
            </select>
          </label>
        )}
        {showCategory && (
          <label className="block">
            <span className={FILTER_LABEL}>Nabídka</span>
            <select
              className={FILTER_CONTROL}
              value={value.category_type}
              onChange={(e) => set('category_type', e.target.value)}
            >
              <option value="">vše</option>
              <option value="prodej">prodej</option>
              <option value="pronajem">pronájem</option>
              <option value="drazba">dražba</option>
            </select>
          </label>
        )}
        <label className="block">
          <span className={FILTER_LABEL}>Verdict</span>
          <select
            className={FILTER_CONTROL}
            value={value.verdict}
            onChange={(e) => set('verdict', e.target.value)}
          >
            <option value="">vše</option>
            <option value="unreviewed">unreviewed</option>
            <option value="same">same</option>
            <option value="different">different</option>
            <option value="same_building_different_unit">same building</option>
            <option value="same_project_different_unit">same project</option>
            <option value="unsure">unsure</option>
          </select>
        </label>
        {children}
      </div>
    </div>
  );
}

/* HOW MUCH OF THE QUEUE IS ON SCREEN. A keyset page cannot count itself, so the
 * total arrives with the first page and the loaded rows are counted here. When
 * the server sent no count the loaded number is still said plainly — "20 groups
 * loaded" — because a silent list gives no sense of the work left, and a
 * fabricated total would be worse than none. The noun agrees with the number it
 * follows: "1 groups loaded" is the kind of seam that makes a careful page read
 * as a generated one. */
const plural = (n: number, noun: string): string => (n === 1 ? noun.replace(/s$/, '') : noun);
export function ResultCount({
  shown,
  total,
  noun,
}: {
  shown: number;
  total: number | null;
  noun: string;
}) {
  return (
    <p className="mt-4 text-[0.72rem] text-[var(--color-ink-3)] tabular-nums">
      {total == null
        ? `${fmtCount(shown)} ${plural(shown, noun)} loaded`
        : `${fmtCount(shown)} of ${fmtCount(total)} ${plural(total, noun)}`}
    </p>
  );
}

/* One provisional row so the badge flips on click; the server's own row
 * replaces it as soon as it lands. `id: 0` marks it as not-yet-stored. */
function optimisticVerdict(
  input: AutodedupVerdictInput,
  decidedBy: string,
): AutodedupVerdictRow {
  return {
    id: 0,
    kind: input.kind,
    cluster_key: input.cluster_key ?? null,
    listing_lo: input.listing_lo ?? null,
    listing_hi: input.listing_hi ?? null,
    verdict: input.verdict,
    note: input.note ?? null,
    decided_by: decidedBy,
    decided_at: new Date().toISOString(),
  };
}

/* ------------------------------------------------------------ the unit split
 *
 * A unit is a LETTER, not a free-text label: the operator is partitioning the
 * members of one small group, and a typed name would only add a way to spell the
 * same unit two ways. The map is keyed by `listings.id` and lives at page level
 * so the card and the dialog edit ONE assignment — a dialog opened over a card
 * that already separated two adverts must not start from a blank slate. */
export type UnitMap = Record<number, string>;

export const UNIT_LETTERS: readonly string[] = Array.from({ length: 26 }, (_, i) =>
  String.fromCharCode(65 + i),
);

export interface SplitState {
  units: UnitMap;
  relation: AutodedupSplitRelation;
}

/* The wording the operator reads is the relation BETWEEN two units, which is why
 * "same" is not on offer: two different units are never one property. */
export const SPLIT_RELATIONS: ReadonlyArray<AutodedupSplitRelation> = [
  'same_building_different_unit',
  'same_project_different_unit',
  'different',
];

export const SPLIT_RELATION_LABELS: Record<AutodedupSplitRelation, string> = {
  same_building_different_unit: 'stejná budova, jiné jednotky',
  same_project_different_unit: 'stejný projekt, jiné jednotky',
  different: 'nesouvisí',
};

/* The default is the SHAPE THIS FEATURE WAS BUILT FOR: the operator's group was
 * one development with several buildings. The other two are one click away. */
export const DEFAULT_RELATION: AutodedupSplitRelation = 'same_project_different_unit';

export const EMPTY_SPLIT: SplitState = { units: {}, relation: DEFAULT_RELATION };

/* An unassigned member is in unit A — the whole group is one property until the
 * operator says otherwise, which is exactly what the engine proposed. */
export const unitOf = (units: UnitMap, listingId: number): string => units[listingId] ?? 'A';

export function distinctUnits(
  members: ReadonlyArray<{ listing_id: number }>,
  units: UnitMap,
): string[] {
  return Array.from(new Set(members.map((m) => unitOf(units, m.listing_id)))).sort();
}

/* `A: 101,202 · B: 303` — the assignment in one line, so the card says what it is
 * about to store rather than leaving it to be read off four selects. */
export function splitSummary(
  members: ReadonlyArray<{ listing_id: number }>,
  units: UnitMap,
): string {
  const byUnit = new Map<string, number[]>();
  for (const m of members) {
    const unit = unitOf(units, m.listing_id);
    byUnit.set(unit, [...(byUnit.get(unit) ?? []), m.listing_id]);
  }
  return [...byUnit.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([unit, ids]) => `${unit}: ${ids.join(',')}`)
    .join(' · ');
}

/* EVERY member travels, including the ones the card counted rather than showed:
 * the server requires the assignment to name the whole cluster, and a card that
 * sent only its four visible members would be refused — rightly. */
export function splitInput(
  clusterKey: number,
  generation: string,
  members: ReadonlyArray<{ listing_id: number }>,
  state: SplitState,
): AutodedupSplitInput {
  return {
    cluster_key: clusterKey,
    generation,
    units: members.map((m) => ({ listing_id: m.listing_id, unit: unitOf(state.units, m.listing_id) })),
    relation: state.relation,
  };
}

/* The verdict write, shared by both queue pages: optimistic overlay keyed by a
 * caller-chosen string, rolled back on failure. The LIST IS NEVER INVALIDATED —
 * the review-grid lesson: a queue that reorders under a correcting hand causes
 * the mis-clicks it exists to catch. */
export function useVerdictOverlay() {
  const [overlay, setOverlay] = useState<Record<string, AutodedupVerdictRow>>({});
  /* The IN-FLIGHT KEY, not a global boolean. One shared `isPending` would mark
   * every row in the queue busy while a single write lands, and disabling the
   * button that was just clicked blurs it — a keyboard operator loses their
   * place after every verdict. Nothing is disabled here: the optimistic overlay
   * already shows the press and onError already rolls it back. */
  const [inFlight, setInFlight] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: (vars: { key: string; input: AutodedupVerdictInput }) =>
      postAutodedupVerdict(vars.input),
    onMutate: (vars) => {
      const previous = overlay[vars.key];
      setInFlight(vars.key);
      setOverlay((o) => ({ ...o, [vars.key]: optimisticVerdict(vars.input, 'ukládám…') }));
      return { previous };
    },
    onSuccess: (res, vars) => {
      if (res.data) setOverlay((o) => ({ ...o, [vars.key]: res.data as AutodedupVerdictRow }));
      pushToast(
        'ok',
        res.must_not_link
          ? 'Verdict recorded — this pair is now permanently un-linkable.'
          : 'Verdict recorded.',
      );
    },
    onError: (err: Error, vars, ctx) => {
      setOverlay((o) => {
        const next = { ...o };
        if (ctx?.previous) next[vars.key] = ctx.previous;
        else delete next[vars.key];
        return next;
      });
      pushToast('err', `Verdict failed: ${err.message}`);
    },
    onSettled: () => setInFlight(null),
  });
  const submit = useCallback(
    (key: string, input: AutodedupVerdictInput) => mutation.mutate({ key, input }),
    [mutation],
  );

  /* The SPLIT write rides the same overlay, because what it stores about the
   * group is a cluster verdict: the badge has to flip to it exactly as it does
   * for the whole-group buttons. What it does NOT share is the failure
   * treatment — a rejected split must leave the operator's letters on screen to
   * correct, so the error is kept per cluster and shown in place. */
  const [splitErrors, setSplitErrors] = useState<Record<string, string>>({});
  const [splitResults, setSplitResults] = useState<Record<string, AutodedupSplitResult>>({});
  const splitMutation = useMutation({
    mutationFn: (vars: { key: string; input: AutodedupSplitInput }) =>
      postAutodedupSplitVerdict(vars.input),
    onMutate: (vars) => {
      const previous = overlay[vars.key];
      setInFlight(vars.key);
      setSplitErrors((e) => {
        const next = { ...e };
        delete next[vars.key];
        return next;
      });
      const units = new Set(vars.input.units.map((u) => u.unit));
      setOverlay((o) => ({
        ...o,
        [vars.key]: optimisticVerdict(
          {
            kind: 'cluster',
            cluster_key: vars.input.cluster_key,
            verdict: units.size === 1 ? 'same' : vars.input.relation,
          },
          'ukládám…',
        ),
      }));
      return { previous };
    },
    onSuccess: (res, vars) => {
      const stored = res.data?.cluster_verdict ?? null;
      if (stored) setOverlay((o) => ({ ...o, [vars.key]: stored }));
      if (res.data) setSplitResults((r) => ({ ...r, [vars.key]: res.data as AutodedupSplitResult }));
      pushToast(
        'ok',
        `Split recorded — ${res.data?.n_pairs_negative ?? 0} pair(s) permanently un-linkable.`,
      );
    },
    onError: (err: Error, vars, ctx) => {
      setOverlay((o) => {
        const next = { ...o };
        if (ctx?.previous) next[vars.key] = ctx.previous;
        else delete next[vars.key];
        return next;
      });
      setSplitErrors((e) => ({ ...e, [vars.key]: err.message }));
    },
    onSettled: () => setInFlight(null),
  });
  const submitSplit = useCallback(
    (key: string, input: AutodedupSplitInput) => splitMutation.mutate({ key, input }),
    [splitMutation],
  );

  return { overlay, submit, submitSplit, pendingKey: inFlight, splitErrors, splitResults };
}

/* What a card or the dialog needs to edit ONE group's assignment. Bundled rather
 * than passed as seven props, because both surfaces take exactly the same set and
 * a split edited in the dialog has to be the split the card is holding. */
export interface SplitControls {
  state: SplitState;
  setUnit: (listingId: number, unit: string) => void;
  setRelation: (relation: AutodedupSplitRelation) => void;
  save: (members: ReadonlyArray<{ listing_id: number }>) => void;
  pending: boolean;
  error?: string;
  result?: AutodedupSplitResult;
}

/* One member's unit. Letters up to the member count: a group of three cannot
 * hold four units, and offering 26 where 3 are possible is a control that mostly
 * misleads. */
export function UnitSelect({
  listingId,
  units,
  count,
  onChange,
}: {
  listingId: number;
  units: UnitMap;
  count: number;
  onChange: (unit: string) => void;
}) {
  const letters = UNIT_LETTERS.slice(0, Math.min(Math.max(count, 2), UNIT_LETTERS.length));
  return (
    <label className="flex items-center gap-1.5 text-[0.62rem] text-[var(--color-ink-3)]">
      <span>Jednotka #{listingId}</span>
      <select
        className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-1.5 py-0.5 text-[0.68rem] text-[var(--color-ink)]"
        value={unitOf(units, listingId)}
        onChange={(e) => onChange(e.target.value)}
      >
        {letters.map((letter) => (
          <option key={letter} value={letter}>
            {letter}
          </option>
        ))}
      </select>
    </label>
  );
}

/* The split itself, and it appears ONLY once the operator has actually separated
 * something: a relation select over a group nobody has split is a question with
 * no subject. Every pair across two units becomes a permanent must-not-link, so
 * the row says so in words before the click rather than in a toast after it. */
export function SplitRow({
  members,
  split,
  generation,
}: {
  members: ReadonlyArray<{ listing_id: number }>;
  split: SplitControls;
  generation: string;
}) {
  const units = distinctUnits(members, split.state.units);
  if (units.length < 2) return null;
  return (
    <div className="rounded-[var(--radius-sm)] border border-dashed border-[var(--color-rule-strong)] bg-[var(--color-paper)] px-3 py-2 space-y-2">
      <p className="text-[0.7rem] text-[var(--color-ink-2)]">
        Rozdělit na {units.length} jednotky — <span className="font-mono">{splitSummary(members, split.state.units)}</span>.
        Každá dvojice napříč jednotkami dostane trvalý zákaz spojení.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="block">
          <span className={FILTER_LABEL}>Vztah mezi jednotkami</span>
          <select
            className={`${FILTER_CONTROL} w-auto`}
            value={split.state.relation}
            onChange={(e) => split.setRelation(e.target.value as AutodedupSplitRelation)}
          >
            {SPLIT_RELATIONS.map((relation) => (
              <option key={relation} value={relation}>
                {SPLIT_RELATION_LABELS[relation]}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          /* Not disabled while in flight — the review-queue rule: disabling the
           * button that was just clicked drops focus onto <body>. */
          aria-busy={split.pending}
          onClick={() => split.save(members)}
          className={`rounded-[var(--radius-sm)] border border-[var(--color-rule-strong)] bg-[var(--color-paper-2)] px-3 py-1.5 text-[0.72rem] text-[var(--color-ink)] hover:bg-[var(--color-paper-3)] ${
            split.pending ? 'opacity-60' : ''
          }`}
        >
          {split.pending ? 'Ukládám…' : 'Save split'}
        </button>
        <span className="text-[0.62rem] text-[var(--color-ink-4)]">generace {generation}</span>
      </div>
      {split.result && (
        <p className="text-[0.68rem] text-[var(--color-ink-2)]">
          Uloženo: {SPLIT_RELATION_LABELS[split.state.relation]} ·{' '}
          {split.result.n_pairs_same} dvojic jako stejná jednotka,{' '}
          {split.result.n_pairs_negative} oddělených ·{' '}
          <span className="font-mono">{splitSummary(members, split.state.units)}</span>
        </p>
      )}
      {split.error && <ErrorBanner message={split.error} />}
    </div>
  );
}

interface GroupsPage extends InfiniteListPage<AutodedupGroup> {
  store_ready: boolean;
  /* The server counts the whole filtered set on the FIRST page only, which is
   * the page `useInfiniteList` hands back as `firstPage` — so "20 of N" reads
   * the number from where it was actually sent. */
  total: number | null;
}

/* EVERY enumerated key arriving off the URL is checked here, not only the sort:
 * the server 400s an unknown value, and a hand-edited or stale link should show
 * the queue rather than replace it with a red banner. The two keys the server
 * validates against a closed vocabulary are `sort` and `verdict`; the free ones
 * (generation, block, the numbers) are already "no filter" when unparseable. */
const SORTS: ReadonlyArray<GroupFilterState['sort']> = ['weakest', 'newest', 'largest'];
export const VERDICTS: readonly string[] = [
  '',
  'unreviewed',
  'same',
  'different',
  'same_building_different_unit',
  'same_project_different_unit',
  'unsure',
];

export function sanitizeGroupFilters<T extends GroupFilterState>(raw: T): T {
  const sort = SORTS.includes(raw.sort) ? raw.sort : 'weakest';
  const verdict = VERDICTS.includes(raw.verdict) ? raw.verdict : '';
  return sort === raw.sort && verdict === raw.verdict ? raw : { ...raw, sort, verdict };
}

export default function AutodedupGroups() {
  /* The URL is the state. A filtered queue is bookmarkable, shareable and
   * survives a reload — see lib/useUrlFilters. */
  const [urlFilters, setFilters] = useUrlFilters<GroupFilterState>(EMPTY_FILTERS);
  const filters = useMemo(() => sanitizeGroupFilters(urlFilters), [urlFilters]);
  const [openKey, setOpenKey] = useState<number | null>(null);
  const { overlay, submit, submitSplit, pendingKey, splitErrors, splitResults } =
    useVerdictOverlay();
  /* The assignments, at PAGE level: the card and the dialog are two views of one
   * decision, and a dialog that started from a blank slate would silently throw
   * away the letters the operator had already set on the card. */
  const [splits, setSplits] = useState<Record<number, SplitState>>({});
  const generation = filters.generation || DEFAULT_GENERATION;
  const splitControls = (clusterKey: number): SplitControls => {
    const key = String(clusterKey);
    return {
      state: splits[clusterKey] ?? EMPTY_SPLIT,
      setUnit: (listingId, unit) =>
        setSplits((all) => {
          const current = all[clusterKey] ?? EMPTY_SPLIT;
          return {
            ...all,
            [clusterKey]: { ...current, units: { ...current.units, [listingId]: unit } },
          };
        }),
      setRelation: (relation) =>
        setSplits((all) => ({ ...all, [clusterKey]: { ...(all[clusterKey] ?? EMPTY_SPLIT), relation } })),
      save: (members) =>
        submitSplit(
          key,
          splitInput(clusterKey, generation, members, splits[clusterKey] ?? EMPTY_SPLIT),
        ),
      pending: pendingKey === key,
      error: splitErrors[key],
      result: splitResults[key],
    };
  };

  const list = useInfiniteList<AutodedupGroup, GroupsPage>({
    queryKey: ['autodedup', 'groups', filters],
    queryFn: async (cursor) => {
      const res = await getAutodedupGroups(toQuery(filters, (cursor as string | null) ?? null));
      return {
        rows: res.data?.items ?? [],
        nextCursor: res.data?.next_after ?? undefined,
        store_ready: res.store_ready,
        total: res.data?.total ?? null,
      };
    },
    pageSize: PAGE_SIZE,
    getRowId: (row) => row.cluster_key,
  });

  const storeReady = list.firstPage?.store_ready ?? null;
  const rows = list.rows;
  const total = list.firstPage?.total ?? null;

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">AUTODEDUP · Groups</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[52rem]">
          Adverts the engine believes are one real-world property — the same flat on several
          portals, or the same flat re-posted months later. <strong>Nothing has been merged</strong>
          : the trial runs in shadow mode and this page records your opinion inside the program's
          own schema. Weakest link first, because a group is only as right as its worst edge.
        </p>
      </header>

      <EvidenceLegend />

      <FilterBar value={filters} onChange={setFilters}>
        <label className="block">
          <span className={FILTER_LABEL}>Size ≥</span>
          <input
            className={FILTER_CONTROL}
            inputMode="numeric"
            value={filters.min_size}
            onChange={(e) => setFilters({ ...filters, min_size: e.target.value })}
          />
        </label>
        <label className="block">
          <span className={FILTER_LABEL}>Size ≤</span>
          <input
            className={FILTER_CONTROL}
            inputMode="numeric"
            value={filters.max_size}
            onChange={(e) => setFilters({ ...filters, max_size: e.target.value })}
          />
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
          <span className={FILTER_LABEL}>Score ≤</span>
          <input
            className={FILTER_CONTROL}
            inputMode="decimal"
            value={filters.max_score}
            onChange={(e) => setFilters({ ...filters, max_score: e.target.value })}
          />
        </label>
        <label className="block">
          <span className={FILTER_LABEL}>Shared photos</span>
          <select
            className={FILTER_CONTROL}
            value={filters.shared_photo}
            onChange={(e) => setFilters({ ...filters, shared_photo: e.target.value })}
          >
            <option value="">vše</option>
            <option value="1">jen varované</option>
            <option value="0">bez varování</option>
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
        <label className="block">
          <span className={FILTER_LABEL}>Sort</span>
          <select
            className={FILTER_CONTROL}
            value={filters.sort}
            onChange={(e) =>
              setFilters({ ...filters, sort: e.target.value as GroupFilterState['sort'] })
            }
          >
            <option value="weakest">weakest edge first</option>
            <option value="newest">newest first</option>
            <option value="largest">largest first</option>
          </select>
        </label>
      </FilterBar>

      {list.error && <ErrorBanner message={list.error.message} />}

      {list.isLoading && (
        <p className="mt-6 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Loading proposed groups…
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
          No group matches these filters — {NOT_YET} a proposal to review here.
        </p>
      )}

      {rows.length > 0 && (
        <ResultCount shown={rows.length} total={total} noun="groups" />
      )}

      {rows.length > 0 && (
        <ul className="mt-3 space-y-4">
          {rows.map((group, i) => (
            <GroupCard
              key={group.cluster_key}
              group={group}
              eager={i < 2}
              verdict={overlay[String(group.cluster_key)] ?? group.verdict}
              pending={pendingKey === String(group.cluster_key)}
              onVerdict={(value) =>
                submit(String(group.cluster_key), {
                  kind: 'cluster',
                  cluster_key: group.cluster_key,
                  verdict: value,
                })
              }
              onOpen={() => setOpenKey(group.cluster_key)}
              split={splitControls(group.cluster_key)}
              generation={generation}
            />
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

      {openKey != null && (
        <GroupDialog
          clusterKey={openKey}
          generation={generation}
          split={splitControls(openKey)}
          onClose={() => setOpenKey(null)}
        />
      )}
    </div>
  );
}

function GroupCard({
  group,
  verdict,
  onVerdict,
  onOpen,
  pending,
  eager,
  split,
  generation,
}: {
  group: AutodedupGroup;
  verdict: AutodedupVerdictRow | null;
  onVerdict: (value: AutodedupVerdictValue) => void;
  onOpen: () => void;
  pending: boolean;
  eager: boolean;
  split: SplitControls;
  generation: string;
}) {
  const shown = group.members.slice(0, VISIBLE_MEMBERS);
  const hidden = group.members.length - shown.length;
  return (
    <li className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm text-[var(--color-ink)]">
          <span className="font-mono text-[0.78rem]">#{group.cluster_key}</span>{' '}
          <span className="text-[var(--color-ink-3)]">
            {fmtCount(group.size)} adverts · {group.sources.join(' + ')}
          </span>
        </h2>
        <Chip title="Nothing is applied in shadow mode — this is the proposal's own status">
          {group.status}
        </Chip>
        <button
          type="button"
          onClick={onOpen}
          className="ml-auto rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.7rem] text-[var(--color-ink-2)] hover:text-[var(--color-ink)]"
        >
          Open
        </button>
      </div>

      <EvidenceChips
        minEdgeScore={group.min_edge_score}
        nCertificates={group.n_certificate_edges}
        families={group.family_names}
        nJudged={group.n_judged_edges}
        sharedPhoto={group.shared_photo_warning}
        maxGapDays={group.max_gap_days}
      />

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {shown.map((m) => (
          <div key={m.listing_id} className="space-y-1">
            <ListingMini member={m} eager={eager} />
            <UnitSelect
              listingId={m.listing_id}
              units={split.state.units}
              count={group.members.length}
              onChange={(unit) => split.setUnit(m.listing_id, unit)}
            />
          </div>
        ))}
      </div>
      {hidden > 0 && (
        <p className="text-[0.7rem] text-[var(--color-ink-3)]">
          +{hidden} further advert{hidden === 1 ? '' : 's'} in this group — open it to see them
          {/* They still travel with a split, in unit A until the dialog says otherwise. */}
          {' '}and to set their unit.
        </p>
      )}

      <SplitRow members={group.members} split={split} generation={generation} />

      <VerdictButtons
        kind="cluster"
        verdict={verdict}
        onVerdict={onVerdict}
        pending={pending}
        labels={GROUP_LABELS}
      />
    </li>
  );
}

const TH = 'py-1 pr-3 text-left font-medium whitespace-nowrap align-top';
const TD = 'py-1 pr-3 align-top';

function GroupDialog({
  clusterKey,
  generation,
  split,
  onClose,
}: {
  clusterKey: number;
  generation: string;
  split: SplitControls;
  onClose: () => void;
}) {
  const detail = useQuery({
    queryKey: ['autodedup', 'group', clusterKey, generation],
    queryFn: () => getAutodedupGroup(clusterKey, generation),
  });
  const data = detail.data?.data ?? null;
  const titleId = `autodedup-group-${clusterKey}`;

  const judgeByPair = useMemo(() => {
    const out: Record<string, AutodedupJudgementRow> = {};
    for (const j of data?.judgements ?? []) out[`${j.listing_lo}:${j.listing_hi}`] = j;
    return out;
  }, [data]);

  return (
    <Dialog open onClose={onClose} labelledBy={titleId} className="w-[64rem] max-w-full p-5">
      <h2 id={titleId} className="text-lg">
        Group <span className="font-mono">#{clusterKey}</span>
      </h2>
      {detail.isPending && (
        <p className="mt-4 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Loading the group…
        </p>
      )}
      {detail.error && <ErrorBanner message={(detail.error as Error).message} />}
      {data && (
        <div className="mt-4 space-y-5">
          <ul className="space-y-3">
            {data.members.map((m) => (
              <MemberRow
                key={m.listing_id}
                member={m}
                split={split}
                count={data.members.length}
              />
            ))}
          </ul>

          {/* The dialog is where a group too large for one card row is split:
            * every member is on screen here, so the assignment can be completed
            * rather than left half-set. */}
          <SplitRow members={data.members} split={split} generation={generation} />

          <section>
            <h3 className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
              Edges
            </h3>
            <div className="mt-1 overflow-x-auto">
              <table className="w-full text-[0.72rem]">
                <thead>
                  <tr className="text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                    <th className={TH}>Pair</th>
                    <th className={TH}>Score</th>
                    <th className={TH}>Zone</th>
                    <th className={TH}>Certificate</th>
                    <th className={TH}>Judge</th>
                    <th className={TH}></th>
                  </tr>
                </thead>
                <tbody>
                  {data.pairs.map((p) => {
                    const judge = judgeByPair[`${p.listing_lo}:${p.listing_hi}`];
                    return (
                      <tr
                        key={`${p.listing_lo}:${p.listing_hi}`}
                        className="border-t border-[var(--color-rule-soft)]"
                      >
                        <td className={`${TD} font-mono tabular-nums`}>
                          {p.listing_lo} · {p.listing_hi}
                        </td>
                        <td className={`${TD} font-mono tabular-nums`}>{fmtScore(p.score)}</td>
                        <td className={TD}>{p.zone ?? '—'}</td>
                        <td className={TD}>{p.certificate ?? '—'}</td>
                        <td className={TD}>{judge ? <JudgeChip judgement={judge} /> : '—'}</td>
                        <td className={TD}>
                          <Link
                            to={pairHref(p.listing_lo, p.listing_hi, generation)}
                            className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
                          >
                            Evidence
                          </Link>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          {data.conflicts.length > 0 && (
            <section>
              <h3 className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
                Conflicts
              </h3>
              <ul className="mt-1 space-y-1 text-[0.72rem] text-[var(--color-brick)]">
                {data.conflicts.map((c) => (
                  <li key={c.id}>
                    {c.kind}
                    {c.invariant ? ` · ${c.invariant}` : ''}
                    {c.listing_lo != null ? ` · ${c.listing_lo}–${c.listing_hi}` : ''}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
      <div className="mt-5">
        <button
          type="button"
          onClick={onClose}
          className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-3 py-1.5 text-sm text-[var(--color-ink-2)] hover:text-[var(--color-ink)]"
        >
          Close
        </button>
      </div>
    </Dialog>
  );
}

function MemberRow({
  member,
  split,
  count,
}: {
  member: AutodedupMemberDetail;
  split: SplitControls;
  count: number;
}) {
  /* The carousel wants render-ready urls; these photos carry no CLIP tag on
   * this surface, so both decorations are explicitly null rather than faked.
   * The drawer shows ALL the photos, which is why it renders the carousel and
   * the attributes rather than the single-cover card the queue uses. */
  const images = member.images.map((img) => ({
    url: imageSrc(img),
    tag: null,
    confidence: null,
    renderScore: null,
  }));
  const inApp = memberListingPath(member);
  return (
    <li className="grid gap-3 sm:grid-cols-[18rem_1fr] items-start">
      <ImageCarousel images={images} aspect="aspect-[4/3]" />
      <div className="space-y-1">
        <p className="text-[0.75rem] text-[var(--color-ink-2)]">
          <span className="font-mono">#{member.listing_id}</span> · {member.source} ·{' '}
          {member.is_active ? 'aktivní' : 'staženo'}
        </p>
        <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[0.72rem] max-w-[22rem]">
          {memberAttrs(member).map(([label, value]) => (
            <div key={label} className="flex items-baseline justify-between gap-2">
              <dt className="text-[var(--color-ink-4)]">{label}</dt>
              <dd className="font-mono tabular-nums text-[var(--color-ink-2)]">{value}</dd>
            </div>
          ))}
        </dl>
        <UnitSelect
          listingId={member.listing_id}
          units={split.state.units}
          count={count}
          onChange={(unit) => split.setUnit(member.listing_id, unit)}
        />
        <p className="flex items-center gap-3 text-[0.7rem]">
          {inApp && (
            <Link
              to={inApp}
              className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
            >
              Detail
            </Link>
          )}
          {member.source_url && (
            <a
              href={member.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
            >
              Na portálu
            </a>
          )}
        </p>
      </div>
    </li>
  );
}
