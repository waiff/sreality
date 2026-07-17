// A toggle-style filter pill: on/off, an optional trailing count badge (omit
// `count` for the plain form). Shared by every dedup surface with a row of
// single-select text filters (outcome, source, property type, factor) —
// Decision history and the manual review Queue.
export default function FilterChip({
  on,
  label,
  count,
  onClick,
}: {
  on: boolean;
  label: string;
  count?: number;
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
      {count != null && (
        <span className={on ? 'ml-1.5 opacity-70' : 'ml-1.5 text-[var(--color-ink-4)]'}>
          {count}
        </span>
      )}
    </button>
  );
}
