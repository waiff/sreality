/* Inline rename / recolour / delete for a single tag.
 *
 * Wired into both the CurationBlock TagPicker (per-listing) and the
 * Filters TagsPicker (Browse sidebar). The trigger sits next to each
 * tag row; the popover opens beneath it. Saving issues a PATCH /tags/{id}
 * which preserves listing_tags rows (the join is by tag_id, not name).
 *
 * Invalidates the global tags index plus every per-listing membership
 * cache so renamed/recoloured chips repaint everywhere without a reload.
 */

import { useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { deleteTag, updateTag } from '@/lib/api';
import { curationKeys } from '@/lib/queries';
import type { Tag, TagColor } from '@/lib/types';
import { TAG_COLORS } from '@/lib/types';

interface Props {
  tag: Tag;
  /* Names of every other tag (lowercased) so the form can flag a
   * collision before the server has to. The host already loads the
   * full /tags index for both pickers, so passing this list is free. */
  otherNames: string[];
  /* Callback fired after a successful delete — lets the host close
   * dropdowns or remove the tag from local filter state. */
  onDeleted?: (tagId: number) => void;
}

export default function TagEditPopover({ tag, otherNames, onDeleted }: Props) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <span ref={containerRef} className="relative inline-flex">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        aria-label={`Edit tag ${tag.name}`}
        title="Edit tag"
        className="inline-flex items-center justify-center w-5 h-5 rounded-[var(--radius-xs)] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)] transition-colors"
      >
        <PencilGlyph />
      </button>
      {open && (
        <Popover
          tag={tag}
          otherNames={otherNames}
          onClose={() => setOpen(false)}
          onDeleted={onDeleted}
        />
      )}
    </span>
  );
}

function Popover({
  tag,
  otherNames,
  onClose,
  onDeleted,
}: {
  tag: Tag;
  otherNames: string[];
  onClose: () => void;
  onDeleted?: (tagId: number) => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(tag.name);
  const [color, setColor] = useState<TagColor>(tag.color);
  const [error, setError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const trimmed = name.trim();
  const dup =
    trimmed.length > 0 &&
    trimmed.toLowerCase() !== tag.name.toLowerCase() &&
    otherNames.includes(trimmed.toLowerCase());
  const dirty = trimmed !== tag.name || color !== tag.color;

  const invalidateAll = () => {
    qc.invalidateQueries({ queryKey: curationKeys.tags });
    qc.invalidateQueries({
      predicate: (q) => {
        const k = q.queryKey;
        return (
          Array.isArray(k) &&
          k[0] === 'curation' &&
          (k[1] === 'listing-tags' || k[1] === 'collections')
        );
      },
    });
  };

  const save = useMutation({
    mutationFn: () =>
      updateTag(tag.id, {
        name: trimmed !== tag.name ? trimmed : undefined,
        color: color !== tag.color ? color : undefined,
      }),
    onSuccess: () => {
      setError(null);
      invalidateAll();
      onClose();
    },
    onError: (err: Error) => setError(err.message || 'Failed to save'),
  });

  const del = useMutation({
    mutationFn: () => deleteTag(tag.id),
    onSuccess: () => {
      invalidateAll();
      onDeleted?.(tag.id);
      onClose();
    },
    onError: (err: Error) => setError(err.message || 'Failed to delete'),
  });

  const disabled = !dirty || dup || trimmed.length === 0 || save.isPending;

  return (
    <div
      role="dialog"
      aria-label={`Edit tag ${tag.name}`}
      onClick={(e) => e.stopPropagation()}
      className="absolute z-30 right-0 top-6 w-[18rem] rounded-[var(--radius-md)] bg-[var(--color-paper-3)] border border-[var(--color-rule-strong)] shadow-[0_4px_16px_rgba(0,0,0,0.06)] p-2.5"
    >
      <p className="text-[0.65rem] tracking-[0.18em] uppercase text-[var(--color-ink-4)]">
        Edit tag
      </p>
      <input
        type="text"
        value={name}
        onChange={(e) => setName(e.target.value)}
        maxLength={50}
        autoFocus
        className="mt-1.5 w-full px-2.5 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-rule-strong)]"
      />
      <div className="mt-2 flex items-center gap-1 flex-wrap">
        {TAG_COLORS.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => setColor(c)}
            aria-label={c}
            aria-pressed={color === c}
            className={[
              'w-5 h-5 rounded-full border transition-shadow',
              color === c
                ? 'ring-2 ring-offset-1 ring-offset-[var(--color-paper-3)]'
                : '',
            ].join(' ')}
            style={
              {
                background: `var(--color-tag-${c}-soft)`,
                borderColor: `var(--color-tag-${c})`,
                ['--tw-ring-color' as string]: `var(--color-tag-${c})`,
              } as React.CSSProperties
            }
          />
        ))}
      </div>
      {dup && (
        <p className="mt-1.5 text-[0.7rem] text-[var(--color-brick)]">
          Another tag named "{trimmed}" already exists.
        </p>
      )}
      {error && !dup && (
        <p className="mt-1.5 text-[0.7rem] text-[var(--color-brick)]">{error}</p>
      )}
      <div className="mt-2.5 flex items-center justify-between gap-2">
        {confirmingDelete ? (
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => del.mutate()}
              disabled={del.isPending}
              className="px-2.5 py-1 text-[0.7rem] tracking-wide rounded-[var(--radius-sm)] bg-[var(--color-brick-soft)] text-[var(--color-brick)] hover:bg-[var(--color-brick)]/15 disabled:opacity-50 transition-colors"
            >
              {del.isPending ? 'Deleting…' : `Delete "${tag.name}"`}
            </button>
            <button
              type="button"
              onClick={() => setConfirmingDelete(false)}
              className="px-2 py-1 text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setConfirmingDelete(true)}
            className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] hover:text-[var(--color-brick)] transition-colors"
          >
            Delete tag
          </button>
        )}
        <button
          type="button"
          onClick={() => save.mutate()}
          disabled={disabled}
          className="px-3 py-1 text-[0.75rem] rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {save.isPending ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  );
}

function PencilGlyph() {
  return (
    <svg width="11" height="11" viewBox="0 0 11 11" aria-hidden fill="none">
      <path
        d="M1.5 9.5 L1.5 7.5 L7 2 L9 4 L3.5 9.5 Z"
        stroke="currentColor"
        strokeWidth="1"
        strokeLinejoin="round"
      />
    </svg>
  );
}
