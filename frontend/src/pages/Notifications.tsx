/* The unified Notifications feed: watchdog matches AND collection-monitor
 * change events, from one endpoint (the LEFT-join feed serves both source
 * kinds). A read/triage surface — filter by source / change kind / seen, mark
 * one or all read, deep-link to the listing. The deal-pipeline, the watchdog
 * manage pages, and this feed stay distinct; this is where "what changed about
 * things I care about" lives, and it drives the red nav unread badge. */

import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import {
  getNotificationUnreadCount,
  listNotifications,
  markAllNotificationsSeen,
  markWatchdogDispatchSeen,
} from '@/lib/api';
import { notificationKeys } from '@/lib/queries';
import { listingPath } from '@/lib/listingUrl';
import { usePageTitle } from '@/lib/pageTitle';
import { fmtAbsolute, fmtCount, fmtCzk, fmtRelative } from '@/lib/format';
import { listingKindLabel } from '@/lib/enums';
import type {
  NotificationSourceKind,
  WatchdogDispatch,
  WatchdogSeenFilter,
} from '@/lib/types';

type SourceFilter = NotificationSourceKind | 'all';

const POLL_MS = 30_000;

/* change_kind → label + the colour of its leading dot (only design tokens that
 * are known to exist; the chip itself stays neutral). */
const CHANGE_META: Record<string, { label: string; dot: string }> = {
  new:           { label: 'New match',     dot: 'var(--color-copper)' },
  price_drop:    { label: 'Price drop',    dot: 'var(--color-sage)' },
  price_rise:    { label: 'Price rise',    dot: 'var(--color-brick)' },
  inactive:      { label: 'Delisted',      dot: 'var(--color-ink-4)' },
  reactivated:   { label: 'Relisted',      dot: 'var(--color-copper)' },
  new_source:    { label: 'New source',    dot: 'var(--color-copper)' },
  broker_change: { label: 'Broker change', dot: 'var(--color-ink-3)' },
};

const SOURCE_TABS: ReadonlyArray<{ key: SourceFilter; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'collection_monitor', label: 'Monitoring' },
  { key: 'watchdog', label: 'Watchdogs' },
];

const SEEN_TABS: ReadonlyArray<{ key: WatchdogSeenFilter; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'unseen', label: 'Unread' },
];

