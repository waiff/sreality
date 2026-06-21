import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import ImageCarousel from '@/components/ImageCarousel';
import InfiniteSentinel from '@/components/InfiniteSentinel';
import Spinner from '@/components/Spinner';
import { FunnelIcon } from '@/components/icons';
import { useScrollRestoration } from '@/lib/useScrollRestoration';
import {
  curationKeys,
  fetchPipelineMemberSet,
  fetchPropertyCollectionMemberSet,
  pipelineKeys,
  sortToParam,
  type CardRow,
  type SortSpec,
} from '@/lib/queries';
import {
  addPipelineCard,
  addPropertiesToCollection,
  listCollections,
  removePipelineCard,
  removePropertyFromCollection,
} from '@/lib/api';
import {
  fmtArea, fmtCzk, fmtPricePerM2,
  fmtShortDate, fmtTomDays,
} from '@/lib/format';
import { categoryMainLabel } from '@/lib/enums';
import { portalLabel } from '@/lib/portals';
import { placePrimary } from '@/lib/placeLabel';
import type { ListingEstimate } from '@/lib/types';
import { runSurfaceUrl } from '@/lib/runLinks';
import { listingPath } from '@/lib/listingUrl';

interface Props {
  rows: CardRow[] | null;
  total: number | null;
  sort: SortSpec;
  isLoading: boolean;
  /* Infinite scroll: the next page is in flight, there are more pages, and
   * the trigger to load them. The cohort total above drives the progress
   * label; these drive the bottom sentinel. */
  isFetchingNextPage: boolean;
  hasNextPage: boolean;
  onReachEnd: () => void;
  /* Stable per-cohort key (filters + sort) used to save/restore the card
   * column's scroll position across "open a card → Back". */
  restorationKey: string;
  hasFilters: boolean;
  /* Truthy whenever the operator has narrowed by map area. Drives the
   * "0 in this map area — Show all" empty state. */
  hasBounds: boolean;
  /* Hover-sync set: when a sreality_id appears here (because the
   * user is hovering the matching pin on the map, or the matching row
   * in the table), the card lights up. Cards push their own hover
   * state outward via onHover. */
  hoveredIds: ReadonlySet<number>;
  onHover: (ids: ReadonlyArray<number> | null) => void;
  /* Which pane originated the hover. Map-origin hovers get the full
   * "find it in the archive" treatment — non-matching cards recede and
   * the first match scrolls into view. List-origin hovers (the
   * operator's own pointer) must not dim or scroll the very grid
   * they're sweeping. */
  hoverOrigin?: 'map' | 'list' | null;
  onSort: (next: SortSpec) => void;
  onClearFilters: () => void;
  onClearBounds: () => void;
  /* Dedup merge mode: when on, cards show a selection checkbox and a click
   * toggles selection instead of navigating. selected holds the picked
   * property_ids. */
  mergeMode: boolean;
  selectedPropertyIds: ReadonlySet<number>;
  onToggleSelect: (propertyId: number) => void;
  /* On-card estimate: latest rent estimate per listing id (keyed by
   * sreality_id), the set of ids whose run is being kicked off right now
   * (optimistic spinner), and the trigger. Estimate runs on apartment cards
   * only. estimates is undefined until the lookup resolves. */
  estimates: Record<number, ListingEstimate> | undefined;
  estimatingIds: ReadonlySet<number>;
  onEstimate: (srealityId: number) => void;
}

