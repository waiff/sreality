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
 * the relation named for THOSE TWO LETTERS — and every pair across letters
 * becomes a permanent must-not-link, which is why the relation is named rather
 * than assumed, and named per unit pair rather than once for the whole group
 * (two units of one building and a third building of the same development are
 * routinely in one group, and one value cannot say both).
 *
 * THE STORED RULING IS READ BACK, NOT REMEMBERED. A split lives in the store as
 * pair verdicts, and the page derives the assignment from the group's
 * `member_verdicts` before the operator can act on it. Page state that a reload
 * throws away would show "A" over every member of a group that was partitioned
 * last week — and the next save would re-derive all pairs as one unit and
 * retract the permanent must-not-links the first save wrote. For the same
 * reason the split row STAYS once a split is stored: putting every member back
 * into one unit is the undo, and the whole-group "Confirm" is not it.
 *
 * "NOT YET" IS A REAL ANSWER. Before migration 528 and before the first score
 * run there is nothing to show, and the page says so in words. A zero would read
 * as "the engine found no duplicates", which is the one wrong answer.
 */

import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import {
  ApiError,
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
  type AutodedupSplitUnit,
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
import ListingMini, {
  MissingPhotoTile,
  memberAttrs,
  memberListingPath,
} from '@/components/autodedup/ListingMini';
import VerdictButtons, { GROUP_LABELS } from '@/components/autodedup/VerdictButtons';
import VerdictNotes, {
  EMPTY_ANNOTATION,
  annotationInput,
  useVerdictAnnotations,
  type VerdictAnnotation,
} from '@/components/autodedup/VerdictNotes';
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

/* Two units, in one order — the key both the relation map and the matrix read. */
export const unitPairKey = (a: string, b: string): string =>
  a <= b ? `${a}|${b}` : `${b}|${a}`;

export interface SplitState {
  units: UnitMap;
  /* The FILL for a unit pair the operator has not named, not the answer for all
   * of them: see `relations`. */
  relation: AutodedupSplitRelation;
  /* THE RELATION IS PER UNIT PAIR. A group regularly holds two units of one
   * BUILDING and a third advert from a different building of the same
   * development. One value for the whole split would stamp a shared building
   * onto the adverts that do not share one, or throw the building away for the
   * ones that do — and both are written as permanent must-not-links AND as
   * calibration labels, corrupting the one distinction the developer rails are
   * measured against. */
  relations: Record<string, AutodedupSplitRelation>;
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

export const EMPTY_SPLIT: SplitState = { units: {}, relation: DEFAULT_RELATION, relations: {} };

/* An unassigned member is in unit A — the whole group is one property until the
 * operator says otherwise, which is exactly what the engine proposed. */
export const unitOf = (units: UnitMap, listingId: number): string => units[listingId] ?? 'A';

export const relationOf = (
  state: SplitState,
  unitA: string,
  unitB: string,
): AutodedupSplitRelation => state.relations[unitPairKey(unitA, unitB)] ?? state.relation;

export function distinctUnits(
  members: ReadonlyArray<{ listing_id: number }>,
  units: UnitMap,
): string[] {
  return Array.from(new Set(members.map((m) => unitOf(units, m.listing_id)))).sort();
}

/* Every unordered pair of the units in play — one row of the relation matrix
 * each, and one wire entry each. */
export function unitPairs(units: readonly string[]): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  for (let i = 0; i < units.length; i += 1) {
    for (let j = i + 1; j < units.length; j += 1) out.push([units[i], units[j]]);
  }
  return out;
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

/* The same line, read off a SUBMITTED assignment rather than off the live
 * selects. The receipt has to say what was stored, and live state stops being
 * that the moment the operator touches a select after saving. */
export function unitsSummary(units: ReadonlyArray<AutodedupSplitUnit>): string {
  return splitSummary(
    units.map((u) => ({ listing_id: u.listing_id })),
    Object.fromEntries(units.map((u) => [u.listing_id, u.unit])),
  );
}

/* THE STORED RULING, READ BACK. A split lives in the store as pair verdicts —
 * members ruled `same` are one unit, a negative pair verdict is the relation
 * between two units — so the assignment is DERIVED from them rather than kept in
 * page state that a reload throws away. Without this the card shows "A" over
 * every member of a group it knows was split, and the next save quietly retracts
 * the permanent must-not-links the first one wrote.
 *
 * Null means nobody has ruled on any pair of this group: there is nothing stored
 * to show, which is a different statement from "everything is one unit". */
export function deriveSplit(
  members: ReadonlyArray<{ listing_id: number }>,
  verdicts: ReadonlyArray<AutodedupVerdictRow>,
): SplitState | null {
  const inside = new Set(members.map((m) => m.listing_id));
  /* The server sends newest first, so the FIRST row for a pair is its live word. */
  const latest = new Map<string, AutodedupVerdictValue>();
  for (const v of verdicts) {
    if (v.kind && v.kind !== 'pair') continue;
    const lo = v.listing_lo;
    const hi = v.listing_hi;
    if (lo == null || hi == null || !inside.has(lo) || !inside.has(hi)) continue;
    if (v.verdict !== 'same' && !SPLIT_RELATIONS.includes(v.verdict as AutodedupSplitRelation)) {
      continue; /* "unsure" says nothing about the partition. */
    }
    const key = `${lo}:${hi}`;
    if (!latest.has(key)) latest.set(key, v.verdict);
  }
  if (latest.size === 0) return null;

  /* Members joined by a `same` verdict are one unit — the transitive closure, so
   * a chain of pair rulings lands in one letter rather than three. */
  const parent = new Map<number, number>();
  const find = (x: number): number => {
    const up = parent.get(x);
    if (up == null || up === x) return x;
    const root = find(up);
    parent.set(x, root);
    return root;
  };
  const union = (a: number, b: number) => {
    const [ra, rb] = [find(a), find(b)];
    if (ra !== rb) parent.set(rb, ra);
  };
  for (const m of members) parent.set(m.listing_id, m.listing_id);
  for (const [key, verdict] of latest) {
    if (verdict !== 'same') continue;
    const [lo, hi] = key.split(':').map(Number);
    union(lo, hi);
  }

  /* Letters follow the member order, so the first advert on the card is A. */
  const letterOf = new Map<number, string>();
  const units: UnitMap = {};
  for (const m of members) {
    const root = find(m.listing_id);
    if (!letterOf.has(root)) letterOf.set(root, UNIT_LETTERS[letterOf.size] ?? 'A');
    units[m.listing_id] = letterOf.get(root)!;
  }

  const relations: Record<string, AutodedupSplitRelation> = {};
  for (const [key, verdict] of latest) {
    if (verdict === 'same') continue;
    const [lo, hi] = key.split(':').map(Number);
    const pair = unitPairKey(units[lo], units[hi]);
    if (!(pair in relations)) relations[pair] = verdict as AutodedupSplitRelation;
  }
  return { units, relation: DEFAULT_RELATION, relations };
}

/* EVERY member travels, including the ones the card counted rather than showed:
 * the server requires the assignment to name the whole cluster, and a card that
 * sent only its four visible members would be refused — rightly. The relation of
 * every unit pair travels too, named rather than left to the server's fill. */
export function splitInput(
  clusterKey: number,
  generation: string,
  members: ReadonlyArray<{ listing_id: number }>,
  state: SplitState,
  confirmRetract = false,
  annotation: VerdictAnnotation = EMPTY_ANNOTATION,
): AutodedupSplitInput {
  const units = distinctUnits(members, state.units);
  return {
    cluster_key: clusterKey,
    generation,
    units: members.map((m) => ({ listing_id: m.listing_id, unit: unitOf(state.units, m.listing_id) })),
    relation: state.relation,
    relations: unitPairs(units).map(([unit_a, unit_b]) => ({
      unit_a,
      unit_b,
      relation: relationOf(state, unit_a, unit_b),
    })),
    ...(confirmRetract ? { confirm_retract: true } : {}),
    /* ONE set for the whole split: it is one ruling, and the server stamps it on
     * every pair row and on the cluster row. */
    ...annotationInput(annotation),
  };
}

export interface SplitError {
  message: string;
  /* The server refused because the split takes back an earlier ruling (409). */
  needsConfirm: boolean;
}

export interface SplitReceipt {
  input: AutodedupSplitInput;
  result: AutodedupSplitResult;
}

/* How much two adverts have in common, weakest first — the server's own order.
 * A split that says different things about different unit pairs is summarised on
 * the CLUSTER by its weakest claim, the only statement true of the whole group,
 * so the optimistic badge has to agree with the row that is about to land. */
const RELATION_STRENGTH: Record<AutodedupSplitRelation, number> = {
  different: 0,
  same_project_different_unit: 1,
  same_building_different_unit: 2,
};

export function clusterVerdictOf(input: AutodedupSplitInput): AutodedupVerdictValue {
  const used = new Set(input.units.map((u) => u.unit));
  if (used.size < 2) return 'same';
  const relations = (input.relations ?? []).map((r) => r.relation);
  if (relations.length === 0) return input.relation;
  return relations.reduce((weakest, r) =>
    RELATION_STRENGTH[r] < RELATION_STRENGTH[weakest] ? r : weakest,
  );
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
      const retracted = res.must_not_link_retracted ?? 0;
      pushToast(
        'ok',
        res.must_not_link
          ? 'Verdict recorded — this pair is now permanently un-linkable.'
          : retracted > 0
            /* A cluster confirmed as one property drops every veto inside it —
             * said out loud, because it is the permanent half of the click. */
            ? `Verdict recorded — ${retracted} pair(s) are linkable again.`
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
  const [splitErrors, setSplitErrors] = useState<Record<string, SplitError>>({});
  /* The SUBMITTED assignment is kept beside the server's counts. A receipt read
   * off live state is not a receipt: change a select after saving and the line
   * would confirm, in the server's own numbers, a ruling that was never sent. */
  const [splitResults, setSplitResults] = useState<Record<string, SplitReceipt>>({});
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
      setOverlay((o) => ({
        ...o,
        [vars.key]: optimisticVerdict(
          {
            kind: 'cluster',
            cluster_key: vars.input.cluster_key,
            verdict: clusterVerdictOf(vars.input),
          },
          'ukládám…',
        ),
      }));
      return { previous };
    },
    onSuccess: (res, vars) => {
      const stored = res.data?.cluster_verdict ?? null;
      if (stored) setOverlay((o) => ({ ...o, [vars.key]: stored }));
      if (res.data) {
        setSplitResults((r) => ({
          ...r,
          [vars.key]: { input: vars.input, result: res.data as AutodedupSplitResult },
        }));
      }
      const reversed = res.data?.reversed_pairs?.length ?? 0;
      pushToast(
        'ok',
        `Split recorded — ${res.data?.n_pairs_negative ?? 0} pair(s) permanently un-linkable`
          + (reversed > 0 ? `, ${reversed} earlier ruling(s) taken back.` : '.'),
      );
    },
    onError: (err: Error, vars, ctx) => {
      setOverlay((o) => {
        const next = { ...o };
        if (ctx?.previous) next[vars.key] = ctx.previous;
        else delete next[vars.key];
        return next;
      });
      /* 409 is not a failure: the server is asking whether the operator really
       * means to take back a veto they wrote earlier. The letters stay, the
       * reason is shown in place, and the Save button arms rather than retries. */
      setSplitErrors((e) => ({
        ...e,
        [vars.key]: {
          message: err.message,
          needsConfirm: err instanceof ApiError && err.status === 409,
        },
      }));
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
  setRelation: (unitA: string, unitB: string, relation: AutodedupSplitRelation) => void;
  save: (members: ReadonlyArray<{ listing_id: number }>, confirmRetract?: boolean) => void;
  pending: boolean;
  /* WHY this split — the chips and the note, sent with the save. NOT hydrated
   * from the stored cluster verdict the way a plain verdict's is: the server
   * writes the ASSIGNMENT into that note (`A: 11,12 | B: 13`), with the
   * operator's own words merely in front of it, so reading it back into the
   * input and re-posting would store the summary twice. */
  annotation: VerdictAnnotation;
  setAnnotation: (next: VerdictAnnotation) => void;
  error?: SplitError;
  receipt?: SplitReceipt;
  /* The ruling that is STORED, derived from the group's pair verdicts. Present
   * means this group already carries a split — which is what keeps the row on
   * screen when the operator merges every member back into one unit. */
  stored: SplitState | null;
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

/* THE SPLIT. It appears once the operator has actually separated something — a
 * relation select over a group nobody has split is a question with no subject —
 * AND it stays once the group carries a stored split, however the operator then
 * assigns the letters. That second half is not a detail: putting every member
 * back into one unit is the UNDO, the one assignment the server implements as
 * "every pair same, every veto retracted", and a row that vanished at one unit
 * made it unreachable from this page. The whole-group "Confirm" is not that
 * undo either — it writes `same` on the cluster and leaves the pair vetoes
 * standing, a contradiction visible on no surface at all.
 *
 * Every pair across two units becomes a permanent must-not-link, so the row says
 * so in words before the click rather than in a toast after it. */
export function SplitRow({
  members,
  split,
  generation,
  notesOpen = false,
}: {
  members: ReadonlyArray<{ listing_id: number }>;
  split: SplitControls;
  generation: string;
  /* Open where one group is the whole screen (the dialog); collapsed on a queue
   * card, which is a scroll. */
  notesOpen?: boolean;
}) {
  const units = distinctUnits(members, split.state.units);
  const one = units.length < 2;
  if (one && !split.stored && !split.receipt) return null;
  const pairs = unitPairs(units);
  const confirm = split.error?.needsConfirm ?? false;
  return (
    <div className="rounded-[var(--radius-sm)] border border-dashed border-[var(--color-rule-strong)] bg-[var(--color-paper)] px-3 py-2 space-y-2">
      <p className="text-[0.7rem] text-[var(--color-ink-2)]">
        {one ? (
          <>
            Sloučit zpět: všech {members.length} inzerátů jako jedna jednotka —{' '}
            <span className="font-mono">{splitSummary(members, split.state.units)}</span>.
            Uložením se zruší dřívější zákazy spojení mezi nimi.
          </>
        ) : (
          <>
            Rozdělit na {units.length} jednotky —{' '}
            <span className="font-mono">{splitSummary(members, split.state.units)}</span>.
            Každá dvojice napříč jednotkami dostane trvalý zákaz spojení.
          </>
        )}
      </p>
      {split.stored && (
        /* WHAT IS STORED, not what is on the selects. Without it a reload shows a
         * blank assignment over a group that was ruled on last week. */
        <p className="text-[0.66rem] text-[var(--color-ink-3)]">
          Uloženo dříve:{' '}
          <span className="font-mono">{splitSummary(members, split.stored.units)}</span>
        </p>
      )}
      {pairs.length > 0 && (
        <div className="flex flex-wrap items-end gap-2">
          {/* One relation PER UNIT PAIR: a group can hold two units of one
            * building and a third advert from a different building of the same
            * development, and one value cannot say both. */}
          {pairs.map(([a, b]) => (
            <label key={unitPairKey(a, b)} className="block">
              <span className={FILTER_LABEL}>{`Vztah ${a} ↔ ${b}`}</span>
              <select
                className={`${FILTER_CONTROL} w-auto`}
                value={relationOf(split.state, a, b)}
                onChange={(e) => split.setRelation(a, b, e.target.value as AutodedupSplitRelation)}
              >
                {SPLIT_RELATIONS.map((relation) => (
                  <option key={relation} value={relation}>
                    {SPLIT_RELATION_LABELS[relation]}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          /* Not disabled while in flight — the review-queue rule: disabling the
           * button that was just clicked drops focus onto <body>. */
          aria-busy={split.pending}
          onClick={() => split.save(members, confirm)}
          className={`rounded-[var(--radius-sm)] border px-3 py-1.5 text-[0.72rem] hover:bg-[var(--color-paper-3)] ${
            confirm
              ? 'border-[var(--color-brick)] bg-[var(--color-paper-2)] text-[var(--color-brick)]'
              : 'border-[var(--color-rule-strong)] bg-[var(--color-paper-2)] text-[var(--color-ink)]'
          } ${split.pending ? 'opacity-60' : ''}`}
        >
          {split.pending
            ? 'Ukládám…'
            : confirm
              ? 'Přepsat a uložit'
              : one
                ? 'Sloučit zpět'
                : 'Save split'}
        </button>
        <span className="text-[0.62rem] text-[var(--color-ink-4)]">generace {generation}</span>
      </div>
      {/* The split's own reasons — one set for the whole ruling. There is no
        * "Uložit poznámku" here: the split IS the save button above, and a
        * second one would write a second, different ruling. The toggle NAMES its
        * destination: a card can show this picker and the cluster verdict's at
        * once, and two identical labels over two different drafts silently drop
        * whichever set the operator did not then save. */}
      <VerdictNotes
        defaultOpen={notesOpen}
        value={split.annotation}
        onChange={split.setAnnotation}
        pending={split.pending}
        label="důvod rozdělení"
      />
      {split.receipt && (
        <p className="text-[0.68rem] text-[var(--color-ink-2)]">
          Uloženo: {receiptRelations(split.receipt.input)} ·{' '}
          {split.receipt.result.n_pairs_same} dvojic jako stejná jednotka,{' '}
          {split.receipt.result.n_pairs_negative} oddělených ·{' '}
          <span className="font-mono">{unitsSummary(split.receipt.input.units)}</span>
        </p>
      )}
      {split.error && <ErrorBanner message={split.error.message} />}
    </div>
  );
}

/* The relations of the SUBMITTED split, in the receipt's own words: `A ↔ B:
 * stejná budova…`, or the single relation when the split said one thing. */
export function receiptRelations(input: AutodedupSplitInput): string {
  const relations = input.relations ?? [];
  if (relations.length === 0) return 'jedna jednotka';
  const distinct = new Set(relations.map((r) => r.relation));
  if (distinct.size === 1) return SPLIT_RELATION_LABELS[relations[0].relation];
  return relations
    .map((r) => `${r.unit_a} ↔ ${r.unit_b}: ${SPLIT_RELATION_LABELS[r.relation]}`)
    .join(' · ');
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
  /* The reason chips and the note, at page level for the same reason the
   * assignments are: the card and the dialog edit one decision. */
  const notes = useVerdictAnnotations();
  /* The assignments, at PAGE level: the card and the dialog are two views of one
   * decision, and a dialog that started from a blank slate would silently throw
   * away the letters the operator had already set on the card. */
  const [splits, setSplits] = useState<Record<number, SplitState>>({});
  const generation = filters.generation || DEFAULT_GENERATION;

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

  /* The ruling that is STORED, per cluster, read back off the members' pair
   * verdicts — so an assignment the operator made yesterday is on screen before
   * they can act on it. The untouched card edits THAT, never a blank slate. */
  const storedSplits = useMemo(() => {
    const out: Record<number, SplitState | null> = {};
    for (const row of rows) out[row.cluster_key] = deriveSplit(row.members, row.member_verdicts);
    return out;
  }, [rows]);

  const splitControls = (clusterKey: number): SplitControls => {
    const key = String(clusterKey);
    const stored = storedSplits[clusterKey] ?? null;
    const base = splits[clusterKey] ?? stored ?? EMPTY_SPLIT;
    /* Keyed apart from the cluster verdict's own annotation (`split:` prefix):
     * the two are different statements about the same group, and one draft
     * serving both would carry the reasons of a click into the next save. */
    const splitNoteKey = `split:${clusterKey}`;
    const edit = (patch: (current: SplitState) => SplitState) =>
      setSplits((all) => ({ ...all, [clusterKey]: patch(all[clusterKey] ?? stored ?? EMPTY_SPLIT) }));
    return {
      state: base,
      setUnit: (listingId, unit) =>
        edit((current) => ({ ...current, units: { ...current.units, [listingId]: unit } })),
      setRelation: (unitA, unitB, relation) =>
        edit((current) => ({
          ...current,
          relations: { ...current.relations, [unitPairKey(unitA, unitB)]: relation },
        })),
      save: (members, confirmRetract = false) =>
        submitSplit(
          key,
          splitInput(
            clusterKey,
            generation,
            members,
            base,
            confirmRetract,
            notes.annotationOf(splitNoteKey, null),
          ),
        ),
      pending: pendingKey === key,
      annotation: notes.annotationOf(splitNoteKey, null),
      setAnnotation: (next) => notes.setAnnotation(splitNoteKey, next),
      error: splitErrors[key],
      receipt: splitResults[key],
      stored,
    };
  };

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
              notes={notes}
              onVerdict={(value, annotation) =>
                submit(String(group.cluster_key), {
                  kind: 'cluster',
                  cluster_key: group.cluster_key,
                  verdict: value,
                  ...annotation,
                })
              }
              onSaveNote={(stored) =>
                submit(String(group.cluster_key), {
                  kind: 'cluster',
                  cluster_key: group.cluster_key,
                  verdict: stored.verdict,
                  ...annotationInput(
                    notes.annotationOf(String(group.cluster_key), stored),
                  ),
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
  onSaveNote,
  onOpen,
  pending,
  eager,
  split,
  generation,
  notes,
}: {
  group: AutodedupGroup;
  verdict: AutodedupVerdictRow | null;
  onVerdict: (
    value: AutodedupVerdictValue,
    annotation: { reasons: string[]; note: string | null },
  ) => void;
  /* Re-post the STORED verdict with an edited annotation — the same endpoint and
   * the same overlay, which is why it takes the stored row rather than a value. */
  onSaveNote: (stored: AutodedupVerdictRow) => void;
  onOpen: () => void;
  pending: boolean;
  eager: boolean;
  split: SplitControls;
  generation: string;
  notes: ReturnType<typeof useVerdictAnnotations>;
}) {
  const shown = group.members.slice(0, VISIBLE_MEMBERS);
  const hidden = group.members.length - shown.length;
  const noteKey = String(group.cluster_key);
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
        /* EVERY member gets a select, not only the four the card shows. The
         * assignment travels whole — the server refuses a partial one — so a
         * member with no control on screen would be ruled on by omission, and
         * the operator would have asserted something about adverts they never
         * saw. The photos are still one click away; the letter is not. */
        <div className="space-y-1">
          <p className="text-[0.7rem] text-[var(--color-ink-3)]">
            +{hidden} further advert{hidden === 1 ? '' : 's'} in this group — open it to see
            them; their unit is set here.
          </p>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {group.members.slice(VISIBLE_MEMBERS).map((m) => (
              <UnitSelect
                key={m.listing_id}
                listingId={m.listing_id}
                units={split.state.units}
                count={group.members.length}
                onChange={(unit) => split.setUnit(m.listing_id, unit)}
              />
            ))}
          </div>
        </div>
      )}

      <SplitRow members={group.members} split={split} generation={generation} />

      <VerdictButtons
        kind="cluster"
        verdict={verdict}
        onVerdict={onVerdict}
        pending={pending}
        labels={GROUP_LABELS}
        annotation={notes.annotationOf(noteKey, verdict)}
      />
      <VerdictNotes
        value={notes.annotationOf(noteKey, verdict)}
        onChange={(next) => notes.setAnnotation(noteKey, next)}
        dirty={notes.isDirty(noteKey, verdict)}
        pending={pending}
        onSave={() => verdict && onSaveNote(verdict)}
        label="důvod verdiktu"
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
          <SplitRow members={data.members} split={split} generation={generation} notesOpen />

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
      <ImageCarousel
        images={images}
        aspect="aspect-[4/3]"
        /* A frame the portal refuses says so, here too: the dialog is where the
         * operator looks hardest at the photos. */
        fallback={<MissingPhotoTile source={member.source} reason="foto nedostupné" />}
      />
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
