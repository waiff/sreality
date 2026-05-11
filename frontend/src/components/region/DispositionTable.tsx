import { fmtCount, fmtCzk, fmtArea } from '@/lib/format';
import type { RegionDispositionRow } from '@/lib/types';

interface Props {
  rows: RegionDispositionRow[];
}

const NBSP = ' ';
const fmtPpm2 = (n: number | null): string =>
  n == null ? '—' : `${new Intl.NumberFormat('cs-CZ').format(n)}${NBSP}Kč/m²`;

export default function DispositionTable({ rows }: Props) {
  if (rows.length === 0) {
    return (
      <p className="text-sm text-[var(--color-ink-3)] italic">
        No listings to break down.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
            <th className="py-2 pr-4 font-medium">Disposition</th>
            <th className="py-2 px-3 font-medium text-right">Listings</th>
            <th className="py-2 px-3 font-medium text-right">Median price</th>
            <th className="py-2 px-3 font-medium text-right">Median Kč/m²</th>
            <th className="py-2 pl-3 font-medium text-right">Median area</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.disposition}
              className="border-t border-[var(--color-rule-soft)]"
            >
              <td className="py-2 pr-4 font-mono text-[var(--color-ink)]">
                {r.disposition}
              </td>
              <td className="py-2 px-3 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
                {fmtCount(r.n)}
              </td>
              <td className="py-2 px-3 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
                {fmtCzk(r.median_price)}
              </td>
              <td className="py-2 px-3 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
                {fmtPpm2(r.median_ppm2)}
              </td>
              <td className="py-2 pl-3 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
                {fmtArea(r.median_area)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
