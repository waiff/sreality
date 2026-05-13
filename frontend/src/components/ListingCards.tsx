import { Link } from 'react-router-dom';
import { CARD_PAGE_SIZE, type CardRow } from '@/lib/queries';
import { fmtArea, fmtCzk, fmtPricePerM2, fmtRelative } from '@/lib/format';

interface Props {
  rows: CardRow[] | null;
  total: number | null;
  page: number;
  isLoading: boolean;
  hasFilters: boolean;
  onPage: (page: number) => void;
  onClearFilters: () => void;
}

export default function ListingCards({
  rows,
  total,
  page,
  isLoading,
  hasFilters,
  onPage,
  onClearFilters,
}: Props) {
  const showSkeleton = isLoading && rows == null;
  const isEmpty = !showSkeleton && rows != null && rows.length === 0;

  const totalPages =
    total != null && total > 0 ? Math.ceil(total / CARD_PAGE_SIZE) : 1;
  const start = (page - 1) * CARD_PAGE_SIZE + 1;
  const end = Math.min(start + (rows?.length ?? 0) - 1, total ?? 0);

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-center justify-between px-1 pb-3 text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
        <span>
          {showSkeleton
            ? 'Loading…'
            : total == null
              ? '—'
              : total === 0
                ? '0 listings'
                : `${start.toLocaleString('cs-CZ')}–${end.toLocaleString('cs-CZ')} of ${total.toLocaleString('cs-CZ')}`}
        </span>
        <Pager page={page} totalPages={totalPages} onPage={onPage} />
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto pr-1">
        {showSkeleton && <SkeletonGrid />}
        {isEmpty && (
          <EmptyState
            hasFilters={hasFilters}
            onClearFilters={onClearFilters}
          />
        )}
        {!showSkeleton && rows && rows.length > 0 && (
          <ul className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {rows.map((r) => (
              <li key={r.sreality_id}>
                <Card r={r} />
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

function Card({ r }: { r: CardRow }) {
  const title = formatTitle(r);
  const place = [r.locality, r.district].filter(Boolean).join(', ');
  const isRent = r.category_type === 'pronajem';
  const priceSuffix = isRent && r.price_czk != null ? ' / měsíc' : '';
  return (
    <Link
      to={`/listing/${r.sreality_id}`}
      className="group block rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] hover:border-[var(--color-rule-strong)] transition-colors overflow-hidden"
    >
      <div className="aspect-[4/3] bg-[var(--color-inset)] overflow-hidden relative">
        {r.image_url ? (
          <img
            src={r.image_url}
            alt=""
            loading="lazy"
            className="w-full h-full object-cover transition-transform duration-200 group-hover:scale-[1.02]"
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.visibility = 'hidden';
            }}
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-[0.7rem] tracking-wider uppercase text-[var(--color-ink-4)]">
            no image
          </div>
        )}
        {!r.is_active && (
          <span className="absolute top-2 left-2 px-1.5 py-0.5 text-[0.65rem] tracking-wider uppercase rounded-[var(--radius-xs)] bg-[var(--color-paper-3)]/95 border border-[var(--color-rule)] text-[var(--color-ink-3)]">
            inactive
          </span>
        )}
      </div>
      <div className="p-3">
        <h3 className="text-sm leading-snug text-[var(--color-ink)] line-clamp-2">
          {title}
        </h3>
        {place && (
          <p className="mt-0.5 text-[0.75rem] text-[var(--color-ink-3)] truncate">
            {place}
          </p>
        )}
        <div className="mt-2 flex items-baseline justify-between gap-2">
          <p className="text-sm font-medium text-[var(--color-ink)] tabular-nums">
            {fmtCzk(r.price_czk)}
            <span className="text-[var(--color-ink-3)] text-xs">{priceSuffix}</span>
          </p>
          <p className="text-[0.7rem] text-[var(--color-ink-4)] tabular-nums">
            {fmtPricePerM2(r.price_czk, r.area_m2)}
          </p>
        </div>
        <p className="mt-0.5 text-[0.7rem] text-[var(--color-ink-4)] tabular-nums">
          last seen {fmtRelative(r.last_seen_at)}
        </p>
      </div>
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
    <ul className="grid grid-cols-1 sm:grid-cols-2 gap-3">
      {Array.from({ length: 6 }).map((_, i) => (
        <li key={i} className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] overflow-hidden">
          <div className="aspect-[4/3] bg-[var(--color-inset)] animate-pulse" />
          <div className="p-3 space-y-2">
            <div className="h-3.5 w-3/4 bg-[var(--color-inset)] rounded animate-pulse" />
            <div className="h-3 w-1/2 bg-[var(--color-inset)] rounded animate-pulse" />
            <div className="h-3.5 w-1/3 bg-[var(--color-inset)] rounded animate-pulse" />
          </div>
        </li>
      ))}
    </ul>
  );
}

function EmptyState({
  hasFilters,
  onClearFilters,
}: {
  hasFilters: boolean;
  onClearFilters: () => void;
}) {
  return (
    <div className="rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] bg-[var(--color-paper-2)] p-8 text-center">
      <p className="text-sm text-[var(--color-ink-2)]">No listings match these filters.</p>
      {hasFilters && (
        <button
          type="button"
          onClick={onClearFilters}
          className="mt-3 text-[0.75rem] tracking-wide uppercase text-[var(--color-copper)] hover:underline"
        >
          Clear filters
        </button>
      )}
    </div>
  );
}
