import { useMemo } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, keepPreviousData } from '@tanstack/react-query';
import {
  estimationKeys,
  fetchEstimationsList,
  submitEstimation,
} from '@/lib/queries';
import {
  fmtAbsolute,
  fmtArea,
  fmtCount,
  fmtCzk,
  fmtDateSlash,
  fmtRelative,
  fmtTime24,
} from '@/lib/format';
import type {
  Confidence,
  EstimationListResponse,
  EstimationRun,
  EstimationSource,
  EstimationStatus,
} from '@/lib/types';
import { ApiError } from '@/lib/api';
import { runSurfaceUrl } from '@/lib/runLinks';
import { buildRerunPayload, canRerun } from '@/lib/rerun';
import { ControlGroup } from '@/components/controls';
import { useNewEstimationModal } from '@/components/NewEstimationModal';

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
    refetchInterval: (q) => {
      const rows = q.state.data?.data ?? [];
      return rows.some(
        (r) => r.status === 'pending' || r.status === 'running',
      )
        ? 3000
        : false;
    },
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
  const { open } = useNewEstimationModal();
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
      <button
        type="button"
        onClick={() => open()}
        className="shrink-0 inline-flex items-center gap-2 px-4 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
      >
        <span>+ New estimation</span>
      </button>
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
              <Th align="left">City</Th>
              <Th align="left">Disp · m²</Th>
              <Th align="left">Listing</Th>
              <Th align="left">Status</Th>
              <Th align="right">Estimate</Th>
              <Th align="right">Yield</Th>
              <Th align="left">Feedback</Th>
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
  return (
    <tr className="border-b border-[var(--color-rule-soft)] last:border-b-0 hover:bg-[var(--color-copper-soft)]/40 transition-colors">
      <WhenCell run={run} />
      <td className="px-4 py-2.5 align-middle">
        <SourceBadge source={run.source} />
      </td>
      <td className="px-4 py-2.5 align-middle max-w-[160px]">
        <CityCell run={run} />
      </td>
      <td className="px-4 py-2.5 align-middle">
        <DispoAreaCell run={run} />
      </td>
      <td className="px-4 py-2.5 align-middle">
        <ListingLinkCell run={run} />
      </td>
      <td className="px-4 py-2.5 align-middle">
        <StatusCell run={run} />
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums">
        <EstimateCell run={run} />
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums">
        <YieldCell run={run} />
      </td>
      <td className="px-4 py-2.5 align-middle">
        <FeedbackCell run={run} />
      </td>
    </tr>
  );
}

function WhenCell({ run }: { run: EstimationRun }) {
  return (
    <td
      className="px-4 py-2.5 align-middle tabular-nums"
      title={`${fmtAbsolute(run.created_at)} · ${fmtRelative(run.created_at)}`}
    >
      <Link
        to={runSurfaceUrl(run)}
        className="block leading-tight hover:text-[var(--color-copper)]"
      >
        <div className="text-[var(--color-ink-2)]">{fmtDateSlash(run.created_at)}</div>
        <div className="text-[0.7rem] text-[var(--color-ink-4)]">{fmtTime24(run.created_at)}</div>
      </Link>
    </td>
  );
}

function CityCell({ run }: { run: EstimationRun }) {
  const city = run.locality_display ?? null;
  if (!city) {
    return <span className="text-[var(--color-ink-4)]">—</span>;
  }
  return (
    <span className="text-[var(--color-ink-2)] truncate block" title={city}>
      {city}
    </span>
  );
}

function DispoAreaCell({ run }: { run: EstimationRun }) {
  const dispo = run.input_spec?.disposition ?? null;
  const area = run.input_spec?.area_m2 ?? null;
  if (dispo == null && area == null) {
    return <span className="text-[var(--color-ink-4)]">—</span>;
  }
  const parts: string[] = [];
  if (dispo) parts.push(dispo);
  if (area != null) parts.push(fmtArea(area));
  return (
    <span className="tabular-nums text-[var(--color-ink-2)]">
      {parts.join(' · ')}
    </span>
  );
}

function YieldCell({ run }: { run: EstimationRun }) {
  if (run.gross_yield_pct == null) {
    return <span className="text-[var(--color-ink-4)]">—</span>;
  }
  return (
    <span className="text-[var(--color-ink-2)]">
      {run.gross_yield_pct.toFixed(2)}&nbsp;%
    </span>
  );
}

