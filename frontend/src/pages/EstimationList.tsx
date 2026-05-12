import { useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import {
  estimationKeys,
  fetchEstimationsList,
} from '@/lib/queries';
import {
  fmtAbsolute,
  fmtCount,
  fmtCzk,
  fmtRelative,
} from '@/lib/format';
import type {
  Confidence,
  EstimationListResponse,
  EstimationRun,
  EstimationSource,
  EstimationStatus,
} from '@/lib/types';
import { ControlGroup } from '@/components/controls';

const PAGE_SIZE = 50;

const SOURCES: ReadonlyArray<EstimationSource> = ['ui', 'api', 'clickup'];
const STATUSES: ReadonlyArray<EstimationStatus> = ['success', 'failed', 'pending', 'running'];

export default function EstimationList() {
  const [params, setParams] = useSearchParams();

  const source = (params.get('source') as EstimationSource | null) ?? null;
  const status = (params.get('status') as EstimationStatus | null) ?? null;
  const page = Math.max(1, parseInt(params.get('page') ?? '1', 10) || 1);

  const offset = (page - 1) * PAGE_SIZE;
  const queryParams = useMemo(
    () => ({
      source: source ?? undefined,
      status: status ?? undefined,
      limit: PAGE_SIZE,
      offset,
    }),
    [source, status, offset],
  );

  const listQ = useQuery<EstimationListResponse, Error>({
    queryKey: estimationKeys.list(queryParams),
    queryFn: () => fetchEstimationsList(queryParams),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });

  const setFilter = (key: 'source' | 'status', value: string | null) => {
    const sp = new URLSearchParams(params);
    if (value == null) sp.delete(key);
    else sp.set(key, value);
    sp.delete('page');
    setParams(sp, { replace: false });
  };

  const setPage = (next: number) => {
    const sp = new URLSearchParams(params);
    if (next <= 1) sp.delete('page');
    else sp.set('page', String(next));
    setParams(sp, { replace: false });
  };

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto">
      <Header />
      <div className="mt-6">
        <FilterBar
          source={source}
          status={status}
          onSource={(v) => setFilter('source', v)}
          onStatus={(v) => setFilter('status', v)}
        />
      </div>

      <div className="mt-6">
        {listQ.isLoading && !listQ.data ? (
          <div className="text-sm text-[var(--color-ink-3)]">Loading…</div>
        ) : listQ.error ? (
          <div className="text-sm text-[var(--color-brick)]">
            Failed to load: {listQ.error.message}
          </div>
        ) : !listQ.data || listQ.data.data.length === 0 ? (
          <EmptyState filtered={source != null || status != null} />
        ) : (
          <RunsTable
            rows={listQ.data.data}
            total={listQ.data.total}
            page={page}
            onPage={setPage}
          />
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Header                                                                     */
/* -------------------------------------------------------------------------- */

function Header() {
  return (
    <header className="flex items-end justify-between gap-6">
      <div>
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Estimations
        </p>
        <h1
          className="mt-1.5 text-[2.1rem] leading-tight"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          Past runs
        </h1>
        <p className="mt-2 text-sm text-[var(--color-ink-2)]">
          Every estimation — from this UI, the API, or ClickUp — lands here.
        </p>
      </div>
      <Link
        to="/estimate"
        className="shrink-0 inline-flex items-center gap-2 px-4 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
      >
        <span>+ New estimation</span>
      </Link>
    </header>
  );
}

/* -------------------------------------------------------------------------- */
/* Filter bar                                                                 */
/* -------------------------------------------------------------------------- */

function FilterBar({
  source,
  status,
  onSource,
  onStatus,
}: {
  source: EstimationSource | null;
  status: EstimationStatus | null;
  onSource: (v: string | null) => void;
  onStatus: (v: string | null) => void;
}) {
  return (
    <ControlGroup title="Filter runs">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        <SegmentedFilter label="Source" value={source} options={SOURCES} onChange={onSource} />
        <SegmentedFilter label="Status" value={status} options={STATUSES} onChange={onStatus} />
      </div>
    </ControlGroup>
  );
}

function SegmentedFilter<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T | null;
  options: ReadonlyArray<T>;
  onChange: (v: T | null) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        {label}
      </span>
      <div className="inline-flex items-center gap-0.5 p-0.5 rounded-[var(--radius-sm)] bg-[var(--color-paper-2)] border border-[var(--color-rule)]">
        <Pill active={value == null} onClick={() => onChange(null)}>all</Pill>
        {options.map((opt) => (
          <Pill key={opt} active={value === opt} onClick={() => onChange(opt)}>
            {opt}
          </Pill>
        ))}
      </div>
    </div>
  );
}

function Pill({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={[
        'px-2.5 py-0.5 text-[0.7rem] tracking-wide rounded-[var(--radius-xs)] transition-colors',
        active
          ? 'bg-[var(--color-copper)] text-white'
          : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
      ].join(' ')}
    >
      {children}
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/* Table                                                                      */
/* -------------------------------------------------------------------------- */

function RunsTable({
  rows,
  total,
  page,
  onPage,
}: {
  rows: EstimationRun[];
  total: number;
  page: number;
  onPage: (n: number) => void;
}) {
  const totalPages = total > 0 ? Math.ceil(total / PAGE_SIZE) : 1;
  const start = (page - 1) * PAGE_SIZE + 1;
  const end = Math.min(start + rows.length - 1, total);

  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-[var(--color-paper-2)] border-b border-[var(--color-rule)]">
            <tr>
              <Th align="left">When</Th>
              <Th align="left">Source</Th>
              <Th align="left">Kind</Th>
              <Th align="left">Status</Th>
              <Th align="left">Input</Th>
              <Th align="right">Estimate</Th>
              <Th align="left">Confidence</Th>
              <Th align="right">N</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <Row key={r.id} run={r} />
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex items-center justify-between gap-4 px-4 py-2.5 border-t border-[var(--color-rule)] bg-[var(--color-paper)]">
        <p className="text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
          {total === 0
            ? 'No runs'
            : <>Showing <span className="text-[var(--color-ink-2)]">{fmtCount(start)}–{fmtCount(end)}</span> of <span className="text-[var(--color-ink-2)]">{fmtCount(total)}</span></>}
        </p>
        <Pagination page={page} totalPages={totalPages} onPage={onPage} disabled={total === 0} />
      </div>
    </div>
  );
}

function Th({ align, children }: { align: 'left' | 'right'; children: React.ReactNode }) {
  return (
    <th
      scope="col"
      className={[
        'px-4 py-2.5 text-[0.7rem] tracking-[0.14em] uppercase font-medium text-[var(--color-ink-3)]',
        align === 'right' ? 'text-right' : 'text-left',
      ].join(' ')}
    >
      {children}
    </th>
  );
}

function Row({ run }: { run: EstimationRun }) {
  const compsCount = run.comparables_used?.length ?? null;
  return (
    <tr className="border-b border-[var(--color-rule-soft)] last:border-b-0 hover:bg-[var(--color-copper-soft)]/40 transition-colors">
      <td
        className="px-4 py-2.5 align-middle text-[var(--color-ink-2)] tabular-nums"
        title={fmtAbsolute(run.created_at)}
      >
        <Link
          to={`/estimation/${run.id}`}
          className="hover:text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          {fmtRelative(run.created_at)}
        </Link>
      </td>
      <td className="px-4 py-2.5 align-middle">
        <SourceBadge source={run.source} />
      </td>
      <td className="px-4 py-2.5 align-middle">
        <KindBadge kind={run.estimate_kind} />
      </td>
      <td className="px-4 py-2.5 align-middle">
        <StatusPill status={run.status} />
      </td>
      <td className="px-4 py-2.5 align-middle max-w-[300px]">
        <InputCell run={run} />
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink)]">
        <EstimateCell run={run} />
      </td>
      <td className="px-4 py-2.5 align-middle">
        {run.confidence ? <ConfidenceBadge confidence={run.confidence} /> : <span className="text-[var(--color-ink-4)]">—</span>}
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {compsCount != null ? compsCount : <span className="text-[var(--color-ink-4)]">—</span>}
      </td>
    </tr>
  );
}

function InputCell({ run }: { run: EstimationRun }) {
  if (run.input_url) {
    return (
      <a
        href={run.input_url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-[var(--color-copper)] hover:underline underline-offset-2 truncate block"
        title={run.input_url}
      >
        {short(run.input_url)}
      </a>
    );
  }
  if (run.input_sreality_id != null) {
    return (
      <Link
        to={`/listing/${run.input_sreality_id}`}
        className="font-mono tabular-nums text-[var(--color-copper)] hover:underline underline-offset-2"
      >
        id {run.input_sreality_id}
      </Link>
    );
  }
  return <span className="text-[0.78rem] text-[var(--color-ink-3)]">spec only</span>;
}

function short(url: string): string {
  try {
    const u = new URL(url);
    return `${u.host}${u.pathname}`.replace(/\/$/, '');
  } catch {
    return url;
  }
}

/* -------------------------------------------------------------------------- */
/* Pills                                                                      */
/* -------------------------------------------------------------------------- */

function SourceBadge({ source }: { source: EstimationSource }) {
  return (
    <span className="inline-block px-2 py-0.5 text-[0.6rem] tracking-[0.16em] uppercase rounded-[var(--radius-xs)] bg-[var(--color-paper)] text-[var(--color-ink-3)] border border-[var(--color-rule)]">
      {source}
    </span>
  );
}

function KindBadge({ kind }: { kind: EstimationRun['estimate_kind'] }) {
  const resolved = kind ?? 'rent';
  const label = resolved === 'sale' ? 'sale' : 'rent';
  return (
    <span className="inline-block px-2 py-0.5 text-[0.6rem] tracking-[0.16em] uppercase rounded-[var(--radius-xs)] bg-[var(--color-paper)] text-[var(--color-ink-3)] border border-[var(--color-rule)]">
      {label}
    </span>
  );
}

function EstimateCell({ run }: { run: EstimationRun }) {
  const kind = run.estimate_kind ?? 'rent';
  if (kind === 'sale') {
    if (run.estimated_sale_price_czk == null) {
      return <span className="text-[var(--color-ink-4)]">—</span>;
    }
    return <>{fmtCzk(run.estimated_sale_price_czk)}</>;
  }
  if (run.estimated_monthly_rent_czk == null) {
    return <span className="text-[var(--color-ink-4)]">—</span>;
  }
  return (
    <>
      {fmtCzk(run.estimated_monthly_rent_czk)}
      <span className="ml-1 text-[var(--color-ink-3)] text-[0.7rem]">/mo</span>
    </>
  );
}

function StatusPill({ status }: { status: EstimationStatus }) {
  const tone = statusTone(status);
  return (
    <span className={['inline-block px-2 py-0.5 text-[0.65rem] tracking-wide uppercase rounded-[var(--radius-xs)] font-medium', tone].join(' ')}>
      {status}
    </span>
  );
}

function statusTone(status: EstimationStatus): string {
  if (status === 'success') return 'bg-[var(--color-sage-soft)] text-[var(--color-sage)]';
  if (status === 'failed') return 'bg-[var(--color-brick-soft)] text-[var(--color-brick)]';
  return 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)]';
}

