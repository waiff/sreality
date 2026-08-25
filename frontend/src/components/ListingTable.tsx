import { Link } from 'react-router-dom';
import InfiniteSentinel from '@/components/InfiniteSentinel';
import PipelineFunnelButton from '@/components/PipelineFunnelButton';
import PriceDelta from '@/components/PriceDelta';
import {
  type TableRow,
  type SortSpec,
  type SortField,
} from '@/lib/queries';
import {
  fmtCount, fmtArea, fmtCzk, fmtMeasuredPricePerM2, fmtRelative, fmtAbsolute,
  fmtFurnished, fmtOwnership, fmtParkingLots,
} from '@/lib/format';
import { ppm2Basis } from '@/lib/measure';
import type { Furnished, Ownership } from '@/lib/types';
import { placePrimary } from '@/lib/placeLabel';
import { listingKindLabel } from '@/lib/enums';
import { listingRowPath } from '@/lib/listingUrl';

interface Column {
  field: SortField | 'furnished' | 'ownership' | 'pipeline';
  label: string;
  /* Header text for assistive tech when `label` is intentionally blank (an
     icon-only column). */
  srLabel?: string;
  align?: 'left' | 'right';
  sortable: boolean;
}

const COLUMNS: ReadonlyArray<Column> = [
  /* The deal-pipeline funnel (rule #22 — the affordance belongs on EVERY
     surface a property appears on; the Table was the one that never got it).
     Not sortable: membership is operator state, not a listing attribute, and
     the cohort-level way to see only pipeline rows is the Pipeline scope. */
  { field: 'pipeline',      label: '',            align: 'left',  sortable: false,
    srLabel: 'Pipeline' },
  /* Not sortable: sreality_id mixes real positive ids with synthetic negative
   * ones (non-sreality portals), so ordering by it is meaningless. */
  { field: 'sreality_id',   label: 'ID',          align: 'left',  sortable: false },
  { field: 'district',      label: 'Location',    align: 'left',  sortable: true  },
  { field: 'disposition',   label: 'Type',        align: 'left',  sortable: true  },
  { field: 'area_m2',       label: 'Area',        align: 'right', sortable: true  },
  { field: 'estate_area',   label: 'Lot',         align: 'right', sortable: true  },
  { field: 'price_czk',     label: 'Price',       align: 'right', sortable: true  },
  { field: 'price_per_m2',  label: 'Price / m²',  align: 'right', sortable: true  },
  { field: 'parking_lots',  label: 'Parking',     align: 'right', sortable: true  },
  { field: 'furnished',     label: 'Furnished',   align: 'left',  sortable: false },
  { field: 'ownership',     label: 'Ownership',   align: 'left',  sortable: false },
  { field: 'last_seen_at',  label: 'Last seen',   align: 'left',  sortable: true  },
  { field: 'is_active',     label: 'Status',      align: 'left',  sortable: true  },
];

interface Props {
  rows: TableRow[] | null;
  total: number | null;
  /* `total` is an approximate (planner-estimate) cohort size — render "~N". */
  totalApprox?: boolean;
  sort: SortSpec;
  isLoading: boolean;
  /* Suppresses the "no results" empty row when the query errored — the page
   * ErrorBanner explains the failure instead. Mirrors ListingCards. */
  isError?: boolean;
  isFetchingNextPage: boolean;
  hasNextPage: boolean;
  onReachEnd: () => void;
  hasFilters: boolean;
  hoveredIds: ReadonlySet<number>;
  onHover: (ids: ReadonlyArray<number> | null) => void;
  onSort: (field: SortField) => void;
  onClearFilters: () => void;
  /* The cohort is filtered by deal-pipeline membership (the Pipeline scope), so
     a funnel write changes which rows match — see usePipelineCard. */
  pipelineScoped: boolean;
}

