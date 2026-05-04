import { Link } from 'react-router-dom';
import {
  TABLE_PAGE_SIZE,
  type TableRow,
  type SortSpec,
  type SortField,
} from '@/lib/queries';
import { fmtCount, fmtArea, fmtCzk, fmtPricePerM2, fmtRelative, fmtAbsolute } from '@/lib/format';

interface Column {
  field: SortField | 'price_per_m2';
  label: string;
  align?: 'left' | 'right';
  sortable: boolean;
}

const COLUMNS: ReadonlyArray<Column> = [
  { field: 'sreality_id',   label: 'ID',          align: 'left',  sortable: true  },
  { field: 'district',      label: 'District',    align: 'left',  sortable: true  },
  { field: 'disposition',   label: 'Type',        align: 'left',  sortable: true  },
  { field: 'area_m2',       label: 'Area',        align: 'right', sortable: true  },
  { field: 'price_czk',     label: 'Price',       align: 'right', sortable: true  },
  { field: 'price_per_m2',  label: 'Price / m²',  align: 'right', sortable: false },
  { field: 'last_seen_at',  label: 'Last seen',   align: 'left',  sortable: true  },
  { field: 'is_active',     label: 'Status',      align: 'left',  sortable: true  },
];

interface Props {
  rows: TableRow[] | null;
  total: number | null;
  page: number;
  sort: SortSpec;
  isLoading: boolean;
  hasFilters: boolean;
  onSort: (field: SortField) => void;
  onPage: (page: number) => void;
  onClearFilters: () => void;
}

export default function ListingTable({
  rows,
  total,
  page,
  sort,
  isLoading,
  hasFilters,
  onSort,
  onPage,
  onClearFilters,
}: Props) {
  const showSkeleton = isLoading && rows == null;
  const isEmpty = !showSkeleton && rows != null && rows.length === 0;

  const totalPages = total != null && total > 0 ? Math.ceil(total / TABLE_PAGE_SIZE) : 1;
  const start = (page - 1) * TABLE_PAGE_SIZE + 1;
  const end = Math.min(start + (rows?.length ?? 0) - 1, total ?? 0);

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
            {!showSkeleton && rows?.map((r) => <Row key={r.sreality_id} row={r} />)}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between gap-4 px-4 py-2.5 border-t border-[var(--color-rule)] bg-[var(--color-paper)]">
        <p className="text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
          {total == null
            ? <>—</>
            : total === 0
              ? <>No listings</>
              : <>Showing <span className="text-[var(--color-ink-2)]">{fmtCount(start)}–{fmtCount(end)}</span> of <span className="text-[var(--color-ink-2)]">{fmtCount(total)}</span></>}
        </p>
        <Pagination page={page} totalPages={totalPages} onPage={onPage} disabled={total == null || total === 0} />
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
        {col.label}
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

function Row({ row }: { row: TableRow }) {
  return (
    <tr className="border-b border-[var(--color-rule-soft)] hover:bg-[var(--color-copper-soft)]/40 transition-colors">
      <td className="px-4 py-2.5 align-middle">
        <Link
          to={`/listing/${row.sreality_id}`}
          className="font-mono tabular-nums text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          {row.sreality_id}
        </Link>
      </td>
      <td className="px-4 py-2.5 align-middle text-[var(--color-ink)] truncate max-w-[260px]">
        {row.district ?? <span className="text-[var(--color-ink-4)]">—</span>}
      </td>
      <td className="px-4 py-2.5 align-middle font-mono tabular-nums text-[var(--color-ink-2)]">
        {row.disposition ?? <span className="text-[var(--color-ink-4)]">—</span>}
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink)]">
        {fmtArea(row.area_m2)}
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink)]">
        {fmtCzk(row.price_czk)}
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {fmtPricePerM2(row.price_czk, row.area_m2)}
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

/* -------------------------------------------------------------------------- */

function Pagination({
  page,
  totalPages,
  onPage,
  disabled,
}: {
  page: number;
  totalPages: number;
  onPage: (n: number) => void;
  disabled: boolean;
}) {
  const prevDisabled = disabled || page <= 1;
  const nextDisabled = disabled || page >= totalPages;
  return (
    <nav className="flex items-center gap-1" aria-label="Table pagination">
      <PageBtn onClick={() => onPage(page - 1)} disabled={prevDisabled} ariaLabel="Previous page">←</PageBtn>
      <span className="px-2 text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
        page <span className="text-[var(--color-ink-2)]">{page}</span> / {totalPages}
      </span>
      <PageBtn onClick={() => onPage(page + 1)} disabled={nextDisabled} ariaLabel="Next page">→</PageBtn>
    </nav>
  );
}

function PageBtn({
  onClick,
  disabled,
  ariaLabel,
  children,
}: {
  onClick: () => void;
  disabled: boolean;
  ariaLabel: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      className="w-7 h-7 inline-flex items-center justify-center rounded-[var(--radius-xs)] text-sm text-[var(--color-ink-2)] hover:bg-[var(--color-copper-soft)] hover:text-[var(--color-copper)] disabled:opacity-30 disabled:hover:bg-transparent disabled:cursor-not-allowed transition-colors"
    >
      {children}
    </button>
  );
}
