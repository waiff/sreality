/* The "save to collection" panel: every collection as a checkable row,
 * monitored ones first and marked with a bell. Shared by the Browse card's
 * over-photo glyph and the listing header's button, the way
 * <PipelineStageMenu> is shared by every pipeline affordance — three surfaces
 * growing three answers to "which collections is this in, and how do I change
 * that" is the failure mode this file exists to prevent.
 *
 * The panel is the shared <AnchoredPopover> (portalled to <body>, so it escapes
 * the card photo's `overflow-hidden` frame and the card's own <Link>), which
 * owns dismissal: outside pointerdown, Escape (returning focus to the trigger),
 * and the anchor scrolling out of view.
 *
 * Membership is a PROP, not a read: every surface answers it from the one
 * shared member map (curationKeys.propertyCollectionMembers). Writes are here,
 * so both surfaces issue the same call and revalidate through the one helper —
 * which is what keeps a save made in the header visible on the card grid (and
 * in the block below it) without a reload.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import type { RefObject } from 'react';

import AnchoredPopover from '@/components/AnchoredPopover';
import {
  addPropertiesToCollection,
  listCollections,
  removePropertyFromCollection,
} from '@/lib/api';
import { revalidateCollections } from '@/lib/collectionCache';
import { curationKeys } from '@/lib/queries';
import { ROUTES } from '@/lib/routes';

/* The trigger's accessible name, and the panel's — one string so a test that
 * finds the button finds the panel it opens. */
export const COLLECTION_SAVE_LABEL = 'Uložit do kolekce';

export default function CollectionSaveMenu({
  property_id,
  memberIds,
  anchorRef,
  onClose,
  id,
  cohortScoped = false,
}: {
  property_id: number;
  /* Collection ids this property is already in — owned by the caller's read. */
  memberIds: Set<number>;
  anchorRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  /* DOM id, so the trigger's aria-controls can point at the panel. */
  id?: string;
  /* The surrounding cohort is scoped to collections — see revalidateCollections. */
  cohortScoped?: boolean;
}) {
  const qc = useQueryClient();
  const collectionsQ = useQuery({
    queryKey: curationKeys.collections,
    queryFn: listCollections,
    staleTime: 30_000,
  });

  const add = useMutation({
    mutationFn: (cid: number) => addPropertiesToCollection(cid, [property_id]),
    onSuccess: (_, cid) =>
      revalidateCollections(qc, { collection_id: cid, cohortScoped }),
  });
  const remove = useMutation({
    mutationFn: (cid: number) => removePropertyFromCollection(cid, property_id),
    onSuccess: (_, cid) =>
      revalidateCollections(qc, { collection_id: cid, cohortScoped }),
  });
  const pending = add.isPending || remove.isPending;

  // Monitored collections first, then alphabetical.
  const sorted = [...(collectionsQ.data?.data ?? [])].sort(
    (a, b) =>
      (b.monitoring_enabled ? 1 : 0) - (a.monitoring_enabled ? 1 : 0) ||
      a.name.localeCompare(b.name),
  );

  return (
    <AnchoredPopover
      id={id}
      anchorRef={anchorRef}
      onClose={onClose}
      ariaLabel={COLLECTION_SAVE_LABEL}
      className="w-56 p-1.5"
    >
      <p className="px-1.5 py-1 text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
        Save to collection
      </p>
      {collectionsQ.isLoading ? (
        <p className="px-1.5 py-1.5 text-[0.78rem] text-[var(--color-ink-3)]">
          Loading…
        </p>
      ) : collectionsQ.isError ? (
        /* A failed read is NOT an empty one: the old panel answered both with
         * "Create a collection →", which tells an operator whose collections
         * exist that they have none. */
        <p className="px-1.5 py-1.5 text-[0.78rem] text-[var(--color-ink-3)]">
          Kolekce se nepodařilo načíst
        </p>
      ) : sorted.length === 0 ? (
        <Link
          to={ROUTES.collections.build()}
          className="block px-1.5 py-1.5 text-[0.78rem] text-[var(--color-copper)] hover:underline"
        >
          Create a collection →
        </Link>
      ) : (
        <ul className="max-h-60 overflow-y-auto">
          {sorted.map((c) => {
            const member = memberIds.has(c.id);
            return (
              <li key={c.id}>
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => (member ? remove : add).mutate(c.id)}
                  className="w-full flex items-center gap-2 px-1.5 py-1.5 text-left text-[0.82rem] rounded-[var(--radius-xs)] hover:bg-[var(--color-copper-soft)] disabled:opacity-60"
                >
                  <span
                    aria-hidden
                    className={[
                      'inline-flex items-center justify-center w-4 h-4 shrink-0 rounded-[3px] border text-[0.6rem] leading-none',
                      member
                        ? 'bg-[var(--color-copper)] border-[var(--color-copper)] text-white'
                        : 'border-[var(--color-rule-strong)] text-transparent',
                    ].join(' ')}
                  >
                    ✓
                  </span>
                  <span className="truncate text-[var(--color-ink)]">{c.name}</span>
                  {c.monitoring_enabled && (
                    <span
                      title="Monitored — alerts on changes"
                      className="ml-auto shrink-0 text-[var(--color-copper)]"
                    >
                      <BellGlyph />
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </AnchoredPopover>
  );
}

function BellGlyph() {
  return (
    <svg
      width="9"
      height="9"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M8 1.5a3.5 3.5 0 0 0-3.5 3.5c0 3-1.5 4-1.5 4h10s-1.5-1-1.5-4A3.5 3.5 0 0 0 8 1.5ZM6.5 12.5a1.5 1.5 0 0 0 3 0" />
    </svg>
  );
}
