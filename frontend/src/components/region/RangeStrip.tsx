import type { PercentileTriple } from '@/lib/types';

interface Props {
  label: string;
  triple: PercentileTriple;
  format: (n: number) => string;
}

/* A printer's-ruler row: thin grey baseline with three notches.
 * The median tick is slightly thicker and copper-coloured. */
export default function RangeStrip({ label, triple, format }: Props) {
  const { p25, p50, p75 } = triple;
  const span = Math.max(1, p75 - p25);
  const medianPct = ((p50 - p25) / span) * 100;

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          {label}
        </p>
        <p className="font-display text-[1.55rem] leading-none tabular-nums text-[var(--color-ink)]">
          {format(p50)}
        </p>
      </div>

      <div className="mt-3 relative h-2">
        <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-[var(--color-rule-strong)]" />
        <span
          className="absolute top-0 left-0 w-px h-2 bg-[var(--color-ink-3)]"
          aria-hidden
        />
        <span
          className="absolute top-0 right-0 w-px h-2 bg-[var(--color-ink-3)]"
          aria-hidden
        />
        <span
          className="absolute top-[-1px] w-[2px] h-3 bg-[var(--color-copper)]"
          style={{ left: `calc(${medianPct}% - 1px)` }}
          aria-hidden
        />
      </div>

      <div className="mt-1.5 flex items-baseline justify-between text-[0.7rem] tabular-nums text-[var(--color-ink-3)] font-mono">
        <span>{format(p25)}</span>
        <span className="text-[0.65rem] tracking-wide uppercase text-[var(--color-ink-4)]">
          p25 · median · p75
        </span>
        <span>{format(p75)}</span>
      </div>
    </div>
  );
}
