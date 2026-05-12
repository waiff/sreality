import type { ChangeEvent, ReactNode } from 'react';

export type TriValue = 'any' | 'yes' | 'no';

/* -------------------------------------------------------------------------- */
/* ControlGroup — a titled group of sections. Display-font small-caps         */
/* legend sits above a heavier top rule so groups read louder than the        */
/* Section labels nested inside them. First group in a container drops the    */
/* top edge so it doesn't double up with the host's own border.               */
/* -------------------------------------------------------------------------- */

export function ControlGroup({
  title,
  children,
  className = '',
}: {
  title: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <fieldset
      className={[
        'm-0 p-0 border-0 first:border-t-0 first:pt-0',
        'pt-6 border-t border-[var(--color-rule-strong)]',
        className,
      ].join(' ')}
    >
      <legend
        className="block w-full mb-5 text-[0.65rem] tracking-[0.22em] uppercase text-[var(--color-ink-2)] font-medium"
        style={{ fontFamily: 'var(--font-display)' }}
      >
        {title}
      </legend>
      <div className="space-y-6">{children}</div>
    </fieldset>
  );
}

/* -------------------------------------------------------------------------- */
/* Section + Label scaffolding                                                */
/* -------------------------------------------------------------------------- */

export function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <Label>{label}</Label>
      <div className="mt-2.5">{children}</div>
    </div>
  );
}

export function Label({ children }: { children: ReactNode }) {
  return (
    <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
      {children}
    </p>
  );
}

/* -------------------------------------------------------------------------- */
/* PickButton — segmented-cell pill used for enum / status / seen-within /    */
/* tri-state / disposition pickers. Two visual modes: 'soft' (tinted copper   */
/* on selected) is the default and reads as "one of many tagged options";    */
/* 'solid' (filled copper background, white text) is louder and reserved for  */
/* the Disposition grid where each cell is a fixed property of the listing.   */
/* -------------------------------------------------------------------------- */

export function PickButton({
  on,
  onClick,
  children,
  variant = 'soft',
  className = '',
  ariaLabel,
}: {
  on: boolean;
  onClick: () => void;
  children: ReactNode;
  variant?: 'soft' | 'solid';
  className?: string;
  ariaLabel?: string;
}) {
  const onClasses =
    variant === 'solid'
      ? 'bg-[var(--color-copper)] text-white border-[var(--color-copper)]'
      : 'bg-[var(--color-copper-soft)] text-[var(--color-copper)] border-[var(--color-copper)]';
  const offClasses =
    'bg-[var(--color-paper-2)] text-[var(--color-ink-3)] border-[var(--color-rule)] hover:text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)]';
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      aria-label={ariaLabel}
      className={[
        'px-2 py-1.5 text-xs rounded-[var(--radius-sm)] border transition-colors',
        on ? onClasses : offClasses,
        className,
      ].join(' ')}
    >
      {children}
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/* NumberCell — paired-input cell used inside RangeFilter and any standalone  */
/* numeric input slot. Inset background + tabular monospace so values align   */
/* vertically across rows.                                                    */
/* -------------------------------------------------------------------------- */

export function NumberCell({
  value,
  placeholder,
  onChange,
  ariaLabel,
}: {
  value: number | null;
  placeholder: string;
  onChange: (e: ChangeEvent<HTMLInputElement>) => void;
  ariaLabel?: string;
}) {
  return (
    <input
      type="text"
      inputMode="numeric"
      value={value ?? ''}
      placeholder={placeholder}
      onChange={onChange}
      aria-label={ariaLabel}
      className="w-full min-w-0 px-2 py-1.5 text-sm font-mono tabular-nums rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
    />
  );
}

/* -------------------------------------------------------------------------- */
/* TriRow — any / yes / no segmented control on a single label row. Used     */
/* by Filters.tsx and EstimateForm.tsx with structurally identical string     */
/* unions; the generic <T extends TriValue> keeps each caller's narrower      */
/* type intact at the boundary.                                               */
/* -------------------------------------------------------------------------- */

const TRI_OPTS: ReadonlyArray<{ value: TriValue; label: string }> = [
  { value: 'any', label: 'any' },
  { value: 'yes', label: 'yes' },
  { value: 'no',  label: 'no'  },
];

export function TriRow<T extends TriValue>({
  label,
  value,
  onChange,
}: {
  label: string;
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-sm text-[var(--color-ink-2)]">{label}</span>
      <div className="grid grid-cols-3 gap-0.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-0.5">
        {TRI_OPTS.map((opt) => {
          const on = value === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              onClick={() => onChange(opt.value as T)}
              className={[
                'px-2.5 py-0.5 text-[0.7rem] rounded-[var(--radius-xs)] transition-colors',
                on
                  ? 'bg-[var(--color-copper)] text-white'
                  : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
              ].join(' ')}
              aria-pressed={on}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
