/* The external rows under the listing header map, in two questions. WHERE is
 * this: Mapy.cz, Google Maps and iKatastr opened at the listing's resolved point.
 * What does it SELL for: sreality's Cenová mapa for its street / part of town /
 * town. Sits directly below "Explore area" (our own market view of the asking
 * side). Reas.cz was the second price chip until its registered sales became a
 * table on this page (SoldCompsBlock) — a link out to a source we now read is
 * one more place to keep in step, so it went with the wave that read it.
 *
 * Rendered only where a coordinate exists (same gate as the map itself). The
 * point carries the resolver's precision and no more — see lib/geoLinks. The
 * Cenová mapa link is the one that needs a lookup (it addresses places by
 * Seznam's ids); it renders at once with the national map and sharpens when the
 * lookup answers, so a slow or failed lookup never costs the operator a link. */

import { useQuery } from '@tanstack/react-query';

import {
  externalMapLinks,
  srealityPriceMapLink,
  type ExternalMapLink,
} from '@/lib/geoLinks';
import { fetchSrealityPriceMap } from '@/lib/maps';

export default function ExternalMapLinks({
  lat,
  lng,
  label,
}: {
  lat: number;
  lng: number;
  /* The listing's display_label — what the Cenová mapa lookup searches for. */
  label: string | null;
}) {
  const priceMapQ = useQuery({
    queryKey: ['sreality-price-map', label, lat, lng],
    queryFn: ({ signal }) => fetchSrealityPriceMap(label as string, lat, lng, signal),
    enabled: !!label,
    // Seznam's locality ids don't move; the server caches a day as well.
    staleTime: Infinity,
    retry: false,
  });
  const links = [...externalMapLinks(lat, lng), srealityPriceMapLink(priceMapQ.data)];
  /* Two rows rather than one: four equal chips don't fit the 400px map column
   * ("Mapy.cz" already truncated at four across on a 360px phone, measured), and
   * the split is the one the operator reads in anyway. Literal class names, so
   * Tailwind sees them. */
  return (
    <div className="space-y-1.5">
      <ChipRow links={links.filter((l) => l.group === 'place')} className="grid-cols-3" />
      <ChipRow links={links.filter((l) => l.group === 'price')} className="grid-cols-1" />
    </div>
  );
}

function ChipRow({ links, className }: { links: ExternalMapLink[]; className: string }) {
  return (
    <div className={`grid gap-1.5 ${className}`}>
      {links.map((l) => (
        <a
          key={l.key}
          href={l.url}
          target="_blank"
          rel="noopener noreferrer"
          title={l.title}
          className="min-w-0 inline-flex items-center justify-center gap-1 px-2 py-1 text-[0.7rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-3)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)] transition-colors"
        >
          <span className="truncate">{l.label}</span>
          <OutArrow />
        </a>
      ))}
    </div>
  );
}

function OutArrow() {
  return (
    <svg width="9" height="9" viewBox="0 0 10 10" className="shrink-0" aria-hidden>
      <line
        x1="1"
        y1="9"
        x2="8.5"
        y2="1.5"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeLinecap="round"
      />
      <polyline
        points="3.5,1.5 8.5,1.5 8.5,6.5"
        stroke="currentColor"
        strokeWidth="1.25"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
