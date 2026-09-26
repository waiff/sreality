/* "Sloučené inzeráty" — every advert this property is made of, one row each: THE
 * list of adverts on the property page (decision 11), a singleton's one advert
 * included. The header above speaks with the canonical advert; each advert's own
 * facts are here, in its row.
 *
 * The operator's brief: a Browse-like overview of which listings were merged
 * into the property, one expandable row per advert, the photos visible even
 * collapsed and the description on expand. Collapsed, a row answers "which
 * advert is this" (portal, price, area and disposition, the span it was seen
 * over, its first photos, the link out); expanded, it answers "is it really the
 * same flat" (every photo, the advert's own words, its broker). An old advert
 * address lands here with that advert's row open.
 *
 * Built from parts the review pages already trust rather than a second gallery:
 * ImageCarousel for the photos, MissingPhotoTile for a frame a portal refuses,
 * MemberText for the description (its unit-token marks — "byt č. 14", "ve
 * 4. patře" — are exactly what separates two units of one building). The link
 * out is the row's stored `source_url`, never rebuilt.
 *
 * Admin sessions also see where each advert came from (the merge ledger's origin,
 * on expand) and a per-row two-step 'Rozdělit' on every advert a detach would
 * move: exactly that advert goes back to its origin — or, if no merge brought it,
 * to a new record of its own — any property size, any merge origin, with an
 * optional free-text reason kept on the "different" ruling. */

import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import ImageCarousel from '@/components/ImageCarousel';
import MemberText from '@/components/autodedup/MemberText';
import { MissingPhotoTile } from '@/components/autodedup/ListingMini';
import { SectionLabel } from '@/components/section';
import {
  DETACH_REASON_MAX,
  detachListing,
  fetchPropertyOrigins,
  type AdvertOrigin,
} from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { fetchListingBroker } from '@/lib/brokers';
import { fmtArea, fmtCount, fmtCzk, fmtDateSlash, fmtFloor } from '@/lib/format';
import { taggedImageUrls, useListingPhotos } from '@/lib/hydration/useCardHydration';
import { imageSrc } from '@/lib/imageUrl';
import { propertyPath } from '@/lib/listingUrl';
import { areaKindOf } from '@/lib/measure';
import {
  STATE_STAYS,
  detachOutcomeNote,
  inzeratu,
  mergeOriginLabel,
  mergedAdvertsKeys,
  refreshAfterDetach,
  unmovedReason,
} from '@/lib/mergedAdverts';
import { portalLabel } from '@/lib/portals';
import { fetchListingsForListingIds } from '@/lib/queries';
import { ROUTES, withQuery } from '@/lib/routes';
import { pushToast } from '@/lib/toast';
import type { ImagePublic, ListingPublic, PropertySource } from '@/lib/types';

/* Photos a collapsed row shows before counting the rest. */
export const COLLAPSED_THUMBS = 6;
/* A client-side retention cap over one read of every advert's album — high
 * enough that no real album is cut, finite so the hydration key stays a number. */
const PHOTOS_PER_ADVERT = 200;

/* An admin session's read of one advert's origin; null for any other session. */
type OriginRead = { status: 'pending' | 'error' | 'success'; origin: AdvertOrigin | undefined };

interface SectionProps {
  propertyId: number;
  /* The advert the page's header speaks with — marked. */
  canonicalListingId: number;
  sources: PropertySource[];
  /* The advert an old advert address asked for: its row opens. */
  openAdvertId?: number | null;
}

