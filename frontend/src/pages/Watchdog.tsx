import { useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';

import {
  kickoffWatchdogDispatchEstimate,
  listWatchdogDispatches,
  listWatchdogSubscriptions,
  markWatchdogDispatchSeen,
  runWatchdogMatcher,
} from '@/lib/api';
import { watchdogKeys } from '@/lib/queries';
import {
  fmtAbsolute,
  fmtCount,
  fmtCzk,
  fmtRelative,
} from '@/lib/format';
import type {
  WatchdogDispatch,
  WatchdogDispatchesResponse,
  WatchdogSeenFilter,
  WatchdogSubscription,
} from '@/lib/types';

const PAGE_SIZE = 50;
const POLL_INTERVAL_MS = 5_000;

const SEEN_OPTIONS: ReadonlyArray<WatchdogSeenFilter> = ['all', 'unseen', 'seen'];

export default function Watchdog() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const subscriptionId = params.get('subscription') ?? null;
  const seen = (params.get('seen') as WatchdogSeenFilter | null) ?? 'all';
  const page = Math.max(1, parseInt(params.get('page') ?? '1', 10) || 1);
  const offset = (page - 1) * PAGE_SIZE;

  const queryParams = useMemo(
    () => ({
      subscription_id: subscriptionId ?? undefined,
      seen,
      limit: PAGE_SIZE,
      offset,
    }),
    [subscriptionId, seen, offset],
  );

  const dispatchesQ = useQuery<WatchdogDispatchesResponse, Error>({
    queryKey: watchdogKeys.dispatches(queryParams),
    queryFn: () => listWatchdogDispatches(queryParams),
    placeholderData: keepPreviousData,
    /* Refresh while a kicked-off estimation is still pending. We could
     * scope this to "only poll if any visible row has a non-terminal
     * status", but a single 5-second tick is cheap and keeps the feed
     * fresh as new matches arrive from the background matcher too. */
    refetchInterval: POLL_INTERVAL_MS,
  });

  const subscriptionsQ = useQuery<
    { data: WatchdogSubscription[]; total: number },
    Error
  >({
    queryKey: watchdogKeys.subscriptions,
    queryFn: () => listWatchdogSubscriptions(),
    staleTime: 30_000,
  });

  const matcherMut = useMutation({
    mutationFn: runWatchdogMatcher,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: watchdogKeys.all });
    },
  });

  const estimateMut = useMutation({
    mutationFn: (dispatchId: string) =>
      kickoffWatchdogDispatchEstimate(dispatchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: watchdogKeys.all });
    },
  });

  const markSeenMut = useMutation({
    mutationFn: (dispatchId: string) => markWatchdogDispatchSeen(dispatchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: watchdogKeys.all });
    },
  });

  const setFilter = (key: 'subscription' | 'seen', value: string | null) => {
    const sp = new URLSearchParams(params);
    if (value == null || value === '' || value === 'all') sp.delete(key);
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

  const data = dispatchesQ.data;
  const total = data?.total ?? 0;
  const totalPages = total > 0 ? Math.ceil(total / PAGE_SIZE) : 1;
  const start = (page - 1) * PAGE_SIZE + 1;
  const end = Math.min(start + (data?.data.length ?? 0) - 1, total);
  const subscriptions = subscriptionsQ.data?.data ?? [];

  return (
    <div className="px-6 py-8 max-w-6xl mx-auto">
      <Header
        onRunMatcher={() => matcherMut.mutate()}
        matcherPending={matcherMut.isPending}
        matcherResult={matcherMut.data?.data ?? null}
      />

      <div className="mt-6 flex flex-wrap items-center gap-x-6 gap-y-3">
        <SubscriptionFilter
          value={subscriptionId}
          subscriptions={subscriptions}
          onChange={(v) => setFilter('subscription', v)}
        />
        <SeenFilter value={seen} onChange={(v) => setFilter('seen', v)} />
      </div>

      <div className="mt-6">
        {dispatchesQ.isLoading && !data ? (
          <div className="text-sm text-[var(--color-ink-3)]">Loading…</div>
        ) : dispatchesQ.error ? (
          <div className="text-sm text-[var(--color-brick)]">
            Failed to load: {dispatchesQ.error.message}
          </div>
        ) : !data || data.data.length === 0 ? (
          <EmptyState
            filtered={subscriptionId != null || seen !== 'all'}
            hasAnyWatchdog={subscriptions.length > 0}
          />
        ) : (
          <DispatchesTable
            rows={data.data}
            total={total}
            start={start}
            end={end}
            page={page}
            totalPages={totalPages}
            onPage={setPage}
            onKickoff={(id) => estimateMut.mutate(id)}
            onMarkSeen={(id) => markSeenMut.mutate(id)}
            kickoffPending={estimateMut.isPending ? estimateMut.variables ?? null : null}
          />
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Header                                                                     */
/* -------------------------------------------------------------------------- */

function Header({
  onRunMatcher,
  matcherPending,
  matcherResult,
}: {
  onRunMatcher: () => void;
  matcherPending: boolean;
  matcherResult: {
    subscriptions_evaluated: number;
    matches_inserted: number;
    listings_in_window: number;
  } | null;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div>
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Watchdog
        </p>
        <h1
          className="mt-1.5 text-[2.1rem] leading-tight"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          New-listing notifications
        </h1>
        <p className="mt-2 text-sm text-[var(--color-ink-2)]">
          Saved filters fire a notification the moment a freshly scraped
          listing matches. Click <em>Run estimation</em> on any row to
          kick off a deterministic yield calculation in the background —
          the result appears here when it lands.
        </p>
        {matcherResult ? (
          <p className="mt-2 text-[0.75rem] text-[var(--color-ink-3)]">
            Last matcher run: inspected{' '}
            <span className="tabular-nums text-[var(--color-ink-2)]">
              {fmtCount(matcherResult.listings_in_window)}
            </span>{' '}
            new listings against{' '}
            <span className="tabular-nums text-[var(--color-ink-2)]">
              {matcherResult.subscriptions_evaluated}
            </span>{' '}
            active watchdog{matcherResult.subscriptions_evaluated === 1 ? '' : 's'},{' '}
            <span className="tabular-nums text-[var(--color-ink-2)]">
              {fmtCount(matcherResult.matches_inserted)}
            </span>{' '}
            match{matcherResult.matches_inserted === 1 ? '' : 'es'} fired.
          </p>
        ) : null}
      </div>
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={onRunMatcher}
          disabled={matcherPending}
          className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:text-[var(--color-ink)] hover:border-[var(--color-rule-strong)] transition-colors disabled:opacity-50"
          title="Run the matcher once now, instead of waiting for the next scheduler tick."
        >
          {matcherPending ? 'Running…' : 'Run matcher now'}
        </button>
        <Link
          to="/watchdog/manage"
          className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
        >
          Manage watchdogs
        </Link>
      </div>
    </header>
  );
}

/* -------------------------------------------------------------------------- */
/* Filter row                                                                 */
/* -------------------------------------------------------------------------- */

function SubscriptionFilter({
  value,
  subscriptions,
  onChange,
}: {
  value: string | null;
  subscriptions: WatchdogSubscription[];
  onChange: (v: string | null) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        Watchdog
      </span>
      <select
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value || null)}
        className="px-2 py-1 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)]"
      >
        <option value="">All watchdogs</option>
        {subscriptions.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name} {s.is_active ? '' : '(paused)'}
          </option>
        ))}
      </select>
    </div>
  );
}

