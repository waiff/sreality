import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';

import {
  dismissDedupCluster,
  getAppSetting,
  getDedupSummary,
  isApiConfigured,
  listDedupCandidates,
  listDedupMerges,
  mergeDedupCluster,
  mergeDedupPropertySet,
  unmergeMergeGroup,
  updateAppSetting,
} from '@/lib/api';
import {
  dedupKeys,
  fetchDedupEngineRuns,
  fetchImagesByListingIds,
  fetchListingDetailByIds,
  fetchPropertySourcesByPropertyIds,
  type DedupEngineRun,
} from '@/lib/queries';
import {
  clusterCandidates,
  diffCluster,
  type DedupCluster,
  type DiffVerdict,
  type ListingDetailLite,
} from '@/lib/dedupDiff';
import { imageSrc } from '@/lib/imageUrl';
import { portalListingUrl, portalShort } from '@/lib/portals';
import { fmtArea, fmtCount, fmtCzk, fmtRelative } from '@/lib/format';
import ImageCarousel from '@/components/ImageCarousel';
import { listingPath, propertyListingPath } from '@/lib/listingUrl';
import type {
  DedupCandidatesResponse,
  DedupPropertySide,
  DedupSummaryBucket,
  DedupSummaryResponse,
  ImagePublic,
  MergeGroup,
  MergesResponse,
  PropertySource,
} from '@/lib/types';

const POLL_MS = 60_000;
/* API sentinel: filter candidates whose markers_matched.verdict IS NULL (most
 * buckets), so clicking a (reason, no-verdict) backlog bucket drills in exactly. */
const NULL_VERDICT = '(none)';

type Bucket = { reason: string; verdict: string | null };
const BTN = 'px-3 py-1.5 text-sm rounded-[var(--radius-sm)] transition-colors disabled:opacity-50';

type ImagesMap = Map<number, ImagePublic[]>;
type SourcesMap = Map<number, PropertySource[]>;
type DetailMap = Map<number, ListingDetailLite>;

