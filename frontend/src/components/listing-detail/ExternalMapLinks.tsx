/* The external-map row under the listing header map: Mapy.cz, Google Maps and
 * iKatastr, each opened at this listing's resolved point. Sits directly below
 * "Explore area" (our own market view) — that button explores the NEIGHBOURHOOD,
 * these three answer "where exactly is this, and on whose parcel".
 *
 * Rendered only where a coordinate exists (same gate as the map itself). The
 * point carries the resolver's precision and no more — see lib/geoLinks. */

import { externalMapLinks } from '@/lib/geoLinks';

export default function ExternalMapLinks({
  lat,
  lng,
}: {
  lat: number;
  lng: number;
}) {
  const links = externalMapLinks(lat, lng);
  return (
    <div className="flex items-stretch gap-1.5">
      {links.map((l) => (
        <a
          key={l.key}
          href={l.url}
          target="_blank"
          rel="noopener noreferrer"
          title={l.title}
          className="flex-1 min-w-0 inline-flex items-center justify-center gap-1 px-2 py-1 text-[0.7rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-3)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)] transition-colors"
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
