import { useEffect, useMemo, useRef, useState } from 'react';

/* A small searchable, creatable single-select field — type to filter `options` by
 * label, click one to commit its canonical `value`, or keep typing and commit the raw
 * text as a brand-new value (free text IS creation here; no separate confirm step).
 *
 * `value`/`onChange` are the canonical identifier (e.g. a CLIP fine_tag key), NOT the
 * displayed text — mirrors this app's `EnumOptionLite` convention (filter-controls/
 * SingleSelectDropdown, MultiselectChips): options carry a stable `value` plus a
 * human `label`, and only the label is ever shown or matched against as you type.
 *
 * Query vs. committed value are two different states (matches LocationTypeahead,
 * this app's other typeahead): opening the field with its prefilled value intact
 * shows the FULL option list, not a self-match — filtering only kicks in once the
 * operator actually edits the text. Focus also select-alls the text, so one keypress
 * cleanly replaces a prefilled value rather than appending to it. */

export interface LabelOption {
  value: string;
  label: string;
  /** Current training-example count for this label, shown in brackets — matches the
   * coverage chips at the top of the audit page. Omitted for options where a count
   * isn't meaningful (e.g. the free-text "Create" option). */
  count?: number;
}

export default function LabelCombobox({
  value,
  onChange,
  options,
  placeholder = 'label…',
}: {
  value: string;
  onChange: (next: string) => void;
  options: ReadonlyArray<LabelOption>;
  placeholder?: string;
}) {
  const labelFor = (v: string) => options.find((o) => o.value === v)?.label ?? v;

  const [text, setText] = useState(() => labelFor(value));
  const [dirty, setDirty] = useState(false);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Resync the displayed text when the committed value changes from OUTSIDE (a
  // different image, a save round-trip confirming) — never while the operator is
  // mid-edit in this field.
  useEffect(() => {
    setText(labelFor(value));
    setDirty(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const q = dirty ? text.trim().toLowerCase() : '';
  const matches = useMemo(
    () => (q ? options.filter((o) => o.label.toLowerCase().includes(q)) : options),
    [options, q],
  );

  const exactMatch = options.find((o) => o.label.toLowerCase() === text.trim().toLowerCase());
  const showCreate = dirty && text.trim().length > 0 && !exactMatch;

  const commit = (opt: LabelOption) => {
    setText(opt.label);
    setDirty(false);
    setOpen(false);
    if (opt.value !== value) onChange(opt.value);
  };

  // Typed free text that was never explicitly picked/created (blurred away instead) —
  // still needs to land as the committed value so a sibling "Train" button click reads
  // the right text. Blur reliably fires before a sibling button's own click (mousedown
  // moves focus first), so this always resolves before that click handler runs.
  const commitTypedText = () => {
    if (!dirty) return;
    const trimmed = text.trim();
    if (!trimmed) {
      setText(labelFor(value));
      setDirty(false);
      return;
    }
    commit(exactMatch ?? { value: trimmed, label: trimmed });
  };

  return (
    <div ref={ref} className="relative">
      <input
        type="text"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setDirty(true);
          setOpen(true);
        }}
        onFocus={(e) => {
          setOpen(true);
          e.target.select();
        }}
        onBlur={commitTypedText}
        placeholder={placeholder}
        className="w-full px-2 py-1 text-[0.78rem] rounded-[var(--radius-xs)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
      />
      {open && (matches.length > 0 || showCreate) && (
        <ul
          role="listbox"
          className="absolute z-20 mt-1 w-full max-h-56 overflow-y-auto rounded-[var(--radius-sm)] bg-[var(--color-paper-3)] border border-[var(--color-rule-strong)] shadow-[0_4px_16px_rgba(0,0,0,0.06)] py-1"
        >
          {matches.map((opt) => (
            <li key={opt.value} role="option" aria-selected={opt.value === value}>
              <button
                type="button"
                // Keep focus on the input through the click so `onBlur` never fires
                // (and double-commits) before this option's own commit runs.
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => commit(opt)}
                className="w-full px-2.5 py-1 text-left text-[0.78rem] text-[var(--color-ink)] hover:bg-[var(--color-copper-soft)]"
              >
                {opt.label}
                {opt.count != null && (
                  <span className="ml-1 text-[var(--color-ink-4)]">({opt.count})</span>
                )}
              </button>
            </li>
          ))}
          {showCreate && (
            <li role="option" aria-selected={false}>
              <button
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => commit({ value: text.trim(), label: text.trim() })}
                className="w-full px-2.5 py-1 text-left text-[0.78rem] text-[var(--color-copper)] hover:bg-[var(--color-copper-soft)]"
              >
                Create “{text.trim()}”
              </button>
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