export default function ListingCards({
  rows,
  total,
  sort,
  isLoading,
  isFetchingNextPage,
  hasNextPage,
  onReachEnd,
  restorationKey,
  hasFilters,
  hasBounds,
  hoveredIds,
  onHover,
  hoverOrigin = null,
  onSort,
  onClearFilters,
  onClearBounds,
  mergeMode,
  selectedPropertyIds,
  onToggleSelect,
  estimates,
  estimatingIds,
  onEstimate,
}: Props) {
  const showSkeleton = isLoading && rows == null;
  const isEmpty = !showSkeleton && rows != null && rows.length === 0;

  /* The card column is an independently-scrolling fixed-height element
   * (overflow-y-auto below); the infinite sentinel observes it as its root
   * and scroll restoration saves/restores its scrollTop. */
  const scrollRef = useRef<HTMLDivElement>(null);
  useScrollRestoration(
    scrollRef,
    restorationKey,
    !showSkeleton && rows != null && rows.length > 0,
  );

  /* Map-origin hover: dim the rest of the grid only when the map is
   * pointing at something actually on this page — otherwise a far-off
   * cluster hover would grey the whole grid with nothing lit. */
  const hoveredOnPage =
    hoverOrigin === 'map' && rows != null
      ? rows.filter((r) => hoveredIds.has(r.sreality_id))
      : [];
  const mapHover = hoveredOnPage.length > 0;
  const firstHoveredId = mapHover ? hoveredOnPage[0].sreality_id : null;

  /* "N of total" — N is what has accumulated so far via infinite scroll. */
  const loaded = rows?.length ?? 0;

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-center justify-between px-1 pb-3 text-[0.75rem] text-[var(--color-ink-3)] tabular-nums gap-3">
        <span className="min-w-0 truncate">
          {showSkeleton
            ? 'Loading…'
            : total == null
              ? '—'
              : total === 0
                ? '0 listings'
                : `${loaded.toLocaleString('cs-CZ')} of ${total.toLocaleString('cs-CZ')}`}
        </span>
        <div className="flex items-center gap-2 shrink-0">
          <SortDropdown sort={sort} onChange={onSort} />
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 min-h-0 overflow-y-auto pr-1">
        {showSkeleton && <SkeletonGrid />}
        {isEmpty && (
          <EmptyState
            hasFilters={hasFilters}
            hasBounds={hasBounds}
            onClearFilters={onClearFilters}
            onClearBounds={onClearBounds}
          />
        )}
        {!showSkeleton && rows && rows.length > 0 && (
          <>
            <ul className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-2">
              {rows.map((r) => (
                <li key={r.sreality_id}>
                  <Card
                    r={r}
                    hovered={hoveredIds.has(r.sreality_id)}
                    dimmed={mapHover && !hoveredIds.has(r.sreality_id)}
                    scrollOnHover={mapHover && r.sreality_id === firstHoveredId}
                    onHover={onHover}
                    mergeMode={mergeMode}
                    selected={selectedPropertyIds.has(r.property_id)}
                    onToggleSelect={onToggleSelect}
                    estimate={estimates?.[r.sreality_id]}
                    estimating={estimatingIds.has(r.sreality_id)}
                    onEstimate={onEstimate}
                  />
                </li>
              ))}
            </ul>
            <InfiniteSentinel
              onReach={onReachEnd}
              hasNextPage={hasNextPage}
              isFetchingNextPage={isFetchingNextPage}
              loadedCount={loaded}
              total={total}
              rootRef={scrollRef}
            />
          </>
        )}
      </div>
    </div>
  );
}

/* Pipeline bookmark toggle on a Browse card. Reads the shared member set (one
 * deduped query across all cards) and toggles the property in/out of the
 * pipeline's entry stage. Stops the click from triggering the card's Link. */
function BookmarkButton({ property_id }: { property_id: number }) {
  const qc = useQueryClient();
  const membersQ = useQuery({
    queryKey: pipelineKeys.members,
    queryFn: fetchPipelineMemberSet,
    staleTime: 30_000,
  });
  const inPipeline = membersQ.data?.has(property_id) ?? false;
  const onDone = () =>
    qc.invalidateQueries({ queryKey: pipelineKeys.members });
  const add = useMutation({
    mutationFn: () => addPipelineCard(property_id),
    onSuccess: onDone,
  });
  const remove = useMutation({
    mutationFn: () => removePipelineCard(property_id),
    onSuccess: onDone,
  });
  const pending = add.isPending || remove.isPending;

  return (
    <button
      type="button"
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        if (pending) return;
        (inPipeline ? remove : add).mutate();
      }}
      disabled={pending}
      aria-pressed={inPipeline}
      aria-label={inPipeline ? 'Odebrat z pipeline' : 'Přidat do pipeline'}
      title={inPipeline ? 'V pipeline — odebrat' : 'Přidat do pipeline'}
      className={[
        'flex items-center justify-center w-6 h-6 rounded-[var(--radius-xs)] border backdrop-blur transition-colors disabled:opacity-60',
        inPipeline
          ? 'bg-[var(--color-copper-soft)]/90 border-[var(--color-copper)] text-[var(--color-copper)]'
          : 'bg-[var(--color-paper-3)]/85 border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] hover:border-[var(--color-copper)]',
      ].join(' ')}
    >
      <FunnelIcon filled={inPipeline} className="h-3.5 w-3.5" />
    </button>
  );
}