export default function Dedup() {
  const qc = useQueryClient();

  // Which backlog bucket the operator drilled into (null = the whole queue).
  const [bucket, setBucket] = useState<Bucket | null>(null);

  const summaryQ = useQuery<DedupSummaryResponse, Error>({
    queryKey: dedupKeys.summary('proposed'),
    queryFn: () => getDedupSummary('proposed'),
    placeholderData: keepPreviousData,
    refetchInterval: POLL_MS,
  });

  const candidatesQ = useQuery<DedupCandidatesResponse, Error>({
    queryKey: dedupKeys.candidates({
      status: 'proposed',
      reason: bucket?.reason ?? null,
      verdict: bucket ? bucket.verdict ?? NULL_VERDICT : null,
    }),
    queryFn: () => listDedupCandidates({
      status: 'proposed',
      limit: 100,
      ...(bucket
        ? { reason: bucket.reason, verdict: bucket.verdict ?? NULL_VERDICT }
        : {}),
    }),
    placeholderData: keepPreviousData,
    refetchInterval: POLL_MS,
  });

  const mergesQ = useQuery<MergesResponse, Error>({
    queryKey: dedupKeys.merges({ limit: 50 }),
    queryFn: () => listDedupMerges({ limit: 50 }),
    placeholderData: keepPreviousData,
  });

  const engineRunsQ = useQuery<DedupEngineRun[], Error>({
    queryKey: dedupKeys.engineRuns(14),
    queryFn: () => fetchDedupEngineRuns(14),
    placeholderData: keepPreviousData,
  });

  const candidates = candidatesQ.data?.data ?? [];
  const clusters = useMemo(() => clusterCandidates(candidates), [candidates]);
  const merges = mergesQ.data?.data ?? [];
  const activeMerges = useMemo(() => merges.filter((m) => !m.fully_undone), [merges]);

  /* Unique ids across both sides of every candidate on screen — the keys for
   * the three batched lookups. ≤100 candidates → ≤200 ids, well under the anon
   * 3 s statement timeout. */
  const propertyIds = useMemo(() => {
    const s = new Set<number>();
    for (const c of candidates) {
      s.add(c.left_property.property_id);
      s.add(c.right_property.property_id);
    }
    return [...s];
  }, [candidates]);

  const srealityIds = useMemo(() => {
    const s = new Set<number>();
    for (const c of candidates) {
      if (c.left_property.sreality_id != null) s.add(c.left_property.sreality_id);
      if (c.right_property.sreality_id != null) s.add(c.right_property.sreality_id);
    }
    return [...s];
  }, [candidates]);

  const sourcesQ = useQuery<SourcesMap, Error>({
    queryKey: dedupKeys.sources(propertyIds),
    queryFn: () => fetchPropertySourcesByPropertyIds(propertyIds),
    enabled: propertyIds.length > 0,
    placeholderData: keepPreviousData,
  });

  const imagesQ = useQuery<ImagesMap, Error>({
    queryKey: dedupKeys.images(srealityIds),
    queryFn: () => fetchImagesByListingIds(srealityIds, 8),
    enabled: srealityIds.length > 0,
    placeholderData: keepPreviousData,
  });

  const detailQ = useQuery<DetailMap, Error>({
    queryKey: dedupKeys.detail(srealityIds),
    queryFn: () => fetchListingDetailByIds(srealityIds),
    enabled: srealityIds.length > 0,
    placeholderData: keepPreviousData,
  });

  const sourcesMap = sourcesQ.data ?? new Map();
  const imagesMap = imagesQ.data ?? new Map();
  const detailMap = detailQ.data ?? new Map();

  const invalidate = () => qc.invalidateQueries({ queryKey: dedupKeys.all });
  const mergeMut = useMutation({ mutationFn: mergeDedupCluster, onSuccess: invalidate });
  const mergeSetMut = useMutation({ mutationFn: mergeDedupPropertySet, onSuccess: invalidate });
  const dismissMut = useMutation({ mutationFn: dismissDedupCluster, onSuccess: invalidate });
  const unmergeMut = useMutation({ mutationFn: unmergeMergeGroup, onSuccess: invalidate });

  const sameIds = (a: number[] | undefined, b: number[]) =>
    a != null && a.length === b.length && a.every((v, i) => v === b[i]);
  // A subset merge for THIS cluster is in flight when the property-set mutation's
  // ids are all members of the cluster.
  const subsetBusyFor = (memberIds: number[]) =>
    mergeSetMut.isPending
    && (mergeSetMut.variables ?? []).every((id) => memberIds.includes(id));

  const filteredTotal = candidatesQ.data?.total ?? 0;
  const returned = candidatesQ.data?.returned ?? candidates.length;

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto">
      <Header proposed={summaryQ.data?.data.total ?? candidates.length} />

      <AutoDedupToggle />

      <AutomationDashboard runs={engineRunsQ.data ?? []} loading={engineRunsQ.isLoading} />

      <ReviewBacklog
        summary={summaryQ.data?.data}
        loading={summaryQ.isLoading}
        selected={bucket}
        onSelect={setBucket}
      />

      <Section
        title="Needs review"
        eyebrow={
          bucket
            ? `${bucketLabel(bucket.reason, bucket.verdict).label} · ${fmtCount(filteredTotal)}${returned < filteredTotal ? ` (showing ${fmtCount(returned)})` : ''}`
            : 'Proposed matches'
        }
        isEmpty={candidates.length === 0}
        empty={
          candidatesQ.isLoading
            ? 'Loading…'
            : candidatesQ.error
              ? `Failed to load: ${candidatesQ.error.message}`
              : bucket
                ? 'Nothing in this bucket.'
                : 'Nothing awaiting review. The engine queues a pair here only when two listings share a street and disposition but it can’t confidently confirm they’re the same property by photos.'
        }
      >
        <div className="space-y-3">
          {clusters.map((cl) => (
            <ClusterCard
              key={cl.key}
              cluster={cl}
              imagesMap={imagesMap}
              sourcesMap={sourcesMap}
              detailMap={detailMap}
              onMergeAll={() => mergeMut.mutate(cl.candidateIds)}
              onMergeSubset={(propertyIds) => mergeSetMut.mutate(propertyIds)}
              onDismiss={() => dismissMut.mutate(cl.candidateIds)}
              busy={
                (mergeMut.isPending && sameIds(mergeMut.variables, cl.candidateIds))
                || (dismissMut.isPending && sameIds(dismissMut.variables, cl.candidateIds))
                || subsetBusyFor(cl.members.map((m) => m.property_id))
              }
            />
          ))}
        </div>
      </Section>

      <Section
        title="Recent merges"
        eyebrow="Auto + operator — every merge is reversible"
        isEmpty={activeMerges.length === 0}
        empty={mergesQ.isLoading ? 'Loading…' : 'No merges yet.'}
      >
        <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] overflow-hidden">
          {activeMerges.map((m) => (
            <MergeRow
              key={m.merge_group_id}
              merge={m}
              onUndo={() => unmergeMut.mutate(m.merge_group_id)}
              busy={unmergeMut.isPending && unmergeMut.variables === m.merge_group_id}
            />
          ))}
        </div>
      </Section>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

