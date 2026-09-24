/* "Sloučené inzeráty" — every advert this property is made of, one row each.
 *
 * The operator's brief: a Browse-like overview of which listings were merged
 * into the property, one expandable row per advert, the photos visible even
 * collapsed and the description on expand. Collapsed, a row answers "which
 * advert is this" (portal, price, area and disposition, the span it was seen
 * over, its first photos); expanded, it answers "is it really the same flat"
 * (every photo, the advert's own words, its broker, the link out).
 *
 * Built from parts the review pages already trust rather than a second gallery:
 * ImageCarousel for the photos, MissingPhotoTile for a frame a portal refuses,
 * MemberText for the description (its unit-token marks — "byt č. 14", "ve
 * 4. patře" — are exactly what separates two units of one building). The link
 * out is the row's stored `source_url`, never rebuilt.
 *
 * The per-row 'Rozdělit' is the only UI for the existing unmerge route, behind
 * its own switch and admin sessions only. What it can honestly offer from a
 * row is decided in lib/mergedAdverts.planRowUnmerge — read that header before
 * changing any copy here. */

import { useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import ImageCarousel from '@/components/ImageCarousel';
import MemberText from '@/components/autodedup/MemberText';
import { MissingPhotoTile } from '@/components/autodedup/ListingMini';
import { SectionLabel } from '@/components/section';
import { unmergeMergeGroup } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { fetchListingBroker } from '@/lib/brokers';
import { fmtArea, fmtCount, fmtCzk, fmtDateSlash, fmtFloor } from '@/lib/format';
import { taggedImageUrls, useListingPhotos } from '@/lib/hydration/useCardHydration';
import { imageSrc } from '@/lib/imageUrl';
import { listingRowPath } from '@/lib/listingUrl';
import { areaKindOf } from '@/lib/measure';
import {
  findActivePropertyMergeGroups,
  inzeratu,
  mergedAdvertsKeys,
  planRowUnmerge,
  refreshAfterUnmerge,
  type UnmergePlan,
} from '@/lib/mergedAdverts';
import { portalLabel } from '@/lib/portals';
import { fetchListingsForListingIds } from '@/lib/queries';
import { ROUTES } from '@/lib/routes';
import { pushToast } from '@/lib/toast';
import type { ImagePublic, ListingPublic, PropertySource } from '@/lib/types';

/* Photos a collapsed row shows before counting the rest. */
export const COLLAPSED_THUMBS = 6;
/* A client-side retention cap over one read of every advert's album — high
 * enough that no real album is cut, finite so the hydration key stays a number. */
const PHOTOS_PER_ADVERT = 200;

interface SectionProps {
  /* The property the rows belong to (the sources read's own property_id). */
  propertyId: number;
  /* The advert this page is open on — marked, and never linked to itself. */
  currentListingId: number;
  sources: PropertySource[];
  /* MERGED_ADVERTS_UNMERGE_ENABLED, handed down by the page. */
  unmergeEnabled: boolean;
}

export default function MergedAdvertsSection(props: SectionProps) {
  if (props.sources.length < 2) return null;
  /* useAuth only when the write can show at all: with the switch off the section
   * needs no session state, and stays renderable outside an AuthProvider. */
  return props.unmergeEnabled ? (
    <WithAdminCheck {...props} />
  ) : (
    <SectionBody {...props} canUnmerge={false} />
  );
}

function WithAdminCheck(props: SectionProps) {
  const { isAdmin } = useAuth();
  return <SectionBody {...props} canUnmerge={isAdmin} />;
}

function SectionBody({
  propertyId,
  currentListingId,
  sources,
  canUnmerge,
}: SectionProps & { canUnmerge: boolean }) {
  const ids = useMemo(() => sources.map((s) => s.id), [sources]);

  /* Area, disposition, floor and the description — per advert, from the same
   * listings_public read the page's own header uses. One request for the set. */
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
        <SectionLabel>Sloučené inzeráty</SectionLabel>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
          {fmtCount(sources.length)} {inzeratu(sources.length)} · {fmtCount(portals)}{' '}
          {portals === 1 ? 'portál' : portals <= 4 ? 'portály' : 'portálů'}
        </p>
      </div>
      <p className="mt-1 text-[0.75rem] text-[var(--color-ink-3)]">
        Inzeráty, které tvoří tuto nemovitost. Rozbalte řádek pro popis, všechny
        fotky a makléře.
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
            isCurrent={s.id === currentListingId}
            propertyId={propertyId}
            rowCount={sources.length}
            currentListingId={currentListingId}
            canUnmerge={canUnmerge}
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
  isCurrent,
  propertyId,
  rowCount,
  currentListingId,
  canUnmerge,
}: {
  source: PropertySource;
  detail: ListingPublic | null;
  detailsLoading: boolean;
  images: ImagePublic[];
  imagesLoading: boolean;
  isCurrent: boolean;
  propertyId: number;
  rowCount: number;
  currentListingId: number;
  canUnmerge: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [unmergeArmed, setUnmergeArmed] = useState(false);
  const panelId = `merged-advert-${source.id}`;
  const portal = portalLabel(source.source) ?? source.source;
  const facts = [
    detail?.area_m2 != null ? fmtArea(detail.area_m2, areaKindOf(detail.category_main)) : null,
    detail?.disposition ?? null,
  ].filter(Boolean);

  return (
    <li
      className={[
        'rounded-[var(--radius-sm)] border bg-[var(--color-paper-2)]',
        isCurrent ? 'border-[var(--color-rule-strong)]' : 'border-[var(--color-rule-soft)]',
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
              {isCurrent && (
                <span className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
                  tento inzerát
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
          {canUnmerge && !unmergeArmed && (
            <button
              type="button"
              onClick={() => setUnmergeArmed(true)}
              className="shrink-0 rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2 py-0.5 text-[0.72rem] text-[var(--color-ink-3)] transition-colors hover:border-[var(--color-brick)] hover:bg-[var(--color-brick-soft)] hover:text-[var(--color-brick)]"
            >
              Rozdělit
              <span className="sr-only"> ({portal})</span>
            </button>
          )}
        </div>
        {/* Outside the toggle (a button may hold only phrasing content); a click on
            a photo opens the row too, the header stays the keyboard control. */}
        <ThumbStrip
          images={images}
          loading={imagesLoading}
          sourceKey={source.source}
          onOpen={() => setExpanded(true)}
        />
      </div>

      {canUnmerge && unmergeArmed && (
        <UnmergeConfirm
          propertyId={propertyId}
          rowCount={rowCount}
          currentListingId={currentListingId}
          onCancel={() => setUnmergeArmed(false)}
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
            <p className="flex flex-wrap items-center gap-3 text-[0.75rem]">
              {!isCurrent && (
                <Link
                  to={listingRowPath(source)}
                  className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
                >
                  Otevřít detail
                </Link>
              )}
              {source.source_url ? (
                <a
                  href={source.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
                >
                  Na portálu {portal} ↗
                </a>
              ) : (
                <span className="text-[var(--color-ink-4)]">Odkaz na portál chybí</span>
              )}
            </p>
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

function nemovitosti(n: number): string {
  if (n === 1) return 'nemovitost';
  if (n >= 2 && n <= 4) return 'nemovitosti';
  return 'nemovitostí';
}

/* "Všechny 3 inzeráty" but "všech 5 inzerátů" — the quantifier declines too. */
function allAdverts(n: number): string {
  return n >= 2 && n <= 4 ? `Všechny ${n} inzeráty` : `Všech ${n} inzerátů`;
}

function groupOrigin(source: string): string {
  return source === 'auto' ? 'automatické' : 'ruční';
}

/* Step two of the split: find what can be undone from here, say it in words,
 * and only then offer the write. The ledger is read now, not on page load. */
function UnmergeConfirm({
  propertyId,
  rowCount,
  currentListingId,
  onCancel,
}: {
  propertyId: number;
  rowCount: number;
  currentListingId: number;
  onCancel: () => void;
}) {
  const qc = useQueryClient();
  const scanQ = useQuery({
    queryKey: mergedAdvertsKeys.groups(propertyId, rowCount),
    queryFn: () => findActivePropertyMergeGroups(propertyId, rowCount),
    staleTime: 30_000,
  });
  const unmerge = useMutation({
    mutationFn: (mergeGroupId: string) => unmergeMergeGroup(mergeGroupId),
    /* Errors (a 404 for a group undone meanwhile, a 5xx) surface through the
     * global MutationCache toast; the panel stays open so nothing looks done. */
    onSuccess: async (res) => {
      const moved = res.data.listings_moved_back;
      pushToast('ok', `Rozděleno — ${moved} ${inzeratu(moved)} zpět v původní nemovitosti.`);
      const conflicts = res.data.conflicts.length;
      if (conflicts > 0) {
        pushToast(
          'info',
          `${conflicts} ${inzeratu(conflicts)} mezitím patří jinam a ${
            conflicts === 1 ? 'zůstal' : 'zůstaly'
          } na místě.`,
        );
      }
      onCancel();
      await refreshAfterUnmerge(qc, currentListingId);
    },
  });

  const plan: UnmergePlan | null = scanQ.data ? planRowUnmerge(scanQ.data, rowCount) : null;

  let body: ReactNode;
  let confirm: { label: string; groupId: string } | null = null;
  if (scanQ.isLoading) {
    body = 'Hledám sloučení této nemovitosti…';
  } else if (scanQ.isError) {
    body = `Knihu sloučení se nepodařilo načíst: ${(scanQ.error as Error).message}`;
  } else if (plan?.kind === 'pair') {
    body = (
      <>
        <strong className="font-medium text-[var(--color-ink)]">Oddělit tento inzerát?</strong>{' '}
        Tyto 2 inzeráty přestanou být jedna nemovitost — vrátí se{' '}
        {groupOrigin(plan.group.source)} sloučení ze dne {fmtDateSlash(plan.group.merged_at)}.
      </>
    );
    confirm = { label: 'Ano, oddělit', groupId: plan.group.merge_group_id };
  } else if (plan?.kind === 'whole-group') {
    const originals = plan.group.retired_count + 1;
    body = (
      <>
        <strong className="font-medium text-[var(--color-ink)]">
          Jeden inzerát samostatně oddělit nejde.
        </strong>{' '}
        {allAdverts(rowCount)} spojilo jedno {groupOrigin(plan.group.source)} sloučení ze dne{' '}
        {fmtDateSlash(plan.group.merged_at)}. Vrátit jde jen celé: nemovitost se rozpadne
        zpět na {originals} {nemovitosti(originals)}.
      </>
    );
    confirm = { label: 'Ano, vrátit celé sloučení', groupId: plan.group.merge_group_id };
  } else if (plan?.kind === 'ambiguous') {
    body = (
      <>
        Z knihy sloučení nejde poznat, které sloučení přivedlo právě tento inzerát
        (nemovitost jich má víc, nebo je jedno nevysvětluje celou), takže ho odsud
        oddělit nejde.
      </>
    );
  } else if (plan?.kind === 'not-found') {
    body = plan.exhaustive
      ? 'Kniha sloučení pro tuto nemovitost nemá žádné sloučení, které by šlo vrátit — její inzeráty spojilo starší seskupení.'
      : `Mezi posledními ${fmtCount(plan.scanned)} sloučeními tato nemovitost není; starší sloučení odsud vrátit nejde.`;
  }

  return (
    <div
      role="group"
      aria-label="Rozdělit nemovitost"
      className="mx-3 mb-2 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/40 bg-[var(--color-brick-soft)] px-3 py-2"
    >
      <p className="text-[0.75rem] leading-snug text-[var(--color-ink-2)]">{body}</p>
      <div className="mt-2 flex items-center gap-1.5">
        {confirm && (
          <button
            type="button"
            autoFocus
            disabled={unmerge.isPending}
            onClick={() => unmerge.mutate(confirm.groupId)}
            className="rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-2 py-0.5 text-[0.72rem] text-[var(--color-brick)] transition-colors hover:bg-[var(--color-brick)]/10 disabled:opacity-50"
          >
            {unmerge.isPending ? 'Rozděluji…' : confirm.label}
          </button>
        )}
        <button
          type="button"
          disabled={unmerge.isPending}
          onClick={onCancel}
          className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2 py-0.5 text-[0.72rem] text-[var(--color-ink-2)] transition-colors hover:border-[var(--color-rule-strong)] hover:bg-[var(--color-rule-soft)] disabled:opacity-50"
        >
          {confirm ? 'Zrušit' : 'Zavřít'}
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
