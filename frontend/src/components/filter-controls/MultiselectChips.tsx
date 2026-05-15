/* MultiselectChips — multi-select rendered as a grid of toggleable pills.
 *
 * Used for dispositions (10 options), districts when sourced from a
 * closed list, etc. Mirrors the existing DispositionPicker in
 * Filters.tsx but in a registry-driven shape: takes an option list
 * with value + label and emits a list of selected values.
 */

import { PickButton } from '@/components/controls';

import type { EnumOptionLite } from './types';

export function MultiselectChips<T extends string | number>({
  value,
  options,
  onChange,
  cols = 5,
}: {
  value: ReadonlyArray<T>;
  options: ReadonlyArray<EnumOptionLite<T>>;
  onChange: (next: T[]) => void;
  cols?: number;
}) {
  const selected = new Set(value);
  const toggle = (v: T) => {
    if (selected.has(v)) onChange(value.filter((x) => x !== v));
    else onChange([...value, v]);
  };
  return (
    <div
      className="grid gap-1"
      style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
    >
      {options.map((opt) => (
        <PickButton
          key={String(opt.value)}
          on={selected.has(opt.value)}
          onClick={() => toggle(opt.value)}
          variant="solid"
        >
          {opt.label}
        </PickButton>
      ))}
    </div>
  );
}
