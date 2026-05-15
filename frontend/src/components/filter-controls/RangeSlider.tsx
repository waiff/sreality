/* RangeSlider — dual-thumb slider stacked over a pair of number inputs.
 *
 * Extracted from the original Browse `RangeFilter` in Filters.tsx so
 * both the Browse sidebar and the registry-driven <FilterForm> render
 * the same widget for bounded numeric ranges (price, area, estate
 * area, usable area, garden area).
 *
 * Conventions inherited from the Browse implementation:
 *   - Value is a [lo, hi] tuple of nullable numbers; null on either
 *     side means "unbounded on that end".
 *   - The slider always shows thumbs at the current value clamped to
 *     bounds; the paired inputs show the actual numeric value (null
 *     renders as empty).
 *   - Dragging the lo thumb above the hi value clamps; the same for
 *     hi below lo. Typing min > max in the inputs is swapped on blur.
 *   - When a thumb is dragged to the very edge of bounds, the
 *     corresponding side of `value` is reported as `null` (an
 *     explicit "no lower / upper bound").
 *   - `unit` is rendered next to the inputs in a quieter style.
 */

import type { ChangeEvent } from 'react';

import { NumberCell } from '@/components/controls';

export interface RangeBounds {
  min: number;
  max: number;
  step: number;
}

export function RangeSlider({
  bounds,
  value,
  onChange,
  unit,
  ariaLabel,
}: {
  bounds: RangeBounds;
  value: [number | null, number | null];
  onChange: (next: [number | null, number | null]) => void;
  unit?: string;
  ariaLabel?: string;
}) {
  const lo = value[0] ?? bounds.min;
  const hi = value[1] ?? bounds.max;
  const span = bounds.max - bounds.min;

  const setLo = (n: number) => {
    const clamped = Math.max(bounds.min, Math.min(n, hi));
    onChange([clamped === bounds.min ? null : clamped, value[1]]);
  };
  const setHi = (n: number) => {
    const clamped = Math.min(bounds.max, Math.max(n, lo));
    onChange([value[0], clamped === bounds.max ? null : clamped]);
  };

  const onNumber = (which: 0 | 1) => (e: ChangeEvent<HTMLInputElement>) => {
    const raw = e.target.value.replace(/\s/g, '');
    if (raw === '') {
      onChange(which === 0 ? [null, value[1]] : [value[0], null]);
      return;
    }
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 0) return;
    const clamped = Math.max(bounds.min, Math.min(n, bounds.max));
    if (which === 0) {
      onChange([clamped === bounds.min ? null : clamped, value[1]]);
    } else {
      onChange([value[0], clamped === bounds.max ? null : clamped]);
    }
  };

  const onCommit = () => {
    const a = value[0];
    const b = value[1];
    if (a != null && b != null && a > b) onChange([b, a]);
  };

  const baseAria = ariaLabel ?? 'range';
  return (
    <div>
      <div className="relative h-6">
        <div className="absolute inset-x-0 top-1/2 h-0.5 -translate-y-1/2 bg-[var(--color-rule-strong)] rounded-full" />
        <div
          className="absolute top-1/2 h-0.5 -translate-y-1/2 bg-[var(--color-copper)] rounded-full"
          style={{
            left:  `${((lo - bounds.min) / span) * 100}%`,
            right: `${100 - ((hi - bounds.min) / span) * 100}%`,
          }}
        />
        <input
          type="range"
          min={bounds.min}
          max={bounds.max}
          step={bounds.step}
          value={lo}
          onChange={(e) => setLo(Number(e.target.value))}
          className="range-slider"
          aria-label={`${baseAria} minimum`}
        />
        <input
          type="range"
          min={bounds.min}
          max={bounds.max}
          step={bounds.step}
          value={hi}
          onChange={(e) => setHi(Number(e.target.value))}
          className="range-slider"
          style={{ zIndex: 1 }}
          aria-label={`${baseAria} maximum`}
        />
      </div>
      <div className="mt-3 flex items-center gap-2">
        <NumberCell
          value={value[0]}
          placeholder={String(bounds.min)}
          onChange={onNumber(0)}
          onBlur={onCommit}
          ariaLabel={`${baseAria} minimum value`}
        />
        <span className="text-[var(--color-ink-3)] text-sm">—</span>
        <NumberCell
          value={value[1]}
          placeholder={String(bounds.max)}
          onChange={onNumber(1)}
          onBlur={onCommit}
          ariaLabel={`${baseAria} maximum value`}
        />
        {unit ? (
          <span className="text-[var(--color-ink-3)] text-xs ml-1 tracking-wide">{unit}</span>
        ) : null}
      </div>
    </div>
  );
}
