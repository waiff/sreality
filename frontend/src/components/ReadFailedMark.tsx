/* A curation read that failed with nothing to show (MS16): never drawn as an
 * empty mark or as the "add" verb, which would claim there is nothing there, but
 * as a button that says what failed and reads it again. A failed background
 * refetch keeps its good data, so only a read with no data counts as failed. */

import { AlertIcon } from '@/components/icons';

export function readFailed(q: { isError: boolean; data: unknown }): boolean {
  return q.isError && q.data === undefined;
}

const VARIANT_CLASS = {
  overlay:
    'flex h-6 w-6 items-center justify-center rounded-[var(--radius-xs)] border border-[var(--color-brick)] bg-[var(--color-paper-3)]/90 text-[var(--color-brick)] backdrop-blur',
  inline:
    'flex h-6 w-6 items-center justify-center rounded-[var(--radius-xs)] border border-transparent text-[var(--color-brick)] hover:border-[var(--color-brick)]',
  header:
    'inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/40 bg-[var(--color-brick-soft)] px-3 py-1.5 text-[0.8rem] text-[var(--color-brick)]',
} as const;

export default function ReadFailedMark({
  what,
  onRetry,
  variant = 'overlay',
}: {
  /* The thing that failed, as the sentence's subject: "Pipeline", "Kolekce"… */
  what: string;
  onRetry: () => void;
  /* `overlay` over a card photo, `inline` on a table row, `header` spelled out. */
  variant?: keyof typeof VARIANT_CLASS;
}) {
  const spelled = variant === 'header';
  return (
    <button
      type="button"
      onClick={onRetry}
      title={`${what} se nepodařilo načíst — zkusit znovu`}
      className={VARIANT_CLASS[variant]}
    >
      <AlertIcon className="h-3.5 w-3.5 shrink-0" />
      <span className={spelled ? undefined : 'sr-only'}>{what} se nepodařilo načíst. </span>
      <span className={spelled ? 'underline underline-offset-2' : 'sr-only'}>Zkusit znovu</span>
    </button>
  );
}