function SeenFilter({
  value,
  onChange,
}: {
  value: WatchdogSeenFilter;
  onChange: (v: string | null) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        Read
      </span>
      <div className="inline-flex items-center gap-0.5 p-0.5 rounded-[var(--radius-sm)] bg-[var(--color-paper-2)] border border-[var(--color-rule)]">
        {SEEN_OPTIONS.map((opt) => (
          <button
            key={opt}
            type="button"
            onClick={() => onChange(opt === 'all' ? null : opt)}
            className={[
              'px-2.5 py-0.5 text-[0.7rem] tracking-wide rounded-[var(--radius-xs)] transition-colors',
              value === opt
                ? 'bg-[var(--color-copper)] text-white'
                : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
            ].join(' ')}
            aria-pressed={value === opt}
          >
            {opt}
          </button>
        ))}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Dispatches table                                                           */
/* -------------------------------------------------------------------------- */

function DispatchesTable({
  rows,
  total,
  start,
  end,
  page,
  totalPages,
  onPage,
  onKickoff,
  onMarkSeen,
  kickoffPending,
}: {
  rows: WatchdogDispatch[];
  total: number;
  start: number;
  end: number;
  page: number;
  totalPages: number;
  onPage: (n: number) => void;
  onKickoff: (dispatchId: string) => void;
  onMarkSeen: (dispatchId: string) => void;
  kickoffPending: string | null;
}) {
  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-[var(--color-paper-2)] border-b border-[var(--color-rule)]">
            <tr>
              <Th align="left">Listing</Th>
              <Th align="left">Disposition</Th>
              <Th align="right">Price</Th>
              <Th align="left">When</Th>
              <Th align="left">Watchdog</Th>
              <Th align="left">Estimation</Th>
              <Th align="right"> </Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <Row
                key={r.id}
                dispatch={r}
                onKickoff={onKickoff}
                onMarkSeen={onMarkSeen}
                kickoffPending={kickoffPending === r.id}
              />
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex items-center justify-between gap-4 px-4 py-2.5 border-t border-[var(--color-rule)] bg-[var(--color-paper)]">
        <p className="text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
          {total === 0 ? (
            'No notifications'
          ) : (
            <>
              Showing{' '}
              <span className="text-[var(--color-ink-2)]">
                {fmtCount(start)}–{fmtCount(end)}
              </span>{' '}
              of{' '}
              <span className="text-[var(--color-ink-2)]">{fmtCount(total)}</span>
            </>
          )}
        </p>
        <Pagination
          page={page}
          totalPages={totalPages}
          onPage={onPage}
          disabled={total === 0}
        />
      </div>
    </div>
  );
}

function Th({
  align,
  children,
}: {
  align: 'left' | 'right';
  children: React.ReactNode;
}) {
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

function Row({
  dispatch,
  onKickoff,
  onMarkSeen,
  kickoffPending,
}: {
  dispatch: WatchdogDispatch;
  onKickoff: (dispatchId: string) => void;
  onMarkSeen: (dispatchId: string) => void;
  kickoffPending: boolean;
}) {
  const unread = dispatch.seen_at == null;
  const handleListingClick = () => {
    if (unread) onMarkSeen(dispatch.id);
  };
  return (
    <tr
      className={[
        'border-b border-[var(--color-rule-soft)] last:border-b-0 transition-colors',
        unread
          ? 'bg-[var(--color-copper-soft)]/30 hover:bg-[var(--color-copper-soft)]/50'
          : 'hover:bg-[var(--color-paper)]',
      ].join(' ')}
    >
      <td className="px-4 py-2.5 align-middle max-w-[280px]">
        <Link
          to={`/listing/${dispatch.sreality_id}`}
          onClick={handleListingClick}
          className="block hover:text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          <span className="block text-[var(--color-ink)] truncate">
            {dispatch.locality ?? dispatch.district ?? `id ${dispatch.sreality_id}`}
          </span>
          {dispatch.district && dispatch.locality ? (
            <span className="block text-[0.7rem] text-[var(--color-ink-3)] truncate">
              {dispatch.district}
            </span>
          ) : null}
        </Link>
      </td>
      <td className="px-4 py-2.5 align-middle text-[var(--color-ink-2)] tabular-nums">
        {dispatch.disposition ?? <span className="text-[var(--color-ink-4)]">—</span>}
      </td>
      <td className="px-4 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink)]">
        <PriceCell dispatch={dispatch} />
      </td>
      <td
        className="px-4 py-2.5 align-middle text-[var(--color-ink-2)] tabular-nums"
        title={fmtAbsolute(dispatch.dispatched_at)}
      >
        {fmtRelative(dispatch.dispatched_at)}
      </td>
      <td className="px-4 py-2.5 align-middle">
        <span className="inline-block px-2 py-0.5 text-[0.65rem] tracking-wide rounded-[var(--radius-xs)] bg-[var(--color-paper)] border border-[var(--color-rule)] text-[var(--color-ink-2)]">
          {dispatch.subscription_name}
        </span>
      </td>
      <td className="px-4 py-2.5 align-middle">
        <EstimationCell dispatch={dispatch} />
      </td>
      <td className="px-4 py-2.5 align-middle text-right">
        <KickoffButton
          dispatch={dispatch}
          onKickoff={() => onKickoff(dispatch.id)}
          pending={kickoffPending}
        />
      </td>
    </tr>
  );
}

function PriceCell({ dispatch }: { dispatch: WatchdogDispatch }) {
  if (dispatch.price_czk == null) {
    return <span className="text-[var(--color-ink-4)]">—</span>;
  }
  const isRental = dispatch.category_type === 'pronajem';
  return (
    <>
      {fmtCzk(dispatch.price_czk)}
      {isRental ? (
        <span className="ml-1 text-[var(--color-ink-3)] text-[0.7rem]">/mo</span>
      ) : null}
    </>
  );
}

function EstimationCell({ dispatch }: { dispatch: WatchdogDispatch }) {
  if (dispatch.estimation_run_id == null) {
    return <span className="text-[var(--color-ink-4)]">—</span>;
  }
  const status = dispatch.estimation_status;
  if (status === 'pending' || status === 'running') {
    return (
      <span className="inline-flex items-center gap-1.5 text-[0.75rem] text-[var(--color-ochre)]">
        <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" aria-hidden />
        {status}…
      </span>
    );
  }
  if (status === 'failed') {
    return (
      <Link
        to={`/estimation/${dispatch.estimation_run_id}`}
        className="text-[0.75rem] text-[var(--color-brick)] hover:underline underline-offset-2"
      >
        failed
      </Link>
    );
  }
  const isRental = dispatch.estimation_kind === 'rent';
  const point = isRental
    ? dispatch.estimated_monthly_rent_czk
    : dispatch.estimated_sale_price_czk;
  return (
    <Link
      to={`/estimation/${dispatch.estimation_run_id}`}
      className="block hover:text-[var(--color-copper)] hover:underline underline-offset-2"
    >
      <span className="block font-mono tabular-nums text-[var(--color-ink)]">
        {point != null ? fmtCzk(point) : '—'}
        {isRental && point != null ? (
          <span className="ml-1 text-[var(--color-ink-3)] text-[0.7rem]">/mo</span>
        ) : null}
      </span>
      {dispatch.gross_yield_pct != null ? (
        <span className="block text-[0.7rem] text-[var(--color-ink-3)] tabular-nums">
          yield {dispatch.gross_yield_pct.toFixed(1)}%
        </span>
      ) : null}
    </Link>
  );
}

function KickoffButton({
  dispatch,
  onKickoff,
  pending,
}: {
  dispatch: WatchdogDispatch;
  onKickoff: () => void;
  pending: boolean;
}) {
  if (dispatch.estimation_run_id != null) {
    const status = dispatch.estimation_status;
    if (status === 'pending' || status === 'running') {
      return (
        <span className="text-[0.7rem] tracking-wide uppercase text-[var(--color-ink-4)]">
          in flight
        </span>
      );
    }
    return (
      <Link
        to={`/estimation/${dispatch.estimation_run_id}`}
        className="text-[0.75rem] text-[var(--color-copper)] hover:underline underline-offset-2"
      >
        open →
      </Link>
    );
  }
  return (
    <button
      type="button"
      onClick={onKickoff}
      disabled={pending}
      className="px-2.5 py-1 text-[0.75rem] rounded-[var(--radius-sm)] border border-[var(--color-copper)] text-[var(--color-copper)] hover:bg-[var(--color-copper-soft)]/60 transition-colors disabled:opacity-50"
    >
      {pending ? 'Starting…' : 'Run estimation'}
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/* Empty + pagination                                                         */
/* -------------------------------------------------------------------------- */

function EmptyState({
  filtered,
  hasAnyWatchdog,
}: {
  filtered: boolean;
  hasAnyWatchdog: boolean;
}) {
  return (
    <div className="px-6 py-16 text-center border border-dashed border-[var(--color-rule)] rounded-[var(--radius-md)]">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        {filtered ? 'No matches' : 'Nothing fired yet'}
      </p>
      <p className="mt-2 text-sm text-[var(--color-ink-2)]">
        {filtered
          ? 'No notifications match these filters.'
          : hasAnyWatchdog
            ? 'No new listings have matched your watchdogs since the last scrape. The matcher wakes up automatically; you can also click Run matcher now.'
            : 'Define a watchdog (a saved filter) and you’ll get a notification when a freshly scraped listing matches.'}
      </p>
      {!hasAnyWatchdog ? (
        <Link
          to="/watchdog/new"
          className="mt-4 inline-block text-sm text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          Create a watchdog →
        </Link>
      ) : null}
    </div>
  );
}

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
    <nav className="flex items-center gap-1" aria-label="Feed pagination">
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
