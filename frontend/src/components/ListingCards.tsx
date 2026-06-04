import { type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import ImageCarousel from '@/components/ImageCarousel';
import {
  CARD_PAGE_SIZE,
  sortToParam,
  type CardRow,
  type SortSpec,
} from '@/lib/queries';
import {
  fmtArea, fmtCzk, fmtPricePerM2,
  fmtShortDate, fmtTomDays,
} from '@/lib/format';
import { portalLabel } from '@/lib/portals';

interface Props {
  rows: CardRow[] | null;
  total: number | null;
  page: number;
  sort: SortSpec;
  isLoading: boolean;
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
  onPage: (page: number) => void;
  onSort: (next: SortSpec) => void;
  onClearFilters: () => void;
  onClearBounds: () => void;
  /* Dedup merge mode: when on, cards show a selection checkbox and a click
   * toggles selection instead of navigating. selected holds the picked
   * property_ids. */
  mergeMode: boolean;
  selectedPropertyIds: ReadonlySet<number>;
  onToggleSelect: (propertyId: number) => void;
}

export default function ListingCards({
  rows,
  total,
  page,
  sort,
  isLoading,
  hasFilters,
  hasBounds,
  hoveredIds,
  onHover,
  onPage,
  onSort,
  onClearFilters,
  onClearBounds,
  mergeMode,
  selectedPropertyIds,
  onToggleSelect,
}: Props) {
  const showSkeleton = isLoading && rows == null;
  const isEmpty = !showSkeleton && rows != null && rows.length === 0;

  const totalPages =
    total != null && total > 0 ? Math.ceil(total / CARD_PAGE_SIZE) : 1;
  const start = (page - 1) * CARD_PAGE_SIZE + 1;
  const end = Math.min(start + (rows?.length ?? 0) - 1, total ?? 0);

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
                : `${start.toLocaleString('cs-CZ')}–${end.toLocaleString('cs-CZ')} of ${total.toLocaleString('cs-CZ')}`}
        </span>
        <div className="flex items-center gap-2 shrink-0">
          <SortDropdown sort={sort} onChange={onSort} />
          <Pager page={page} totalPages={totalPages} onPage={onPage} />
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto pr-1">
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
          <ul className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-2">
            {rows.map((r) => (
              <li key={r.sreality_id}>
                <Card
                  r={r}
                  hovered={hoveredIds.has(r.sreality_id)}
                  onHover={onHover}
                  mergeMode={mergeMode}
                  selected={selectedPropertyIds.has(r.property_id)}
                  onToggleSelect={onToggleSelect}
                />
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="pt-3 flex items-center justify-end">
        <Pager page={page} totalPages={totalPages} onPage={onPage} />
      </div>
    </div>
  );
}

function Card({
  r,
  hovered,
  onHover,
  mergeMode,
  selected,
  onToggleSelect,
}: {
  r: CardRow;
  hovered: boolean;
  onHover: (ids: ReadonlyArray<number> | null) => void;
  mergeMode: boolean;
  selected: boolean;
  onToggleSelect: (propertyId: number) => void;
}) {
  const title = formatTitle(r);
  const place = [r.locality, r.district].filter(Boolean).join(', ');
  const isRent = r.category_type === 'pronajem';
  const priceSuffix = isRent && r.price_czk != null ? ' /měs' : '';
  const inactive = !r.is_active;
  /* Inactive cards recede via surface tint + softened ink, not via
   * an opacity layer (which fights anti-aliasing and reads as broken
   * rather than archived). Surface drops to --color-inset, matching
   * "filed away" in the paper / archive language; image gets a
   * gentle desaturation so it still reads as a photo. */
  const surface = selected
    ? 'bg-[var(--color-paper-2)] border-[var(--color-copper)] ring-1 ring-[var(--color-copper)]'
    : hovered
      ? 'bg-[var(--color-paper-2)] border-[var(--color-copper)]'
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
    'group block rounded-[var(--radius-sm)] border transition-colors overflow-hidden',
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
      </div>
    </>
  );

  // In merge mode the card is a toggle, not a link — clicking selects it for
  // the dedup merge instead of navigating to the detail page.
  if (mergeMode) {
    return (
      <div
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
      to={`/listing/${r.sreality_id}`}
      onMouseEnter={() => onHover([r.sreality_id])}
      onMouseLeave={() => onHover(null)}
      className={wrapperClass}
    >
      {body}
    </Link>
  );
}

function formatTitle(r: CardRow): string {
  const kind = (() => {
    if (r.category_main === 'byt') return 'Byt';
    if (r.category_main === 'dum') return 'Dům';
    if (r.category_main === 'komercni') return 'Komerční prostor';
    return 'Nemovitost';
  })();
  const deal = r.category_type === 'pronajem' ? 'k pronájmu' : 'na prodej';
  const parts = [`${kind} ${deal}`];
  if (r.disposition) parts.push(r.disposition);
  if (r.area_m2 != null) parts.push(fmtArea(r.area_m2));
  return parts.join(' · ');
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

function Pager({
  page,
  totalPages,
  onPage,
}: {
  page: number;
  totalPages: number;
  onPage: (p: number) => void;
}) {
  if (totalPages <= 1) return null;
  return (
    <div className="flex items-center gap-1">
      <PagerButton onClick={() => onPage(Math.max(1, page - 1))} disabled={page <= 1}>
        ‹
      </PagerButton>
      <span className="px-1 text-[0.7rem] text-[var(--color-ink-3)] tabular-nums">
        {page} / {totalPages}
      </span>
      <PagerButton
        onClick={() => onPage(Math.min(totalPages, page + 1))}
        disabled={page >= totalPages}
      >
        ›
      </PagerButton>
    </div>
  );
}

function PagerButton({
  onClick,
  disabled,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="w-7 h-7 inline-flex items-center justify-center rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:text-[var(--color-ink)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors text-sm leading-none"
    >
      {children}
    </button>
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