/* Adjacent to the pipeline funnel (rule #22 keeps the funnel the sole pipeline
 * affordance): a distinct "save to collection" control — a layers glyph that
 * opens a popover of collections with checkmarks (monitored ones first, marked
 * with a bell). Orthogonal to the pipeline: collections are m2m groupings,
 * monitoring opts a collection into change alerts. Stops the card Link. */
function CollectionSaveButton({ property_id }: { property_id: number }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const collectionsQ = useQuery({
    queryKey: curationKeys.collections,
    queryFn: listCollections,
    staleTime: 30_000,
    enabled: open,
  });
  // One shared read across ALL cards (React Query dedupes the key), mirroring
  // the pipeline BookmarkButton — avoids one anon query per card on Browse.
  const membersQ = useQuery({
    queryKey: curationKeys.propertyCollectionMembers,
    queryFn: fetchPropertyCollectionMemberSet,
    staleTime: 30_000,
  });

  const memberIds = new Set(membersQ.data?.get(property_id) ?? []);
  const inAny = memberIds.size > 0;

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: curationKeys.propertyCollectionMembers });
    // Keep the per-property key (the listing-detail CurationBlock) consistent.
    qc.invalidateQueries({
      queryKey: curationKeys.propertyCollections(property_id),
    });
    qc.invalidateQueries({ queryKey: curationKeys.collections });
  };
  const add = useMutation({
    mutationFn: (cid: number) => addPropertiesToCollection(cid, [property_id]),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (cid: number) => removePropertyFromCollection(cid, property_id),
    onSuccess: invalidate,
  });
  const pending = add.isPending || remove.isPending;

  // Monitored collections first, then alphabetical.
  const sorted = [...(collectionsQ.data?.data ?? [])].sort(
    (a, b) =>
      (b.monitoring_enabled ? 1 : 0) - (a.monitoring_enabled ? 1 : 0) ||
      a.name.localeCompare(b.name),
  );

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        aria-label="Uložit do kolekce"
        aria-expanded={open}
        title="Uložit do kolekce"
        className={[
          'flex items-center justify-center w-6 h-6 rounded-[var(--radius-xs)] border backdrop-blur transition-colors',
          inAny
            ? 'bg-[var(--color-copper-soft)]/90 border-[var(--color-copper)] text-[var(--color-copper)]'
            : 'bg-[var(--color-paper-3)]/85 border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] hover:border-[var(--color-copper)]',
        ].join(' ')}
      >
        <CollectionGlyph filled={inAny} />
      </button>
      {open && (
        <div
          className="absolute top-7 left-0 z-20 w-56 rounded-[var(--radius-md)] bg-[var(--color-paper-3)] border border-[var(--color-rule-strong)] shadow-[0_4px_16px_rgba(0,0,0,0.08)] p-1.5"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
          }}
        >
          <p className="px-1.5 py-1 text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
            Save to collection
          </p>
          {collectionsQ.isLoading ? (
            <p className="px-1.5 py-1.5 text-[0.78rem] text-[var(--color-ink-3)]">
              Loading…
            </p>
          ) : sorted.length === 0 ? (
            <Link
              to="/collections"
              className="block px-1.5 py-1.5 text-[0.78rem] text-[var(--color-copper)] hover:underline"
            >
              Create a collection →
            </Link>
          ) : (
            <ul className="max-h-60 overflow-y-auto">
              {sorted.map((c) => {
                const member = memberIds.has(c.id);
                return (
                  <li key={c.id}>
                    <button
                      type="button"
                      disabled={pending}
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        (member ? remove : add).mutate(c.id);
                      }}
                      className="w-full flex items-center gap-2 px-1.5 py-1.5 text-left text-[0.82rem] rounded-[var(--radius-xs)] hover:bg-[var(--color-copper-soft)] disabled:opacity-60"
                    >
                      <span
                        aria-hidden
                        className={[
                          'inline-flex items-center justify-center w-4 h-4 shrink-0 rounded-[3px] border text-[0.6rem] leading-none',
                          member
                            ? 'bg-[var(--color-copper)] border-[var(--color-copper)] text-white'
                            : 'border-[var(--color-rule-strong)] text-transparent',
                        ].join(' ')}
                      >
                        ✓
                      </span>
                      <span className="truncate text-[var(--color-ink)]">
                        {c.name}
                      </span>
                      {c.monitoring_enabled && (
                        <span
                          title="Monitored — alerts on changes"
                          className="ml-auto shrink-0 text-[var(--color-copper)]"
                        >
                          <BellGlyph />
                        </span>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function CollectionGlyph({ filled }: { filled: boolean }) {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 16 16"
      fill={filled ? 'currentColor' : 'none'}
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M4 2.5 H12 V13.5 L8 10.75 L4 13.5 Z" strokeLinecap="round" />
    </svg>
  );
}

function BellGlyph() {
  return (
    <svg
      width="9"
      height="9"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M8 1.5a3.5 3.5 0 0 0-3.5 3.5c0 3-1.5 4-1.5 4h10s-1.5-1-1.5-4A3.5 3.5 0 0 0 8 1.5ZM6.5 12.5a1.5 1.5 0 0 0 3 0" />
    </svg>
  );
}

function Card({
  r,
  hovered,
  dimmed,
  scrollOnHover,
  onHover,
  mergeMode,
  selected,
  onToggleSelect,
  estimate,
  estimating,
  onEstimate,
}: {
  r: CardRow;
  hovered: boolean;
  /* A map-origin hover is lighting OTHER cards — this one recedes so
   * the group reads at a glance. */
  dimmed: boolean;
  /* First match of a map-origin hover: gently pull it into view in
   * case it sits below the fold of the card column. */
  scrollOnHover: boolean;
  onHover: (ids: ReadonlyArray<number> | null) => void;
  mergeMode: boolean;
  selected: boolean;
  onToggleSelect: (propertyId: number) => void;
  estimate: ListingEstimate | undefined;
  estimating: boolean;
  onEstimate: (srealityId: number) => void;
}) {
  /* Callback ref so the one ref serves both wrappers (Link → anchor,
   * merge-mode → div). */
  const wrapperElRef = useRef<HTMLElement | null>(null);
  const setWrapperEl = (el: HTMLElement | null) => {
    wrapperElRef.current = el;
  };
  useEffect(() => {
    if (hovered && scrollOnHover) {
      wrapperElRef.current?.scrollIntoView({
        block: 'nearest',
        behavior: 'smooth',
      });
    }
  }, [hovered, scrollOnHover]);
  const title = formatTitle(r);
  /* Precise place first (geo town when the free-text locality is just the okres
   * — the Bazoš "Jihlava"-for-Telč case), then the district/okres for context,
   * de-duped so we never render "Telč, Telč". */
  const place = [...new Set([placePrimary(r), r.district].filter(Boolean) as string[])].join(', ');
  const isRent = r.category_type === 'pronajem';
  const priceSuffix = isRent && r.price_czk != null ? ' /měs' : '';
  const inactive = !r.is_active;
  /* Inactive cards recede via surface tint + softened ink, not via
   * an opacity layer (which fights anti-aliasing and reads as broken
   * rather than archived). Surface drops to --color-inset, matching
   * "filed away" in the paper / archive language; image gets a
   * gentle desaturation so it still reads as a photo. */
  /* The hover-link state is OCHRE — the same surveyor's-mark color the
   * map uses for its locator halo, so the two ends of the link read as
   * one gesture. Copper stays reserved for committed selection. */
  const surface = selected
    ? 'bg-[var(--color-paper-2)] border-[var(--color-copper)] ring-1 ring-[var(--color-copper)]'
    : hovered
      ? 'bg-[var(--color-ochre-soft)] border-[var(--color-ochre)] ring-1 ring-[var(--color-ochre)]'
      : inactive
        ? 'bg-[var(--color-inset)] border-[var(--color-rule-soft)] hover:border-[var(--color-rule)]'
        : 'bg-[var(--color-paper-2)] border-[var(--color-rule)] hover:border-[var(--color-rule-strong)]';
  const titleColor  = inactive ? 'text-[var(--color-ink-2)]' : 'text-[var(--color-ink)]';
  const priceColor  = inactive ? 'text-[var(--color-ink-2)]' : 'text-[var(--color-ink)]';
  /* Status now reads off the card surface, so inactive photos desaturate
   * a touch harder than before — the only signal left in the photo lane. */
  const imageFilter = inactive
    ? 'saturate-[0.4] brightness-[0.97]'
    : '';

  /* One lifespan badge replaces the old status / od / viděno / TOM stack.
   * Active cards show the open run "od <date> · <N dní>"; inactive cards
   * show the closed run "<from> – <to> · <N dní>". The day count — the
   * operator's headline time-on-market metric — stays the copper accent,
   * folded inline. The Aktivní/Neaktivní word survives in the title. */
  const days = r.tom_days != null ? fmtTomDays(r.tom_days) : null;
  const lifespanTitle = inactive
    ? `Neaktivní${days ? ` · bylo na trhu ${days}` : ''} (${fmtShortDate(r.first_seen_at)} – ${fmtShortDate(r.last_seen_at)})`
    : `Aktivní${days ? ` · na trhu ${days}` : ''} (od ${fmtShortDate(r.first_seen_at)})`;

  const wrapperClass = [
    'group block rounded-[var(--radius-sm)] border overflow-hidden',
    'transition-[border-color,background-color,opacity] duration-150',
    dimmed ? 'opacity-50' : '',
    mergeMode ? 'cursor-pointer' : '',
    surface,
  ].join(' ');

  // The card body, rendered once and wrapped below by either a <Link> (normal)
  // or a selectable <div> (merge mode). Building it as a value — not a nested
  // component — keeps ImageCarousel's internal state across re-renders.
  const body = (
    <>
      <ImageCarousel urls={r.image_urls} imgClassName={imageFilter} hoverZoom fadeChevrons>
        {mergeMode && (
          <div className="absolute top-1 left-1 z-10">
            <span
              className={[
                'flex items-center justify-center w-6 h-6 rounded-[var(--radius-xs)] border',
                selected
                  ? 'bg-[var(--color-copper)] border-[var(--color-copper)] text-white'
                  : 'bg-[var(--color-paper)]/90 border-[var(--color-rule)] text-transparent',
              ].join(' ')}
              aria-label={selected ? 'Selected' : 'Not selected'}
            >
              <svg width="14" height="14" viewBox="0 0 12 12" fill="none" stroke="currentColor"
                strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M2.5 6.2 5 8.5l4.5-5" />
              </svg>
            </span>
          </div>
        )}
        {!mergeMode && (
          <div className="absolute top-1 left-1 z-10 flex items-center gap-1">
            <BookmarkButton property_id={r.property_id} />
            <CollectionSaveButton property_id={r.property_id} />
          </div>
        )}
        {/* Metadata margin: two file-tab badges down the right edge of
          * the photo — the lifespan run, then the source portal. Status
          * is carried by the card surface, not a pill. Borders-only,
          * paper-3/85 + backdrop-blur over the photo. */}
        <div className="absolute top-1 right-1 flex flex-col items-end gap-1">
          <CardBadge title={lifespanTitle}>
            {inactive ? (
              <>
                {fmtShortDate(r.first_seen_at)}
                <span className="opacity-50 mx-1">–</span>
                {fmtShortDate(r.last_seen_at)}
              </>
            ) : (
              <>
                <span className="opacity-60 mr-1">od</span>
                {fmtShortDate(r.first_seen_at)}
              </>
            )}
            {days && (
              <>
                <span className="opacity-40 mx-1">·</span>
                <span className="text-[var(--color-copper)]">{days}</span>
              </>
            )}
          </CardBadge>
          {portalLabel(r.source) && (
            <CardBadge title="Zdrojový portál">
              <span className="opacity-60 mr-1">portál</span>
              {portalLabel(r.source)}
            </CardBadge>
          )}
        </div>
      </ImageCarousel>
      <div className="p-2">
        <h3 className={`text-[0.78rem] leading-snug line-clamp-2 ${titleColor}`}>
          {title}
        </h3>
        {place && (
          <p className="mt-0.5 text-[0.68rem] text-[var(--color-ink-3)] truncate">
            {place}
          </p>
        )}
        <div className="mt-1 flex items-baseline justify-between gap-1">
          <p className={`text-[0.78rem] font-medium tabular-nums ${priceColor}`}>
            {r.price_czk != null ? (
              <>
                {fmtCzk(r.price_czk)}
                <span className="text-[var(--color-ink-3)] text-[0.65rem]">{priceSuffix}</span>
              </>
            ) : (
              <span className="text-[var(--color-ink-3)] text-[0.7rem]">Cena na vyžádání</span>
            )}
          </p>
          <p className="text-[0.62rem] text-[var(--color-ink-4)] tabular-nums whitespace-nowrap">
            {fmtPricePerM2(r.price_czk, r.area_m2)}
          </p>
        </div>
        {r.mf_gross_yield_pct != null && (
          <p
            className="mt-0.5 text-[0.62rem] text-[var(--color-ink-3)] tabular-nums"
            title="Hrubý výnos dle cenové mapy nájemného MF (nájem ÷ cena)"
          >
            Výnos MF{' '}
            <span className="text-[var(--color-ink)] font-medium">
              {r.mf_gross_yield_pct.toLocaleString('cs-CZ', {
                minimumFractionDigits: 1,
                maximumFractionDigits: 1,
              })}{' '}%
            </span>
          </p>
        )}
        {r.category_main === 'byt' && (
          <div className="mt-1 flex justify-end">
            <EstimateCorner
              srealityId={r.sreality_id}
              estimate={estimate}
              estimating={estimating}
              onEstimate={onEstimate}
            />
          </div>
        )}
      </div>
    </>
  );

  // In merge mode the card is a toggle, not a link — clicking selects it for
  // the dedup merge instead of navigating to the detail page.
  if (mergeMode) {
    return (
      <div
        ref={setWrapperEl}
        role="button"
        tabIndex={0}
        onClick={() => onToggleSelect(r.property_id)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onToggleSelect(r.property_id);
          }
        }}
        className={wrapperClass}
      >
        {body}
      </div>
    );
  }
  return (
    <Link
      ref={setWrapperEl}
      to={listingPath(r.sreality_id)}
      onMouseEnter={() => onHover([r.sreality_id])}
      onMouseLeave={() => onHover(null)}
      className={wrapperClass}
    >
      {body}
    </Link>
  );
}

function formatTitle(r: CardRow): string {
  const kind = categoryMainLabel(r.category_main);
  const deal = r.category_type === 'pronajem' ? 'k pronájmu' : 'na prodej';
  const parts = [`${kind} ${deal}`];
  if (r.disposition) parts.push(r.disposition);
  if (r.area_m2 != null) parts.push(fmtArea(r.area_m2));
  return parts.join(' · ');
}

/* -------------------------------------------------------------------------- */
/* EstimateCorner — bottom-right control on apartment cards. Runs the standard */
/* (agent) rental estimate on click; once a run exists it shows that run's     */
/* result IN PLACE of the button: the gross yield when the asking price is     */
/* known, otherwise the estimated monthly rent. Distinct from the muted        */
/* "Výnos MF" line above (a statistical reference) — copper accent marks it as */
/* our own estimate. Lives inside the card <Link> / merge toggle, so every     */
/* handler stops propagation to avoid navigating / toggling selection.         */
/* -------------------------------------------------------------------------- */

function EstimateCorner({
  srealityId,
  estimate,
  estimating,
  onEstimate,
}: {
  srealityId: number;
  estimate: ListingEstimate | undefined;
  estimating: boolean;
  onEstimate: (srealityId: number) => void;
}) {
  const navigate = useNavigate();
  const stop = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  const running =
    estimating ||
    estimate?.status === 'pending' ||
    estimate?.status === 'running';

  if (running) {
    return (
      <span
        className="inline-flex items-center gap-1 text-[0.62rem] text-[var(--color-ink-3)] tabular-nums"
        title="Odhad nájmu probíhá…"
      >
        <Spinner />
        Odhaduji…
      </span>
    );
  }

  if (estimate && estimate.status === 'success') {
    const label =
      estimate.gross_yield_pct != null
        ? `Výnos ~ ${estimate.gross_yield_pct.toLocaleString('cs-CZ', {
            minimumFractionDigits: 1,
            maximumFractionDigits: 1,
          })} %`
        : estimate.estimated_monthly_rent_czk != null
          ? `Nájem ~ ${fmtCzk(estimate.estimated_monthly_rent_czk)}/měs`
          : null;
    if (label != null) {
      return (
        <button
          type="button"
          onClick={(e) => {
            stop(e);
            navigate(
              runSurfaceUrl({ id: estimate.run_id, input_sreality_id: estimate.sreality_id }),
            );
          }}
          title="Náš odhad nájmu — otevřít detail odhadu"
          className="inline-flex items-center rounded-[var(--radius-xs)] border border-[var(--color-copper)] px-1.5 py-0.5 text-[0.62rem] font-medium tabular-nums text-[var(--color-copper)] hover:bg-[var(--color-copper)]/10 transition-colors"
        >
          {label}
        </button>
      );
    }
  }

  const failed = estimate?.status === 'failed';
  return (
    <button
      type="button"
      onClick={(e) => {
        stop(e);
        onEstimate(srealityId);
      }}
      title={failed ? 'Odhad selhal — zkusit znovu' : 'Spustit odhad nájmu a výnosu'}
      className="inline-flex items-center gap-1 rounded-[var(--radius-xs)] border border-[var(--color-rule)] px-1.5 py-0.5 text-[0.62rem] font-medium text-[var(--color-ink-2)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)] transition-colors"
    >
      {failed ? 'Odhad ↻' : 'Odhad'}
    </button>
  );
}


/* -------------------------------------------------------------------------- */
/* CardBadge — the building block of the metadata stack on the right margin of */
/* a listing card. One muted treatment: a near-opaque paper-3 base + rule      */
/* border + ink-2 text, so it stays legible over any photo without shouting.   */
/* Semantic colour (the copper time-on-market figure) is applied per-child by  */
/* the caller, not via a tone prop — status itself now reads off the card      */
/* surface rather than a coloured pill. 0.6rem uppercase tracking keeps the    */
/* file-tab silhouette consistent across cards.                                */
/* -------------------------------------------------------------------------- */

function CardBadge({
  children,
  title,
}: {
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={[
        'inline-flex items-center px-1.5 py-0.5 text-[0.6rem] tracking-[0.12em]',
        'uppercase rounded-[var(--radius-xs)] border backdrop-blur-sm font-medium',
        'tabular-nums whitespace-nowrap',
        'bg-[var(--color-paper-3)]/85 border-[var(--color-rule)] text-[var(--color-ink-2)]',
      ].join(' ')}
    >
      {children}
    </span>
  );
}

/* Headline sort orders for the cards lane. Two bookend the file by
 * date (first_seen_at — when the listing entered our archive), two
 * by price, two by price/m², two by MF gross yield. The default keeps
 * last_seen_at desc so the dropdown's "selected" option matches the
 * URL on a fresh load even though that order isn't in the menu —
 * operators land on "newest in archive" mentally and the existing
 * default already approximates that. mf_gross_yield_pct is only
 * populated for sale apartments; the query orders nullsFirst:false so
 * every rental / non-apartment listing sorts to the end either way. */
const SORT_PRESETS: ReadonlyArray<{ label: string; spec: SortSpec }> = [
  { label: 'Newest first',      spec: { field: 'first_seen_at', direction: 'desc' } },
  { label: 'Oldest first',      spec: { field: 'first_seen_at', direction: 'asc'  } },
  { label: 'Price: low → high', spec: { field: 'price_czk',     direction: 'asc'  } },
  { label: 'Price: high → low', spec: { field: 'price_czk',     direction: 'desc' } },
  { label: 'Price/m²: low → high', spec: { field: 'price_per_m2', direction: 'asc'  } },
  { label: 'Price/m²: high → low', spec: { field: 'price_per_m2', direction: 'desc' } },
  { label: 'Výnos MF: high → low', spec: { field: 'mf_gross_yield_pct', direction: 'desc' } },
  { label: 'Výnos MF: low → high', spec: { field: 'mf_gross_yield_pct', direction: 'asc'  } },
];

function SortDropdown({
  sort,
  onChange,
}: {
  sort: SortSpec;
  onChange: (next: SortSpec) => void;
}) {
  const current = sortToParam(sort);
  return (
    <label className="inline-flex items-center gap-1.5">
      <span className="text-[0.65rem] tracking-[0.12em] uppercase text-[var(--color-ink-3)]">
        Sort
      </span>
      <select
        value={current}
        onChange={(e) => {
          const picked = SORT_PRESETS.find(
            (p) => sortToParam(p.spec) === e.target.value,
          );
          if (picked) onChange(picked.spec);
        }}
        className="px-2 py-1 text-[0.7rem] rounded-[var(--radius-sm)] bg-[var(--color-paper-2)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] focus:outline-none focus:border-[var(--color-rule-strong)] transition-colors"
      >
        {!SORT_PRESETS.some((p) => sortToParam(p.spec) === current) && (
          <option value={current}>Default</option>
        )}
        {SORT_PRESETS.map((p) => (
          <option key={sortToParam(p.spec)} value={sortToParam(p.spec)}>
            {p.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function SkeletonGrid() {
  return (
    <ul className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-2">
      {Array.from({ length: 8 }).map((_, i) => (
        <li
          key={i}
          className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] overflow-hidden"
        >
          <div className="aspect-[5/4] bg-[var(--color-inset)] animate-pulse" />
          <div className="p-2 space-y-1.5">
            <div className="h-3 w-3/4 bg-[var(--color-inset)] rounded animate-pulse" />
            <div className="h-2.5 w-1/2 bg-[var(--color-inset)] rounded animate-pulse" />
            <div className="h-3 w-1/3 bg-[var(--color-inset)] rounded animate-pulse" />
          </div>
        </li>
      ))}
    </ul>
  );
}

function EmptyState({
  hasFilters,
  hasBounds,
  onClearFilters,
  onClearBounds,
}: {
  hasFilters: boolean;
  hasBounds: boolean;
  onClearFilters: () => void;
  onClearBounds: () => void;
}) {
  const message = hasBounds
    ? 'No listings in this map area.'
    : 'No listings match these filters.';
  return (
    <div className="rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] bg-[var(--color-paper-2)] p-8 text-center">
      <p className="text-sm text-[var(--color-ink-2)]">{message}</p>
      <div className="mt-3 flex items-center justify-center gap-3">
        {hasBounds && (
          <button
            type="button"
            onClick={onClearBounds}
            className="text-[0.75rem] tracking-wide uppercase text-[var(--color-copper)] hover:underline"
          >
            Show all
          </button>
        )}
        {hasFilters && (
          <button
            type="button"
            onClick={onClearFilters}
            className="text-[0.75rem] tracking-wide uppercase text-[var(--color-copper)] hover:underline"
          >
            Clear filters
          </button>
        )}
      </div>
    </div>
  );
}
