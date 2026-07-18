/* The property's child listings (property_sources_public, migration 093), one
 * card per portal observation — each with ONLY its own thumbnails/price/dates,
 * never a pooled cross-source gallery. This is the deliberate alternative to
 * merging attribution: a merge groups records, it never blends their content
 * (rule #15's forensic-verdict-only auto-merge gate exists for the same
 * reason — never guess which pixels belong to which advert). Follow the "View
 * listing" link for that source's own full detail page (its own images,
 * description, price/URL history — /listing/:sreality_id already scopes all of
 * that by sreality_id, see ListingDetail + fetchImagesByListing). */
import { Link } from 'react-router-dom';
import { imageSrc } from '@/lib/imageUrl';
import { fmtCzk, fmtShortDate } from '@/lib/format';
import { portalShort, portalListingUrl, type SrealityCategory } from '@/lib/portals';
import { listingPath } from '@/lib/listingUrl';
import type { ImagePublic, PropertySource } from '@/lib/types';

export function SourcesList({
  sources,
  imagesBySource,
  category,
}: {
  sources: PropertySource[];
  imagesBySource: Map<number, ImagePublic[]>;
  category: SrealityCategory;
}) {
  if (sources.length === 0) return null;
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <SectionLabel>Sources</SectionLabel>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
          {sources.length} {sources.length === 1 ? 'listing' : 'listings'}
        </p>
      </div>
      <ul className="mt-3 space-y-2">
        {sources.map((s) => (
          <SourceCard
            key={`${s.source}-${s.sreality_id}`}
            source={s}
            images={imagesBySource.get(s.sreality_id) ?? []}
            category={category}
          />
        ))}
      </ul>
    </div>
  );
}

function SourceCard({
  source,
  images,
  category,
}: {
  source: PropertySource;
  images: ImagePublic[];
  category: SrealityCategory;
}) {
  const external = portalListingUrl(
    source.source,
    source.source_url,
    source.source_id_native ?? source.sreality_id,
    category,
  );
  const thumbs = images.slice(0, 4);
  return (
    <li className="flex gap-3 rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper-2)] p-3">
      <div className="flex shrink-0 gap-1">
        {thumbs.length > 0 ? (
          thumbs.map((img) => (
            <img
              key={img.id}
              src={imageSrc(img)}
              alt=""
              loading="lazy"
              className="h-14 w-14 rounded-[var(--radius-xs)] object-cover border border-[var(--color-rule)]"
            />
          ))
        ) : (
          <div className="h-14 w-14 rounded-[var(--radius-xs)] border border-dashed border-[var(--color-rule)] flex items-center justify-center text-[0.55rem] text-[var(--color-ink-4)] text-center px-1">
            no photos
          </div>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm text-[var(--color-ink)] capitalize">
            {portalShort(source.source)}
          </span>
          <StatusPill isActive={source.is_active} />
        </div>
        <p className="mt-0.5 font-mono tabular-nums text-sm text-[var(--color-ink)]">
          {source.price_czk != null ? fmtCzk(source.price_czk) : 'Cena na vyžádání'}
        </p>
        <p className="text-[0.72rem] text-[var(--color-ink-3)] tabular-nums">
          {fmtShortDate(source.first_seen_at)} –{' '}
          {source.is_active ? 'now' : fmtShortDate(source.last_seen_at)}
        </p>
        <div className="mt-1.5 flex flex-wrap items-center gap-3 text-[0.75rem]">
          <Link
            to={listingPath(source.sreality_id)}
            className="text-[var(--color-copper)] hover:text-[var(--color-copper-2)]"
          >
            View listing →
          </Link>
          {external && (
            <a
              href={external}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[var(--color-ink-3)] hover:text-[var(--color-copper)]"
            >
              open portal ↗
            </a>
          )}
        </div>
      </div>
    </li>
  );
}

function StatusPill({ isActive }: { isActive: boolean }) {
  return (
    <span
      className={[
        'inline-block px-1.5 py-0.5 text-[0.6rem] tracking-wide uppercase rounded-[var(--radius-xs)] border',
        isActive
          ? 'border-[var(--color-sage)]/40 text-[var(--color-sage)]'
          : 'border-[var(--color-rule)] text-[var(--color-ink-4)]',
      ].join(' ')}
    >
      {isActive ? 'active' : 'inactive'}
    </span>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
      {children}
    </p>
  );
}