function FeedbackCell({ run }: { run: EstimationRun }) {
  if (!run.has_feedback) {
    return (
      <span
        className="inline-flex items-center px-2 py-0.5 text-[0.7rem] tracking-wide rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-4)] cursor-not-allowed select-none"
        title="No feedback was given on this run"
      >
        view
      </span>
    );
  }
  return (
    <Link
      to={runSurfaceUrl(run, '#feedback')}
      className="inline-flex items-center px-2 py-0.5 text-[0.7rem] tracking-wide rounded-[var(--radius-xs)] border border-[var(--color-copper)]/30 bg-[var(--color-copper-soft)] text-[var(--color-copper)] hover:bg-[var(--color-copper)] hover:text-white transition-colors"
    >
      view
    </Link>
  );
}

function ListingLinkCell({ run }: { run: EstimationRun }) {
  // In-app listing page first — it's the primary surface for the run.
  // The external portal URL stays reachable as a secondary ↗.
  if (run.input_sreality_id != null) {
    return (
      <span className="inline-flex items-center gap-1.5">
        <Link
          to={`/listing/${run.input_sreality_id}`}
          className="text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          Listing
        </Link>
        {run.input_url && (
          <a
            href={run.input_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[var(--color-ink-3)] hover:text-[var(--color-copper)]"
            title={run.input_url}
          >
            ↗
          </a>
        )}
      </span>
    );
  }
  if (run.input_url) {
    return (
      <a
        href={run.input_url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-[var(--color-copper)] hover:underline underline-offset-2"
        title={run.input_url}
      >
        Link ↗
      </a>
    );
  }
  return <span className="text-[0.78rem] text-[var(--color-ink-3)]">spec only</span>;
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

function EstimateCell({ run }: { run: EstimationRun }) {
  const kind = run.estimate_kind ?? 'rent';
  const tone = estimateTone(run.confidence);
  if (kind === 'sale') {
    if (run.estimated_sale_price_czk == null) {
      return <span className="text-[var(--color-ink-4)]">—</span>;
    }
    return <span className={tone}>{fmtCzk(run.estimated_sale_price_czk)}</span>;
  }
  if (run.estimated_monthly_rent_czk == null) {
    return <span className="text-[var(--color-ink-4)]">—</span>;
  }
  return (
    <span className={tone}>
      {fmtCzk(run.estimated_monthly_rent_czk)}
      <span className="ml-1 text-[var(--color-ink-3)] text-[0.7rem]">/mo</span>
    </span>
  );
}

function estimateTone(confidence: Confidence | null): string {
  if (confidence === 'high') return 'text-[var(--color-sage)]';
  if (confidence === 'medium') return 'text-[var(--color-copper)]';
  if (confidence === 'low') return 'text-[var(--color-ochre)]';
  return 'text-[var(--color-ink)]';
}

function StatusCell({ run }: { run: EstimationRun }) {
  if (run.status !== 'failed' || !canRerun(run)) {
    return <StatusPill status={run.status} />;
  }
  return (
    <div className="flex flex-col items-start gap-1">
      <StatusPill status={run.status} />
      <RerunInlineButton run={run} />
    </div>
  );
}

function RerunInlineButton({ run }: { run: EstimationRun }) {
  const navigate = useNavigate();
  const mut = useMutation<EstimationRun, ApiError, void>({
    mutationFn: () => submitEstimation(buildRerunPayload(run)),
    onSuccess: (next) => navigate(runSurfaceUrl(next)),
  });
  return (
    <>
      <button
        type="button"
        onClick={() => mut.mutate()}
        disabled={mut.isPending}
        className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)] hover:text-[var(--color-copper)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
      >
        {mut.isPending ? 'Re-running…' : 'Re-run'}
      </button>
      {mut.error && (
        <span className="text-[0.7rem] text-[var(--color-brick)]">
          {mut.error.message || `HTTP ${mut.error.status}`}
        </span>
      )}
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
  const { open } = useNewEstimationModal();
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
      <button
        type="button"
        onClick={() => open()}
        className="mt-4 inline-block text-sm text-[var(--color-copper)] hover:underline underline-offset-2"
      >
        Start an estimation →
      </button>
    </div>
  );
}
