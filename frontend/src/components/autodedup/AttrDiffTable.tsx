/* AUTODEDUP · the attribute diff.
 *
 * §12 asks for "an attribute diff table highlighting only the disagreeing
 * fields", and the emphasis is on ONLY: on a residual pair almost everything
 * matches, so the two or three rows that do not are the entire content of the
 * screen. A row is marked when both sides carry a value and the values differ —
 * an ABSENT value on one side is not a disagreement, it is a gap, and the two
 * are coloured apart because a gap is never evidence against a duplicate.
 */

import type { AutodedupMember } from '@/lib/api';
import { categoryMainLabel, categoryTypeLabel } from '@/lib/enums';
import { fmtArea, fmtCzk, fmtShortDate } from '@/lib/format';
import { portalLabel } from '@/lib/portals';

export interface DiffRow {
  label: string;
  a: string | null;
  b: string | null;
}

const text = (v: string | number | null | undefined): string | null =>
  v == null || v === '' ? null : String(v);

/* The comparable surface of a listing, in the order an operator reads it:
 * what it is, then what it costs, then where in the building, then when it
 * lived. `portal` leads because a same-portal pair is a different question
 * from a cross-portal one. */
const floorText = (m: AutodedupMember): string | null => {
  if (m.floor == null) return null;
  return m.total_floors == null ? String(m.floor) : `${m.floor} / ${m.total_floors}`;
};

export function memberDiffRows(a: AutodedupMember, b: AutodedupMember): DiffRow[] {
  return [
    {
      label: 'Portál',
      a: a.source ? portalLabel(a.source) : null,
      b: b.source ? portalLabel(b.source) : null,
    },
    /* NULL-CHECK BEFORE FORMATTING, always. Every one of these formatters has a
     * non-null answer for null — `categoryMainLabel(null)` is "Nemovitost",
     * `categoryTypeLabel(null)` and `fmtShortDate(null)` are "—" — so formatting
     * first turns a GAP into two different strings and paints it as a
     * contradiction on the one screen whose job is weighing contradictions. */
    {
      label: 'Druh',
      a: a.category_main == null ? null : text(categoryMainLabel(a.category_main)),
      b: b.category_main == null ? null : text(categoryMainLabel(b.category_main)),
    },
    {
      label: 'Nabídka',
      a: a.category_type == null ? null : text(categoryTypeLabel(a.category_type)),
      b: b.category_type == null ? null : text(categoryTypeLabel(b.category_type)),
    },
    { label: 'Dispozice', a: text(a.disposition), b: text(b.disposition) },
    {
      label: 'Plocha',
      a: a.area_m2 == null ? null : fmtArea(a.area_m2),
      b: b.area_m2 == null ? null : fmtArea(b.area_m2),
    },
    {
      /* Floor WITHIN the building: "2 / 5" against "2 / 6" is two different
       * buildings, which the bare storey number hides. Shown only when the
       * payload carries the total — never padded with a guess. */
      label: 'Patro',
      a: floorText(a),
      b: floorText(b),
    },
    {
      label: 'Cena',
      a: a.price_czk == null ? null : fmtCzk(a.price_czk),
      b: b.price_czk == null ? null : fmtCzk(b.price_czk),
    },
    {
      label: 'První viděno',
      a: a.first_seen_at == null ? null : text(fmtShortDate(a.first_seen_at)),
      b: b.first_seen_at == null ? null : text(fmtShortDate(b.first_seen_at)),
    },
    {
      label: 'Naposledy viděno',
      a: a.last_seen_at == null ? null : text(fmtShortDate(a.last_seen_at)),
      b: b.last_seen_at == null ? null : text(fmtShortDate(b.last_seen_at)),
    },
    {
      label: 'Stav',
      a: a.is_active == null ? null : a.is_active ? 'aktivní' : 'staženo',
      b: b.is_active == null ? null : b.is_active ? 'aktivní' : 'staženo',
    },
  ];
}

export function differs(row: DiffRow): boolean {
  return row.a != null && row.b != null && row.a !== row.b;
}

const TH = 'py-1 pr-3 text-left font-medium whitespace-nowrap align-top';
const TD = 'py-1 pr-3 align-top';

export default function AttrDiffTable({
  rows,
  /* Show every row, or only the ones that disagree. The residual view defaults
   * to everything — the matches are the argument FOR the pair. */
  onlyDiffs = false,
  captionA = 'A',
  captionB = 'B',
}: {
  rows: DiffRow[];
  onlyDiffs?: boolean;
  captionA?: string;
  captionB?: string;
}) {
  const shown = onlyDiffs ? rows.filter(differs) : rows;
  if (shown.length === 0) {
    return (
      <p className="text-[0.72rem] text-[var(--color-ink-3)]">
        Every compared attribute agrees.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[0.72rem]">
        <thead>
          <tr className="text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
            <th className={TH}>Pole</th>
            <th className={TH}>{captionA}</th>
            <th className={TH}>{captionB}</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((row) => {
            const bad = differs(row);
            const gap = row.a == null || row.b == null;
            return (
              <tr
                key={row.label}
                className={`border-t border-[var(--color-rule-soft)] ${
                  bad ? 'bg-[var(--color-brick-soft)]' : ''
                }`}
              >
                <td className={`${TD} text-[var(--color-ink-4)]`}>{row.label}</td>
                {[row.a, row.b].map((value, i) => (
                  <td
                    key={i}
                    className={`${TD} font-mono tabular-nums ${
                      bad
                        ? 'text-[var(--color-brick)]'
                        : gap
                          ? 'text-[var(--color-ink-4)]'
                          : 'text-[var(--color-ink-2)]'
                    }`}
                  >
                    {value ?? 'chybí'}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