/* What the autonomous engine did — eligibility breakdown + how each recent run
 * resolved its candidates (auto-merged by address / identical photos / a High
 * visual verdict, vs left for review). Reads dedup_engine_runs_public. */
function AutomationDashboard({
  runs,
  loading,
}: {
  runs: DedupEngineRun[];
  loading: boolean;
}) {
  const latest = runs[0] ?? null;
  const autoTotal = latest
    ? latest.auto_address + latest.auto_phash + latest.auto_visual
    : 0;
  return (
    <section className="mt-8">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Automation
      </p>
      <h2 className="mt-1 text-xl" style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}>
        Engine activity
      </h2>
      {latest == null ? (
        <div className="mt-3 px-6 py-8 text-center border border-dashed border-[var(--color-rule)] rounded-[var(--radius-md)] text-sm text-[var(--color-ink-3)]">
          {loading ? 'Loading…' : 'The dedup engine hasn’t run yet. Stats appear after its first run.'}
        </div>
      ) : (
        <>
          <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-2">
            <Stat label="Eligible" value={latest.eligible} hint="street + disposition" />
            <Stat label="Loc. unclear" value={latest.flagged_location} hint="no street" muted />
            <Stat label="Disp. unclear" value={latest.flagged_disposition} hint="no disposition" muted />
            <Stat label="Auto-merged" value={autoTotal} hint="this run" accent />
          </div>
          <div className="mt-2 grid grid-cols-2 sm:grid-cols-5 gap-2">
            <Stat label="By address" value={latest.auto_address} small />
            <Stat label="By photos" value={latest.auto_phash} small />
            <Stat label="By visual" value={latest.auto_visual} small />
            <Stat label="Auto-dismissed" value={latest.auto_dismissed ?? 0} small />
            <Stat label="Queued" value={latest.queued} small />
          </div>
          {runs.length > 1 ? <RunTrend runs={runs} /> : null}
          <p className="mt-2 text-[0.7rem] text-[var(--color-ink-4)]">
            Last run {fmtRelative(latest.started_at)} · {fmtCount(latest.pairs_considered)} pairs examined ·
            {' '}{fmtCount(latest.vision_calls)} vision calls
          </p>
        </>
      )}
    </section>
  );
}

function Stat({
  label,
  value,
  hint,
  accent,
  muted,
  small,
}: {
  label: string;
  value: number;
  hint?: string;
  accent?: boolean;
  muted?: boolean;
  small?: boolean;
}) {
  const valueColor = accent
    ? 'text-[var(--color-copper-2)]'
    : muted
      ? 'text-[var(--color-ink-3)]'
      : 'text-[var(--color-ink)]';
  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper-2)] px-3 py-2">
      <div className={`font-mono tabular-nums ${small ? 'text-base' : 'text-xl'} ${valueColor}`}>
        {fmtCount(value)}
      </div>
      <div className="text-[0.62rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">{label}</div>
      {hint ? <div className="text-[0.62rem] text-[var(--color-ink-4)]">{hint}</div> : null}
    </div>
  );
}

/* A tiny sparkline-ish bar row: auto-merges per recent run, newest on the right. */
function RunTrend({ runs }: { runs: DedupEngineRun[] }) {
  const ordered = [...runs].reverse();
  const max = Math.max(1, ...ordered.map((r) => r.auto_address + r.auto_phash + r.auto_visual));
  return (
    <div className="mt-3 flex items-end gap-1 h-12" title="Auto-merges per recent run">
      {ordered.map((r) => {
        const total = r.auto_address + r.auto_phash + r.auto_visual;
        const h = Math.round((total / max) * 100);
        return (
          <div
            key={r.id}
            className="flex-1 bg-[var(--color-copper)]/70 rounded-t-[var(--radius-xs)] min-h-[2px]"
            style={{ height: `${Math.max(h, 3)}%` }}
            title={`${total} auto-merged`}
          />
        );
      })}
    </div>
  );
}

/* Human label + hint + tone for a backlog bucket (reason × verdict). Maps each
 * "why this queued" to an operator-actionable category; unknown reasons fall back
 * to the raw key so a future engine reason still shows up (just unlabelled). */
