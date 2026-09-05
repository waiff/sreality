import type { ChangeEvent, ReactNode } from 'react';
import { useId, useState } from 'react';

export type TriValue = 'any' | 'yes' | 'no';

/* -------------------------------------------------------------------------- */
/* CollapsibleGroup — the top tier of the filter panel: an expand/collapse    */
/* band that holds several ControlGroups. The header is a full-width button    */
/* (WAI-ARIA accordion: aria-expanded + aria-controls on the trigger). Reads   */
/* as the dominant landmark via the rule-strong divider, the chevron, and the  */
/* wide-tracked display heading — a tier clearly above the ControlGroup titles */
/* nested inside it. `active` surfaces a copper dot while collapsed so folding  */
/* a band never hides a filter the operator has set.                           */
/* -------------------------------------------------------------------------- */

export function CollapsibleGroup({
  title,
  children,
  defaultOpen = false,
  active = false,
}: {
  title: string;
  children: ReactNode;
  defaultOpen?: boolean;
  active?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const panelId = useId();
  return (
    <section className="border-t border-[var(--color-rule-strong)] first:border-t-0">
      <h3 className="m-0">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          aria-controls={panelId}
          className="group flex w-full items-center gap-2.5 py-4 text-left"
        >
          <span
            className="text-[0.85rem] tracking-[0.15em] uppercase font-semibold text-[var(--color-ink)]"
            style={{ fontFamily: 'var(--font-display)' }}
          >
            {title}
          </span>
          {active && !open && (
            <span
              aria-label="has active filters"
              className="h-1.5 w-1.5 rounded-full bg-[var(--color-copper)]"
            />
          )}
          <svg
            viewBox="0 0 16 16"
            width="14"
            height="14"
            fill="none"
            aria-hidden="true"
            className={[
              'ml-auto shrink-0 text-[var(--color-ink-4)] transition-[transform,color] duration-200',
              'group-hover:text-[var(--color-ink-2)]',
              open ? 'rotate-180' : '',
            ].join(' ')}
          >
            <path
              d="M4 6l4 4 4-4"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </h3>
      {open && (
        <div id={panelId} className="pb-6 space-y-6">
          {children}
        </div>
      )}
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* ControlGroup — a titled group of sections (mid tier). A plain div with      */
/* role="group" + aria-labelledby, NOT a fieldset: a fieldset's padding-top    */
/* lands below its legend, so the title hugged the divider with an oversized   */
/* gap beneath it. The heading sits one notch quieter than a CollapsibleGroup  */
/* band (ink-2, smaller) and clearly above the small-caps Section labels.      */
/* `bordered` (default true) draws the rule-strong divider for standalone use; */
/* inside a band, pass bordered={false} so the band owns the separation and    */
/* groups stack on whitespace alone.                                           */
/* -------------------------------------------------------------------------- */

export function ControlGroup({
  title,
  children,
  className = '',
  bordered = true,
  layout = 'stack',
}: {
  title: string;
  children: ReactNode;
  className?: string;
  bordered?: boolean;
  /* 'stack' (default) lays the rows out one-per-line. 'grid' flows them
   * into an auto-fitting multi-column grid: each row gets a min width of
   * 11rem and the browser packs as many columns as the group's width
   * allows. Because the sidebar is operator-resizable, widening it
   * directly turns a tall single column of rows into a short 2–3 column
   * block — no viewport breakpoints, the layout tracks the actual
   * container width. Only pass 'grid' for groups whose rows are all
   * compact (range inputs / selects / tri-state toggles); pill grids and
   * full-width widgets stay 'stack'. */
  layout?: 'stack' | 'grid';
}) {
  const headingId = useId();
  return (
    <div
      role="group"
      aria-labelledby={headingId}
      className={[
        bordered
          ? 'pt-7 border-t border-[var(--color-rule-strong)] first:border-t-0 first:pt-0'
          : '',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <p
        id={headingId}
        className="mb-4 text-[0.78rem] tracking-[0.05em] uppercase text-[var(--color-ink-2)] font-semibold"
        style={{ fontFamily: 'var(--font-display)' }}
      >
        {title}
      </p>
      <div
        className={
          layout === 'grid'
            ? 'grid gap-x-4 gap-y-5 [grid-template-columns:repeat(auto-fit,minmax(11rem,1fr))]'
            : 'space-y-5'
        }
      >
        {children}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Section + Label scaffolding                                                */
/* Labels are deliberately quieter than ControlGroup titles: smaller, wider   */
/* tracking, ink-tertiary. They mark fields inside a group, never groups.    */
/* -------------------------------------------------------------------------- */

/* Field — the ONE caption-over-control(s) primitive.
 *
 * Six copies of this existed (Section here, plus a local `Field` in Brokers,
 * Outreach, Datasets, Settings and DefinitionEditor). Three of them wrapped
 * their children in a bare <label>. For a single <input> that is correct HTML;
 * for a GROUP of pills it is two defects at once: the first pill inherits the
 * whole row's text as its accessible name (and the rest get no association at
 * all), and a click on the caption ACTIVATES that first pill — clicking the
 * word "Nabídka" silently selected "Prodej". Tests had grown textContent
 * workarounds to route around it.
 *
 * `as="group"` is the DEFAULT and the safe form: role="group" +
 * aria-labelledby. A group name is ancestor context — it never replaces or
 * pollutes a child's name, a group of one is legal, and the element has no
 * activation behaviour, so a mis-classified site degrades to "no click-to-
 * focus", never to a false relationship. `as="control"` is the plain <label>
 * wrap, for exactly one labelable child (input / textarea / select / button).
 *
 * `help` renders under the caption and is announced via aria-describedby on
 * the group form. */
// eslint-disable-next-line no-restricted-syntax -- THE Field; the ban exists so a seventh local copy cannot appear
export function Field({
  label,
  help,
  as = 'group',
  className = '',
  children,
}: {
  label: string;
  help?: string;
  as?: 'group' | 'control';
  className?: string;
  children: ReactNode;
}) {
  const captionId = useId();
  const helpId = useId();
  const caption = <Label id={captionId}>{label}</Label>;
  const helpEl = help ? (
    <p id={helpId} className="mt-0.5 text-[0.7rem] text-[var(--color-ink-4)]">
      {help}
    </p>
  ) : null;

  if (as === 'control') {
    // Help sits OUTSIDE the <label>: everything inside a label joins the
    // control's accessible name, and "System prompt one sentence — …" is not a
    // name. The wrap itself is sanctioned: exactly one labelable child.
    return (
      <div className={className}>
        <label className="block">
          {caption}
          <div className="mt-2">{children}</div>
        </label>
        {helpEl}
      </div>
    );
  }
  return (
    <div
      role="group"
      aria-labelledby={captionId}
      aria-describedby={help ? helpId : undefined}
      className={className}
    >
      {caption}
      {helpEl}
      <div className="mt-2">{children}</div>
    </div>
  );
}

/* Section is Field's original name in the Browse sidebar. Kept as an alias so
 * its 18 call sites stay untouched and pixel-stable; it now carries the group
 * role and name it always should have. */
export function Section({ label, children }: { label: string; children: ReactNode }) {
  return <Field label={label}>{children}</Field>;
}

export function Label({ children, id }: { children: ReactNode; id?: string }) {
  return (
    <p id={id} className="text-[0.62rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
      {children}
    </p>
  );
}

/* Segmented — a row of PickButtons acting as a single-value choice. Lived as
 * two byte-for-byte copies in Brokers and Outreach; a Field caption is what
 * names it, so it belongs beside Field. */
export function Segmented<T extends string | number | null>({
  options,
  value,
  onChange,
}: {
  options: ReadonlyArray<{ value: T; label: string }>;
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1">
      {options.map((o) => (
        <PickButton key={String(o.value)} on={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </PickButton>
      ))}
    </div>
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
  disabled = false,
}: {
  on: boolean;
  onClick: () => void;
  children: ReactNode;
  variant?: 'soft' | 'solid';
  className?: string;
  ariaLabel?: string;
  disabled?: boolean;
}) {
  const onClasses =
    variant === 'solid'
      ? 'bg-[var(--color-copper)] text-white border-[var(--color-copper)]'
      : 'bg-[var(--color-copper-soft)] text-[var(--color-copper)] border-[var(--color-copper)]';
  const offClasses =
    'bg-[var(--color-paper-2)] text-[var(--color-ink-3)] border-[var(--color-rule)] hover:text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)]';
  const disabledClasses =
    'bg-[var(--color-paper-2)] text-[var(--color-ink-3)] border-[var(--color-rule)] opacity-50 cursor-not-allowed';
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={on}
      aria-label={ariaLabel}
      className={[
        'px-2 py-1.5 text-xs rounded-[var(--radius-sm)] border transition-colors',
        disabled ? disabledClasses : on ? onClasses : offClasses,
        className,
      ].join(' ')}
    >
      {children}
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/* Switch — pill on/off toggle. One implementation shared by every boolean    */
/* control in the app (filter-visibility cells, region-priority toggles, the  */
/* NEW DEDUP boolean settings) instead of each screen redrawing its own pill. */
/* -------------------------------------------------------------------------- */

export function Switch({
  on,
  onChange,
  pending = false,
  disabled = false,
  ariaLabel,
}: {
  on: boolean;
  onChange: (next: boolean) => void;
  pending?: boolean;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!on)}
      aria-pressed={on}
      aria-label={ariaLabel}
      disabled={pending || disabled}
      className={[
        'inline-flex shrink-0 items-center justify-center w-9 h-5 rounded-full border transition-colors',
        on
          ? 'bg-[var(--color-sage-soft)] border-[var(--color-sage)]/60'
          : 'bg-[var(--color-paper-2)] border-[var(--color-rule)]',
        pending ? 'opacity-50 cursor-wait' : disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer',
      ].join(' ')}
    >
      <span
        className={[
          'w-3 h-3 rounded-full transition-transform',
          on ? 'translate-x-2 bg-[var(--color-sage)]' : '-translate-x-2 bg-[var(--color-ink-4)]',
        ].join(' ')}
        aria-hidden
      />
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
  onBlur,
  ariaLabel,
}: {
  /* A string value lets callers that buffer raw text (FilterForm's
   * BufferedNumberInput) keep mid-typing intermediates like '-' alive. */
  value: number | string | null;
  placeholder: string;
  onChange: (e: ChangeEvent<HTMLInputElement>) => void;
  onBlur?: () => void;
  ariaLabel?: string;
}) {
  return (
    <input
      type="text"
      inputMode="numeric"
      value={value ?? ''}
      placeholder={placeholder}
      onChange={onChange}
      onBlur={onBlur}
      aria-label={ariaLabel}
      className="w-full min-w-0 px-2 py-1.5 text-sm font-mono tabular-nums rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
    />
  );
}

/* -------------------------------------------------------------------------- */
/* TriRow — any / yes / no segmented control on a single label row. Used     */
/* by Filters.tsx (and any future spec form) with structurally identical      */
/* string unions; the generic <T extends TriValue> keeps each caller's        */
/* narrower type intact at the boundary.                                      */
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
