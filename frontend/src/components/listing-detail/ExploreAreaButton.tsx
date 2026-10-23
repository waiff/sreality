/* "Explore area" under the header map (operator 2026-10-23: back, as the twin of the
 * neighbourhood tile's "Prozkoumat okolí"): the same opener over the same scope, so both
 * open the explore window on one sample, lens and dashboard (./neighbourhoodExplore).
 * Lazy, like the module it opens through: the property page is in the first visit. */
import type { PropertyPublic } from '@/lib/queries';
import type { ImagePublic } from '@/lib/types';
import { useExploreNeighbourhood } from './neighbourhoodExplore';

export default function ExploreAreaButton({
  property,
  images,
}: {
  property: PropertyPublic;
  images: ImagePublic[];
}) {
  const explore = useExploreNeighbourhood(property, images);
  if (!explore) return null;
  return (
    <button
      type="button"
      onClick={explore}
      className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 text-[0.8rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-2)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)] transition-colors"
      title="Celý Browse nad okolím této nemovitosti: mapa, inzeráty a statistiky; totéž co „Prozkoumat okolí“"
    >
      <MapPinGlyph />
      <span>Explore area</span>
    </button>
  );
}

function MapPinGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M8 1.5c2.5 0 4.5 2 4.5 4.5 0 3-4.5 8-4.5 8S3.5 9 3.5 6C3.5 3.5 5.5 1.5 8 1.5z"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      <circle cx="8" cy="6" r="1.6" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}