export default function ListingTable({
  rows,
  total,
  totalApprox = false,
  sort,
  isLoading,
  isError = false,
  isFetchingNextPage,
  hasNextPage,
  onReachEnd,
  hasFilters,
  hoveredIds,
  onHover,
  onSort,
  onClearFilters,
  pipelineScoped,
}: Props) {
  const showSkeleton = isLoading && rows == null;
  const isEmpty = !showSkeleton && !isError && rows != null && rows.length === 0;
  const loaded = rows?.length ?? 0;

  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-[var(--color-paper-2)] border-b border-[var(--color-rule)]">
            <tr>
              {COLUMNS.map((col) => (
                <Th
                  key={col.field}
                  col={col}
                  active={col.field === sort.field}
                  direction={sort.direction}
                  onClick={() => col.sortable && onSort(col.field as SortField)}
                />
              ))}
            </tr>
          </thead>
          <tbody>
            {showSkeleton && <SkeletonRows />}
            {isEmpty && <EmptyRow hasFilters={hasFilters} onClear={onClearFilters} />}
            {!showSkeleton && rows?.map((r) => (
              <Row
                key={r.listing_id}
                row={r}
                hovered={hoveredIds.has(r.listing_id)}
                onHover={onHover}
                pipelineScoped={pipelineScoped}
              />
            ))}
          </tbody>
        </table>
      </div>

      {!showSkeleton && !isEmpty && (
        <InfiniteSentinel
          onReach={onReachEnd}
          hasNextPage={hasNextPage}
          isFetchingNextPage={isFetchingNextPage}
          loadedCount={loaded}
          total={total}
        />
      )}

      <div className="flex items-center justify-between gap-4 px-4 py-2.5 border-t border-[var(--color-rule)] bg-[var(--color-paper)]">
        <p className="text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
          {total == null
            ? <>—</>
            : total === 0
              ? <>No listings</>
              : <>Showing <span className="text-[var(--color-ink-2)]">{fmtCount(loaded)}</span> of <span className="text-[var(--color-ink-2)]">{totalApprox ? '~' : ''}{fmtCount(total)}</span></>}
        </p>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function Th({
  col,
  active,
  direction,
  onClick,
}: {
  col: Column;
  active: boolean;
  direction: 'asc' | 'desc';
  onClick: () => void;
}) {
  const align = col.align === 'right' ? 'text-right' : 'text-left';
  const cursor = col.sortable ? 'cursor-pointer hover:text-[var(--color-ink)]' : 'cursor-default';
  return (
    <th
      scope="col"
      onClick={onClick}
      className={[
        'px-4 py-2.5 text-[0.7rem] tracking-[0.14em] uppercase font-medium select-none transition-colors',
        align,
        cursor,
        active ? 'text-[var(--color-copper)]' : 'text-[var(--color-ink-3)]',
      ].join(' ')}
      aria-sort={active ? (direction === 'asc' ? 'ascending' : 'descending') : 'none'}
    >
      <span className="inline-flex items-center gap-1.5">
        {col.label || <span className="sr-only">{col.srLabel}</span>}
        {col.sortable && (
          <SortIndicator active={active} direction={direction} />
        )}
      </span>
    </th>
  );
}

function SortIndicator({ active, direction }: { active: boolean; direction: 'asc' | 'desc' }) {
  if (!active) {
    return (
      <svg width="8" height="10" viewBox="0 0 8 10" className="text-[var(--color-ink-4)] flex-shrink-0">
        <path d="M4 1 L7 4 L1 4 Z" fill="currentColor" opacity=".55" />
        <path d="M4 9 L1 6 L7 6 Z" fill="currentColor" opacity=".55" />
      </svg>
    );
  }
  return (
    <svg width="8" height="10" viewBox="0 0 8 10" className="text-[var(--color-copper)] flex-shrink-0">
      {direction === 'asc'
        ? <path d="M4 1 L7 4 L1 4 Z" fill="currentColor" />
        : <path d="M4 9 L1 6 L7 6 Z" fill="currentColor" />}
    </svg>
  );
}

/* -------------------------------------------------------------------------- */

function Row({
  row,
  hovered,
  onHover,
  pipelineScoped,
}: {
  row: TableRow;
  hovered: boolean;
  onHover: (ids: ReadonlyArray<number> | null) => void;
  pipelineScoped: boolean;
}) {
  /* Cross-source hover: own mouseenter sets the shared id; the same
   * highlight also fires when the matching pin is hovered on the map.
   * Background is the same copper-soft tint either way — the eye
   * doesn't need to distinguish "I hovered this" from "the map
   * surfaced this", just "these belong together". */
  return (
    <tr
      onMouseEnter={() => onHover([row.listing_id])}
      onMouseLeave={() => onHover(null)}
      className={[
        'border-b border-[var(--color-rule-soft)] transition-colors',
        hovered
          ? 'bg-[var(--color-copper-soft)]'
          : 'hover:bg-[var(--color-copper-soft)]/40',
      ].join(' ')}
    >
      <td className="pl-3 pr-1 py-2.5 align-middle">
        <PipelineFunnelButton
          property_id={row.property_id}
          cohortScoped={pipelineScoped}
          variant="inline"
        />
      </td>
      <td className="px-4 py-2.5 align-middle">
        <Link
          to={listingRowPath(row)}
          state={{ listingId: row.listing_id }}
          className="font-mono tabular-nums text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          {/* Portal-native id; a post-Gate-2 non-sreality row has none, so the
              cell shows a dash while the link falls back to the property route. */}
          {row.sreality_id ?? '—'}
        </Link>
      </td>
      <td className="px-4 py-2.5 align-middle text-[var(--color-ink)] truncate max-w-[260px]">
        {placePrimary(row) ?? <span className="text-[var(--color-ink-4)]">—</span>}
      </td>
      <td className="px-4 py-2.5 align-middle font-mono tabular-nums text-[var(--color-ink-2)]">
        {listingKindLabel(row) ?? <span className="text-[var(--color-ink-4)]">—</span>}
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink)]">
        {fmtArea(row.area_m2)}
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {row.estate_area == null
          ? <span className="text-[var(--color-ink-4)]">—</span>
          : fmtArea(row.estate_area)}
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink)]">
        <span className="inline-flex items-baseline justify-end gap-1.5">
          {fmtCzk(row.price_czk)}
          {/* A rental price is a MONTHLY figure and the cards have always said
              so; the table printed the same number bare, so a 18 000 Kč rent and
              an 18 000 Kč (absurd) sale read identically. */}
          {row.category_type === 'pronajem' && row.price_czk != null && (
            <span className="text-[var(--color-ink-4)] text-[0.7rem]">/měs</span>
          )}
          {/* Same component the cards and the pipeline board use. */}
          <PriceDelta
            pct={row.total_price_change_pct}
            changes={row.price_change_count}
            muted={!row.is_active}
          />
        </span>
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {/* Basis resolved PER ROW, never per table: the default Browse cohort
            can be `deal=any`, so a sale row and a rent row sit in the same
            column and must carry different units. */}
        {fmtMeasuredPricePerM2(row.price_per_m2, ppm2Basis(row.category_main, row.category_type))}
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {row.parking_lots == null
          ? <span className="text-[var(--color-ink-4)]">—</span>
          : fmtParkingLots(row.parking_lots)}
      </td>
      <td className="px-4 py-2.5 align-middle text-[var(--color-ink-2)]">
        {row.furnished == null
          ? <span className="text-[var(--color-ink-4)]">—</span>
          : fmtFurnished(row.furnished as Furnished)}
      </td>
      <td className="px-4 py-2.5 align-middle text-[var(--color-ink-2)]">
        {row.ownership == null
          ? <span className="text-[var(--color-ink-4)]">—</span>
          : fmtOwnership(row.ownership as Ownership)}
      </td>
      <td
        className="px-4 py-2.5 align-middle text-[var(--color-ink-2)] tabular-nums"
        title={fmtAbsolute(row.last_seen_at)}
      >
        {fmtRelative(row.last_seen_at)}
      </td>
      <td className="px-4 py-2.5 align-middle">
        <StatusPill active={row.is_active} />
      </td>
    </tr>
  );
}

