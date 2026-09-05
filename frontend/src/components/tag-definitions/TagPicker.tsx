import { forwardRef, useMemo } from 'react';
import type { NewDedupTag } from '@/lib/api';
import { FAMILY_FALLBACK, tagFamily, tagShortLabel } from '@/lib/tagFamily';

/* An id-keyed picker over the taxonomy. A definition points at OTHER TAGS by
 * tag_id, never by label text, so a rename can't rot it — which rules out the
 * free-text-creating LabelCombobox the labeling page uses.
 *
 * Native <select> on purpose: an overlay listbox inside this page's two
 * independently scrolling columns is exactly the overflow-clipping / stacking
 * failure this codebase has hit before, and a native control is keyboard- and
 * jsdom-native for free. */
interface Props {
  value: number | null;
  onChange: (id: number | null) => void;
  tags: ReadonlyArray<NewDedupTag>;
  /* Tags this row must not offer — the subject tag itself, plus every tag
   * already picked in a sibling row. */
  excludeIds?: ReadonlyArray<number>;
  allowEmpty?: boolean;
  ariaLabel: string;
  className?: string;
}

const TagPicker = forwardRef<HTMLSelectElement, Props>(function TagPicker(
  { value, onChange, tags, excludeIds = [], allowEmpty = false, ariaLabel, className = '' },
  ref,
) {
  const groups = useMemo(() => {
    const excluded = new Set(excludeIds);
    // The current value always stays offerable, even if a sibling row grabbed
    // it — otherwise re-rendering would silently blank this row's selection.
    const usable = tags.filter((t) => !excluded.has(t.id) || t.id === value);
    const byFamily = new Map<string, NewDedupTag[]>();
    for (const t of usable) {
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
  }, [tags, excludeIds, value]);

  return (
    <select
      ref={ref}
      aria-label={ariaLabel}
      value={value == null ? '' : String(value)}
      onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
      className={[
        'min-w-0 px-1.5 py-1 text-[0.76rem] font-mono rounded-[var(--radius-xs)]',
        'border border-[var(--color-rule)] bg-[var(--color-inset)] text-[var(--color-ink-2)]',
        ' focus-visible:border-[var(--color-copper)]',
        className,
      ].join(' ')}
    >
      <option value="">{allowEmpty ? '— no tag —' : '— pick a tag —'}</option>
      {groups.map(([family, rows]) => (
        <optgroup key={family} label={family}>
          {rows.map((t) => (
            <option key={t.id} value={String(t.id)}>
              {tagShortLabel(t.label)}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  );
});

export default TagPicker;
