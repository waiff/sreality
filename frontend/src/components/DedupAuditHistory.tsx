import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { getDedupAudit, unmergeMergeGroup, type DedupAuditRow } from '@/lib/api';
import { FILTER_REGISTRY } from '@/lib/filterRegistry.generated';
import { fmtRelative } from '@/lib/format';
import { listingPath } from '@/lib/listingUrl';
import DedupFactors from '@/components/DedupFactors';

/* The unified decision ledger — every terminal dedup decision (merged / dismissed),
 * engine AND operator, with the evidence + inline undo. Replaces the old separate
 * "Recent merges" panel: a merge here carries its undo handle, so the operator
 * reverses a bad merge from the same place they read it. Filter by property type
 * (the Browse/Pipeline TYPE chips), outcome, and source. Civic-archive: copper =
 * merged, brick = dismissed. */

const OUTCOMES = [
  { id: '', label: 'Vše' },
  { id: 'merged', label: 'Sloučeno' },
  { id: 'dismissed', label: 'Zamítnuto' },
];

const SOURCES = [
  { id: '', label: 'Vše' },
  { id: 'engine', label: 'Engine' },
  { id: 'operator', label: 'Operátor' },
];

// Property-type chips from the SAME generated registry as Browse's TYPE tabs.
const CATEGORY_MAIN_ENUM =
  FILTER_REGISTRY.filters.find((f) => f.id === 'category_main')?.enum_values ?? [];
const TYPES = [
  { id: '', label: 'Vše' },
  ...CATEGORY_MAIN_ENUM.map((o) => ({ id: String(o.value), label: o.label_cs })),
];

const OUTCOME_STYLE: Record<string, string> = {
  merged:
    'text-[var(--color-copper)] border-[var(--color-copper)] bg-[var(--color-copper-soft)]',
  dismissed:
    'text-[var(--color-brick)] border-[var(--color-brick)] bg-[var(--color-brick-soft)]',
};
const OUTCOME_LABEL: Record<string, string> = {
  merged: 'sloučeno',
  dismissed: 'zamítnuto',
};

function Chip({
  on,
  label,
  onClick,
}: {
  on: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'px-2.5 py-1 rounded-[var(--radius-sm)] border text-[0.78rem] transition-colors',
        on
          ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
          : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)]',
      ].join(' ')}
    >
      {label}
    </button>
  );
}

export default function DedupAuditHistory() {
  const [outcome, setOutcome] = useState('');
  const [type, setType] = useState('');
  const [source, setSource] = useState('');
  const q = useQuery({
    queryKey: ['dedup', 'audit', outcome, type, source],
    queryFn: () =>
      getDedupAudit({
        outcome: outcome || undefined,
        category_main: type || undefined,
        source: source || undefined,
        limit: 150,
      }),
  });
  const rows = q.data?.data ?? [];

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          {OUTCOMES.map((o) => (
            <Chip
              key={o.id}
              on={outcome === o.id}
              label={o.label}
              onClick={() => setOutcome(o.id)}
            />
          ))}
          <span className="mx-1 h-4 w-px bg-[var(--color-rule)]" />
          {SOURCES.map((s) => (
            <Chip
              key={s.id}
              on={source === s.id}
              label={s.label}
              onClick={() => setSource(s.id)}
            />
          ))}
          {q.data && (
            <span className="ml-1 text-xs text-[var(--color-ink-4)] tabular-nums">
              {q.data.total} celkem
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {TYPES.map((t) => (
            <Chip
              key={t.id}
              on={type === t.id}
              label={t.label}
              onClick={() => setType(t.id)}
            />
          ))}
        </div>
      </div>

      {q.isLoading ? (
        <p className="text-sm text-[var(--color-ink-3)]">Načítám…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-[var(--color-ink-3)]">
          Zatím žádná rozhodnutí pro tento filtr. Engine zapisuje sloučení/zamítnutí
          při každém běhu; operátorská rozhodnutí se objeví hned.
        </p>
      ) : (
        <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] divide-y divide-[var(--color-rule)] bg-[var(--color-paper)]">
          {rows.map((r, i) => (
            <AuditRow key={`${r.merge_group_id ?? ''}-${i}`} r={r} />
          ))}
        </div>
      )}
    </div>
  );
}

function AuditRow({ r }: { r: DedupAuditRow }) {
  const qc = useQueryClient();
  const undo = useMutation({
    mutationFn: () => unmergeMergeGroup(r.merge_group_id as string),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['dedup'] }),
  });
  const canUndo =
    r.outcome === 'merged' && r.merge_group_id != null && !r.undone;

  return (
    <div className="flex flex-col gap-1.5 px-3 py-2.5">
      <div className="flex items-center gap-2 text-sm">
        <span
          className={[
            'shrink-0 inline-flex items-center px-1.5 py-0.5 rounded-[var(--radius-xs)] border text-[0.64rem] uppercase tracking-[0.08em]',
            OUTCOME_STYLE[r.outcome] ??
              'border-[var(--color-rule)] text-[var(--color-ink-3)]',
          ].join(' ')}
        >
          {OUTCOME_LABEL[r.outcome] ?? r.outcome}
        </span>
        {r.source && (
          <span className="shrink-0 text-[0.64rem] uppercase tracking-[0.08em] text-[var(--color-ink-4)]">
            {r.source === 'operator' ? 'operátor' : r.source}
          </span>
        )}
        <span className="shrink-0 text-[0.7rem] text-[var(--color-ink-4)] font-mono">
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
        </span>
        <span className="shrink-0 text-[0.72rem] text-[var(--color-ink-4)] tabular-nums">
          {fmtRelative(r.run_at)}
        </span>
        {r.undone ? (
          <span className="shrink-0 text-[0.7rem] text-[var(--color-ink-4)] italic">
            vráceno
          </span>
        ) : canUndo ? (
          <button
            type="button"
            onClick={() => undo.mutate()}
            disabled={undo.isPending}
            className="shrink-0 text-[0.72rem] text-[var(--color-ink-3)] hover:text-[var(--color-brick)] underline decoration-dotted underline-offset-2 disabled:opacity-50"
          >
            {undo.isPending ? 'Vracím…' : 'Vrátit'}
          </button>
        ) : null}
      </div>
      <DedupFactors
        factors={r.detail}
        leftSrealityId={r.left_sreality_id}
        rightSrealityId={r.right_sreality_id}
      />
    </div>
  );
}
