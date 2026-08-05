import type { GrainNoticeState, GrainVariant } from '@/lib/browseLayout';

/* What a Browse row MEANS, stated instead of left to be inferred. Browse reads
 * two different relations depending on the portal filter, and they answer
 * different questions — so the same cohort legitimately produces different rows:
 *
 *   one portal  → listing_feed_public, LISTING grain. That portal's own posts
 *                 in that portal's own order (migration 370). Two posts for the
 *                 same flat are two rows, because on the portal they are two.
 *   otherwise   → browse_list / properties_map_mv, PROPERTY grain. One row per
 *                 property, and where several portals carry it the displayed
 *                 record is consolidated: price / disposition / category from
 *                 the representative child (active-first, then source trust),
 *                 every other field from the golden-record CTEs in
 *                 recompute_property_stats.py, which take the best NON-NULL
 *                 value per field in trust order. So a card can legitimately
 *                 mix values from several portals and match no single portal's
 *                 page — 9.4 percent of multi-portal active properties even
 *                 disagree on price outright (median spread 7.4 percent,
 *                 measured live 2026-08-05).
 *
 * Dismissible per variant and remembered per browser: orientation for the first
 * encounter, not a warning worth re-reading daily. */
export default function RowGrainNotice({
  portalMirror,
  notice,
}: {
  /* The single selected portal, or null when zero or several are selected —
   * which is exactly the condition that picks the relation, so it also picks
   * the copy. */
  portalMirror: string | null;
  notice: GrainNoticeState;
}) {
  const variant: GrainVariant = portalMirror ? 'mirror' : 'merged';
  if (notice.dismissed(variant)) return null;

  return (
    <div
      role="note"
      className="mt-3 flex items-start gap-2.5 px-3 py-2 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[0.8rem] leading-relaxed text-[var(--color-ink-2)]"
    >
      {/* U+2139 renders as tofu in the Inter stack — inline SVG, like every
        * other icon in this tree. */}
      <svg
        width="14"
        height="14"
        viewBox="0 0 14 14"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        aria-hidden
        className="shrink-0 mt-1 text-[var(--color-copper)]"
      >
        <circle cx="7" cy="7" r="6" />
        <path d="M7 6.4v3.4M7 4.1v.1" />
      </svg>
      <p className="min-w-0 flex-1">
        {variant === 'mirror' ? (
          <>
            <strong className="font-medium text-[var(--color-ink)]">
              Mirroring {portalMirror}.
            </strong>{' '}
            Each row is a single {portalMirror} listing, ordered the way {portalMirror}{' '}
            orders it, showing that listing's own data — so a property posted twice on{' '}
            {portalMirror} appears twice here, just as it does there.
          </>
        ) : (
          <>
            <strong className="font-medium text-[var(--color-ink)]">
              One row per property.
            </strong>{' '}
            A property carried by several portals appears once, as a merged record — price
            and layout from its most trusted live listing, other fields the best value
            found across portals — so a value here can differ from a given portal's page.
            Pick a single portal to see that portal's own listings instead.
          </>
        )}
      </p>
      <button
        type="button"
        onClick={() => notice.dismiss(variant)}
        aria-label="Dismiss this note"
        title="Dismiss"
        className="shrink-0 -mr-1 px-1 text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)] transition-colors"
      >
        ×
      </button>
    </div>
  );
}
