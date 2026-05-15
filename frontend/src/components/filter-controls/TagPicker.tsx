/* TagPicker — operator-tag selector with colour chips.
 *
 * Extracted from Browse's `TagsPicker` into filter-controls so both
 * <FilterForm> consumers can share it. Tags are fetched via the
 * existing `listTags` admin endpoint (cached for a minute); the
 * widget renders the selected tags as removable coloured chips and
 * the remaining ones as add-buttons with inline edit popovers.
 *
 * AND-semantics — a listing matches only if it carries every
 * selected tag id. Same predicate the backend's browse_stats and
 * watchdog matchers honour (migrations 055 / 060).
 *
 * Interface mirrors the rest of filter-controls:
 *   value: number[] | null  // null = no constraint; [] is normalised on emission
 *   onChange(next): caller persists.
 */

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';

import { listTags } from '@/lib/api';
import { curationKeys } from '@/lib/queries';
import type { Tag } from '@/lib/types';
import TagEditPopover from '@/components/curation/TagEditPopover';

export function TagPicker({
  value,
  onChange,
}: {
  value: ReadonlyArray<number> | null;
  onChange: (next: number[] | null) => void;
}) {
  const selected = useMemo(() => value ?? [], [value]);

  const tagsQ = useQuery({
    queryKey: curationKeys.tags,
    queryFn: listTags,
    staleTime: 60_000,
  });
  const tags = tagsQ.data?.data ?? [];
  const byId = useMemo(() => {
    const m = new Map<number, Tag>();
    for (const t of tags) m.set(t.id, t);
    return m;
  }, [tags]);

  const remaining = tags.filter((t) => !selected.includes(t.id));

  const emit = (next: number[]) =>
    onChange(next.length === 0 ? null : next);

  const add = (id: number) => emit([...selected, id]);
  const remove = (id: number) => emit(selected.filter((x) => x !== id));

  if (tagsQ.isLoading) {
    return (
      <p className="text-[0.75rem] text-[var(--color-ink-4)]">Loading…</p>
    );
  }

  if (tags.length === 0) {
    return (
      <p className="text-[0.75rem] text-[var(--color-ink-4)]">
        No tags yet. Add one from any listing&apos;s detail page.
      </p>
    );
  }

  return (
    <div>
      {selected.length > 0 && (
        <ul className="flex flex-wrap gap-1.5">
          {selected.map((id) => {
            const t = byId.get(id);
            if (!t) return null;
            return (
              <li key={id}>
                <TagChip t={t} onRemove={() => remove(id)} />
              </li>
            );
          })}
        </ul>
      )}
      {remaining.length > 0 && (
        <div className={selected.length > 0 ? 'mt-2' : ''}>
          <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
            Add
          </p>
          <ul className="mt-1.5 flex flex-wrap gap-1.5">
            {remaining.map((t) => (
              <li key={t.id}>
                <TagAddButton
                  t={t}
                  onAdd={() => add(t.id)}
                  otherNames={tags
                    .filter((x) => x.id !== t.id)
                    .map((x) => x.name.toLowerCase())}
                  onDeleted={(id) => {
                    if (selected.includes(id)) remove(id);
                  }}
                />
              </li>
            ))}
          </ul>
        </div>
      )}
      {selected.length > 0 && (
        <p className="mt-2 text-[0.65rem] text-[var(--color-ink-4)]">
          A listing must carry every selected tag.
        </p>
      )}
    </div>
  );
}

function TagChip({ t, onRemove }: { t: Tag; onRemove: () => void }) {
  return (
    <button
      type="button"
      onClick={onRemove}
      aria-label={`Remove ${t.name}`}
      className="group inline-flex items-center gap-1.5 px-2 py-1 text-xs rounded-[var(--radius-sm)] border transition-colors"
      style={{
        background: `var(--color-tag-${t.color}-soft)`,
        color: `var(--color-tag-${t.color})`,
        borderColor: `var(--color-tag-${t.color})`,
      }}
    >
      <span>{t.name}</span>
      <span aria-hidden className="opacity-60 group-hover:opacity-100">
        ×
      </span>
    </button>
  );
}

function TagAddButton({
  t,
  onAdd,
  otherNames,
  onDeleted,
}: {
  t: Tag;
  onAdd: () => void;
  otherNames: string[];
  onDeleted: (id: number) => void;
}) {
  return (
    <span className="inline-flex items-center gap-0.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] hover:border-[var(--color-rule-strong)] transition-colors">
      <button
        type="button"
        onClick={onAdd}
        className="inline-flex items-center gap-1.5 px-2 py-1 text-xs text-[var(--color-ink-3)] hover:text-[var(--color-ink)]"
      >
        <span
          aria-hidden
          className="w-2 h-2 rounded-full"
          style={{ background: `var(--color-tag-${t.color})` }}
        />
        <span>{t.name}</span>
      </button>
      <span className="pr-0.5">
        <TagEditPopover
          tag={t}
          otherNames={otherNames}
          onDeleted={onDeleted}
        />
      </span>
    </span>
  );
}
