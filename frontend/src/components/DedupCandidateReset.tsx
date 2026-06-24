import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import { archiveResetDedupCandidates } from '@/lib/api';

const BTN =
  'px-3 py-1.5 text-sm rounded-[var(--radius-sm)] transition-colors disabled:opacity-50';

/* Archive + reset the candidate queue ("disregard candidates, keep a backup, redo
 * all"). Rare + destructive, so it sits quietly with the app's two-step confirm
 * and a brick accent — not a prominent button. */
export default function DedupCandidateReset() {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const mut = useMutation({
    mutationFn: archiveResetDedupCandidates,
    onSuccess: (r) => {
      setDone(`Archived ${r.archived} · cleared ${r.deleted} · batch ${r.batch}`);
      setConfirming(false);
      qc.invalidateQueries({ queryKey: ['dedup'] });
    },
  });

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-3.5 flex items-start justify-between gap-4">
      <div className="min-w-0">
        <div className="text-sm text-[var(--color-ink-2)]">
          Archive &amp; reset the candidate queue
        </div>
        <p className="text-[0.76rem] text-[var(--color-ink-3)] mt-0.5 leading-snug">
          Snapshots every proposed candidate to a backup table, then clears the
          queue so the engine regenerates fresh. Merges &amp; dismissals are
          untouched. Use when re-running with new settings (e.g. the CLIP flip).
        </p>
        {done && (
          <p className="text-[0.76rem] text-[var(--color-copper)] mt-1 font-mono tabular-nums">
            {done}
          </p>
        )}
      </div>
      <div className="shrink-0">
        {!confirming ? (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className={`${BTN} border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-brick)] hover:border-[var(--color-brick)]`}
          >
            Archive &amp; reset…
          </button>
        ) : (
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => mut.mutate()}
              disabled={mut.isPending}
              className={`${BTN} border border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[var(--color-brick)]`}
            >
              {mut.isPending ? 'Working…' : 'Confirm reset'}
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className={`${BTN} text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]`}
            >
              Cancel
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
