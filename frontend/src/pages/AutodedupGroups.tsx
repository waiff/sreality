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
  postAutodedupVerdict,
  type AutodedupGroup,
  type AutodedupGroupFilters,
  type AutodedupJudgementRow,
  type AutodedupMemberDetail,
  type AutodedupVerdictInput,
  type AutodedupVerdictRow,
  type AutodedupVerdictValue,
} from '@/lib/api';
import { ROUTES, withQuery, type RoutePath } from '@/lib/routes';
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
  return {
    generation: f.generation || DEFAULT_GENERATION,
    after,
    limit: PAGE_SIZE,
    block: num(f.block),
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

export function FilterBar({
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
  value: GroupFilterState;
  onChange: (next: GroupFilterState) => void;
  children?: ReactNode;
  showSource?: boolean;
  showCategory?: boolean;
}) {
  const set = <K extends keyof GroupFilterState>(key: K, v: GroupFilterState[K]) =>
    onChange({ ...value, [key]: v });
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
        <label className="block">
          <span className={FILTER_LABEL}>Block</span>
          <input
            className={FILTER_CONTROL}
            inputMode="numeric"
            value={value.block}
            onChange={(e) => set('block', e.target.value)}
          />
        </label>
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
            <option value="unsure">unsure</option>
          </select>
        </label>
        {children}
      </div>
    </div>
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
  return { overlay, submit, pendingKey: inFlight };
}

interface GroupsPage extends InfiniteListPage<AutodedupGroup> {
  store_ready: boolean;
}

export default function AutodedupGroups() {
  const [filters, setFilters] = useState<GroupFilterState>(EMPTY_FILTERS);
  const [openKey, setOpenKey] = useState<number | null>(null);
  const { overlay, submit, pendingKey } = useVerdictOverlay();

  const list = useInfiniteList<AutodedupGroup, GroupsPage>({
    queryKey: ['autodedup', 'groups', filters],
    queryFn: async (cursor) => {
      const res = await getAutodedupGroups(toQuery(filters, (cursor as string | null) ?? null));
      return {
        rows: res.data?.items ?? [],
        nextCursor: res.data?.next_after ?? undefined,
        store_ready: res.store_ready,
      };
    },
    pageSize: PAGE_SIZE,
    getRowId: (row) => row.cluster_key,
  });

  const storeReady = list.firstPage?.store_ready ?? null;
  const rows = list.rows;

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
        <ul className="mt-5 space-y-4">
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
          generation={filters.generation || DEFAULT_GENERATION}
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
}: {
  group: AutodedupGroup;
  verdict: AutodedupVerdictRow | null;
  onVerdict: (value: AutodedupVerdictValue) => void;
  onOpen: () => void;
  pending: boolean;
  eager: boolean;
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
          <ListingMini key={m.listing_id} member={m} eager={eager} />
        ))}
      </div>
      {hidden > 0 && (
        <p className="text-[0.7rem] text-[var(--color-ink-3)]">
          +{hidden} further advert{hidden === 1 ? '' : 's'} in this group — open it to see them.
        </p>
      )}

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
  onClose,
}: {
  clusterKey: number;
  generation: string;
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
              <MemberRow key={m.listing_id} member={m} />
            ))}
          </ul>

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

function MemberRow({ member }: { member: AutodedupMemberDetail }) {
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