function bucketLabel(
  reason: string,
  verdict: string | null,
): { label: string; hint: string; tone: 'sage' | 'brick' | 'copper' | 'muted' } {
  if (reason === 'auto_merge_off:address_exact')
    return { label: 'Mergeable now', hint: 'exact address; queued while auto-merge was off', tone: 'sage' };
  if (reason === 'auto_merge_off:image_phash')
    return { label: 'Mergeable now (photos)', hint: 'identical photos; queued while auto-merge was off', tone: 'sage' };
  if (reason === 'auto_merge_off')
    return { label: 'Auto-merge was off', hint: 'queued without a photo check', tone: 'muted' };
  if (reason === 'no_images')
    return { label: 'No photos compared', hint: 'classify didn’t run — retryable', tone: 'muted' };
  if (reason === 'vision_unavailable')
    return { label: 'No visual check', hint: 'vision tools were unavailable — retryable', tone: 'muted' };
  if (reason === 'visual_inconclusive' && verdict === 'Low')
    return { label: 'Compared — different', hint: 'photos clearly differ', tone: 'brick' };
  if (reason === 'visual_inconclusive' && verdict === 'Medium')
    return { label: 'Compared — ambiguous', hint: 'needs your eye', tone: 'copper' };
  if (reason === 'visual_inconclusive')
    return { label: 'Compared — inconclusive', hint: 'no clear verdict', tone: 'muted' };
  if (reason === 'site_plan_different_unit')
    return { label: 'Different unit (site plan)', hint: 'development guard', tone: 'brick' };
  if (reason === 'visual_match')
    return { label: 'Visual match', hint: 'High verdict', tone: 'sage' };
  if (reason === 'image_phash')
    return { label: 'Identical photos', hint: '', tone: 'sage' };
  if (reason === '(legacy)')
    return { label: 'Legacy (no reason)', hint: 'from an older engine version', tone: 'muted' };
  return { label: reason, hint: '', tone: 'muted' };
}

const TONE_DOT: Record<'sage' | 'brick' | 'copper' | 'muted', string> = {
  sage: 'bg-[var(--color-sage)]',
  brick: 'bg-[var(--color-brick)]',
  copper: 'bg-[var(--color-copper)]',
  muted: 'bg-[var(--color-ink-4)]',
};

/* The WHOLE pending queue + what it's made of — so the operator sees the real
 * backlog (not just the page of cards) and can drill into any bucket. Reads
 * /dedup/summary. Clicking a bucket filters "Needs review"; "All" clears it. */
function ReviewBacklog({
  summary,
  loading,
  selected,
  onSelect,
}: {
  summary: DedupSummaryResponse['data'] | undefined;
  loading: boolean;
  selected: Bucket | null;
  onSelect: (b: Bucket | null) => void;
}) {
  const buckets = summary?.buckets ?? [];
  const isSel = (b: DedupSummaryBucket) =>
    selected != null && selected.reason === b.reason && selected.verdict === b.verdict;
  return (
    <section className="mt-8">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Backlog
      </p>
      <h2 className="mt-1 text-xl" style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}>
        Review queue · {fmtCount(summary?.total ?? 0)}
      </h2>
      {summary == null ? (
        <div className="mt-3 px-6 py-8 text-center border border-dashed border-[var(--color-rule)] rounded-[var(--radius-md)] text-sm text-[var(--color-ink-3)]">
          {loading ? 'Loading…' : 'No pending candidates.'}
        </div>
      ) : (
        <div className="mt-3 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] overflow-hidden">
          <BacklogRow
            label="All pending"
            hint="clear the filter"
            count={summary.total}
            active={selected == null}
            tone="muted"
            onClick={() => onSelect(null)}
          />
          {buckets.map((b) => {
            const meta = bucketLabel(b.reason, b.verdict);
            return (
              <BacklogRow
                key={`${b.reason}:${b.verdict ?? ''}`}
                label={meta.label}
                hint={meta.hint}
                count={b.count}
                active={isSel(b)}
                tone={meta.tone}
                onClick={() => onSelect({ reason: b.reason, verdict: b.verdict })}
              />
            );
          })}
        </div>
      )}
    </section>
  );
}

