import type { ReactNode } from 'react';

export interface Tab<T extends string> {
  key: T;
  label: string;
  badge?: ReactNode;
}

interface Props<T extends string> {
  tabs: ReadonlyArray<Tab<T>>;
  active: T;
  onChange: (next: T) => void;
}

export default function Tabs<T extends string>({ tabs, active, onChange }: Props<T>) {
  return (
    <div role="tablist" className="flex items-center gap-1 border-b border-[var(--color-rule)]">
      {tabs.map((t) => {
        const on = t.key === active;
        return (
          <button
            key={t.key}
            role="tab"
            aria-selected={on}
            onClick={() => onChange(t.key)}
            className={[
              'relative px-4 py-2.5 text-sm tracking-wide transition-colors',
              on
                ? 'text-[var(--color-ink)]'
                : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
            ].join(' ')}
          >
            <span className="inline-flex items-center gap-2">
              {t.label}
              {t.badge != null && (
                <span className="font-mono tabular-nums text-[0.7rem] text-[var(--color-ink-3)]">
                  {t.badge}
                </span>
              )}
            </span>
            <span
              className="absolute left-2 right-2 -bottom-px h-px transition-colors"
              style={{ background: on ? 'var(--color-copper)' : 'transparent' }}
            />
          </button>
        );
      })}
    </div>
  );
}
