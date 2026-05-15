/* SingleSelectDropdown — <select> styled to match the inset NumberCell.
 *
 * Used for enum filters with too many options for a pill grid
 * (category_sub_cb, dispositions if the form is too narrow, etc.).
 * `null` is rendered as a blank "any" option at the top.
 */

import type { EnumOptionLite } from './types';

export function SingleSelectDropdown<T extends string | number>({
  value,
  options,
  onChange,
  placeholder = 'any',
}: {
  value: T | null;
  options: ReadonlyArray<EnumOptionLite<T>>;
  onChange: (v: T | null) => void;
  placeholder?: string;
}) {
  return (
    <select
      value={value == null ? '' : String(value)}
      onChange={(e) => {
        const raw = e.target.value;
        if (raw === '') {
          onChange(null);
          return;
        }
        const matched = options.find((o) => String(o.value) === raw);
        onChange(matched ? matched.value : null);
      }}
      className="w-full px-2 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-rule-strong)]"
    >
      <option value="">{placeholder}</option>
      {options.map((opt) => (
        <option key={String(opt.value)} value={String(opt.value)}>
          {opt.label}
        </option>
      ))}
    </select>
  );
}