function StatusPill({ active }: { active: boolean }) {
  if (active) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-xs)] text-[0.65rem] tracking-wide uppercase font-medium bg-[var(--color-sage-soft)] text-[var(--color-sage)]">
        active
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-xs)] text-[0.65rem] tracking-wide uppercase font-medium bg-[var(--color-brick-soft)] text-[var(--color-brick)]">
      inactive
    </span>
  );
}

/* -------------------------------------------------------------------------- */

function SkeletonRows() {
  return (
    <>
      {Array.from({ length: 8 }).map((_, i) => (
        <tr key={i} className="border-b border-[var(--color-rule-soft)]">
          {COLUMNS.map((col, j) => (
            <td key={j} className="px-4 py-3">
              <span
                className="block h-3 rounded-[var(--radius-xs)] bg-[var(--color-inset)] animate-pulse"
                style={{ width: col.align === 'right' ? '60%' : '80%', marginLeft: col.align === 'right' ? 'auto' : 0 }}
              />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

function EmptyRow({
  hasFilters,
  onClear,
}: {
  hasFilters: boolean;
  onClear: () => void;
}) {
  return (
    <tr>
      <td colSpan={COLUMNS.length} className="px-4 py-16 text-center">
        <p className="text-sm text-[var(--color-ink-3)]">
          No listings match these filters.
        </p>
        {hasFilters && (
          <button
            type="button"
            onClick={onClear}
            className="mt-3 text-[0.75rem] tracking-wide text-[var(--color-copper)] hover:underline underline-offset-2"
          >
            Clear filters →
          </button>
        )}
      </td>
    </tr>
  );
}

