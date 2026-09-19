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
 * FILTERS ARE KEYS, NEVER PREDICATES. Every control sends a NAME the server
 * validates against its own registry; nothing here composes SQL, and an unknown
 * value is the server's 400 rather than this page's problem.
 *
 * A GROUP IS NOT ALWAYS ONE ANSWER, so every member carries a unit letter and
 * two letters raise the split row. The machinery — the letters, the relation per
 * unit pair, the stored ruling read back off the members' pair verdicts — is
 * `components/autodedup/UnitSplit`, shared with the candidate-group view on
 * /autodedup/residual, which rules on the adverts the engine did NOT merge using
 * this same card. The pieces this page used to own and now shares (the filter
 * bar, the member grid, the dialog's member row, the verdict overlay) are
 * re-exported below, because they are this queue's vocabulary too and a page
 * that changed all of its import sites in the same breath as the extraction is
 * how a refactor turns into a regression.
 *
 * "NOT YET" IS A REAL ANSWER. Before migration 528 and before the first score
 * run there is nothing to show, and the page says so in words. A zero would read
 * as "the engine found no duplicates", which is the one wrong answer.
 */

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import {
  getAutodedupGroup,
  getAutodedupGroups,
  type AutodedupGroup,
  type AutodedupGroupFilters,
  type AutodedupJudgementRow,
  type AutodedupVerdictRow,
  type AutodedupVerdictValue,
} from '@/lib/api';
import { useUrlFilters } from '@/lib/useUrlFilters';
import { fmtCount } from '@/lib/format';
import Dialog from '@/components/Dialog';
import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import EvidenceChips, {
  Chip,
  EvidenceLegend,
  fmtScore,
} from '@/components/autodedup/EvidenceChips';
import { parseBlockValue } from '@/components/autodedup/BlockSelect';
import StaleVerdictNotice from '@/components/autodedup/StaleVerdictNotice';
import {
  GenerationNotice,
  useAutodedupGenerations,
} from '@/components/autodedup/GenerationSelect';
import VerdictButtons, { GROUP_LABELS } from '@/components/autodedup/VerdictButtons';
import VerdictNotes, {
  annotationInput,
  useVerdictAnnotations,
} from '@/components/autodedup/VerdictNotes';
import { JudgeChip } from '@/components/autodedup/PairCard';
import ValidationStrip, { BlindToggle } from '@/components/autodedup/ValidationStrip';
import { useInfiniteList, type InfiniteListPage } from '@/lib/useInfiniteList';
import MemberGrid from '@/components/autodedup/MemberGrid';
import MemberRow from '@/components/autodedup/MemberRow';
import {
  FILTER_CONTROL,
  FILTER_LABEL,
  FilterBar,
  ResultCount,
} from '@/components/autodedup/FilterBar';
import {
  EMPTY_FILTERS,
  flag,
  num,
  pairHref,
  sanitizeGroupFilters,
  type GroupFilterState,
} from '@/components/autodedup/filterState';
import {
  EMPTY_SPLIT,
  SplitRow,
  UnitSelect,
  deriveSplit,
  splitInput,
  unitPairKey,
  type SplitControls,
  type SplitState,
} from '@/components/autodedup/UnitSplit';
import useVerdictOverlay from '@/components/autodedup/useVerdictOverlay';

/* The shared vocabulary, re-exported from where this queue's siblings expect to
 * find it. */
export {
  FILTER_CONTROL,
  FILTER_LABEL,
  FilterBar,
  ResultCount,
  SOURCES,
} from '@/components/autodedup/FilterBar';
export {
  DEFAULT_SEED,
  EMPTY_FILTERS,
  VERDICTS,
  pairHref,
  sanitizeGroupFilters,
  type GroupFilterState,
} from '@/components/autodedup/filterState';
export {
  DEFAULT_RELATION,
  EMPTY_SPLIT,
  SPLIT_RELATIONS,
  SPLIT_RELATION_LABELS,
  SplitRow,
  UNIT_LETTERS,
  UnitSelect,
  clusterVerdictOf,
  deriveSplit,
  distinctUnits,
  receiptRelations,
  relationOf,
  splitInput,
  splitSummary,
  unitOf,
  unitPairKey,
  unitPairs,
  unitsSummary,
  type SplitControls,
  type SplitError,
  type SplitReceipt,
  type SplitState,
  type UnitMap,
} from '@/components/autodedup/UnitSplit';
export { default as useVerdictOverlay } from '@/components/autodedup/useVerdictOverlay';
export { MEMBERS_BEFORE_FOLD, EAGER_MEMBERS } from '@/components/autodedup/MemberGrid';

const NOT_YET = 'not yet';
const PAGE_SIZE = 20;

/* The wire shape. An empty control is a MISSING parameter, never an empty
 * string: `lib/api`'s request() drops both, and spelling it here keeps the
 * query key stable so a cleared filter refetches the same page it started on. */
export function toQuery(f: GroupFilterState, after: string | null): AutodedupGroupFilters {
  /* ONE url key, TWO wire parameters: a block is a code and a grain (migration
   * 529), and the code alone names two different blocks — a town and a quarter
   * that happen to share a number. */
  const block = parseBlockValue(f.block);
  return {
    /* Omitted, never defaulted — the server answers with the pass it read. */
    generation: f.generation || null,
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
    /* Sent only with the order it names: a seed on a weakest-edge query is a
     * parameter the statement never reads, and it would split the react-query
     * cache by a value that changes nothing. */
    seed: f.sort === 'random' ? f.seed : null,
  };
}

interface GroupsPage extends InfiniteListPage<AutodedupGroup> {
  store_ready: boolean;
  /* WHICH PASS this queue actually is — the server's answer, not the request's:
   * with no generation named the page asks for "the newest" and only the reply
   * says which one that was. */
  generation: string | null;
  /* The server counts the whole filtered set on the FIRST page only, which is
   * the page `useInfiniteList` hands back as `firstPage` — so "20 of N" reads
   * the number from where it was actually sent. */
  total: number | null;
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
  /* What passes exist, and which is current — the picker's vocabulary, and the
   * one fact that lets this page notice it is showing a superseded queue. */
  const { latest } = useAutodedupGenerations();

  const list = useInfiniteList<AutodedupGroup, GroupsPage>({
    queryKey: ['autodedup', 'groups', filters],
    queryFn: async (cursor) => {
      const res = await getAutodedupGroups(toQuery(filters, (cursor as string | null) ?? null));
      return {
        rows: res.data?.items ?? [],
        nextCursor: res.data?.next_after ?? undefined,
        store_ready: res.store_ready,
        generation: res.data?.generation ?? null,
        total: res.data?.total ?? null,
      };
    },
    pageSize: PAGE_SIZE,
    getRowId: (row) => row.cluster_key,
  });

  const storeReady = list.firstPage?.store_ready ?? null;
  const rows = list.rows;
  const total = list.firstPage?.total ?? null;
  /* The pass the QUEUE read: what the URL named, else what the server answered
   * with. Null only before the first page lands. */
  const generation = filters.generation || list.firstPage?.generation || null;
  /* Default OFF on this surface — so anything that is not the explicit '1' is
   * "show the judge", including a hand-edited link. */
  const blind = filters.blind === '1';

  /* Per-row actions name the pass the ROW came from, never the queue's: a split
   * is stored against one clustering, and the evidence link must open the same
   * one the card was drawn from. */
  const generationOf = (clusterKey: number): string =>
    rows.find((row) => row.cluster_key === clusterKey)?.generation ?? generation ?? '';

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
            generationOf(clusterKey),
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

      <FilterBar value={filters} onChange={setFilters} showChangedVerdict>
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
            {/* The one order that is not a working order: a seeded shuffle, so
              * the error rate measured on it is about the engine rather than
              * about the top of a queue sorted by where the errors live. */}
            <option value="random">náhodný vzorek</option>
          </select>
        </label>
      </FilterBar>

      <GenerationNotice
        generation={generation}
        latest={latest}
        onLatest={() => setFilters({ ...filters, generation: '' })}
      />

      <ValidationStrip
        surface="groups"
        generation={generation}
        seed={filters.seed}
        sampleOrder={filters.sort === 'random'}
      />

      {/* OFF by default here: the groups queue is a working surface, and the
        * judge's word on an edge is evidence the operator is entitled to. It is
        * offered because a session that wants an unbiased error rate on GROUPS
        * needs the same blinding the residual queue has by default. */}
      <BlindToggle
        checked={blind}
        onChange={(next) => setFilters({ ...filters, blind: next ? '1' : '0' })}
      />

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
              blind={blind}
              verdict={overlay[String(group.cluster_key)] ?? group.verdict}
              pending={pendingKey === String(group.cluster_key)}
              notes={notes}
              onVerdict={(value, annotation) =>
                submit(String(group.cluster_key), {
                  kind: 'cluster',
                  cluster_key: group.cluster_key,
                  /* WHICH PASS this ruling was taken on (E58). The key alone
                   * names a different set of adverts in every generation, and
                   * the server refuses a cluster verdict that does not say. */
                  generation: group.generation,
                  verdict: value,
                  ...annotation,
                })
              }
              onSaveNote={(stored) =>
                submit(String(group.cluster_key), {
                  kind: 'cluster',
                  cluster_key: group.cluster_key,
                  generation: group.generation,
                  verdict: stored.verdict,
                  ...annotationInput(
                    notes.annotationOf(String(group.cluster_key), stored),
                  ),
                })
              }
              onOpen={() => setOpenKey(group.cluster_key)}
              split={splitControls(group.cluster_key)}
              generation={group.generation}
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
          generation={generationOf(openKey)}
          split={splitControls(openKey)}
          /* The drawer is the same review, so it blinds with the queue — and
            * un-blinds on the same condition: the cluster carries a verdict. */
          blind={blind && (overlay[String(openKey)] ?? null) == null
            && (rows.find((r) => r.cluster_key === openKey)?.verdict ?? null) == null}
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
  blind,
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
  /* Hide every judge artefact on this card until it carries a verdict. */
  blind: boolean;
  split: SplitControls;
  generation: string;
  notes: ReturnType<typeof useVerdictAnnotations>;
}) {
  /* A group being ruled unit by unit is never folded, whatever its size: the
   * assignment travels WHOLE, so a letter set over adverts the operator cannot
   * see is a ruling by omission. Any unit touched — or a split already stored —
   * opens the card. */
  const splitStarted = Object.keys(split.state.units).length > 0 || split.stored != null;
  /* A ruled card shows the judge again: the blinding protects the DECISION, and
   * the operator learns nothing from a chip they can never see. */
  const revealed = !blind || verdict != null;
  const noteKey = String(group.cluster_key);
  /* The overlay wins: once this session has ruled the group again, the ruling is
   * about THIS set of adverts and the hint has served its purpose. */
  const stale = verdict == null ? group.stale_verdict : null;
  const arrived = new Set(stale?.added ?? []);
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
        /* "judged 2" / "not judged" is a judge artefact like the chip is: it
          * says the machine has (or has not) an opinion on this group, which is
          * exactly what blind mode withholds until the operator has ruled. */
        nJudged={revealed ? group.n_judged_edges : null}
        sharedPhoto={group.shared_photo_warning}
        maxGapDays={group.max_gap_days}
      />

      {/* EVERY advert of the group, each with its own gallery and its own unit
        * select. Four fit a row at lg and the rest wrap — the card is as tall as
        * the group is big, which is the honest shape for a surface that asks
        * "are these the same flat?". */}
      <StaleVerdictNotice stale={stale} />

      <MemberGrid
        members={group.members}
        eager={eager}
        unfolded={splitStarted}
        renderUnder={(m) => (
          <>
            {/* WHICH advert is the new one. The notice names the ids; this puts
              * the same fact on the card the operator is actually looking at,
              * which is where the question "is THIS one of them too?" is asked. */}
            {arrived.has(m.listing_id) && (
              <span
                data-testid={`stale-added-${m.listing_id}`}
                className="mb-1 inline-block rounded-[var(--radius-xs)] border border-[var(--color-ochre)] bg-[var(--color-ochre-soft)] px-1.5 py-0.5 text-[0.6rem] tracking-[0.08em] uppercase text-[var(--color-ink-2)]"
              >
                nový od verdiktu
              </span>
            )}
            <UnitSelect
              listingId={m.listing_id}
              units={split.state.units}
              count={group.members.length}
              onChange={(unit) => split.setUnit(m.listing_id, unit)}
            />
          </>
        )}
      />

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
  blind,
  onClose,
}: {
  clusterKey: number;
  generation: string;
  split: SplitControls;
  blind: boolean;
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
          {/* The same hint the card wears (E58) — the dialog is where a group is
            * ruled in full, so it must not be the one surface that hides it. */}
          <StaleVerdictNotice stale={data.stale_verdict ?? null} />
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
                        <td className={TD}>
                          {blind ? (
                            <span className="text-[var(--color-ink-4)]">skryto</span>
                          ) : judge ? (
                            <JudgeChip judgement={judge} />
                          ) : (
                            '—'
                          )}
                        </td>
                        <td className={TD}>
                          <Link
                            to={pairHref(p.listing_lo, p.listing_hi, generation, blind)}
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