export default function MergedAdvertsSection({
  propertyId,
  canonicalListingId,
  sources,
  openAdvertId,
}: SectionProps) {
  const { isAdmin } = useAuth();
  const ids = useMemo(() => sources.map((s) => s.id), [sources]);
  const originsQ = useQuery({
    queryKey: mergedAdvertsKeys.origins(propertyId),
    queryFn: () => fetchPropertyOrigins(propertyId),
    enabled: isAdmin,
    staleTime: 30_000,
  });

  /* Area, disposition, floor and the description — per advert, from
   * listings_public. One request for the set. */
  const detailsQ = useQuery<Map<number, ListingPublic>, Error>({
    queryKey: mergedAdvertsKeys.listings(ids),
    queryFn: () => fetchListingsForListingIds(ids),
    staleTime: 60_000,
  });
  /* Every advert's album in one read (the shared card-photo hydration), so the
   * collapsed strip and the expanded carousel are one cache entry. */
  const { photos, isPending: photosPending } = useListingPhotos(ids, PHOTOS_PER_ADVERT);

  const portals = new Set(sources.map((s) => s.source)).size;

  return (
    <section aria-label="Sloučené inzeráty">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <SectionLabel>{sources.length > 1 ? 'Sloučené inzeráty' : 'Inzerát'}</SectionLabel>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
          {fmtCount(sources.length)} {inzeratu(sources.length)} · {fmtCount(portals)}{' '}
          {portals === 1 ? 'portál' : portals <= 4 ? 'portály' : 'portálů'}
        </p>
      </div>
      <p className="mt-1 text-[0.75rem] text-[var(--color-ink-3)]">
        Inzeráty, které tvoří tuto nemovitost. Rozbalte řádek pro popis, všechny
        fotky a makléře.
        {isAdmin && (
          <>
            {' '}
            <Link
              to={withQuery(ROUTES.autodedupRulings.build(), { property: propertyId })}
              className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
            >
              Rozhodnutí o těchto inzerátech
            </Link>
          </>
        )}
      </p>
      {detailsQ.isError && (
        <p className="mt-2 text-[0.75rem] text-[var(--color-brick)]">
          Údaje inzerátů se nepodařilo načíst: {detailsQ.error.message}
        </p>
      )}
      <ul className="mt-4 space-y-2">
        {sources.map((s) => (
          <MergedAdvertRow
            key={s.id}
            source={s}
            detail={detailsQ.data?.get(s.id) ?? null}
            detailsLoading={detailsQ.isLoading}
            images={photos.get(s.id) ?? []}
            imagesLoading={photosPending}
            isCanonical={s.id === canonicalListingId}
            opened={s.id === openAdvertId}
            propertyId={propertyId}
            originRead={
              isAdmin
                ? {
                    status: originsQ.status,
                    origin: originsQ.data?.adverts.find((a) => a.listing_id === s.id),
                  }
                : null
            }
          />
        ))}
      </ul>
    </section>
  );
}

function priceLabel(price: number | null, categoryType: string | null | undefined): string {
  if (price == null) return 'cena neuvedena';
  return categoryType === 'pronajem' ? `${fmtCzk(price)} / měs` : fmtCzk(price);
}

