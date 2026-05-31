import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';

import {
  dismissDedupCandidate,
  listDedupCandidates,
  listDedupMerges,
  mergeDedupCandidate,
  unmergeMergeGroup,
} from '@/lib/api';
import {
  dedupKeys,
  fetchImagesByListingIds,
  fetchListingDetailByIds,
  fetchPropertySourcesByPropertyIds,
} from '@/lib/queries';
import { diffCandidate, type DiffVerdict, type ListingDetailLite } from '@/lib/dedupDiff';
import { imageSrc } from '@/lib/imageUrl';
import { portalShort } from '@/lib/portals';
import { fmtArea, fmtCount, fmtCzk, fmtRelative } from '@/lib/format';
import ImageCarousel from '@/components/ImageCarousel';
import type {
  DedupCandidate,
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

  const candidates = candidatesQ.data?.data ?? [];
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
  const mergeMut = useMutation({ mutationFn: mergeDedupCandidate, onSuccess: invalidate });
  const dismissMut = useMutation({ mutationFn: dismissDedupCandidate, onSuccess: invalidate });
  const unmergeMut = useMutation({ mutationFn: unmergeMergeGroup, onSuccess: invalidate });

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto">
      <Header proposed={candidates.length} />

      <Section
        title="Needs review"
        eyebrow="Proposed cross-source matches"
        isEmpty={candidates.length === 0}
        empty={
          candidatesQ.isLoading
            ? 'Loading…'
            : candidatesQ.error
              ? `Failed to load: ${candidatesQ.error.message}`
              : 'Nothing awaiting review. Candidates appear as the dedup sweep finds cross-source pairs it can’t confidently auto-merge — which needs a second portal’s listings flowing in.'
        }
      >
        <div className="space-y-3">
          {candidates.map((c) => (
            <CandidateCard
              key={c.id}
              candidate={c}
              imagesMap={imagesMap}
              sourcesMap={sourcesMap}
              detailMap={detailMap}
              onMerge={() => mergeMut.mutate(c.id)}
              onDismiss={() => dismissMut.mutate(c.id)}
              busy={
                (mergeMut.isPending && mergeMut.variables === c.id)
                || (dismissMut.isPending && dismissMut.variables === c.id)
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
        The dedup sweep groups the same real-world property listed on multiple
        portals into one. High-confidence matches merge automatically (reversible
        below); ambiguous ones wait here for your call.
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

function CandidateCard({
  candidate,
  imagesMap,
  sourcesMap,
  detailMap,
  onMerge,
  onDismiss,
  busy,
}: {
  candidate: DedupCandidate;
  imagesMap: ImagesMap;
  sourcesMap: SourcesMap;
  detailMap: DetailMap;
  onMerge: () => void;
  onDismiss: () => void;
  busy: boolean;
}) {
  const L = candidate.left_property;
  const R = candidate.right_property;
  const leftDetail = L.sreality_id != null ? detailMap.get(L.sreality_id) ?? null : null;
  const rightDetail = R.sreality_id != null ? detailMap.get(R.sreality_id) ?? null : null;
  const rows = diffCandidate(candidate, leftDetail, rightDetail);

  const corroborator =
    typeof candidate.markers_matched?.corroborator === 'string'
      ? (candidate.markers_matched.corroborator as string)
      : null;

  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-4">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <div className="flex items-center gap-2 text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          <span>{candidate.tier}</span>
          {candidate.confidence != null ? (
            <span className="text-[var(--color-ink-4)]">· {(candidate.confidence * 100).toFixed(0)}% conf</span>
          ) : null}
          {corroborator ? (
            <span className="text-[var(--color-ink-4)]">· {corroborator}</span>
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
            {busy ? 'Working…' : 'Merge'}
          </button>
        </div>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <PropertyPanel
          side={L}
          urls={urlsFor(L, imagesMap)}
          sources={sourcesMap.get(L.property_id) ?? []}
        />
        <PropertyPanel
          side={R}
          urls={urlsFor(R, imagesMap)}
          sources={sourcesMap.get(R.property_id) ?? []}
        />
      </div>
      <DiffTable rows={rows} />
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
  if (source.source_url) {
    return (
      <a href={source.source_url} target="_blank" rel="noopener noreferrer" className={cls}>
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

function DiffTable({ rows }: { rows: DiffRow[] }) {
  return (
    <table className="w-full mt-3 border-collapse text-[0.8rem]">
      <tbody>
        {rows.map((r) => (
          <tr key={r.key} className="border-t border-[var(--color-rule-soft)]">
            <td className="py-1 pr-2 text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)] whitespace-nowrap align-middle">
              {r.label}
            </td>
            {r.single ? (
              <td colSpan={3} className="py-1 text-center text-[var(--color-ink-2)] tabular-nums">
                <span className="inline-flex items-center gap-1.5">
                  <Verdict v={r.verdict} />
                  {r.a}
                </span>
              </td>
            ) : (
              <>
                <td className="py-1 px-2 text-right tabular-nums text-[var(--color-ink)]">{r.a}</td>
                <td className="py-1 w-6 text-center align-middle">
                  <span className="inline-flex"><Verdict v={r.verdict} /></span>
                </td>
                <td className="py-1 px-2 text-left tabular-nums text-[var(--color-ink)]">{r.b}</td>
              </>
            )}
          </tr>
        ))}
      </tbody>
    </table>
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
