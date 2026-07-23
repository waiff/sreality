/* Operator-recorded manual rental estimates for one listing.
 *
 * Reads + writes go through the bearer-gated FastAPI service; the same
 * data is also exposed read-only via manual_rental_estimates_public
 * (migration 046) for the SPA's anon-key path, but for consistency with
 * the rest of the curation surface we hit the API for both directions.
 */

import { useState } from 'react';
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import {
  ApiError,
  createManualEstimate,
  deleteManualEstimate,
  listManualEstimates,
  updateManualEstimate,
} from '@/lib/api';
import { curationKeys } from '@/lib/queries';
import { fmtAbsolute, fmtCzk, fmtRelative } from '@/lib/format';
import {
  MANUAL_ESTIMATE_SOURCE_KINDS,
  manualEstimateSourceLabel,
  type ManualEstimateSourceKind,
  type ManualRentalEstimate,
} from '@/lib/types';

const RENT_MIN = 1000;
const RENT_MAX = 1_000_000;

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
      {children}
    </p>
  );
}

export default function ManualEstimatesBlock({
  sreality_id,
}: {
  sreality_id: number;
}) {
  const qc = useQueryClient();
  const [showForm, setShowForm] = useState(false);

  const listQ = useQuery({
    queryKey: curationKeys.manualEstimates(sreality_id),
    queryFn: () => listManualEstimates(sreality_id),
    staleTime: 30_000,
  });

  const invalidate = () =>
    qc.invalidateQueries({
      queryKey: curationKeys.manualEstimates(sreality_id),
    });

  const estimates = listQ.data?.data ?? [];

  return (
    <div>
      <div className="flex items-baseline justify-between gap-4">
        <SectionLabel>
          <span>Manual rental estimates</span>
          <span className="ml-2 font-mono tabular-nums text-[var(--color-ink-4)] tracking-normal">
            ({estimates.length})
          </span>
        </SectionLabel>
        <button
          type="button"
          onClick={() => setShowForm((v) => !v)}
          className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
        >
          {showForm ? 'Cancel' : '+ Add estimate'}
        </button>
      </div>

      {showForm && (
        <div className="mt-3">
          <EstimateForm
            sreality_id={sreality_id}
            onSaved={() => {
              setShowForm(false);
              invalidate();
            }}
          />
        </div>
      )}

      {listQ.isLoading ? (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">Loading…</p>
      ) : listQ.error ? (
        <p className="mt-3 text-sm text-[var(--color-brick)]">
          Failed to load: {(listQ.error as Error).message}
        </p>
      ) : estimates.length === 0 ? (
        !showForm && (
          <p className="mt-3 text-sm text-[var(--color-ink-3)]">
            No manual estimates yet.
          </p>
        )
      ) : (
        <ul className="mt-3 space-y-3">
          {estimates.map((e) => (
            <li key={e.id}>
              <EstimateRow
                sreality_id={sreality_id}
                estimate={e}
                onChange={invalidate}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function EstimateRow({
  sreality_id,
  estimate,
  onChange,
}: {
  sreality_id: number;
  estimate: ManualRentalEstimate;
  onChange: () => void;
}) {
  const [editing, setEditing] = useState(false);

  const del = useMutation({
    mutationFn: () => deleteManualEstimate(estimate.id),
    onSuccess: onChange,
  });

  if (editing) {
    return (
      <EstimateForm
        sreality_id={sreality_id}
        initial={estimate}
        onSaved={() => {
          setEditing(false);
          onChange();
        }}
        onCancel={() => setEditing(false)}
      />
    );
  }

  return (
    <div className="border-l-2 border-[var(--color-copper)]/40 pl-3">
      <div className="flex items-baseline justify-between gap-3">
        <div className="flex items-baseline gap-2 min-w-0">
          <span className="text-base font-medium text-[var(--color-ink)] tabular-nums">
            {fmtCzk(estimate.rent_czk)}
            <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] ml-1">
              / month
            </span>
          </span>
          <SourceChip kind={estimate.source_kind} />
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
          >
            Edit
          </button>
          <button
            type="button"
            onClick={() => {
              if (
                typeof window !== 'undefined' &&
                window.confirm('Delete this manual estimate?')
              ) {
                del.mutate();
              }
            }}
            disabled={del.isPending}
            className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-brick)] transition-colors disabled:opacity-50"
          >
            {del.isPending ? 'Deleting…' : 'Delete'}
          </button>
        </div>
      </div>
      <p className="mt-0.5 text-[0.78rem] text-[var(--color-ink-3)]">
        <span className="text-[var(--color-ink-2)]">{estimate.author}</span>
        <span className="text-[var(--color-ink-4)]"> · </span>
        <span
          className="cursor-help"
          title={fmtAbsolute(estimate.created_at)}
        >
          {fmtRelative(estimate.created_at)}
        </span>
        {estimate.updated_at !== estimate.created_at && (
          <>
            <span className="text-[var(--color-ink-4)]"> · </span>
            <span
              className="cursor-help text-[var(--color-ink-4)]"
              title={fmtAbsolute(estimate.updated_at)}
            >
              edited {fmtRelative(estimate.updated_at)}
            </span>
          </>
        )}
      </p>
      {estimate.notes && (
        <p className="mt-1.5 text-sm text-[var(--color-ink)] whitespace-pre-wrap break-words">
          {estimate.notes}
        </p>
      )}
    </div>
  );
}

function SourceChip({ kind }: { kind: ManualEstimateSourceKind }) {
  return (
    <span className="inline-flex items-center px-1.5 py-0.5 text-[0.65rem] tracking-[0.12em] uppercase rounded-[var(--radius-xs)] bg-[var(--color-paper-2)] border border-[var(--color-rule)] text-[var(--color-ink-3)]">
      {manualEstimateSourceLabel(kind)}
    </span>
  );
}

function EstimateForm({
  sreality_id,
  initial,
  onSaved,
  onCancel,
}: {
  sreality_id: number;
  initial?: ManualRentalEstimate;
  onSaved: () => void;
  onCancel?: () => void;
}) {
  const [rentCzk, setRentCzk] = useState<string>(
    initial ? String(initial.rent_czk) : '',
  );
  const [author, setAuthor] = useState<string>(initial?.author ?? '');
  const [sourceKind, setSourceKind] = useState<ManualEstimateSourceKind>(
    initial?.source_kind ?? 'broker',
  );
  const [notes, setNotes] = useState<string>(initial?.notes ?? '');
  const [error, setError] = useState<string | null>(null);

  const isEdit = initial != null;

  const save = useMutation({
    mutationFn: async () => {
      const rentNum = Number(rentCzk);
      if (!Number.isFinite(rentNum) || rentNum < RENT_MIN || rentNum > RENT_MAX) {
        throw new Error(`Rent must be between ${RENT_MIN} and ${RENT_MAX} Kč.`);
      }
      const trimmedAuthor = author.trim();
      if (trimmedAuthor.length === 0) {
        throw new Error('Author is required.');
      }
      const trimmedNotes = notes.trim();
      if (isEdit) {
        return updateManualEstimate(initial!.id, {
          rent_czk:    rentNum,
          author:      trimmedAuthor,
          source_kind: sourceKind,
          notes:       trimmedNotes.length === 0 ? null : trimmedNotes,
        });
      }
      return createManualEstimate(sreality_id, {
        rent_czk:    rentNum,
        author:      trimmedAuthor,
        source_kind: sourceKind,
        notes:       trimmedNotes.length === 0 ? null : trimmedNotes,
      });
    },
    onSuccess: () => {
      setError(null);
      onSaved();
    },
    onError: (err: ApiError | Error) =>
      setError(err.message || 'Failed to save'),
  });

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (save.isPending) return;
        save.mutate();
      }}
      className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-3 space-y-2"
    >
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 min-w-[8rem]">
          <span className="text-[0.65rem] tracking-[0.12em] uppercase text-[var(--color-ink-3)]">
            Rent (Kč/mo)
          </span>
          <input
            type="number"
            inputMode="numeric"
            min={RENT_MIN}
            max={RENT_MAX}
            step={100}
            value={rentCzk}
            onChange={(e) => setRentCzk(e.target.value)}
            required
            className="px-2.5 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-rule-strong)] tabular-nums"
          />
        </label>
        <label className="flex flex-col gap-1 min-w-[10rem] flex-1">
          <span className="text-[0.65rem] tracking-[0.12em] uppercase text-[var(--color-ink-3)]">
            Author
          </span>
          <input
            type="text"
            maxLength={120}
            value={author}
            onChange={(e) => setAuthor(e.target.value)}
            required
            placeholder="who recorded this"
            className="px-2.5 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
          />
        </label>
        <label className="flex flex-col gap-1 min-w-[10rem]">
          <span className="text-[0.65rem] tracking-[0.12em] uppercase text-[var(--color-ink-3)]">
            Source
          </span>
          <select
            value={sourceKind}
            onChange={(e) =>
              setSourceKind(e.target.value as ManualEstimateSourceKind)
            }
            className="px-2.5 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-rule-strong)]"
          >
            {MANUAL_ESTIMATE_SOURCE_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {manualEstimateSourceLabel(kind)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="flex flex-col gap-1">
        <span className="text-[0.65rem] tracking-[0.12em] uppercase text-[var(--color-ink-3)]">
          Notes (optional)
        </span>
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={2}
          maxLength={4000}
          placeholder="context, caveats, who you spoke to…"
          className="w-full px-2.5 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] resize-y"
        />
      </label>
      <div className="flex items-center justify-end gap-2">
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="px-3 py-1 text-[0.75rem] rounded-[var(--radius-sm)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] transition-colors"
          >
            Cancel
          </button>
        )}
        <button
          type="submit"
          disabled={save.isPending}
          className="px-3 py-1 text-[0.78rem] rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {save.isPending ? 'Saving…' : isEdit ? 'Save changes' : 'Save estimate'}
        </button>
      </div>
      {error && (
        <p className="text-[0.7rem] text-[var(--color-brick)]">{error}</p>
      )}
    </form>
  );
}