function MergedAdvertRow({
  source,
  detail,
  detailsLoading,
  images,
  imagesLoading,
  isCanonical,
  opened,
  propertyId,
  originRead,
}: {
  source: PropertySource;
  detail: ListingPublic | null;
  detailsLoading: boolean;
  images: ImagePublic[];
  imagesLoading: boolean;
  isCanonical: boolean;
  opened: boolean;
  propertyId: number;
  originRead: OriginRead | null;
}) {
  const [expanded, setExpanded] = useState(opened);
  const rowRef = useRef<HTMLLIElement | null>(null);
  useEffect(() => {
    if (opened) rowRef.current?.scrollIntoView?.({ block: 'start' });
  }, [opened]);
  const [detachArmed, setDetachArmed] = useState(false);
  const origin = originRead?.origin?.splittable ? originRead.origin : null;
  /* Why a row of a bigger property offers no split (an advert alone has nothing to leave). */
  const unmoved =
    originRead?.origin && !originRead.origin.splittable && originRead.origin.detach_outcome !== 'not_merged'
      ? originRead.origin
      : null;
  const panelId = `merged-advert-${source.id}`;
  const portal = portalLabel(source.source) ?? source.source;
  const facts = [
    detail?.area_m2 != null ? fmtArea(detail.area_m2, areaKindOf(detail.category_main)) : null,
    detail?.disposition ?? null,
  ].filter(Boolean);

  return (
    <li
      ref={rowRef}
      className={[
        'scroll-mt-6 rounded-[var(--radius-sm)] border bg-[var(--color-paper-2)]',
        isCanonical ? 'border-[var(--color-rule-strong)]' : 'border-[var(--color-rule-soft)]',
      ].join(' ')}
    >
      <div className="px-3 py-2">
        <div className="flex items-start gap-2">
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={panelId}
            onClick={() => setExpanded((v) => !v)}
            className="min-w-0 flex-1 text-left"
          >
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <Chevron open={expanded} />
              <span className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-1.5 py-0.5 text-[0.62rem] tracking-[0.08em] uppercase text-[var(--color-ink-2)]">
                {portal}
              </span>
              <span className="inline-flex items-center gap-1 text-[0.7rem] text-[var(--color-ink-3)]">
                <span
                  aria-hidden
                  className={`inline-block h-1.5 w-1.5 rounded-full ${
                    source.is_active ? 'bg-[var(--color-sage)]' : 'bg-[var(--color-ink-4)]'
                  }`}
                />
                {source.is_active ? 'aktivní' : 'staženo'}
              </span>
              {isCanonical && (
                <span
                  title="Záhlaví stránky ukazuje údaje tohoto inzerátu"
                  className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]"
                >
                  v záhlaví
                </span>
              )}
              <span className="font-mono text-[0.85rem] tabular-nums text-[var(--color-ink)]">
                {priceLabel(source.price_czk, detail?.category_type)}
              </span>
              <span className="text-[0.8rem] text-[var(--color-ink-2)]">
                {facts.length > 0 ? facts.join(' · ') : detailsLoading ? '…' : '—'}
              </span>
              <span
                className="text-[0.75rem] tabular-nums text-[var(--color-ink-3)]"
                title="poprvé viděn – naposledy viděn"
              >
                {fmtDateSlash(source.first_seen_at)} –{' '}
                {source.is_active ? 'dosud' : fmtDateSlash(source.last_seen_at)}
              </span>
            </span>
          </button>
          {source.source_url && (
            <a
              href={source.source_url}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={`Na portálu ${portal}`}
              className="shrink-0 text-[0.72rem] text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
            >
              Na portálu ↗
            </a>
          )}
          {origin && !detachArmed && (
            <button
              type="button"
              onClick={() => setDetachArmed(true)}
              className="shrink-0 rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2 py-0.5 text-[0.72rem] text-[var(--color-ink-3)] transition-colors hover:border-[var(--color-brick)] hover:bg-[var(--color-brick-soft)] hover:text-[var(--color-brick)]"
            >
              Rozdělit
              <span className="sr-only"> ({portal})</span>
            </button>
          )}
        </div>
        {unmoved && <UnmovedLine origin={unmoved} />}
        {/* Outside the toggle (a button may hold only phrasing content); a click on
            a photo opens the row too, the header stays the keyboard control. */}
        <ThumbStrip
          images={images}
          loading={imagesLoading}
          sourceKey={source.source}
          onOpen={() => setExpanded(true)}
        />
      </div>

      {origin && detachArmed && (
        <DetachConfirm
          propertyId={propertyId}
          origin={origin}
          isCanonical={isCanonical}
          onCancel={() => setDetachArmed(false)}
        />
      )}

      {expanded && (
        <div
          id={panelId}
          className="grid gap-4 border-t border-[var(--color-rule-soft)] px-3 py-3 sm:grid-cols-[20rem_1fr]"
        >
          <ImageCarousel
            images={taggedImageUrls(images)}
            aspect="aspect-[4/3]"
            fallback={
              <MissingPhotoTile
                source={source.source}
                reason={images.length > 0 ? 'foto nedostupné' : 'bez fota'}
              />
            }
          />
          <div className="min-w-0 space-y-2">
            <dl className="grid max-w-[24rem] grid-cols-2 gap-x-3 gap-y-0.5 text-[0.75rem]">
              {[
                ['Cena', priceLabel(source.price_czk, detail?.category_type)],
                [
                  'Plocha',
                  detail ? fmtArea(detail.area_m2, areaKindOf(detail.category_main)) : '—',
                ],
                ['Dispozice', detail?.disposition ?? '—'],
                ['Patro', fmtFloor(detail?.floor, detail?.total_floors) ?? '—'],
              ].map(([label, value]) => (
                <div key={label} className="flex items-baseline justify-between gap-2">
                  <dt className="text-[var(--color-ink-4)]">{label}</dt>
                  <dd className="font-mono tabular-nums text-[var(--color-ink-2)]">{value}</dd>
                </div>
              ))}
            </dl>
            {detail?.description?.trim() ? (
              <MemberText text={detail.description} label={portal} />
            ) : (
              <p className="text-[0.72rem] text-[var(--color-ink-4)]">
                {detailsLoading ? 'Načítám popis…' : 'Bez popisu.'}
              </p>
            )}
            <BrokerLine listingId={source.id} />
            {originRead && <OriginLine read={originRead} />}
            {!source.source_url && (
              <p className="text-[0.75rem] text-[var(--color-ink-4)]">Odkaz na portál chybí</p>
            )}
          </div>
        </div>
      )}
    </li>
  );
}

