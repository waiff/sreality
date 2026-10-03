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
 * on expand) and, on a property of two or more adverts, a LETTER per advert (the
 * Groups page's unit select, every advert A): one letter is one property, two
 * letters are two. Two letters in use open ONE panel that states the whole
 * partition as one split statement (`splitPlan`, E919): every letter group but
 * the one keeping the record leaves as one property, so the two adverts of one
 * flat leave together instead of being ruled "different" from each other. It
 * names every advert the page shows, so a newcomer the lane merged in meanwhile
 * refuses it (`stale`) instead of being ruled. */

import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import ImageCarousel from '@/components/ImageCarousel';
import MemberText from '@/components/autodedup/MemberText';
import { MissingPhotoTile } from '@/components/autodedup/ListingMini';
import { UnitSelect, type UnitMap } from '@/components/autodedup/UnitSplit';
import { SectionLabel } from '@/components/section';
import {
  DETACH_REASON_MAX,
  fetchPropertyOrigins,
  splitProperty,
  splitRefusal,
  type AdvertOrigin,
  type SplitRefusal,
  type SplitStatement,
} from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { fetchListingBroker } from '@/lib/brokers';
import { fmtArea, fmtCount, fmtCzk, fmtDateSlash, fmtFloor } from '@/lib/format';
import { taggedImageUrls, useListingPhotos } from '@/lib/hydration/useCardHydration';
import { imageSrc } from '@/lib/imageUrl';
import { propertyPath } from '@/lib/listingUrl';
import { areaKindOf } from '@/lib/measure';
import {
  inzeratu,
  mergeOriginLabel,
  mergedAdvertsKeys,
  refreshAfterSplit,
  splitPlan,
  stateStays,
  unitLanding,
  unmovedReason,
  type SplitPlan,
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

const NO_LETTERS: UnitMap = {};

/* The primary advert is the canonical one: the header's photos and facts are its. */
const PRIMARY_TITLE = 'Záhlaví nemovitosti ukazuje fotky a údaje tohoto inzerátu.';

/* The row's half of the split: its letter, and whether it is the property's own advert. */
type RowSplit = {
  units: UnitMap;
  count: number;
  disabled: boolean;
  onLetter: (listingId: number, letter: string) => void;
  ownTitle: string | null;
};

type SplitCall = { propertyId: number; statement: SplitStatement };
/* A refusal, with the statement it refused: "Přesto rozdělit" is offered only
 * while the letters still make that statement. */
type SplitFailure = { statement: SplitStatement; refusal: SplitRefusal | null; message: string };

const STALE_TEXT =
  'Nemovitost se mezitím změnila — načteno znovu, nic se nezapsalo. Zkontrolujte písmena a rozdělte znovu.';

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
  const qc = useQueryClient();
  const navigate = useNavigate();
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
    queryFn: ({ signal }) => fetchListingsForListingIds(ids, { signal }),
    staleTime: 60_000,
  });
  /* Every advert's album in one read (the shared card-photo hydration), so the
   * collapsed strip and the expanded carousel are one cache entry. */
  const { photos, isPending: photosPending } = useListingPhotos(ids, PHOTOS_PER_ADVERT);

  const portals = new Set(sources.map((s) => s.source)).size;
  const portalOf = (listingId: number): string => {
    const s = sources.find((x) => x.id === listingId);
    return s ? (portalLabel(s.source) ?? s.source) : '';
  };
  /* The plan names an advert the way its row does (portal and price): the
   * operator reads the rows, and a listing id appears on none of them. */
  const priceOf = (listingId: number): string => {
    const s = sources.find((x) => x.id === listingId);
    return s ? priceLabel(s.price_czk, detailsQ.data?.get(listingId)?.category_type) : '';
  };

  /* Who stays is decided by which adverts are the property's own, so no letter
   * is offered before the origins are read. */
  const canSplit = isAdmin && originsQ.isSuccess && sources.length >= 2;
  const own = useMemo(
    () =>
      new Set(
        (originsQ.data?.adverts ?? [])
          .filter((a) => a.origin_property_id == null)
          .map((a) => a.listing_id),
      ),
    [originsQ.data],
  );
  const ownShown = ids.filter((id) => own.has(id)).length;
  /* The letters hold for the advert list they were set over: a list that changed
   * (a split landed, a newcomer arrived) starts again at A. */
  const listKey = [...ids].sort((a, b) => a - b).join(',');
  const [letters, setLetters] = useState({ list: listKey, units: NO_LETTERS });
  const units = letters.list === listKey ? letters.units : NO_LETTERS;
  const [reason, setReason] = useState('');
  const [failure, setFailure] = useState<SplitFailure | null>(null);
  const plan = splitPlan(ids, units, own, canonicalListingId);

  const split = useMutation({
    mutationFn: (call: SplitCall) => splitProperty(call.propertyId, call.statement),
    onSuccess: (res, call) => {
      setLetters((prev) => ({ list: prev.list, units: NO_LETTERS }));
      setReason('');
      setFailure(null);
      const left = res.units.filter((u) => u.role === 'separated' && u.moved.length > 0);
      for (const u of left) {
        pushToast(
          'ok',
          `Odděleno — ${fmtCount(u.moved.length)} ${inzeratu(u.moved.length)}: ${unitLanding(u, call.propertyId)}.`,
          0,
          { label: `Otevřít #${u.property_id}`, onClick: () => navigate(propertyPath(u.property_id)) },
        );
      }
      if (left.length === 0) pushToast('info', 'Nic se nepřesunulo — inzeráty už jsou odděleny.');
      refreshAfterSplit(qc);
    },
    /* The panel stays open so nothing looks done. A property that changed since
     * the page read it (`stale`) is re-read; an advert that cannot leave re-reads
     * the origins, so its row says why. */
    onError: (e, call) => {
      const refusal = splitRefusal(e);
      setFailure({ statement: call.statement, refusal, message: e.message });
      if (refusal?.code === 'stale') refreshAfterSplit(qc);
      if (refusal?.code === 'cannot_move') {
        qc.invalidateQueries({ queryKey: mergedAdvertsKeys.origins(call.propertyId) });
      }
    },
  });

  const setLetter = (listingId: number, letter: string) => {
    setLetters({ list: listKey, units: { ...units, [listingId]: letter } });
    setFailure(null);
  };
  const cancel = () => {
    setLetters({ list: listKey, units: NO_LETTERS });
    setReason('');
    setFailure(null);
  };
  /* Only over the partition it refused: a letter moved since clears the refusal. */
  const confirming =
    failure?.refusal?.code === 'reverses_rulings' && samePartition(failure.statement, plan.statement);
  const send = () => {
    if (split.isPending) return;
    setFailure(null);
    const why = reason.trim();
    split.mutate({
      propertyId,
      statement: {
        ...plan.statement,
        ...(why ? { reason: why } : {}),
        ...(confirming ? { confirm_retract: true } : {}),
      },
    });
  };
  const panel = canSplit && plan.leaving.length > 0;
  const ownTitle =
    ownShown > 1
      ? 'Nepřišel sloučením — při rozdělení zůstanou v této nemovitosti inzeráty písmena, které má nejvíc vlastních inzerátů.'
      : 'Nepřišel sloučením — inzeráty s jeho písmenem při rozdělení zůstanou v této nemovitosti.';

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
        {canSplit &&
          ' Nemovitost rozdělíte písmeny u inzerátů: stejné písmeno = jedna nemovitost, různá písmena = různé nemovitosti.'}
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
      {panel && (
        <SplitPanel
          propertyId={propertyId}
          plan={plan}
          portalOf={portalOf}
          priceOf={priceOf}
          canonicalListingId={canonicalListingId}
          reason={reason}
          onReason={setReason}
          pending={split.isPending}
          confirming={confirming}
          failure={failure}
          onSend={send}
          onCancel={cancel}
        />
      )}
      {/* A refusal outlives the letters: a re-read that changed the list resets them. */}
      {!panel && failure && <SplitFailureNote failure={failure} portalOf={portalOf} className="mt-3" />}
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
            originRead={
              isAdmin
                ? {
                    status: originsQ.status,
                    origin: originsQ.data?.adverts.find((a) => a.listing_id === s.id),
                  }
                : null
            }
            split={
              canSplit
                ? {
                    units,
                    count: sources.length,
                    disabled: split.isPending,
                    onLetter: setLetter,
                    ownTitle: own.has(s.id) ? ownTitle : null,
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
  originRead,
  split,
}: {
  source: PropertySource;
  detail: ListingPublic | null;
  detailsLoading: boolean;
  images: ImagePublic[];
  imagesLoading: boolean;
  isCanonical: boolean;
  opened: boolean;
  originRead: OriginRead | null;
  split: RowSplit | null;
}) {
  const [expanded, setExpanded] = useState(opened);
  const rowRef = useRef<HTMLLIElement | null>(null);
  useEffect(() => {
    if (opened) rowRef.current?.scrollIntoView?.({ block: 'start' });
  }, [opened]);
  /* Why an advert cannot leave. Said of neither a lone advert (nothing to leave)
   * nor the last own one: the letters keep its group on the property. */
  const unmoved =
    originRead?.origin &&
    !originRead.origin.splittable &&
    !SAYS_NOTHING.has(originRead.origin.detach_outcome ?? '')
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
              {isCanonical && (
                <span
                  title={PRIMARY_TITLE}
                  className="rounded-[var(--radius-xs)] border border-[var(--color-copper)]/30 bg-[var(--color-copper-soft)] px-1.5 py-0.5 text-[0.62rem] tracking-[0.08em] uppercase text-[var(--color-copper-2)]"
                >
                  hlavní inzerát
                </span>
              )}
              <span className="inline-flex items-center gap-1 text-[0.7rem] text-[var(--color-ink-3)]">
                <span
                  aria-hidden
                  className={`inline-block h-1.5 w-1.5 rounded-full ${
                    source.is_active ? 'bg-[var(--color-sage)]' : 'bg-[var(--color-ink-4)]'
                  }`}
                />
                {source.is_active ? 'aktivní' : 'staženo'}
              </span>
              {split?.ownTitle && (
                <span
                  title={split.ownTitle}
                  className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]"
                >
                  vlastní inzerát
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
          {split && (
            <div className="shrink-0">
              <UnitSelect
                listingId={source.id}
                units={split.units}
                count={split.count}
                disabled={split.disabled}
                onChange={(letter) => split.onLetter(source.id, letter)}
                label={
                  <>
                    Nemovitost<span className="sr-only"> inzerátu {portal} #{source.id}</span>
                  </>
                }
              />
            </div>
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

/* A row a split would not move, and why; where its origin went, when a later
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

const SAYS_NOTHING = new Set(['not_merged', 'last_native']);

const samePartition = (a: SplitStatement, b: SplitStatement): boolean =>
  JSON.stringify([a.adverts, a.separate]) === JSON.stringify([b.adverts, b.separate]);

/* The letters as the statement they make, before anything is written: which
 * group stays on this property and which leave, what gets recorded, what stays. */
function SplitPanel({
  propertyId,
  plan,
  portalOf,
  priceOf,
  canonicalListingId,
  reason,
  onReason,
  pending,
  confirming,
  failure,
  onSend,
  onCancel,
}: {
  propertyId: number;
  plan: SplitPlan;
  portalOf: (listingId: number) => string;
  priceOf: (listingId: number) => string;
  canonicalListingId: number;
  reason: string;
  onReason: (reason: string) => void;
  pending: boolean;
  confirming: boolean;
  failure: SplitFailure | null;
  onSend: () => void;
  onCancel: () => void;
}) {
  const groups = [plan.kept, ...plan.leaving].sort((a, b) => a.letter.localeCompare(b.letter));
  /* The record stays with the own adverts, which need not be the primary one's group. */
  const primaryLeaves = plan.leaving.find((g) => g.listingIds.includes(canonicalListingId));
  return (
    <div
      role="group"
      aria-label="Rozdělení nemovitosti"
      className="mt-3 space-y-2 rounded-[var(--radius-sm)] border border-dashed border-[var(--color-brick)]/40 bg-[var(--color-brick-soft)] px-3 py-2"
    >
      <ul className="space-y-0.5 text-[0.75rem] leading-snug text-[var(--color-ink-2)]">
        {groups.map((g) => (
          <li key={g.letter}>
            <span className="font-mono font-medium text-[var(--color-ink)]">{g.letter}</span> —{' '}
            {g === plan.kept ? (
              <>
                zůstává v nemovitosti <span className="font-mono tabular-nums">#{propertyId}</span>
              </>
            ) : (
              'odejde jako jedna nemovitost'
            )}
            :{' '}
            {g.listingIds.map((id, i) => (
              <Fragment key={id}>
                {i > 0 && ', '}
                <span className="text-[0.68rem] tracking-[0.06em] uppercase">{portalOf(id)}</span>{' '}
                <span className="font-mono tabular-nums" title={`inzerát #${id}`}>
                  {priceOf(id)}
                </span>
              </Fragment>
            ))}
          </li>
        ))}
      </ul>
      <p className="text-[0.72rem] leading-snug text-[var(--color-ink-2)]">
        Různá písmena = různé nemovitosti: každá dvojice inzerátů napříč písmeny se uloží jako
        „různé“ a dostane trvalý zákaz spojení; inzeráty se stejným písmenem zůstanou spolu jako
        jedna nemovitost. {stateStays(propertyId)}
      </p>
      {primaryLeaves && (
        <p className="text-[0.72rem] leading-snug text-[var(--color-ink)]">
          Hlavní inzerát odejde se skupinou{' '}
          <span className="font-mono font-medium">{primaryLeaves.letter}</span>: záhlaví nemovitosti{' '}
          <span className="font-mono tabular-nums">#{propertyId}</span> pak převezme inzerát skupiny{' '}
          <span className="font-mono font-medium">{plan.kept.letter}</span>.
        </p>
      )}
      <textarea
        aria-label="Důvod rozdělení (nepovinné)"
        placeholder="Důvod (nepovinné)"
        maxLength={DETACH_REASON_MAX}
        rows={2}
        value={reason}
        disabled={pending}
        onChange={(e) => onReason(e.target.value)}
        className="block w-full max-w-[32rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.75rem] text-[var(--color-ink)]"
      />
      {failure && <SplitFailureNote failure={failure} portalOf={portalOf} />}
      <div className="flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          /* Not disabled while in flight (SplitRow's rule): disabling the button
           * just clicked drops focus onto <body>. */
          aria-busy={pending}
          onClick={onSend}
          className={`rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-2 py-0.5 text-[0.72rem] text-[var(--color-brick)] transition-colors hover:bg-[var(--color-brick)]/10 ${
            pending ? 'opacity-60' : ''
          }`}
        >
          {pending ? 'Probíhá…' : confirming ? 'Přesto rozdělit' : 'Rozdělit nemovitost'}
        </button>
        <button
          type="button"
          disabled={pending}
          onClick={onCancel}
          className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2 py-0.5 text-[0.72rem] text-[var(--color-ink-2)] transition-colors hover:border-[var(--color-rule-strong)] hover:bg-[var(--color-rule-soft)] disabled:opacity-50"
        >
          Zrušit
        </button>
      </div>
    </div>
  );
}

type Stuck = { listing_id: number; outcome?: unknown };
const isStuck = (x: unknown): x is Stuck =>
  typeof x === 'object' && x !== null && typeof (x as Stuck).listing_id === 'number';
const isPair = (x: unknown): x is [number, number] =>
  Array.isArray(x) && x.length === 2 && x.every((n) => typeof n === 'number');

/* Why the statement wrote nothing, in the page's words; anything unforeseen raw. */
function SplitFailureNote({
  failure,
  portalOf,
  className = '',
}: {
  failure: SplitFailure;
  portalOf: (listingId: number) => string;
  className?: string;
}) {
  const r = failure.refusal;
  const tone = `text-[0.72rem] leading-snug text-[var(--color-brick)] ${className}`;
  const pairs = r?.code === 'reverses_rulings' ? r.ids.filter(isPair) : [];
  if (pairs.length > 0) {
    return (
      <p role="alert" className={tone}>
        Nic se nezapsalo: tím byste vzali zpět své dřívější rozhodnutí „různé“ u{' '}
        {pairs.map(([lo, hi]) => `#${lo} × #${hi}`).join(', ')} a zrušili jejich trvalý zákaz
        spojení.
      </p>
    );
  }
  if (r?.code === 'stale') {
    return (
      <p role="alert" className={tone}>
        {STALE_TEXT}
      </p>
    );
  }
  const stuck = r?.code === 'cannot_move' ? r.ids.filter(isStuck) : [];
  if (stuck.length > 0) {
    return (
      <div role="alert" className={tone}>
        <p>Nic se nezapsalo — tyto inzeráty nemohou odejít:</p>
        <ul className="mt-0.5 space-y-0.5">
          {stuck.map((a) => (
            <li key={a.listing_id}>
              {portalOf(a.listing_id) && `${portalOf(a.listing_id)} `}
              <span className="font-mono tabular-nums">#{a.listing_id}</span>:{' '}
              {typeof a.outcome === 'string' && a.outcome ? unmovedReason(a.outcome) : failure.message}
            </li>
          ))}
        </ul>
      </div>
    );
  }
  return (
    <p role="alert" className={tone}>
      Chyba: {failure.message}
    </p>
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
