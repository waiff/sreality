import { useEffect, useMemo, useRef, useState } from 'react';

/* A small searchable, creatable single-select text field — type to filter `options`,
 * click one to pick it, or just keep typing and use the raw text as a brand-new label
 * (free text IS creation here; there's no separate "confirm create" step). Local-only
 * (options are an in-memory array, no network debounce), unlike LocationTypeahead
 * (Mapy.cz remote suggestions, multi-chip) — this is the lighter single-value sibling
 * for the /phash-audit training-label picker. */

export default function LabelCombobox({
  value,
  onChange,
  options,
  placeholder = 'label…',
}: {
  value: string;
  onChange: (next: string) => void;
  options: ReadonlyArray<string>;
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const matches = useMemo(() => {
    const q = value.trim().toLowerCase();
    const pool = q
      ? options.filter((o) => o.toLowerCase().includes(q))
      : options;
    return pool.slice(0, 8);
  }, [options, value]);

  const exactMatch = options.some((o) => o.toLowerCase() === value.trim().toLowerCase());
  const showCreate = value.trim().length > 0 && !exactMatch;

  return (
    <div ref={ref} className="relative">
      <input
        type="text"
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        placeholder={placeholder}
        className="w-full px-2 py-1 text-[0.78rem] rounded-[var(--radius-xs)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
      />
      {open && (matches.length > 0 || showCreate) && (
        <ul
          role="listbox"
          className="absolute z-20 mt-1 w-full max-h-56 overflow-y-auto rounded-[var(--radius-sm)] bg-[var(--color-paper-3)] border border-[var(--color-rule-strong)] shadow-[0_4px_16px_rgba(0,0,0,0.06)] py-1"
        >
          {matches.map((opt) => (
            <li key={opt}>
              <button
                type="button"
                onClick={() => {
                  onChange(opt);
                  setOpen(false);
                }}
                className="w-full px-2.5 py-1 text-left text-[0.78rem] text-[var(--color-ink)] hover:bg-[var(--color-copper-soft)]"
              >
                {opt}
              </button>
            </li>
          ))}
          {showCreate && (
            <li>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="w-full px-2.5 py-1 text-left text-[0.78rem] text-[var(--color-copper)] hover:bg-[var(--color-copper-soft)]"
              >
                Create “{value.trim()}”
              </button>
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
