/* AUTODEDUP · one listing, as every validation surface shows it.
 *
 * The comparison IS the page, so the unit of comparison is one component: cover
 * photo, portal, the four attributes that decide a duplicate (price, area,
 * disposition, floor), the life span (first seen → last seen) and whether the
 * advert is still live. A re-listing of the same flat is often identical on
 * every attribute and differs ONLY in that span, which is why the dates are
 * never folded away behind a drawer.
 *
 * LINKS. `source_url` is the per-row fact captured at ingest and is the portal
 * link; the in-app link needs the natural key (`source` + `source_id_native`)
 * or the legacy `sreality_id`, and when the payload carries neither there is no
 * honest in-app destination — so the link is simply absent rather than built
 * from `listings.id`, which no SPA route accepts.
 */

import { Link } from 'react-router-dom';

import type { AutodedupMember } from '@/lib/api';
import { imageSrc } from '@/lib/imageUrl';
import { listingRowPath } from '@/lib/listingUrl';
import { portalLabel } from '@/lib/portals';
import { categoryMainLabel, categoryTypeLabel } from '@/lib/enums';
import { fmtArea, fmtCzk, fmtShortDate } from '@/lib/format';
import { type RoutePath } from '@/lib/routes';

export function memberListingPath(m: AutodedupMember): RoutePath | null {
  return listingRowPath({
    source: m.source,
    source_id_native: m.source_id_native ?? null,
    sreality_id: m.sreality_id ?? null,
    property_id: null,
  });
}

/* The attribute line, in one place so the card and the diff table cannot word
 * the same fact differently. `—` for an absent value: this is an audit surface,
 * and a blank cell reads as "the same as the other one". */
export function memberAttrs(m: AutodedupMember): Array<[string, string]> {
  return [
    ['Cena', fmtCzk(m.price_czk)],
    ['Plocha', fmtArea(m.area_m2)],
    ['Dispozice', m.disposition ?? '—'],
    ['Patro', m.floor == null ? '—' : String(m.floor)],
  ];
}

export default function ListingMini({
  member,
  eager = false,
  className = '',
}: {
  member: AutodedupMember;
  /* The first screenful of a review queue is the whole point of the page, so
   * those covers are decoded immediately; anything below it loads lazily. */
  eager?: boolean;
  className?: string;
}) {
  const inApp = memberListingPath(member);
  const kind = [categoryMainLabel(member.category_main), categoryTypeLabel(member.category_type)]
    .filter((s) => s && s !== '—')
    .join(' · ');
  return (
    <div
      className={`rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] overflow-hidden ${className}`}
    >
      <div className="aspect-[4/3] bg-[var(--color-inset)] overflow-hidden">
        {member.cover ? (
          <img
            src={imageSrc(member.cover)}
            alt=""
            loading={eager ? 'eager' : 'lazy'}
            className="h-full w-full object-cover"
          />
        ) : (
          <div className="h-full w-full flex items-center justify-center text-[0.65rem] text-[var(--color-ink-4)]">
            no photo
          </div>
        )}
      </div>

      <div className="px-2.5 py-2 space-y-1">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-1.5 py-0.5 text-[0.6rem] tracking-[0.08em] uppercase text-[var(--color-ink-2)]">
            {portalLabel(member.source) ?? member.source}
          </span>
          <span
            className={`inline-block h-1.5 w-1.5 rounded-full ${
              member.is_active
                ? 'bg-[var(--color-sage)]'
                : 'bg-[var(--color-ink-4)]'
            }`}
            aria-hidden
          />
          <span className="text-[0.62rem] text-[var(--color-ink-3)]">
            {member.is_active ? 'aktivní' : 'staženo'}
          </span>
          <span className="ml-auto text-[0.62rem] text-[var(--color-ink-4)] tabular-nums">
            {member.n_images} foto
          </span>
        </div>

        {kind && <p className="text-[0.68rem] text-[var(--color-ink-3)]">{kind}</p>}

        <dl className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-[0.7rem]">
          {memberAttrs(member).map(([label, value]) => (
            <div key={label} className="flex items-baseline justify-between gap-1">
              <dt className="text-[var(--color-ink-4)]">{label}</dt>
              <dd className="font-mono tabular-nums text-[var(--color-ink-2)]">{value}</dd>
            </div>
          ))}
        </dl>

        <p className="text-[0.62rem] text-[var(--color-ink-3)] tabular-nums">
          {fmtShortDate(member.first_seen_at)} → {fmtShortDate(member.last_seen_at)}
        </p>

        <p className="flex items-center gap-3 text-[0.65rem]">
          {inApp && (
            <Link
              to={inApp}
              className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
            >
              Detail
            </Link>
          )}
          {member.source_url && (
            <a
              href={member.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
            >
              Na portálu
            </a>
          )}
          <span className="ml-auto font-mono text-[0.6rem] text-[var(--color-ink-4)] tabular-nums">
            #{member.listing_id}
          </span>
        </p>
      </div>
    </div>
  );
}
