/* AUTODEDUP · one listing, as every validation surface shows it.
 *
 * The comparison IS the page, so the unit of comparison is one component: cover
 * photo, portal, the four attributes that decide a duplicate (price, area,
 * disposition, floor), the life span (first seen → last seen) and whether the
 * advert is still live. A re-listing of the same flat is often identical on
 * every attribute and differs ONLY in that span, which is why the dates are
 * never folded away behind a drawer.
 *
 * A COVER THAT WILL NOT LOAD IS A LABELLED TILE, NOT A BLANK ONE. When R2 has
 * no copy of the photo yet, `imageSrc` falls back to the portal's own CDN — and
 * several portals refuse a cross-origin request for it (idnes answers
 * ERR_BLOCKED_BY_ORB), so the <img> fails with no event the page can style. The
 * onError below turns that into a tile that names the portal, which is honest
 * ("this advert's photo is not ours to show") where a blank square reads as
 * "this advert has no photos" — a fact the operator is being asked to weigh.
 *
 * LINKS. `source_url` is the per-row fact captured at ingest and is the portal
 * link; the in-app link needs the natural key (`source` + `source_id_native`)
 * or the legacy `sreality_id`, and when the payload carries neither there is no
 * honest in-app destination — so the link is simply absent rather than built
 * from `listings.id`, which no SPA route accepts.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import type { AutodedupMember } from '@/lib/api';
import ImageCarousel from '@/components/ImageCarousel';
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

/* The cover, or a tile that says why there isn't one. Both states are token
 * colours on the same 4:3 box, so a row of cards never jumps when one photo
 * fails to load. */
export function Cover({
  member,
  eager,
  className = '',
}: {
  member: AutodedupMember;
  eager?: boolean;
  className?: string;
}) {
  const [broken, setBroken] = useState(false);
  const portal = portalLabel(member.source) ?? member.source;
  const missing = !member.cover || broken;
  return (
    <div className={`aspect-[4/3] bg-[var(--color-inset)] overflow-hidden ${className}`}>
      {missing ? (
        <div className="h-full w-full flex flex-col items-center justify-center gap-0.5 px-1 text-center">
          <span className="text-[0.6rem] tracking-[0.08em] uppercase text-[var(--color-ink-3)]">
            {portal}
          </span>
          <span className="text-[0.58rem] text-[var(--color-ink-4)]">
            {member.cover ? 'foto nedostupné' : 'bez fota'}
          </span>
        </div>
      ) : (
        <img
          src={imageSrc(member.cover!)}
          alt=""
          loading={eager ? 'eager' : 'lazy'}
          /* The portal CDN can refuse the request cross-origin; the tile above
           * takes over rather than leaving a broken-image glyph. */
          onError={() => setBroken(true)}
          className="h-full w-full object-cover"
        />
      )}
    </div>
  );
}

/* THE PHOTOS, NOT THE PHOTO. A cover is the weakest evidence a portal offers —
 * two adverts for one flat often share nothing but the floor plan, and two
 * different flats in one development share the cover and nothing else. So the
 * card pages the frames the list statement ships (12), with the same carousel
 * the dialog uses; the album's remaining frames are COUNTED, and the dialog is
 * where they are. A member whose payload carries no gallery (a surface that
 * selects the cover only) falls back to the labelled cover tile rather than
 * rendering an empty box. */
export function MemberGallery({
  member,
  eager,
  className = '',
}: {
  member: AutodedupMember;
  eager?: boolean;
  className?: string;
}) {
  const frames = member.images ?? [];
  if (frames.length === 0) {
    return <Cover member={member} eager={eager} className={className} />;
  }
  const rest = (member.n_images ?? frames.length) - frames.length;
  return (
    <div className={`relative ${className}`}>
      <ImageCarousel
        images={frames.map((img) => ({
          url: imageSrc(img),
          /* No CLIP tag on this surface: both decorations are explicitly null
           * rather than faked into a badge that means nothing. */
          tag: null,
          confidence: null,
          renderScore: null,
        }))}
        aspect="aspect-[4/3]"
        eager={eager}
      >
        {rest > 0 && (
          <span className="absolute top-1 left-1 z-[1] rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-3)]/85 px-1.5 py-0.5 text-[0.58rem] tabular-nums text-[var(--color-ink-2)] backdrop-blur-sm">
            +{rest} fotek v detailu
          </span>
        )}
      </ImageCarousel>
    </div>
  );
}

export default function ListingMini({
  member,
  eager = false,
  className = '',
  dense = false,
}: {
  member: AutodedupMember;
  /* The first screenful of a review queue is the whole point of the page, so
   * those covers are decoded immediately; anything below it loads lazily. */
  eager?: boolean;
  className?: string;
  /* QUEUE GRAIN. A residual row is a decision, and the decision is made on the
   * diff table and the reason — which a pair of 600px hero photos pushes below
   * the fold. Dense puts a 160px thumbnail BESIDE the facts instead; the full
   * photos are one click away on the pair page. */
  dense?: boolean;
}) {
  const inApp = memberListingPath(member);
  const kind = [categoryMainLabel(member.category_main), categoryTypeLabel(member.category_type)]
    .filter((s) => s && s !== '—')
    .join(' · ');
  return (
    <div
      className={`rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] overflow-hidden ${
        dense ? 'flex items-stretch' : ''
      } ${className}`}
    >
      <MemberGallery
        member={member}
        eager={eager}
        /* `self-start`, because a flex child stretches to the row's height by
         * default and the 4:3 box would then be as tall as the facts column
         * beside it — a "thumbnail" the size of the thing it replaced. */
        className={
          dense ? 'w-40 shrink-0 self-start border-r border-[var(--color-rule)]' : ''
        }
      />

      <div className={`px-2.5 py-2 space-y-1 ${dense ? 'min-w-0 flex-1' : ''}`}>
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
