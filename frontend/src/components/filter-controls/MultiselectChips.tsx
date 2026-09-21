/* MultiselectChips — multi-select rendered as a wrapping row of toggleable pills.
 *
 * Used for dispositions (10 options), districts when sourced from a
 * closed list, etc. Mirrors the existing DispositionPicker in
 * Filters.tsx but in a registry-driven shape: takes an option list
 * with value + label and emits a list of selected values.
 *
 * The chips USED to be a fixed N-column grid (`repeat(cols, minmax(0,1fr))`),
 * which sizes a column from the container and not from its contents: any
 * caller narrower than N × the longest label got that label CLIPPED by the
 * button's own overflow — "Komerční" as "Komerč", "Zemědělská usedlost" as
 * "Zeměděls usedlost" (measured on Browse's sidebar and on the sold-comps
 * block at 1280 AND 390 px). A wrapping flex row cannot clip: a chip is as wide
 * as its label (floored at 4rem so a row of "1+kk" still reads as a row), and
 * how many fit per row is measured by the browser rather than declared. That
 * deletes the `cols` prop with it — no caller has to guess a column count that
 * the longest translated label then has to live inside.
 */

import { PickButton } from '@/components/controls';

import type { EnumOptionLite } from './types';

export function MultiselectChips<T extends string | number>({
  value,
  options,
  onChange,
}: {
  value: ReadonlyArray<T>;
  options: ReadonlyArray<EnumOptionLite<T>>;
  onChange: (next: T[]) => void;
}) {
  const selected = new Set(value);
  const toggle = (v: T) => {
    if (selected.has(v)) onChange(value.filter((x) => x !== v));
    else onChange([...value, v]);
  };
  return (
    <div className="flex flex-wrap gap-1">
      {options.map((opt) => (
        <PickButton
          key={String(opt.value)}
          on={selected.has(opt.value)}
          onClick={() => toggle(opt.value)}
          variant="solid"
          className="min-w-16"
        >
          {opt.label}
        </PickButton>
      ))}
    </div>
  );
}