/* The first photos, small, so two rows can be told apart without opening either.
 * Each frame that will not load says which portal refused it (MissingPhotoTile),
 * and the album's remainder is counted rather than silently dropped. */
function ThumbStrip({
  images,
  loading,
  sourceKey,
  onOpen,
}: {
  images: ImagePublic[];
  loading: boolean;
  sourceKey: string;
  onOpen: () => void;
}) {
  const shown = images.slice(0, COLLAPSED_THUMBS);
  const rest = images.length - shown.length;
  if (loading && images.length === 0) {
    return (
      <div className="mt-2 flex gap-1.5" aria-hidden>
        {Array.from({ length: 3 }, (_, i) => (
          <div key={i} className="h-12 w-16 rounded-[var(--radius-xs)] bg-[var(--color-inset)]" />
        ))}
      </div>
    );
  }
  return (
    <div
      className="mt-2 flex cursor-pointer flex-wrap items-center gap-1.5"
      data-testid="thumb-strip"
      onClick={onOpen}
    >
      {shown.length === 0 ? (
        <div className="h-12 w-16 overflow-hidden rounded-[var(--radius-xs)] bg-[var(--color-inset)]">
          <MissingPhotoTile source={sourceKey} reason="bez fota" />
        </div>
      ) : (
        shown.map((img) => <Thumb key={img.id} image={img} sourceKey={sourceKey} />)
      )}
      {rest > 0 && (
        <span className="text-[0.7rem] tabular-nums text-[var(--color-ink-3)]">+{rest}</span>
      )}
    </div>
  );
}

function Thumb({ image, sourceKey }: { image: ImagePublic; sourceKey: string }) {
  const [broken, setBroken] = useState(false);
  return (
    <div className="h-12 w-16 overflow-hidden rounded-[var(--radius-xs)] bg-[var(--color-inset)]">
      {broken ? (
        <MissingPhotoTile source={sourceKey} reason="foto nedostupné" />
      ) : (
        <img
          src={imageSrc(image)}
          alt=""
          loading="lazy"
          onError={() => setBroken(true)}
          className="h-full w-full object-cover"
        />
      )}
    </div>
  );
}

/* Who is selling THIS advert — read only when the row is opened, and on the same
 * cache key as the page's own vizitka. Three outcomes kept apart, as there: a
 * broker, "none attributed" (a 404, which is an answer), and a failed read. */
function BrokerLine({ listingId }: { listingId: number }) {
  const q = useQuery({
    queryKey: ['listing-broker', listingId],
    queryFn: () => fetchListingBroker(listingId),
    staleTime: 60_000,
  });
  if (q.isLoading) {
    return <p className="text-[0.72rem] text-[var(--color-ink-4)]">Makléř: načítám…</p>;
  }
  if (q.isError && !q.data) {
    return (
      <p className="text-[0.72rem] text-[var(--color-brick)]">
        Makléře se nepodařilo načíst
      </p>
    );
  }
  const b = q.data ?? null;
  if (!b) {
    return <p className="text-[0.72rem] text-[var(--color-ink-4)]">Makléř: nepřiřazen</p>;
  }
  return (
    <p className="text-[0.75rem] text-[var(--color-ink-2)]">
      <span className="text-[var(--color-ink-4)]">Makléř: </span>
      <Link
        to={ROUTES.brokerDetail.build({ id: b.broker_id })}
        className="text-[var(--color-ink)] hover:text-[var(--color-copper-2)]"
      >
        {b.broker_display_name ?? 'Neznámý makléř'}
      </Link>
      <span className="text-[var(--color-ink-3)]">
        {' '}
        · {b.broker_firm_label ?? 'nezávislý / neznámá kancelář'}
      </span>
    </p>
  );
}

/* "Came from", as information: the property a detach would return this advert to
 * and the merge that took it from there; the property's own advert has none. */
function OriginLine({ read }: { read: OriginRead }) {
  const o = read.origin;
  const text =
    read.status === 'pending'
      ? 'načítám…'
      : read.status === 'error'
        ? 'nepodařilo se načíst'
        : o?.origin_property_id == null
          ? 'tato nemovitost (nepřišel sloučením)'
          : `nemovitost #${o.origin_property_id} · ${mergeOriginLabel(o.merge_source ?? '')} sloučení ze dne ${fmtDateSlash(o.merged_at)}`;
  return (
    <p className="text-[0.75rem] text-[var(--color-ink-2)]">
      <span className="text-[var(--color-ink-4)]">Původ: </span>
      {text}
    </p>
  );
}