function ConfidenceBadge({ confidence }: { confidence: Confidence }) {
  const tone =
    confidence === 'high'
      ? 'text-[var(--color-sage)]'
      : confidence === 'medium'
        ? 'text-[var(--color-copper)]'
        : 'text-[var(--color-ochre)]';
  return (
    <span className={['inline-flex items-center gap-1 text-[0.7rem] tracking-wide', tone].join(' ')}>
      <span className="w-1.5 h-1.5 rounded-full bg-current opacity-70" aria-hidden />
      {confidence}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Pagination + empty state                                                   */
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
  const prev = disabled || page <= 1;
  const next = disabled || page >= totalPages;
  return (
    <nav className="flex items-center gap-1" aria-label="List pagination">
      <PageBtn onClick={() => onPage(page - 1)} disabled={prev} ariaLabel="Previous">
        ←
      </PageBtn>
      <span className="px-2 text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
        page <span className="text-[var(--color-ink-2)]">{page}</span> / {totalPages}
      </span>
      <PageBtn onClick={() => onPage(page + 1)} disabled={next} ariaLabel="Next">
        →
      </PageBtn>
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

function EmptyState({ filtered }: { filtered: boolean }) {
  return (
    <div className="px-6 py-16 text-center border border-dashed border-[var(--color-rule)] rounded-[var(--radius-md)]">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        {filtered ? 'No matches' : 'No estimations yet'}
      </p>
      <p className="mt-2 text-sm text-[var(--color-ink-2)]">
        {filtered
          ? 'No runs match these filters.'
          : 'Past runs will appear here once any user, the API, or ClickUp triggers one.'}
      </p>
      <Link
        to="/estimate"
        className="mt-4 inline-block text-sm text-[var(--color-copper)] hover:underline underline-offset-2"
      >
        Start an estimation →
      </Link>
    </div>
  );
}