export default function Notifications() {
  const qc = useQueryClient();
  const [source, setSource] = useState<SourceFilter>('all');
  const [seen, setSeen] = useState<WatchdogSeenFilter>('all');

  const feedParams = {
    source_kind: source === 'all' ? undefined : source,
    seen,
    limit: 100,
  };

  const feedQ = useQuery({
    queryKey: notificationKeys.feed({ source, seen }),
    queryFn: () => listNotifications(feedParams),
    placeholderData: keepPreviousData,
    refetchInterval: POLL_MS,
  });

  const unreadQ = useQuery({
    queryKey: notificationKeys.unreadCount,
    queryFn: () => getNotificationUnreadCount(),
    refetchInterval: POLL_MS,
  });

  const markAll = useMutation({
    mutationFn: () => markAllNotificationsSeen(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: notificationKeys.all });
    },
  });

  const rows = feedQ.data?.data ?? [];
  const unread = unreadQ.data?.unread_count ?? 0;

  // Tab title carries the unread count, capped like the nav badge so the two
  // never disagree. Zero → the plain "Notifications" route handle.
  usePageTitle(unread > 0 ? `Notifications (${unread > 99 ? '99+' : unread})` : null);

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto">
      <Header
        unread={unread}
        onMarkAll={() => markAll.mutate()}
        markAllPending={markAll.isPending}
      />

      <div className="mt-6 flex flex-wrap items-center gap-x-6 gap-y-3">
        <Tabs<SourceFilter> tabs={SOURCE_TABS} value={source} onChange={setSource} />
        <Tabs<WatchdogSeenFilter> tabs={SEEN_TABS} value={seen} onChange={setSeen} />
      </div>

      <div className="mt-7">
        {feedQ.isLoading && !feedQ.data ? (
          <div className="text-sm text-[var(--color-ink-3)]">Loading…</div>
        ) : feedQ.error ? (
          <div className="text-sm text-[var(--color-brick)]">
            Failed to load: {(feedQ.error as Error).message}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState seen={seen} />
        ) : (
          <ul className="divide-y divide-[var(--color-rule-soft)] border-t border-b border-[var(--color-rule-soft)]">
            {rows.map((d) => (
              <li key={d.id}>
                <NotificationRow dispatch={d} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function Header({
  unread,
  onMarkAll,
  markAllPending,
}: {
  unread: number;
  onMarkAll: () => void;
  markAllPending: boolean;
}) {
  return (
    <header className="flex items-end justify-between gap-4">
      <div>
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Notifications
        </p>
        <h1
          className="mt-1.5 text-[2.1rem] leading-tight"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          What changed
        </h1>
        <p className="mt-2 text-sm text-[var(--color-ink-2)]">
          Watchdog matches and changes to properties in your monitored
          collections — price moves, delistings, relistings, new sources.{' '}
          {unread > 0 && (
            <span className="text-[var(--color-ink-3)]">
              {fmtCount(unread)} unread.
            </span>
          )}
        </p>
      </div>
      <button
        type="button"
        onClick={onMarkAll}
        disabled={markAllPending || unread === 0}
        className="shrink-0 px-3 py-1.5 text-[0.78rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:text-[var(--color-ink)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
      >
        {markAllPending ? 'Marking…' : 'Mark all read'}
      </button>
    </header>
  );
}

function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: ReadonlyArray<{ key: T; label: string }>;
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="inline-flex items-center gap-1">
      {tabs.map((t) => {
        const active = t.key === value;
        return (
          <button
            key={t.key}
            type="button"
            onClick={() => onChange(t.key)}
            className={[
              'px-2.5 py-1 text-[0.78rem] rounded-[var(--radius-sm)] transition-colors',
              active
                ? 'bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
                : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
            ].join(' ')}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

function NotificationRow({ dispatch: d }: { dispatch: WatchdogDispatch }) {
  const qc = useQueryClient();
  const unread = d.seen_at == null;
  const meta = CHANGE_META[d.change_kind] ?? {
    label: d.change_kind,
    dot: 'var(--color-ink-3)',
  };
  const place = d.locality ?? d.district ?? `id ${d.sreality_id}`;
  const sourceLabel =
    d.source_kind === 'collection_monitor'
      ? (d.collection_name ?? 'Monitoring')
      : (d.subscription_name ?? 'Watchdog');

  const markSeen = useMutation({
    mutationFn: () => markWatchdogDispatchSeen(d.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: notificationKeys.all });
    },
  });

  return (
    <Link
      to={listingPath(d.sreality_id)}
      onClick={() => {
        if (unread) markSeen.mutate();
      }}
      className="flex items-start gap-3 py-3.5 group"
    >
      {/* unread dot */}
      <span className="mt-1.5 w-1.5 shrink-0">
        {unread && (
          <span
            className="block w-1.5 h-1.5 rounded-full"
            style={{ background: 'var(--color-copper)' }}
            aria-label="unread"
          />
        )}
      </span>

      {/* change-kind chip */}
      <span className="mt-0.5 inline-flex items-center gap-1.5 shrink-0 px-2 py-0.5 text-[0.7rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-2)]">
        <span
          aria-hidden
          className="w-1.5 h-1.5 rounded-full"
          style={{ background: meta.dot }}
        />
        {meta.label}
      </span>

      <div className="min-w-0 flex-1">
        <p className="text-sm text-[var(--color-ink)] group-hover:text-[var(--color-copper)] transition-colors truncate">
          <span className="font-medium">{place}</span>
          {listingKindLabel(d) && (
            <span className="text-[var(--color-ink-3)]"> · {listingKindLabel(d)}</span>
          )}
          {d.price_czk != null && (
            <span className="text-[var(--color-ink-2)] font-mono tabular-nums">
              {' '}· {fmtCzk(d.price_czk)}
            </span>
          )}
        </p>
        <p className="mt-0.5 text-[0.78rem] text-[var(--color-ink-3)] truncate">
          {sourceLabel}
          {d.prev_price_czk != null && d.trigger_price_czk != null && (
            <span className="font-mono tabular-nums text-[var(--color-ink-4)]">
              {' '}· {fmtCzk(d.prev_price_czk)} → {fmtCzk(d.trigger_price_czk)}
            </span>
          )}
        </p>
      </div>

      <span
        className="shrink-0 text-[0.7rem] tracking-wide text-[var(--color-ink-4)] cursor-help mt-0.5"
        title={fmtAbsolute(d.dispatched_at)}
      >
        {fmtRelative(d.dispatched_at)}
      </span>
    </Link>
  );
}

function EmptyState({ seen }: { seen: WatchdogSeenFilter }) {
  return (
    <div className="px-6 py-12 text-center border border-dashed border-[var(--color-rule)] rounded-[var(--radius-md)]">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        {seen === 'unseen' ? 'Nothing unread' : 'No notifications yet'}
      </p>
      <p className="mt-2 text-sm text-[var(--color-ink-2)]">
        Turn on monitoring for a collection, or save a watchdog, and changes show
        up here.
      </p>
    </div>
  );
}
