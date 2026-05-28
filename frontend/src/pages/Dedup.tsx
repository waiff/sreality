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
import { dedupKeys } from '@/lib/queries';
import { fmtArea, fmtCount, fmtCzk, fmtRelative } from '@/lib/format';
import type {
  DedupCandidate,
  DedupCandidatesResponse,
  DedupPropertySide,
  MergeGroup,
  MergesResponse,
} from '@/lib/types';

const POLL_MS = 60_000;
const BTN = 'px-3 py-1.5 text-sm rounded-[var(--radius-sm)] transition-colors disabled:opacity-50';

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

  const invalidate = () => qc.invalidateQueries({ queryKey: dedupKeys.all });
  const mergeMut = useMutation({ mutationFn: mergeDedupCandidate, onSuccess: invalidate });
  const dismissMut = useMutation({ mutationFn: dismissDedupCandidate, onSuccess: invalidate });
  const unmergeMut = useMutation({ mutationFn: unmergeMergeGroup, onSuccess: invalidate });

  const candidates = candidatesQ.data?.data ?? [];
  const merges = mergesQ.data?.data ?? [];
  const activeMerges = useMemo(() => merges.filter((m) => !m.fully_undone), [merges]);

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

function CandidateCard({
  candidate,
  onMerge,
  onDismiss,
  busy,
}: {
  candidate: DedupCandidate;
  onMerge: () => void;
  onDismiss: () => void;
  busy: boolean;
}) {
  const m = candidate.markers_matched ?? {};
  const distance = typeof m.distance_m === 'number' ? m.distance_m : null;
  const corroborator = typeof m.corroborator === 'string' ? m.corroborator : null;
  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-4">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <div className="flex items-center gap-2 text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          <span>{candidate.tier}</span>
          {candidate.confidence != null ? (
            <span className="text-[var(--color-ink-4)]">· {(candidate.confidence * 100).toFixed(0)}% conf</span>
          ) : null}
          {distance != null ? (
            <span className="text-[var(--color-ink-4)]">· {distance.toFixed(0)} m apart</span>
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
        <PropertyPanel side={candidate.left_property} />
        <PropertyPanel side={candidate.right_property} />
      </div>
    </div>
  );
}

function PropertyPanel({ side }: { side: DedupPropertySide }) {
  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper)] p-3">
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
      <div className="mt-1 text-[0.7rem] text-[var(--color-ink-4)] tabular-nums">
        {fmtCount(side.distinct_site_count)} site{side.distinct_site_count === 1 ? '' : 's'}
      </div>
      {side.sreality_id != null ? (
        <Link
          to={`/listing/${side.sreality_id}`}
          className="mt-1 inline-block text-[0.75rem] text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          open listing →
        </Link>
      ) : null}
    </div>
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
