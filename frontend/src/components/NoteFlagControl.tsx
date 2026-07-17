import { useEffect, useState } from 'react';

/* The generic "flag this + leave a note" control — the same button/textarea/save/remove
 * shell as DecisionFeedbackControl (the merge-decision review flag), reused by the CLIP
 * tag/render-score annotations and the pHash pair notes so all three surfaces read alike
 * without three near-identical components. The caller owns the mutation (each subject —
 * property pair / image / image pair — has its own store), this component owns only the
 * open/draft/save-remove interaction. Civic-archive: brick = flagged. */

export default function NoteFlagControl({
  flagged,
  note,
  flagLabel,
  flaggedLabel,
  notePlaceholder,
  busy = false,
  onSave,
  onRemove,
}: {
  flagged: boolean;
  note: string | null | undefined;
  flagLabel: string;
  flaggedLabel: string;
  notePlaceholder: string;
  busy?: boolean;
  onSave: (input: { flagged: boolean; note: string | null }) => void;
  onRemove?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [draftNote, setDraftNote] = useState(note ?? '');

  useEffect(() => {
    setDraftNote(note ?? '');
  }, [note]);

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          title={flagged ? 'Upravit poznámku' : flagLabel}
          className={[
            'inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[var(--radius-xs)] border text-[0.68rem] transition-colors',
            flagged
              ? 'border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[var(--color-brick)]'
              : 'border-[var(--color-rule)] text-[var(--color-ink-4)] hover:text-[var(--color-brick)] hover:border-[var(--color-brick)]',
          ].join(' ')}
        >
          <span aria-hidden>⚑</span>
          {flagged ? flaggedLabel : flagLabel}
        </button>
        {flagged && note && !open && (
          <span className="min-w-0 flex-1 truncate text-[0.7rem] text-[var(--color-ink-3)] italic">
            „{note}“
          </span>
        )}
      </div>

      {open && (
        <div className="flex flex-col gap-2 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/40 bg-[var(--color-brick-soft)]/40 px-2.5 py-2">
          <textarea
            value={draftNote}
            onChange={(e) => setDraftNote(e.target.value)}
            disabled={busy}
            rows={2}
            placeholder={notePlaceholder}
            className="w-full resize-y rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.74rem] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-brick)]"
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => {
                onSave({ flagged: true, note: draftNote.trim() || null });
                setOpen(false);
              }}
              disabled={busy}
              className="px-2 py-0.5 rounded-[var(--radius-xs)] border border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[0.72rem] text-[var(--color-brick)] hover:bg-[var(--color-brick)] hover:text-[var(--color-paper)] transition-colors disabled:opacity-50"
            >
              {busy ? 'Ukládám…' : flagged ? 'Uložit změny' : 'Označit'}
            </button>
            {flagged && onRemove && (
              <button
                type="button"
                onClick={() => {
                  onRemove();
                  setOpen(false);
                }}
                disabled={busy}
                className="text-[0.72rem] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)] underline decoration-dotted underline-offset-2 disabled:opacity-50"
              >
                Odebrat označení
              </button>
            )}
            <button
              type="button"
              onClick={() => setOpen(false)}
              disabled={busy}
              className="ml-auto text-[0.72rem] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)]"
            >
              Zavřít
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
