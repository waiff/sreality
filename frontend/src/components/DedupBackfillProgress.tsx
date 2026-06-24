import { useQuery } from '@tanstack/react-query';

import { getDedupClipCoverage, type DedupCoverageTier } from '@/lib/api';
import { fmtCount } from '@/lib/format';

/* Free-CLIP backfill tracker. Listing-grain progress per priority tier (the
 * backfill tags these in order), so the operator watches Středočeský domy &
 * komerční — the flip gate — fill first. Civic-archive: copper fill on an inset
 * track, mono tabular counts, polled live. */
export default function DedupBackfillProgress() {
  const q = useQuery({
    queryKey: ['dedup', 'clip-coverage'],
    queryFn: getDedupClipCoverage,
    refetchInterval: 60_000,
  });
  const d = q.data?.data;

  if (q.isLoading)
    return <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>;
  if (!d)
    return (
      <p className="text-sm text-[var(--color-ink-3)]">Coverage unavailable.</p>
    );

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper)] p-4">
      <div className="flex items-baseline justify-between gap-4 mb-3">
        <span className="text-[0.78rem] text-[var(--color-ink-3)] leading-snug">
          Free CLIP tagging — prioritised: dedup candidates → Středočeský domy &amp;
          komerční → byty → the rest of ČR.
        </span>
        <span className="shrink-0 font-mono tabular-nums text-[0.72rem] text-[var(--color-ink-4)]">
          {fmtCount(d.total_tags)} tags · {fmtCount(d.total_embeddings)} embeddings
        </span>
      </div>
      <div className="space-y-2.5">
        {d.tiers.map((t) => (
          <TierBar key={t.key} t={t} />
        ))}
      </div>
    </div>
  );
}

function TierBar({ t }: { t: DedupCoverageTier }) {
  const pct = t.total ? Math.round((100 * t.tagged) / t.total) : 0;
  const done = pct >= 100;
  return (
    <div>
      <div className="flex items-baseline justify-between text-sm mb-1">
        <span className="text-[var(--color-ink-2)]">
          {t.label}
          {done && <span className="ml-1 text-[var(--color-copper)]">✓</span>}
        </span>
        <span className="font-mono tabular-nums text-[0.76rem] text-[var(--color-ink-3)]">
          {fmtCount(t.tagged)} / {fmtCount(t.total)} · {pct}%
        </span>
      </div>
      <div className="h-1.5 rounded-full bg-[var(--color-inset)] overflow-hidden">
        <div
          className="h-full rounded-full bg-[var(--color-copper)] transition-[width] duration-500"
          style={{ width: `${Math.min(100, pct)}%` }}
        />
      </div>
    </div>
  );
}
