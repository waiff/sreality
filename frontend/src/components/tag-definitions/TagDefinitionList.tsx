import { useMemo } from 'react';
import type { NewDedupTag, TagDefinitionStatus } from '@/lib/api';
import { FAMILY_FALLBACK, tagFamily, tagShortLabel } from '@/lib/tagFamily';

/* The left column: every tag, grouped by family, with the one number that says
 * whether its definition is written (`v3`) or not (`—`). Presentational — the
 * page owns selection and every query. */
interface Props {
  tags: ReadonlyArray<NewDedupTag>;
  status: ReadonlyMap<number, TagDefinitionStatus>;
  selectedId: number | null;
  onSelect: (id: number) => void;
  loading: boolean;
}

export default function TagDefinitionList({
  tags,
  status,
  selectedId,
  onSelect,
  loading,
}: Props) {
  const grouped = useMemo(() => {
    const byFamily = new Map<string, NewDedupTag[]>();
    for (const t of tags) {
      const family = tagFamily(t);
      const bucket = byFamily.get(family);
      if (bucket) bucket.push(t);
      else byFamily.set(family, [t]);
    }
    return [...byFamily.entries()]
      .sort((a, b) =>
        a[0] === FAMILY_FALLBACK
          ? 1
          : b[0] === FAMILY_FALLBACK
            ? -1
            : a[0].localeCompare(b[0], 'cs'),
      )
      .map(
        ([family, rows]) =>
          [family, [...rows].sort((a, b) => a.label.localeCompare(b.label, 'cs'))] as const,
      );
  }, [tags]);

  const defined = tags.reduce((n, t) => (status.has(t.id) ? n + 1 : n), 0);

  return (
    <aside className="lg:sticky lg:top-4 lg:max-h-[calc(100dvh-6rem)] lg:overflow-y-auto">
      <p className="text-[0.7rem] font-mono tabular-nums text-[var(--color-ink-3)]">
        {tags.length} tags · {defined} defined
      </p>

      {loading && <p className="mt-3 text-sm text-[var(--color-ink-3)]">Loading…</p>}
      {!loading && tags.length === 0 && (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          No tags yet — the taxonomy is managed from the Labeling page.
        </p>
      )}

      <div className="mt-3 space-y-4">
        {grouped.map(([family, rows]) => (
          <div key={family}>
            <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] mb-1.5">
              {family}
            </p>
            <div className="space-y-0.5">
              {rows.map((t) => {
                const def = status.get(t.id);
                const selected = selectedId === t.id;
                return (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => onSelect(t.id)}
                    aria-current={selected ? 'true' : undefined}
                    className={[
                      'w-full flex items-baseline gap-1.5 px-1.5 py-1 text-left',
                      'rounded-[var(--radius-xs)] border',
                      selected
                        ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)]'
                        : 'border-transparent hover:bg-[var(--color-paper-2)]',
                    ].join(' ')}
                  >
                    <span
                      title={t.label}
                      className={[
                        'min-w-0 flex-1 truncate font-mono text-[0.78rem]',
                        selected
                          ? 'text-[var(--color-copper)]'
                          : t.priority
                            ? 'text-[var(--color-brick)]'
                            : 'text-[var(--color-ink-2)]',
                      ].join(' ')}
                    >
                      {tagShortLabel(t.label)}
                    </span>

                    {!t.active && (
                      <span className="shrink-0 text-[0.65rem] text-[var(--color-ink-4)]">
                        inactive
                      </span>
                    )}

                    {t.priority && (
                      <span className="shrink-0 px-1 py-px text-[0.6rem] rounded-[var(--radius-xs)] border border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[var(--color-brick)]">
                        priority
                      </span>
                    )}
                    {t.ready_for_training && (
                      <span
                        title="Ready for training"
                        className="shrink-0 px-1 py-px text-[0.6rem] rounded-[var(--radius-xs)] border border-[var(--color-sage)] bg-[var(--color-sage-soft)] text-[var(--color-sage)]"
                      >
                        ready
                      </span>
                    )}

                    <span className="shrink-0 w-9 text-right font-mono text-[0.7rem] tabular-nums text-[var(--color-ink-4)]">
                      {t.positive_count}
                    </span>

                    {def ? (
                      <span
                        title={def.means}
                        className="shrink-0 w-6 text-right font-mono text-[0.7rem] tabular-nums text-[var(--color-sage)]"
                      >
                        v{def.version}
                      </span>
                    ) : (
                      <span className="shrink-0 w-6 text-right font-mono text-[0.7rem] text-[var(--color-ink-4)]">
                        —
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}
