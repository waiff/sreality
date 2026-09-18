/* AUTODEDUP · every advert of a set, as a grid of paged galleries.
 *
 * THE GRID SHOWS EVERY ADVERT. It used to render four and count the rest ("+1
 * further advert…"), and the operator read that as the group being smaller than
 * its own header said — "I see only 4 adverts here while it says there should be
 * 5" — which on a surface whose entire question is *are these the same flat?* is
 * the one thing it may not do. All members render as full cards in the same grid
 * (four to a row at lg, wrapping); past twelve the remainder folds behind one
 * button that expands it IN PLACE — not a count and not a link to a drawer,
 * because a split sends every member and a letter set over adverts nobody can
 * see is a ruling by omission. Any set being ruled unit by unit un-folds itself.
 *
 * EACH CARD PAGES ITS PHOTOS, it does not show a cover. A cover is the weakest
 * evidence a portal offers — two adverts for one flat often share only the floor
 * plan, and two different flats in one development share the cover and nothing
 * else — so every member ships its first 12 frames and the card runs the same
 * carousel the dialog does.
 */

import { useState, type ReactNode } from 'react';

import type { AutodedupMember } from '@/lib/api';
import { fmtCount } from '@/lib/format';
import ListingMini from '@/components/autodedup/ListingMini';

/* Four fit a row at lg; the rest wrap onto the next one. Past this many the card
 * would be a page of its own, so the remainder folds. */
export const MEMBERS_BEFORE_FOLD = 12;
/* The photos above the fold are decoded immediately, the rest lazily — a
 * twelve-member group must not fire twelve eager requests. */
export const EAGER_MEMBERS = 2;

export default function MemberGrid({
  members,
  eager = false,
  /* A set being ruled unit by unit is never folded, whatever its size: the
   * assignment travels WHOLE. */
  unfolded = false,
  foldOver = MEMBERS_BEFORE_FOLD,
  /* The per-member control (a unit select, usually). Given the member so one
   * grid serves a card that offers a letter per advert and one that does not. */
  renderUnder,
  columns = 'sm:grid-cols-2 lg:grid-cols-4',
}: {
  members: ReadonlyArray<AutodedupMember>;
  eager?: boolean;
  unfolded?: boolean;
  foldOver?: number;
  renderUnder?: (member: AutodedupMember, index: number) => ReactNode;
  columns?: string;
}) {
  /* The fold is per grid and lives in the grid: expanding one set must not
   * expand the next, and collapsing on a re-render would fight the operator. */
  const [expanded, setExpanded] = useState(false);
  const folded = members.length > foldOver && !expanded && !unfolded;
  const shown = folded ? members.slice(0, foldOver) : members;
  const hidden = members.length - shown.length;
  return (
    <>
      <div className={`grid gap-3 ${columns}`}>
        {shown.map((m, i) => (
          <div key={m.listing_id} className="space-y-1">
            {/* Only the first frames of the first cards are decoded eagerly: a
              * twelve-member group otherwise fires twelve requests at once. */}
            <ListingMini member={m} eager={eager && i < EAGER_MEMBERS} />
            {renderUnder?.(m, i)}
          </div>
        ))}
      </div>
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-3 py-1.5 text-[0.72rem] text-[var(--color-ink-2)] hover:text-[var(--color-ink)]"
        >
          zobrazit všech {fmtCount(members.length)} inzerátů
        </button>
      )}
    </>
  );
}
