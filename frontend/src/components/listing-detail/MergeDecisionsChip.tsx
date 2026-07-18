/* When a property groups several portal observations, it was built by the
 * dedup engine. This chip links to the Decision history scoped to exactly the
 * merges that created THIS property — the evidence + inline undo. It renders
 * nothing for singletons or properties with no recorded merge (including when
 * the audit read 403s for a non-admin session — /dedup/audit is admin-gated,
 * see api/routes/dedup.py — the query just resolves with no data).
 *
 * Shared by ListingDetail (a chip in the header extras row) and PropertyDetail
 * (the same evidence, viewed from the property itself) — one query, one link,
 * one place to change the wording. */
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { getDedupAudit } from '@/lib/api';

export function MergeDecisionsChip({
  propertyId,
  multiSource,
}: {
  propertyId: number | null;
  multiSource: boolean;
}) {
  const q = useQuery({
    queryKey: ['merge-decisions-count', propertyId],
    queryFn: () =>
      getDedupAudit({ property_id: propertyId as number, outcome: 'merged', limit: 1 }),
    enabled: propertyId != null && multiSource,
    staleTime: 60_000,
  });
  const n = q.data?.total ?? 0;
  if (propertyId == null || !multiSource || n === 0) return null;
  return (
    <Link
      to={`/dedup?audit_property=${propertyId}#history`}
      title="Zobrazit rozhodnutí o sloučení (dedup)"
      className="inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-3)] px-3 py-1.5 text-[0.8rem] text-[var(--color-ink-2)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper-2)] transition-colors"
    >
      <span className="text-[var(--color-ink-3)]">Sloučení:</span>
      <span className="tabular-nums">{n} rozhodnutí</span>
      <OutArrow />
    </Link>
  );
}

function OutArrow() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
      <line
        x1="1"
        y1="9"
        x2="8.5"
        y2="1.5"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeLinecap="round"
      />
      <polyline
        points="3.5,1.5 8.5,1.5 8.5,6.5"
        stroke="currentColor"
        strokeWidth="1.25"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
