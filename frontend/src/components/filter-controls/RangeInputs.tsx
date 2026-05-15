/* RangeInputs — paired min/max number inputs.
 *
 * Used for price / area / estate / usable / garden filters where the
 * operator wants an open-ended range. Either side can be null. The
 * onChange callback fires with both bounds on every keystroke; callers
 * coerce to integers / floats as needed via the `coerce` prop.
 */

import { NumberCell } from '@/components/controls';

export function RangeInputs({
  minValue,
  maxValue,
  minPlaceholder = 'min',
  maxPlaceholder = 'max',
  coerce,
  onChange,
  ariaLabelMin,
  ariaLabelMax,
}: {
  minValue: number | null;
  maxValue: number | null;
  minPlaceholder?: string;
  maxPlaceholder?: string;
  coerce?: 'int' | 'float';
  onChange: (lo: number | null, hi: number | null) => void;
  ariaLabelMin?: string;
  ariaLabelMax?: string;
}) {
  const parse = (raw: string): number | null => {
    const trimmed = raw.trim();
    if (trimmed === '') return null;
    const n = Number(trimmed);
    if (!Number.isFinite(n)) return null;
    return coerce === 'int' ? Math.trunc(n) : n;
  };

  return (
    <div className="grid grid-cols-2 gap-2">
      <NumberCell
        value={minValue}
        placeholder={minPlaceholder}
        onChange={(e) => onChange(parse(e.target.value), maxValue)}
        ariaLabel={ariaLabelMin}
      />
      <NumberCell
        value={maxValue}
        placeholder={maxPlaceholder}
        onChange={(e) => onChange(minValue, parse(e.target.value))}
        ariaLabel={ariaLabelMax}
      />
    </div>
  );
}
