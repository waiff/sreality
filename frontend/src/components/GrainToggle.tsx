export type Grain = 'hour' | 'day';

/* The Hour/Day granularity switch shared by the Health reconciliation trends and the
 * /dedup pipeline timeline, so the two read identically. Civic-archive: a small segmented
 * control, active segment in copper. */
export default function GrainToggle({
  grain,
  onChange,
}: {
  grain: Grain;
  onChange: (g: Grain) => void;
}) {
  return (
    <span className="inline-flex rounded-[var(--radius-sm)] border border-[var(--color-rule)] overflow-hidden normal-case tracking-normal">
      {(['hour', 'day'] as const).map((g) => (
        <button
          key={g}
          type="button"
          onClick={() => onChange(g)}
          className="px-1.5 py-0.5 text-[0.6rem] font-medium transition-colors"
          style={
            grain === g
              ? { background: 'var(--color-copper)', color: 'var(--color-paper-3)' }
              : { color: 'var(--color-ink-3)' }
          }
        >
          {g === 'hour' ? 'Hour' : 'Day'}
        </button>
      ))}
    </span>
  );
}
