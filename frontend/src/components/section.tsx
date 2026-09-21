import type { ReactNode } from 'react';

/* The two marks that separate the stacked sections of a detail page or panel.
 * Seven copies of each had drifted apart only in the hairline's margin. */

export function Hairline({ tight = false }: { tight?: boolean }) {
  return <div className={`${tight ? 'my-5' : 'my-7'} h-px bg-[var(--color-rule)]`} />;
}

export function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
      {children}
    </p>
  );
}
