import { TrashIcon } from '@/components/icons';

// A toggle-style filter pill: on/off, an optional trailing count badge (omit
// `count` for the plain form). Shared by every dedup surface with a row of
// single-select text filters (outcome, source, property type, factor) —
// Decision history and the manual review Queue.
//
// `onRemove` (optional) splits the pill into two buttons — the toggle and a small
// trailing trash — for chips that are also deletable things, not just filters
// (/clip-audit's training-set labels). Omitted, the markup is the classic single
// button, unchanged for every existing call site.
export default function FilterChip({
  on,
  label,
  count,
  onClick,
  onRemove,
  removeLabel,
}: {
  on: boolean;
  label: string;
  count?: number;
  onClick: () => void;
  onRemove?: () => void;
  removeLabel?: string;
}) {
  const badge = count != null && (
    <span className={on ? 'ml-1.5 opacity-70' : 'ml-1.5 text-[var(--color-ink-4)]'}>
      {count}
    </span>
  );

  if (onRemove == null) {
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
        {badge}
      </button>
    );
  }

  return (
    <span
      className={[
        'inline-flex items-center rounded-[var(--radius-sm)] border transition-colors',
        on
          ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)]'
          : 'border-[var(--color-rule)] hover:border-[var(--color-rule-strong)]',
      ].join(' ')}
    >
      <button
        type="button"
        onClick={onClick}
        className={[
          'pl-2.5 pr-1 py-1 rounded-l-[var(--radius-sm)] text-[0.78rem]',
          on
            ? 'text-[var(--color-copper)]'
            : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
        ].join(' ')}
      >
        {label}
        {badge}
      </button>
      <button
        type="button"
        onClick={onRemove}
        aria-label={removeLabel}
        title={removeLabel}
        className="pl-1 pr-2 py-1 self-stretch rounded-r-[var(--radius-sm)] text-[var(--color-ink-4)] hover:text-[var(--color-brick)]"
      >
        <TrashIcon className="h-3 w-3" />
      </button>
    </span>
  );
}
