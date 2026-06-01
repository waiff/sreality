import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';

import {
  dismissDedupCluster,
  listDedupCandidates,
  listDedupMerges,
  mergeDedupCluster,
  unmergeMergeGroup,
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
import type {
  DedupCandidatesResponse,
  DedupPropertySide,
  ImagePublic,
  MergeGroup,
  MergesResponse,
  PropertySource,
} from '@/lib/types';

const POLL_MS = 60_000;
const BTN = 'px-3 py-1.5 text-sm rounded-[var(--radius-sm)] transition-colors disabled:opacity-50';

type ImagesMap = Map<number, ImagePublic[]>;
type SourcesMap = Map<number, PropertySource[]>;
type DetailMap = Map<number, ListingDetailLite>;

export default function Dedup() {
  const qc = useQueryClient();

  const candidatesQ = useQuery<DedupCandidatesResponse, Error>({
    queryKey: dedupKeys.candidates({ status: 'proposed' }),
    queryFn: () => listDedupCandidates({ status: 'proposed', limit: 100 }),
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
  const dismissMut = useMutation({ mutationFn: dismissDedupCluster, onSuccess: invalidate });
  const unmergeMut = useMutation({ mutationFn: unmergeMergeGroup, onSuccess: invalidate });

  /* A cluster is "busy" while either mutation is running for its exact id set. */
  const sameIds = (a: number[] | undefined, b: number[]) =>
    a != null && a.length === b.length && a.every((v, i) => v === b[i]);

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto">
      <Header proposed={candidates.length} />

      <AutomationDashboard runs={engineRunsQ.data ?? []} loading={engineRunsQ.isLoading} />

      <Section
        title="Needs review"
        eyebrow="Proposed matches"
        isEmpty={candidates.length === 0}
        empty={
          candidatesQ.isLoading
            ? 'Loading…'
            : candidatesQ.error
              ? `Failed to load: ${candidatesQ.error.message}`
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
              onMerge={() => mergeMut.mutate(cl.candidateIds)}
              onDismiss={() => dismissMut.mutate(cl.candidateIds)}
              busy={
                (mergeMut.isPending && sameIds(mergeMut.variables, cl.candidateIds))
                || (dismissMut.isPending && sameIds(dismissMut.variables, cl.candidateIds))
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
          <div className="mt-2 grid grid-cols-2 sm:grid-cols-4 gap-2">
            <Stat label="By address" value={latest.auto_address} small />
            <Stat label="By photos" value={latest.auto_phash} small />
            <Stat label="By visual" value={latest.auto_visual} small />
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

/* One review card per CLUSTER — N member columns (not always two), a column
 * each. The grid caps at 3 columns so photos stay Browse-card-sized; a 4th+
 * member wraps to the next row. Merge/Dismiss act on the whole cluster. */
function ClusterCard({
  cluster,
  imagesMap,
  sourcesMap,
  detailMap,
  onMerge,
  onDismiss,
  busy,
}: {
  cluster: DedupCluster;
  imagesMap: ImagesMap;
  sourcesMap: SourcesMap;
  detailMap: DetailMap;
  onMerge: () => void;
  onDismiss: () => void;
  busy: boolean;
}) {
  const { members, tier } = cluster;
  const rows = diffCluster(members, (id) => (id != null ? detailMap.get(id) ?? null : null));
  const n = members.length;
  const mergeLabel = n > 2 ? `Merge ${n}` : 'Merge';

  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-4">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <div className="flex items-center gap-2 text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          <span>{tier}</span>
          {n > 2 ? (
            <span className="text-[var(--color-ink-4)]">· {n} listings</span>
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
            disabled={busy}
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
}: {
  side: DedupPropertySide;
  urls: string[];
  sources: PropertySource[];
}) {
  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper)] p-3">
      <ImageCarousel
        urls={urls}
        className="rounded-[var(--radius-xs)] border border-[var(--color-rule-soft)] mb-2"
      />
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
      <PortalChips sources={sources} fallbackId={side.sreality_id} />
    </div>
  );
}

/* The portals this side spans, one chip each — replaces the bare "N sites"
 * count. Chip links to the portal's own page (source_url) in a new tab when
 * known, else to our internal listing view. Active source = sage tint,
 * inactive = muted, mirroring the Browse CardBadge tones. */
function PortalChips({
  sources,
  fallbackId,
}: {
  sources: PropertySource[];
  fallbackId: number | null;
}) {
  if (sources.length === 0) {
    if (fallbackId == null) return null;
    return (
      <div className="mt-2">
        <Link
          to={`/listing/${fallbackId}`}
          className="inline-block text-[0.75rem] text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          open listing →
        </Link>
      </div>
    );
  }
  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {sources.map((s) => (
        <PortalChip key={`${s.source}-${s.sreality_id}`} source={s} />
      ))}
    </div>
  );
}

function PortalChip({ source }: { source: PropertySource }) {
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
  // native id (= sreality_id for sreality rows). Only fall back to the in-app
  // view when we genuinely can't reach the origin portal.
  const external = portalListingUrl(
    source.source,
    source.source_url,
    source.source_id_native ?? source.sreality_id,
  );
  if (external) {
    return (
      <a href={external} target="_blank" rel="noopener noreferrer" className={cls}>
        {label} ↗
      </a>
    );
  }
  return (
    <Link to={`/listing/${source.sreality_id}`} className={cls}>
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
            to={`/listing?property=${merge.survivor_property_id}`}
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
