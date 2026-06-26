import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { getDedupAudit, unmergeMergeGroup, type DedupAuditRow } from '@/lib/api';
import { FILTER_REGISTRY } from '@/lib/filterRegistry.generated';
import { fmtRelative } from '@/lib/format';
import { listingPath } from '@/lib/listingUrl';
import DedupFactors from '@/components/DedupFactors';
import DecisionFeedbackControl from '@/components/DecisionFeedbackControl';

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

// The decision-factor (signal) a decision turned on. phash/cosine carry a numeric
// threshold (review the borderline tail); visual carries a verdict; address has neither.
const FACTORS = [
  { id: '', label: 'Vše' },
  { id: 'phash', label: 'pHash' },
  { id: 'cosine', label: 'Cosine' },
  { id: 'visual', label: 'Vize' },
  { id: 'address', label: 'Adresa' },
];
const VERDICTS = [
  { id: '', label: 'Vše' },
  { id: 'High', label: 'High' },
  { id: 'Medium', label: 'Medium' },
  { id: 'Low', label: 'Low' },
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

export default function DedupAuditHistory({
  scopeProperty,
}: {
  // When set, restrict the feed to the decisions touching ONE property's child
  // listings — the listing-detail "merge decisions" deep link (?audit_property=).
  scopeProperty?: number | null;
} = {}) {
  // Scoped to one property → default to the merges that BUILT it; the operator
  // can broaden to dismissed/all from the chips. Unscoped → the full ledger.
  const [outcome, setOutcome] = useState(scopeProperty != null ? 'merged' : '');
  const [type, setType] = useState('');
  const [source, setSource] = useState('');
  const [onlyFlagged, setOnlyFlagged] = useState(false);
  const [factor, setFactor] = useState('');
  const [verdict, setVerdict] = useState('');
  const [maxText, setMaxText] = useState(''); // raw threshold buffer (un-committed)
  const [factorMax, setFactorMax] = useState<number | null>(null); // committed
  // Broadcast "expand/collapse all photos" to every row's DedupFactors; bump seq each
  // click so a row re-applies it even after individual toggling.
  const [batchPhotos, setBatchPhotos] =
    useState<{ open: boolean; seq: number } | undefined>(undefined);
  const setAllPhotos = (open: boolean) =>
    setBatchPhotos((b) => ({ open, seq: (b?.seq ?? 0) + 1 }));

  // Switching the factor resets its threshold/verdict so stale bounds don't carry over.
  const pickFactor = (f: string) => {
    setFactor(f);
    setMaxText('');
    setFactorMax(null);
    setVerdict('');
  };
  const commitMax = () => {
    const t = maxText.trim().replace(',', '.');
    const v = t === '' ? NaN : parseFloat(t);
    setFactorMax(Number.isFinite(v) ? v : null);
  };

  const numericFactor = factor === 'phash' || factor === 'cosine';
  const q = useQuery({
    queryKey: [
      'dedup', 'audit', outcome, type, source, onlyFlagged, factor, factorMax, verdict,
      scopeProperty ?? null,
    ],
    queryFn: () =>
      getDedupAudit({
        outcome: outcome || undefined,
        category_main: type || undefined,
        source: source || undefined,
        flagged: onlyFlagged || undefined,
        factor: factor || undefined,
        factor_max: numericFactor && factorMax != null ? factorMax : undefined,
        verdict: factor === 'visual' && verdict ? verdict : undefined,
        property_id: scopeProperty ?? undefined,
        limit: 150,
      }),
  });
  const rows = q.data?.data ?? [];

  return (
    <div className="flex flex-col gap-3">
      {scopeProperty != null && (
        <div className="flex flex-wrap items-center gap-2 rounded-[var(--radius-sm)] border border-[var(--color-copper)]/30 bg-[var(--color-copper-soft)] px-3 py-2 text-[0.8rem] text-[var(--color-copper)]">
          <span>
            Rozhodnutí pro nemovitost{' '}
            <span className="font-mono tabular-nums">#{scopeProperty}</span>
          </span>
          <Link
            to="/dedup#history"
            className="ml-auto text-[var(--color-ink-3)] hover:text-[var(--color-copper)] underline decoration-dotted underline-offset-2"
          >
            zobrazit vše
          </Link>
        </div>
      )}
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
          <span className="mx-1 h-4 w-px bg-[var(--color-rule)]" />
          <button
            type="button"
            onClick={() => setOnlyFlagged((v) => !v)}
            title="Jen rozhodnutí, která jsi označil/a jako nesprávná"
            className={[
              'inline-flex items-center gap-1 px-2.5 py-1 rounded-[var(--radius-sm)] border text-[0.78rem] transition-colors',
              onlyFlagged
                ? 'border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[var(--color-brick)]'
                : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-brick)] hover:border-[var(--color-brick)]',
            ].join(' ')}
          >
            <span aria-hidden>⚑</span>
            Jen nesprávná
          </button>
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
          {rows.length > 0 && (
            <span className="ml-auto flex items-center gap-2 text-[0.72rem]">
              <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)]">
                Fotky
              </span>
              <button
                type="button"
                onClick={() => setAllPhotos(true)}
                className="text-[var(--color-ink-3)] hover:text-[var(--color-copper)] underline decoration-dotted underline-offset-2"
              >
                zobrazit vše
              </button>
              <button
                type="button"
                onClick={() => setAllPhotos(false)}
                className="text-[var(--color-ink-3)] hover:text-[var(--color-copper)] underline decoration-dotted underline-offset-2"
              >
                skrýt vše
              </button>
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)] mr-1">
            Faktor
          </span>
          {FACTORS.map((f) => (
            <Chip
              key={f.id}
              on={factor === f.id}
              label={f.label}
              onClick={() => pickFactor(f.id)}
            />
          ))}
          {numericFactor && (
            <span className="inline-flex items-center gap-1 text-[0.78rem] text-[var(--color-ink-3)]">
              ≤
              <input
                value={maxText}
                onChange={(e) => setMaxText(e.target.value)}
                onBlur={commitMax}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commitMax();
                }}
                inputMode="decimal"
                placeholder={factor === 'cosine' ? '0,95' : 'počet'}
                aria-label="Horní práh faktoru"
                className="w-16 px-1.5 py-0.5 text-[0.78rem] font-mono tabular-nums rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
              />
              <span className="text-[0.66rem] text-[var(--color-ink-4)]">
                {factor === 'cosine' ? 'cosine' : 'shod fotek'}
              </span>
            </span>
          )}
          {factor === 'visual' &&
            VERDICTS.map((v) => (
              <Chip
                key={v.id}
                on={verdict === v.id}
                label={v.label}
                onClick={() => setVerdict(v.id)}
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
          {rows.map((r) => (
            <AuditRow key={r.audit_id} r={r} batchPhotos={batchPhotos} />
          ))}
        </div>
      )}
    </div>
  );
}

function AuditRow({
  r,
  batchPhotos,
}: {
  r: DedupAuditRow;
  batchPhotos?: { open: boolean; seq: number };
}) {
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
        breakdown={r.audit_breakdown}
        leftSrealityId={r.left_sreality_id}
        rightSrealityId={r.right_sreality_id}
        categoryMain={r.category_main}
        batchPhotos={batchPhotos}
      />
      <DecisionFeedbackControl
        leftPropertyId={r.left_property_id}
        rightPropertyId={r.right_property_id}
        categoryMain={r.category_main}
        feedback={r.feedback}
      />
    </div>
  );
}
