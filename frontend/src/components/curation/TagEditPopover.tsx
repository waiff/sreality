/* Inline rename / recolour / delete for a single tag.
 *
 * Wired into both the CurationBlock TagPicker (per-listing) and the
 * Filters TagsPicker (Browse sidebar). The trigger sits next to each
 * tag row; the popover opens beneath it. Saving issues a PATCH /tags/{id}
 * which preserves listing_tags rows (the join is by tag_id, not name).
 *
 * Invalidates the global tags index plus every per-listing membership
 * cache so renamed/recoloured chips repaint everywhere without a reload.
 *
 * NOT A MODAL, and it never was one: it announced `role="dialog"` with no
 * `aria-modal`, which is the announcement without any of the behaviour — no
 * focus trap, nothing inert behind it, and the page still scrolling. It is a
 * disclosure hung off a trigger, so it is now the shared
 * <AnchoredPopover>: portalled to <body>, named `role="group"`, focus into the
 * name field on open and back to the pencil on close, and dismissal (outside
 * pointerdown, Escape, the anchor scrolling out of view) owned in one place.
 * Its own document mousedown + keydown listeners are gone with it.
 *
 * The portal is not cosmetic here. In CurationBlock the pencil sits inside the
 * "Add tag" dropdown's `max-h-56 overflow-y-auto` listbox, which CLIPPED this
 * panel — the reason AnchoredPopover exists (see its header, which names this
 * file as one of the absolute popovers that only worked where nothing clipped).
 */

import { useCallback, useId, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { deleteTag, updateTag } from '@/lib/api';
import { curationKeys } from '@/lib/queries';
import type { Tag, TagColor } from '@/lib/types';
import AnchoredPopover from '@/components/AnchoredPopover';
import TagColorPicker from '@/components/TagColorPicker';
import { PencilIcon } from '@/components/icons';

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
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  /* Stable, so AnchoredPopover's positioning effect does not re-subscribe on
   * every render of the tag row. */
  const close = useCallback(() => setOpen(false), []);

  return (
    /* No `relative` any more: the panel is portalled, so a positioning context
     * here would position nothing. The span stays as the inline box the two
     * hosts wrap. */
    <span className="inline-flex">
      <button
        ref={btnRef}
        type="button"
        /* No `stopPropagation`. It guarded against a click handler on an
         * ancestor, and neither host has one — the pencil's neighbour is a
         * SIBLING button in both (CurationBlock's listbox row, the sidebar's
         * add-tag chip). It could not have guarded what actually threatens
         * this popover, a document-level listener, anyway. */
        onClick={() => setOpen((v) => !v)}
        aria-label={`Edit tag ${tag.name}`}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        title="Edit tag"
        className="inline-flex items-center justify-center w-5 h-5 rounded-[var(--radius-xs)] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)] transition-colors"
      >
        <PencilIcon className="h-[11px] w-[11px]" />
      </button>
      {open && (
        <AnchoredPopover
          id={panelId}
          anchorRef={btnRef}
          onClose={close}
          ariaLabel={`Edit tag ${tag.name}`}
          className="w-[18rem] p-2.5"
        >
          <Popover
            tag={tag}
            otherNames={otherNames}
            onClose={close}
            onDeleted={onDeleted}
          />
        </AnchoredPopover>
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
  const captionId = useId();

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

  /* The chrome — the fixed positioning, the surface, the width, the name — is
   * AnchoredPopover's. What is left here is the form. The panel-wide
   * `onClick={(e) => e.stopPropagation()}` went with it: AnchoredPopover
   * deliberately suppresses nothing (its header explains why), and neither
   * host puts this inside anything clickable. */
  return (
    <>
      <p id={captionId} className="text-[0.65rem] tracking-[0.18em] uppercase text-[var(--color-ink-4)]">
        Edit tag
      </p>
      <input
        type="text"
        aria-labelledby={captionId}
        value={name}
        onChange={(e) => setName(e.target.value)}
        maxLength={50}
        /* No `autoFocus`: AnchoredPopover moves focus to the first control in
         * the panel on mount — this input — and back to the pencil on close. */
        className="mt-1.5 w-full px-2.5 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)]"
      />
      <div className="mt-2 flex items-center gap-1 flex-wrap">
        <TagColorPicker value={color} onChange={(c) => c && setColor(c)} size="sm" />
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
    </>
  );
}