function BacklogRow({
  label,
  hint,
  count,
  active,
  tone,
  onClick,
}: {
  label: string;
  hint: string;
  count: number;
  active: boolean;
  tone: 'sage' | 'brick' | 'copper' | 'muted';
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'w-full flex items-center justify-between gap-3 px-4 py-2.5 text-left',
        'border-b border-[var(--color-rule-soft)] last:border-b-0 transition-colors',
        active ? 'bg-[var(--color-paper-3)]' : 'hover:bg-[var(--color-paper)]',
      ].join(' ')}
    >
      <span className="flex items-center gap-2 min-w-0">
        <span className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${TONE_DOT[tone]}`} />
        <span className="text-sm text-[var(--color-ink)] truncate">{label}</span>
        {hint ? (
          <span className="text-[0.72rem] text-[var(--color-ink-4)] truncate hidden sm:inline">· {hint}</span>
        ) : null}
      </span>
      <span className={`font-mono tabular-nums text-sm shrink-0 ${active ? 'text-[var(--color-copper-2)]' : 'text-[var(--color-ink-2)]'}`}>
        {fmtCount(count)}
      </span>
    </button>
  );
}

function Header({ proposed }: { proposed: number }) {
  return (
    <header>
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Dedup
      </p>
      <h1
        className="mt-1.5 text-[2.1rem] leading-tight"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        Cross-source review
      </h1>
      <p className="mt-2 text-sm text-[var(--color-ink-2)] max-w-2xl">
        The dedup engine groups listings that share a street and disposition into
        one real-world property. An exact address (or near-identical photos)
        merges automatically; pairs it can’t confirm by photos wait here for your
        call. Every merge is reversible below.
        {proposed > 0 ? (
          <span className="text-[var(--color-ink)]"> {fmtCount(proposed)} awaiting review.</span>
        ) : null}
      </p>
    </header>
  );
}

/* Operator on/off switch for the engine's automatic merging (app_settings key
 * `dedup_auto_merge_enabled`). Off ⇒ the engine still finds candidates but
 * queues every one here for manual review instead of merging. */
const DEDUP_AUTO_KEY = 'dedup_auto_merge_enabled';

function AutoDedupToggle() {
  const qc = useQueryClient();
  const configured = isApiConfigured();
  const settingQ = useQuery({
    queryKey: ['app_setting', DEDUP_AUTO_KEY],
    queryFn: () => getAppSetting(DEDUP_AUTO_KEY),
    enabled: configured,
    staleTime: 30_000,
  });
  const mut = useMutation({
    mutationFn: (next: boolean) => updateAppSetting(DEDUP_AUTO_KEY, next),
    onSuccess: (row) =>
      qc.setQueryData(['app_setting', DEDUP_AUTO_KEY], row),
  });

  const raw = settingQ.data?.value;
  const enabled = raw === true || raw === 'true';
  const busy = settingQ.isLoading || mut.isPending;
  const disabled = !configured || busy;

  return (
    <div className="mt-5 flex items-center justify-between gap-4 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-3">
      <div className="min-w-0">
        <p className="text-sm font-medium text-[var(--color-ink)]">Auto-dedup</p>
        <p className="mt-0.5 text-[0.78rem] leading-snug text-[var(--color-ink-3)]">
          {!configured
            ? 'API not configured — toggle unavailable.'
            : enabled
              ? 'On — high-confidence matches (exact address, near-identical photos, High visual verdict) merge automatically.'
              : 'Off — the engine still finds candidates but queues all of them here for manual review (no auto-merge, no forensic vision spend).'}
          {mut.isError ? (
            <span className="text-[var(--color-brick)]"> · couldn’t save, try again.</span>
          ) : null}
        </p>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={enabled}
        aria-label="Toggle auto-dedup"
        disabled={disabled}
        onClick={() => mut.mutate(!enabled)}
        className={[
          'relative shrink-0 inline-flex items-center h-6 w-11 rounded-full transition-colors',
          enabled ? 'bg-[var(--color-copper)]' : 'bg-[var(--color-rule-strong)]',
          disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer',
        ].join(' ')}
      >
        <span
          className={[
            'inline-block h-5 w-5 rounded-full bg-white shadow transition-transform',
            enabled ? 'translate-x-[1.375rem]' : 'translate-x-0.5',
          ].join(' ')}
        />
      </button>
    </div>
  );
}

function Section({
  title,
  eyebrow,
  isEmpty,
  empty,
  children,
}: {
  title: string;
  eyebrow: string;
  isEmpty: boolean;
  empty: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-8">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        {eyebrow}
      </p>
      <h2 className="mt-1 text-xl" style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}>
        {title}
      </h2>
      <div className="mt-3">
        {isEmpty ? (
          <div className="px-6 py-10 text-center border border-dashed border-[var(--color-rule)] rounded-[var(--radius-md)] text-sm text-[var(--color-ink-3)]">
            {empty}
          </div>
        ) : (
          children
        )}
      </div>
    </section>
  );
}

function urlsFor(side: DedupPropertySide, imagesMap: ImagesMap): string[] {
  if (side.sreality_id == null) return [];
  return (imagesMap.get(side.sreality_id) ?? []).map(imageSrc);
}

/* One review card per CLUSTER — N member columns (not always two). Each member
 * has a checkbox: with NONE or ALL ticked, Merge folds the whole cluster; with
 * a SUBSET (≥2) ticked, only those merge and the rest stay in the queue. Photos
 * stay Browse-sized; the table scrolls horizontally for a large cluster. */
function ClusterCard({
  cluster,
  imagesMap,
  sourcesMap,
  detailMap,
  onMergeAll,
  onMergeSubset,
  onDismiss,
  busy,
}: {
  cluster: DedupCluster;
  imagesMap: ImagesMap;
  sourcesMap: SourcesMap;
  detailMap: DetailMap;
  onMergeAll: () => void;
  onMergeSubset: (propertyIds: number[]) => void;
  onDismiss: () => void;
  busy: boolean;
}) {
  const { members, tier } = cluster;
  const rows = diffCluster(members, (id) => (id != null ? detailMap.get(id) ?? null : null));
  const n = members.length;

  const [checked, setChecked] = useState<Set<number>>(new Set());
  const toggle = (pid: number) =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) next.delete(pid);
      else next.add(pid);
      return next;
    });
  // Partial selection = ≥2 ticked but not all → Merge acts on the subset only.
  const isSubset = checked.size >= 2 && checked.size < n;
  const mergeLabel = isSubset
    ? `Merge ${checked.size} selected`
    : n > 2
      ? `Merge ${n}`
      : 'Merge';
  const mergeDisabled = busy || checked.size === 1; // one ticked = nothing to merge
  const onMerge = () => (isSubset ? onMergeSubset([...checked]) : onMergeAll());

  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-4">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <div className="flex items-center gap-2 text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          <span>{tier}</span>
          {n > 2 ? (
            <span className="text-[var(--color-ink-4)]">· {n} listings</span>
          ) : null}
          {checked.size >= 2 ? (
            <span className="text-[var(--color-copper-2)]">· {checked.size} selected</span>
          ) : checked.size === 1 ? (
            <span className="text-[var(--color-ink-4)]">· tick ≥2 to merge a subset</span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onDismiss}
            disabled={busy}
            className={`${BTN} border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:text-[var(--color-ink)] hover:border-[var(--color-rule-strong)]`}
          >
            Dismiss
          </button>
          <button
            type="button"
            onClick={onMerge}
            disabled={mergeDisabled}
            className={`${BTN} bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)]`}
          >
            {busy ? 'Working…' : mergeLabel}
          </button>
        </div>
      </div>
      {/* Images and the comparison rows share ONE table + colgroup, so each
          member column lines up exactly under its photo. Fixed ~13rem member
          columns keep the photos Browse-sized; the table left-aligns and a big
          cluster scrolls horizontally rather than stretching. */}
      <div className="overflow-x-auto">
        <table className="border-collapse text-[0.8rem]" style={{ width: 'auto' }}>
          <colgroup>
            <col style={{ width: '6.5rem' }} />
            <col style={{ width: '1.75rem' }} />
            {members.map((m) => (
              <col key={m.property_id} style={{ width: '13rem' }} />
            ))}
          </colgroup>
          <tbody>
            <tr>
              <td />
              <td />
              {members.map((side) => (
                <td key={side.property_id} className="align-top px-1 pb-2">
                  <PropertyPanel
                    side={side}
                    urls={urlsFor(side, imagesMap)}
                    sources={sourcesMap.get(side.property_id) ?? []}
                    detailMap={detailMap}
                    checked={checked.has(side.property_id)}
                    onToggle={() => toggle(side.property_id)}
                  />
                </td>
              ))}
            </tr>
            {rows.map((r) => (
              <tr key={r.key} className="border-t border-[var(--color-rule-soft)]">
                <td className="py-1 pr-2 text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)] whitespace-nowrap align-middle">
                  {r.label}
                </td>
                <td className="py-1 text-center align-middle">
                  <span className="inline-flex"><Verdict v={r.verdict} /></span>
                </td>
                {r.values.map((v, i) => (
                  <td
                    key={members[i]?.property_id ?? i}
                    className="py-1 px-1 text-left tabular-nums text-[var(--color-ink)] align-middle"
                  >
                    {v}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {cluster.visual ? (
        <VisualVerdictNote
          verdict={cluster.visual.verdict}
          rationale={cluster.visual.rationale}
          room={cluster.visual.room}
        />
      ) : null}
    </div>
  );
}

/* The engine's room-aware forensic read, shown when it ran the visual layer but
 * didn't reach a confident-enough verdict to auto-merge — so the operator sees
 * WHY this pair is here, not just that it is. High never reaches the queue
 * (it auto-merges), so this surfaces the Medium/Low/inconclusive cases. */
function VisualVerdictNote({
  verdict,
  rationale,
  room,
}: {
  verdict: string;
  rationale: string | null;
  room: string | null;
}) {
  const tone =
    verdict === 'High'
      ? 'border-[var(--color-sage)]/60 text-[var(--color-sage)]'
      : verdict === 'Medium'
        ? 'border-[var(--color-copper)]/50 text-[var(--color-copper-2)]'
        : 'border-[var(--color-rule)] text-[var(--color-ink-3)]';
  return (
    <div className={`mt-3 rounded-[var(--radius-sm)] border ${tone} bg-[var(--color-paper)] p-2.5`}>
      <div className="flex items-center gap-2 text-[0.62rem] tracking-[0.12em] uppercase">
        <span className="font-semibold">Visual: {verdict}</span>
        {room ? <span className="text-[var(--color-ink-4)]">· {room.replace(/_/g, ' ')}</span> : null}
      </div>
      {rationale ? (
        <p className="mt-1 text-[0.78rem] leading-snug text-[var(--color-ink-2)]">{rationale}</p>
      ) : null}
    </div>
  );
}

function PropertyPanel({
  side,
  urls,
  sources,
  detailMap,
  checked,
  onToggle,
}: {
  side: DedupPropertySide;
  urls: string[];
  sources: PropertySource[];
  detailMap: DetailMap;
  checked: boolean;
  onToggle: () => void;
}) {
  const ring = checked ? 'border-[var(--color-copper)] ring-1 ring-[var(--color-copper)]' : 'border-[var(--color-rule-soft)]';
  return (
    <div className={`rounded-[var(--radius-sm)] border ${ring} bg-[var(--color-paper)] p-3`}>
      <ImageCarousel
        urls={urls}
        className="rounded-[var(--radius-xs)] border border-[var(--color-rule-soft)] mb-2"
      >
        {/* Top-right selection checkbox — tick ≥2 in a cluster to merge only
            those (the rest stay in the queue). */}
        <label
          className="absolute top-1.5 right-1.5 z-10 flex items-center justify-center w-6 h-6
            rounded-[var(--radius-xs)] bg-[var(--color-paper)]/90 border border-[var(--color-rule)]
            cursor-pointer hover:border-[var(--color-copper)]"
          title="Select for a partial merge"
        >
          <input
            type="checkbox"
            checked={checked}
            onChange={onToggle}
            className="accent-[var(--color-copper)] cursor-pointer"
          />
        </label>
      </ImageCarousel>
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono tabular-nums text-[var(--color-ink)]">
          {fmtCzk(side.price_czk)}
        </span>
        <span className="text-[0.7rem] text-[var(--color-ink-4)]">#{side.property_id}</span>
      </div>
      <div className="mt-1 text-sm text-[var(--color-ink-2)]">
        {side.disposition ?? '—'} · {fmtArea(side.area_m2)}
      </div>
      <div className="mt-0.5 text-[0.8rem] text-[var(--color-ink-3)] truncate">
        {side.district ?? '—'}
      </div>
      <PortalChips sources={sources} detailMap={detailMap} />
      {/* Secondary link to the listing's detail page in our own DB. */}
      {side.sreality_id != null ? (
        <Link
          to={listingPath(side.sreality_id)}
          className="mt-1.5 inline-block text-[0.7rem] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          view in database →
        </Link>
      ) : null}
    </div>
  );
}

/* The portals this side spans, one chip each — replaces the bare "N sites"
 * count. Chip links to the portal's own page (source_url) in a new tab when
 * known, else to our internal listing view. Active source = sage tint,
 * inactive = muted, mirroring the Browse CardBadge tones. */
function PortalChips({
  sources,
  detailMap,
}: {
  sources: PropertySource[];
  detailMap: DetailMap;
}) {
  // The panel's "view in database →" link covers the no-sources case, so here we
  // only render the portal chips when we actually have per-source rows.
  if (sources.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {sources.map((s) => (
        <PortalChip
          key={`${s.source}-${s.sreality_id}`}
          source={s}
          detail={detailMap.get(s.sreality_id) ?? null}
        />
      ))}
    </div>
  );
}

function PortalChip({
  source,
  detail,
}: {
  source: PropertySource;
  detail: ListingDetailLite | null;
}) {
  const tone = source.is_active
    ? 'bg-[var(--color-paper-3)]/90 border-[var(--color-sage)]/70 text-[var(--color-sage)]'
    : 'bg-[var(--color-paper)] border-[var(--color-rule)] text-[var(--color-ink-3)]';
  const cls = [
    'inline-flex items-center px-1.5 py-0.5 text-[0.62rem] tracking-[0.1em]',
    'uppercase rounded-[var(--radius-xs)] border font-medium whitespace-nowrap',
    'hover:border-[var(--color-rule-strong)] transition-colors',
    tone,
  ].join(' ');
  const label = portalShort(source.source);
  // sreality's scraper stores no source_url; rebuild the portal URL from the
  // native id (= sreality_id for sreality rows) plus the category triple from
  // the listing detail (a sreality /detail/ path 404s without the real slug).
  // Only fall back to the in-app view when we genuinely can't reach the portal.
  const external = portalListingUrl(
    source.source,
    source.source_url,
    source.source_id_native ?? source.sreality_id,
    detail
      ? {
          categoryType: detail.category_type,
          categoryMain: detail.category_main,
          categorySubCb: detail.category_sub_cb,
        }
      : undefined,
  );
  if (external) {
    return (
      <a href={external} target="_blank" rel="noopener noreferrer" className={cls}>
        {label} ↗
      </a>
    );
  }
  return (
    <Link to={listingPath(source.sreality_id)} className={cls}>
      {label}
    </Link>
  );
}

function Verdict({ v }: { v: DiffVerdict }) {
  const common = {
    width: 12,
    height: 12,
    viewBox: '0 0 12 12',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.6,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
  };
  if (v === 'match') {
    return (
      <svg {...common} className="text-[var(--color-sage)]">
        <path d="M2.5 6.5 L5 9 L9.5 3.5" />
      </svg>
    );
  }
  if (v === 'mismatch') {
    return (
      <svg {...common} className="text-[var(--color-brick)]">
        <path d="M3 3 L9 9 M9 3 L3 9" />
      </svg>
    );
  }
  return (
    <svg {...common} className="text-[var(--color-ink-4)]">
      <path d="M3 6 L9 6" />
    </svg>
  );
}

function MergeRow({
  merge,
  onUndo,
  busy,
}: {
  merge: MergeGroup;
  onUndo: () => void;
  busy: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-4 px-4 py-3 border-b border-[var(--color-rule-soft)] last:border-b-0">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <SourceBadge source={merge.source} />
          <Link
            to={propertyListingPath(merge.survivor_property_id)}
            className="text-sm text-[var(--color-ink)] hover:text-[var(--color-copper)]"
          >
            property #{merge.survivor_property_id}
          </Link>
          <span className="text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
            absorbed {fmtCount(merge.retired_count)}, moved {fmtCount(merge.listings_moved)} listing
            {merge.listings_moved === 1 ? '' : 's'}
          </span>
        </div>
        <div className="mt-0.5 text-[0.7rem] text-[var(--color-ink-4)]">
          {merge.reason} · {fmtRelative(merge.merged_at)}
        </div>
      </div>
      <button
        type="button"
        onClick={onUndo}
        disabled={busy}
        className={`${BTN} border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:text-[var(--color-ink)] hover:border-[var(--color-rule-strong)] shrink-0`}
      >
        {busy ? 'Undoing…' : 'Undo'}
      </button>
    </div>
  );
}

function SourceBadge({ source }: { source: 'auto' | 'operator' }) {
  const auto = source === 'auto';
  return (
    <span
      className={[
        'inline-block px-2 py-0.5 text-[0.6rem] tracking-[0.14em] uppercase rounded-[var(--radius-xs)] border',
        auto
          ? 'bg-[var(--color-copper-soft)] border-[var(--color-copper)] text-[var(--color-copper-2)]'
          : 'bg-[var(--color-paper)] border-[var(--color-rule)] text-[var(--color-ink-3)]',
      ].join(' ')}
    >
      {source}
    </span>
  );
}
