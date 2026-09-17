/* The dismiss control — "I reviewed this property and never want to see it
 * again" (migration 536) — on every surface a property is triaged from: beside
 * the pipeline funnel on the Browse card and Table row, and in the listing
 * header. One click hides (no confirm: nothing is destroyed, and the toast
 * offers Vrátit); on a dismissed property, shown under Browse's reveal or on
 * its own page, one click restores it.
 *
 * Absent while the property is in the caller's pipeline: the two are mutually
 * exclusive (the API refuses the dismissal), and the funnel already says the
 * operator is pursuing it. The members read is the one every funnel shares.
 */

import { useQuery } from '@tanstack/react-query';

import { EyeOffIcon } from '@/components/icons';
import { fetchPipelineMembers, pipelineKeys } from '@/lib/queries';
import { useDismissal } from '@/lib/useDismissal';

export interface DismissButtonProps {
  property_id: number;
  /* `overlay` floats over a card photo, `inline` sits on a table row, `header`
   * is the labelled page-level verb. */
  variant?: 'overlay' | 'inline' | 'header';
}

export default function DismissButton({ property_id, variant = 'overlay' }: DismissButtonProps) {
  const membersQ = useQuery({
    queryKey: pipelineKeys.members,
    queryFn: fetchPipelineMembers,
    staleTime: 30_000,
  });
  const { dismissed, dismiss, restore, pending } = useDismissal(property_id);

  if (membersQ.data?.has(property_id)) return null;

  const on = dismissed === true;
  const label = on ? 'Skryto — znovu zobrazit' : 'Skrýt nemovitost';
  const onClick = () => {
    if (pending || dismissed == null) return;
    if (on) restore.mutate();
    else dismiss.mutate();
  };

  if (variant === 'header') {
    return (
      <button
        type="button"
        onClick={onClick}
        disabled={pending || dismissed == null}
        aria-pressed={on}
        title={label}
        className={[
          'inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border px-3 py-1.5 text-[0.8rem] transition-colors disabled:opacity-60',
          on
            ? 'border-[var(--color-ink-3)] bg-[var(--color-paper-3)] text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)]'
            : 'border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-3)] hover:border-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
        ].join(' ')}
      >
        <EyeOffIcon filled={on} className="h-4 w-4" />
        <span>{on ? 'Skryto' : 'Skrýt'}</span>
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={pending || dismissed == null}
      aria-pressed={on}
      aria-label={label}
      title={label}
      className={[
        'flex h-6 w-6 items-center justify-center rounded-[var(--radius-xs)] border transition-colors disabled:opacity-60',
        variant === 'overlay' ? 'backdrop-blur bg-[var(--color-paper-3)]/85' : 'bg-transparent',
        on
          ? 'border-[var(--color-ink-3)] text-[var(--color-ink-2)]'
          : variant === 'overlay'
            ? 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)] hover:border-[var(--color-ink-3)]'
            : 'border-transparent text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)] hover:border-[var(--color-ink-3)]',
      ].join(' ')}
    >
      <EyeOffIcon filled={on} className="h-3.5 w-3.5" />
    </button>
  );
}
