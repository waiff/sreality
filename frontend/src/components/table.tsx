import type { ReactNode } from 'react';

/* The cells of the app's hand-rolled tables. Each table used to carry its own
 * copy, so a class tweak landed in one and missed the other seven.
 *
 * Three sizes because the tables sit at three depths: full-page (estimations,
 * watchdogs) on the page gutter, the collection table inside a panel, the run
 * panel's comparables table one level deeper again. */
const TH_SIZE = {
  md: 'px-4 py-2.5 text-[0.7rem]',
  sm: 'px-3 py-2.5 text-[0.7rem]',
  xs: 'px-3 py-2 text-[0.65rem]',
} as const;

export function Th({
  align = 'left',
  size = 'md',
  children,
}: {
  align?: 'left' | 'right';
  size?: keyof typeof TH_SIZE;
  children: ReactNode;
}) {
  return (
    <th
      scope="col"
      className={[
        TH_SIZE[size],
        'tracking-[0.14em] uppercase font-medium text-[var(--color-ink-3)]',
        align === 'right' ? 'text-right' : 'text-left',
      ].join(' ')}
    >
      {children}
    </th>
  );
}

/* The numeric summary tables (price-band velocity, region disposition box
 * plots): every column is a number, so the header is always right-aligned and
 * rides tighter tracking than a <Th>. */

export function StatTh({ children }: { children: ReactNode }) {
  return (
    <th className="px-3 py-2 font-medium text-right text-[var(--color-ink-3)] tracking-wide uppercase text-[0.65rem]">
      {children}
    </th>
  );
}

export function StatTd({ children, bold }: { children: ReactNode; bold?: boolean }) {
  return (
    <td
      className={`px-3 py-1.5 text-right ${
        bold ? 'font-medium text-[var(--color-ink)]' : 'text-[var(--color-ink-2)]'
      }`}
    >
      {children}
    </td>
  );
}
