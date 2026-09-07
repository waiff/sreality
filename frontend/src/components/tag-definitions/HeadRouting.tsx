/* Is this tag a HEAD, and for which property types?
 *
 * A tag with no routing categories is not a head: the training-set page, the
 * heads read and the labeler all key on that column. Until now it could only
 * be set by a migration (457 seeded the first eight by hand), which made
 * "add a head" a deploy. This is the click. */
interface Props {
  categories: ReadonlyArray<string>;
  options: ReadonlyArray<string>;
  saving: boolean;
  onChange: (categories: string[]) => void;
}

const LABEL: Record<string, string> = {
  byt: 'byt', dum: 'dům', komercni: 'komerční', pozemek: 'pozemek', ostatni: 'ostatní',
};

export default function HeadRouting({ categories, options, saving, onChange }: Props) {
  const on = new Set(categories);
  const isHead = on.size > 0;
  return (
    <section className="mt-6 border-t border-[var(--color-rule)] pt-4" data-testid="head-routing">
      <h2 className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        Head · {isHead ? `serves ${[...on].map((c) => LABEL[c] ?? c).join(', ')}` : 'not a head'}
      </h2>
      <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">
        Tick the property types this tag routes comparisons for. Any tick makes it a head:
        it appears on the training-set page and the labeler can build its set. No ticks means
        it is a plain tag with no training set.
      </p>
      <div className="mt-2 flex flex-wrap gap-1.5" role="group" aria-label="property types served">
        {options.map((c) => (
          <button
            key={c}
            type="button"
            disabled={saving}
            aria-pressed={on.has(c)}
            onClick={() => {
              const next = new Set(on);
              if (next.has(c)) next.delete(c); else next.add(c);
              onChange([...next]);
            }}
            className={`px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border ${
              on.has(c)
                ? 'border-[var(--color-sage)] bg-[var(--color-sage)]/10 text-[var(--color-ink)]'
                : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'
            } disabled:opacity-40`}
          >
            {LABEL[c] ?? c}
          </button>
        ))}
      </div>
    </section>
  );
}
