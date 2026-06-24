import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { getDedupAudit, type DedupAuditRow } from '@/lib/api';
import { fmtRelative } from '@/lib/format';
import { listingPath } from '@/lib/listingUrl';

/* The engine's decision ledger — what merged / dismissed / queued, with the
 * evidence. Civic-archive: copper = merged (the positive action), brick =
 * dismissed, neutral = queued, so the operator reads each decision at a glance. */

const OUTCOMES = [
  { id: '', label: 'All' },
  { id: 'merged', label: 'Merged' },
  { id: 'dismissed', label: 'Dismissed' },
  { id: 'queued', label: 'Queued' },
];

const OUTCOME_STYLE: Record<string, string> = {
  merged:
    'text-[var(--color-copper)] border-[var(--color-copper)] bg-[var(--color-copper-soft)]',
  dismissed:
    'text-[var(--color-brick)] border-[var(--color-brick)] bg-[var(--color-brick-soft)]',
  queued: 'text-[var(--color-ink-3)] border-[var(--color-rule)]',
};

export default function DedupAuditHistory() {
  const [outcome, setOutcome] = useState('');
  const q = useQuery({
    queryKey: ['dedup', 'audit', outcome],
    queryFn: () => getDedupAudit({ outcome: outcome || undefined, limit: 100 }),
  });
  const rows = q.data?.data ?? [];

  return (
    <div>
      <div className="flex items-center gap-1.5 mb-3">
        {OUTCOMES.map((o) => {
          const on = outcome === o.id;
          return (
            <button
              key={o.id}
              type="button"
              onClick={() => setOutcome(o.id)}
              className={[
                'px-2.5 py-1 rounded-[var(--radius-sm)] border text-[0.78rem] transition-colors',
                on
                  ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
                  : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)]',
              ].join(' ')}
            >
              {o.label}
            </button>
          );
        })}
        {q.data && (
          <span className="ml-1 text-xs text-[var(--color-ink-4)] tabular-nums">
            {q.data.total} total
          </span>
        )}
      </div>

      {q.isLoading ? (
        <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-[var(--color-ink-3)]">
          No engine decisions recorded yet. (The free engine logs pHash + address
          merges; richer rows appear once vision is on.)
        </p>
      ) : (
        <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] divide-y divide-[var(--color-rule)] bg-[var(--color-paper)]">
          {rows.map((r, i) => (
            <AuditRow key={i} r={r} />
          ))}
        </div>
      )}
    </div>
  );
}

function AuditRow({ r }: { r: DedupAuditRow }) {
  const d = r.detail ?? {};
  const bits = ['reason', 'verdict', 'room_type']
    .map((k) => d[k])
    .filter(Boolean)
    .map(String)
    .join(' · ');
  return (
    <div className="flex items-center gap-3 px-3 py-2 text-sm">
      <span
        className={[
          'shrink-0 inline-flex items-center px-1.5 py-0.5 rounded-[var(--radius-xs)] border text-[0.66rem] uppercase tracking-[0.08em]',
          OUTCOME_STYLE[r.outcome] ??
            'border-[var(--color-rule)] text-[var(--color-ink-3)]',
        ].join(' ')}
      >
        {r.outcome}
      </span>
      <span className="shrink-0 w-16 text-[0.72rem] text-[var(--color-ink-4)] font-mono">
        {r.stage}
      </span>
      <span className="min-w-0 flex-1 truncate font-mono text-[0.8rem] text-[var(--color-ink-2)]">
        {r.left_sreality_id != null ? (
          <Link
            to={listingPath(r.left_sreality_id)}
            className="hover:text-[var(--color-copper)]"
          >
            {r.left_sreality_id}
          </Link>
        ) : (
          '—'
        )}
        {' ↔ '}
        {r.right_sreality_id != null ? (
          <Link
            to={listingPath(r.right_sreality_id)}
            className="hover:text-[var(--color-copper)]"
          >
            {r.right_sreality_id}
          </Link>
        ) : (
          '—'
        )}
        {bits && (
          <span className="ml-2 text-[0.74rem] text-[var(--color-ink-3)]">
            · {bits}
          </span>
        )}
      </span>
      <span className="shrink-0 text-[0.72rem] text-[var(--color-ink-4)] tabular-nums">
        {fmtRelative(r.run_at)}
      </span>
    </div>
  );
}
