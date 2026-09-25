/* The save-to-collection control for ONE property, sized for the listing-detail
 * header action bar (between the pipeline button and "New estimation") — the
 * same verb as the Browse card's bookmark glyph, in the header's labelled shape.
 *
 * Orthogonal to the pipeline on purpose (rule #22): the funnel is the single
 * deal state, this is m2m membership, and a monitored collection is what turns
 * membership into change alerts (the bell in the menu). Copper stays the
 * pipeline's accent — out of every collection this button is neutral, and only
 * the SAVED state borrows the soft copper tint the card glyph already uses.
 *
 * Membership reads the ONE shared member map (the CurationBlock under the
 * description and every Browse card glyph subscribe to the same key), so a save made in
 * any of them is immediately true in the others.
 */

import { useCallback, useId, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import CollectionMark from '@/components/CollectionMark';
import CollectionSaveMenu, {
  COLLECTION_SAVE_LABEL,
} from '@/components/CollectionSaveMenu';
import { curationKeys, fetchPropertyCollectionMemberSet } from '@/lib/queries';

export default function CollectionSaveToggle({
  property_id,
}: {
  property_id: number;
}) {
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  /* Stable so the popover's positioning effect doesn't re-subscribe each render. */
  const close = useCallback(() => setOpen(false), []);

  const membershipQ = useQuery({
    queryKey: curationKeys.propertyCollectionMembers,
    queryFn: fetchPropertyCollectionMemberSet,
    staleTime: 30_000,
  });
  const memberIds = new Set(membershipQ.data?.get(property_id) ?? []);
  const count = memberIds.size;

  /* Matches PipelineToggle's skeleton so the two never jump relative to each
   * other while the header's reads land. */
  if (membershipQ.isLoading) {
    return (
      <span
        className="inline-flex h-[1.9rem] w-36 animate-pulse rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]"
        aria-hidden
      />
    );
  }

  /* No aria-label: the visible text IS the name. An aria-label saying "Uložit do
   * kolekce" over a button reading "V kolekci" is exactly the label-in-name
   * mismatch the interactive-semantics program exists to catch — voice control
   * would be asking for a phrase the screen never shows. The verb lives in the
   * title and in the panel this opens, which is named "Uložit do kolekce"
   * whatever the trigger currently reads. */
  return (
    <>
      <button
        ref={btnRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        title={
          count > 0
            ? `${COLLECTION_SAVE_LABEL} — v ${count} ${count === 1 ? 'kolekci' : 'kolekcích'}`
            : COLLECTION_SAVE_LABEL
        }
        className={[
          'inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border px-3 py-1.5 text-[0.8rem] transition-colors',
          count > 0
            ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
            : 'border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-2)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)]',
        ].join(' ')}
      >
        <CollectionMark filled={count > 0} className="h-4 w-4" />
        <span>
          {count === 0
            ? COLLECTION_SAVE_LABEL
            : count === 1
              ? 'V kolekci'
              : `V kolekcích · ${count}`}
        </span>
      </button>
      {open && (
        <CollectionSaveMenu
          id={panelId}
          property_id={property_id}
          memberIds={memberIds}
          anchorRef={btnRef}
          onClose={close}
        />
      )}
    </>
  );
}