/* A row a detach would not move, and why; where its origin went, when a later
   merge took it (the property page follows the merge to its survivor). */
function UnmovedLine({ origin }: { origin: AdvertOrigin }) {
  return (
    <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">
      Nelze oddělit: {unmovedReason(origin.detach_outcome ?? '')}
      {origin.detach_outcome === 'origin_moved_on' && origin.origin_property_id != null && (
        <>
          {' '}
          <Link
            to={propertyPath(origin.origin_property_id)}
            className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
          >
            kam odešla #{origin.origin_property_id}
          </Link>
        </>
      )}
      .
    </p>
  );
}

/* Step two of the split: say where the advert goes, then offer the write. */
function DetachConfirm({
  propertyId,
  origin,
  isCanonical,
  onCancel,
}: {
  propertyId: number;
  origin: AdvertOrigin;
  isCanonical: boolean;
  onCancel: () => void;
}) {
  const qc = useQueryClient();
  /* The header's own advert leaving for a new record: the property's state stays here. */
  const stateStays = isCanonical && origin.origin_property_id == null;
  const [reason, setReason] = useState('');
  const detach = useMutation({
    mutationFn: () => detachListing(propertyId, origin.listing_id, reason.trim() || undefined),
    /* Errors (a 409 the merge code refused, a 5xx) surface through the global
     * MutationCache toast; the panel stays open so nothing looks done. */
    onSuccess: (res) => {
      if (res.detached) {
        pushToast(
          'ok',
          res.outcome === 'split_native'
            ? `Odděleno — inzerát má novou vlastní nemovitost #${res.restored_property_id}.` +
                (stateStays ? ` ${STATE_STAYS}` : '')
            : `Odděleno — inzerát je zpět v nemovitosti #${res.restored_property_id}.`,
        );
      } else {
        pushToast('info', detachOutcomeNote(res.outcome));
      }
      onCancel();
      refreshAfterDetach(qc);
    },
  });

  return (
    <div
      role="group"
      aria-label="Oddělit inzerát"
      className="mx-3 mb-2 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/40 bg-[var(--color-brick-soft)] px-3 py-2"
    >
      <p className="text-[0.75rem] leading-snug text-[var(--color-ink-2)]">
        <strong className="font-medium text-[var(--color-ink)]">Oddělit tento inzerát?</strong>{' '}
        {origin.origin_property_id == null ? (
          <>Nepřivedlo ho sloučení: dostane novou vlastní nemovitost</>
        ) : (
          <>
            Vrátí se do nemovitosti #{origin.origin_property_id}, odkud ho přivedlo{' '}
            {mergeOriginLabel(origin.merge_source ?? '')} sloučení ze dne{' '}
            {fmtDateSlash(origin.merged_at)}
          </>
        )}
        , a zapíše se, že se zbylými inzeráty nejde o stejnou nemovitost.
        {stateStays && ` ${STATE_STAYS}`}
      </p>
      <textarea
        aria-label="Důvod rozdělení (nepovinné)"
        placeholder="Důvod (nepovinné)"
        maxLength={DETACH_REASON_MAX}
        rows={2}
        value={reason}
        disabled={detach.isPending}
        onChange={(e) => setReason(e.target.value)}
        className="mt-2 block w-full max-w-[32rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.75rem] text-[var(--color-ink)]"
      />
      <div className="mt-2 flex items-center gap-1.5">
        <button
          type="button"
          autoFocus
          disabled={detach.isPending}
          onClick={() => detach.mutate()}
          className="rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-2 py-0.5 text-[0.72rem] text-[var(--color-brick)] transition-colors hover:bg-[var(--color-brick)]/10 disabled:opacity-50"
        >
          {detach.isPending ? 'Odděluji…' : 'Ano, oddělit'}
        </button>
        <button
          type="button"
          disabled={detach.isPending}
          onClick={onCancel}
          className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2 py-0.5 text-[0.72rem] text-[var(--color-ink-2)] transition-colors hover:border-[var(--color-rule-strong)] hover:bg-[var(--color-rule-soft)] disabled:opacity-50"
        >
          Zrušit
        </button>
      </div>
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 12 12"
      aria-hidden
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`shrink-0 text-[var(--color-ink-3)] transition-transform ${open ? 'rotate-90' : ''}`}
    >
      <path d="M4.5 3 L8 6 L4.5 9" />
    </svg>
  );
}
