/* Every listing belongs to exactly one `properties` row (migration 091,
 * NOT NULL since the backfill) — this chip is the one place a listing's page
 * points at that canonical real-world property. Always rendered once the
 * property id resolves, singleton or multi-portal: consistency beats hiding
 * it for the common case, and CurationBlock / PipelineToggle already read the
 * property_id unconditionally the same way.
 *
 * The property page (/property/:id) is the one place the aggregate view lives
 * — the listing's own images/description/gallery stay scoped to THIS advert
 * (fetchImagesByListing / listingsQ already filter by sreality_id; see
 * PropertyDetail's SourcesList for the per-source, non-pooled breakdown). */
import { Link } from 'react-router-dom';

export function PropertyLinkChip({
  propertyId,
  sourceCount,
}: {
  propertyId: number | null;
  sourceCount: number;
}) {
  if (propertyId == null) return null;
  return (
    <Link
      to={`/property/${propertyId}`}
      title="Zobrazit nemovitost (sloučené záznamy ze všech portálů)"
      className="inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-3)] px-3 py-1.5 text-[0.8rem] text-[var(--color-ink-2)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper-2)] transition-colors"
    >
      <span className="text-[var(--color-ink-3)]">Nemovitost:</span>
      <span className="tabular-nums">#{propertyId}</span>
      {sourceCount > 1 && (
        <span className="text-[var(--color-ink-4)]">· {sourceCount}×</span>
      )}
      <OutArrow />
    </Link>
  );
}

function OutArrow() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
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
