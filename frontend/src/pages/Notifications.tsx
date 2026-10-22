/* The one Notifications feed: watchdog matches, collection-monitor change
 * events and things shared with me, from one endpoint. A read/triage surface —
 * filter by source / seen, mark one or all read, open what a row is about
 * (lib/dispatchTarget). The deal pipeline, the watchdog list and this feed stay
 * distinct; this is where "what changed about things I care about" lives, and
 * its unseen rows are the count the nav badge, the phone bell and the app icon
 * show (lib/unreadCount). Older rows page in on "Load more" (keyset, newest
 * first).
 *
 * ONE PAGE FOR BOTH FRAMES (W5a): below 768 px the same markup lays out to the
 * phone's 16 px gutters (`max-md:`). The frame's header already names the page
 * and its bell carries the count, so the eyebrow and the explanation step
 * aside (the empty state still explains); the rows' hairlines run to the screen
 * edge as the roll's do, and a row turns into two lines — kind and time, then
 * what it is about — whose prices never split.
 *
 * A coalesced push opens this page as `/notifications?d={delivery}`: the open is
 * reported once and the parameter dropped (lib/push). */

import { useId, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listNotifications,
  markAllNotificationsSeen,
  markWatchdogDispatchSeen,
} from '@/lib/api';
import { notificationKeys } from '@/lib/queries';
import { dispatchTarget } from '@/lib/dispatchTarget';
import { ROUTES } from '@/lib/routes';
import { usePageTitle } from '@/lib/pageTitle';
import { usePhoneShell } from '@/lib/phoneShell';
import { usePushOpenedParam } from '@/lib/push';
import { useUnreadCount } from '@/lib/unreadCount';
import { useInfiniteList } from '@/lib/useInfiniteList';
import { fmtAbsolute, fmtCount, fmtCzk, fmtRelative } from '@/lib/format';
import { listingKindLabel } from '@/lib/enums';
import LoadMore from '@/components/LoadMore';
import type {
  DispatchChangeKind,
  NotificationSourceKind,
  WatchdogDispatch,
  WatchdogSeenFilter,
} from '@/lib/types';

type SourceFilter = NotificationSourceKind | 'all';

const POLL_MS = 30_000;
const PAGE_SIZE = 50;

/* dispatchTarget's answer for a row with nothing to open: the feed itself. */
const FEED = ROUTES.notifications.build();

/* change_kind → label + the colour of its leading dot (only design tokens that
 * are known to exist; the chip itself stays neutral). */
const CHANGE_META: Record<DispatchChangeKind, { label: string; dot: string }> = {
  new:               { label: 'New match',         dot: 'var(--color-copper)' },
  price_drop:        { label: 'Price drop',        dot: 'var(--color-sage)' },
  price_rise:        { label: 'Price rise',        dot: 'var(--color-brick)' },
  inactive:          { label: 'Delisted',          dot: 'var(--color-ink-4)' },
  reactivated:       { label: 'Relisted',          dot: 'var(--color-copper)' },
  new_source:        { label: 'New source',        dot: 'var(--color-copper)' },
  shared_collection: { label: 'Shared collection', dot: 'var(--color-ink-3)' },
  shared_dashboard:  { label: 'Shared dashboard',  dot: 'var(--color-ink-3)' },
  shared_pipeline:   { label: 'Shared pipeline',   dot: 'var(--color-ink-3)' },
};

const SOURCE_TABS: ReadonlyArray<{ key: SourceFilter; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'collection_monitor', label: 'Monitoring' },
  { key: 'watchdog', label: 'Watchdogs' },
  { key: 'share', label: 'Shared' },
];

const SEEN_TABS: ReadonlyArray<{ key: WatchdogSeenFilter; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'unseen', label: 'Unread' },
];

export default function Notifications() {
  const qc = useQueryClient();
  const [source, setSource] = useState<SourceFilter>('all');
  const [seen, setSeen] = useState<WatchdogSeenFilter>('all');
  usePushOpenedParam();

  const feed = useInfiniteList<WatchdogDispatch>({
    queryKey: notificationKeys.feed({ source, seen }),
    queryFn: async (cursor) => {
      const resp = await listNotifications({
        source_kind: source === 'all' ? undefined : source,
        seen,
        limit: PAGE_SIZE,
        cursor: (cursor as string | null) ?? undefined,
      });
      return { rows: resp.data, nextCursor: resp.next_cursor ?? undefined };
    },
    pageSize: PAGE_SIZE,
    getRowId: (d) => d.id,
    refetchInterval: POLL_MS,
  });

  const markAll = useMutation({
    mutationFn: () => markAllNotificationsSeen(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: notificationKeys.all });
    },
  });

  const rows = feed.rows;
  /* Shell's one unread observer (lib/unreadCount), the number the bells show. */
  const { count: unread } = useUnreadCount();

  // Tab title carries the unread count, capped like the nav badge so the two
  // never disagree. Zero → the plain "Notifications" route handle. Not in the
  // phone frame: the title is its header's, beside the bell that has the count.
  const phone = usePhoneShell();
  usePageTitle(!phone && unread > 0 ? `Notifications (${unread > 99 ? '99+' : unread})` : null);

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto max-md:px-4 max-md:pt-5 max-md:pb-6">
      <Header
        unread={unread}
        onMarkAll={() => markAll.mutate()}
        markAllPending={markAll.isPending}
      />

      <div className="mt-6 flex flex-wrap items-center gap-x-6 gap-y-3 max-md:mt-4 max-md:gap-y-2">
        <Tabs<SourceFilter> tabs={SOURCE_TABS} value={source} onChange={setSource} />
        <Tabs<WatchdogSeenFilter> tabs={SEEN_TABS} value={seen} onChange={setSeen} />
      </div>

      <div className="mt-7 max-md:mt-4">
        {feed.isLoading ? (
          <div className="text-sm text-[var(--color-ink-3)]">Loading…</div>
        ) : feed.isError ? (
          <div className="text-sm text-[var(--color-brick)]">
            Failed to load: {feed.error?.message}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState seen={seen} />
        ) : (
          <>
            <ul className="divide-y divide-[var(--color-rule-soft)] border-t border-b border-[var(--color-rule-soft)] max-md:-mx-4">
              {rows.map((d) => (
                <li key={d.id}>
                  <NotificationRow dispatch={d} />
                </li>
              ))}
            </ul>
            <LoadMore list={feed} label="Load more" busyLabel="Loading…" />
          </>
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
    <header className="flex items-end justify-between gap-4 max-md:items-center">
      <div className="min-w-0">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] max-md:hidden">
          Notifications
        </p>
        <h1
          className="mt-1.5 text-[2.1rem] leading-tight max-md:mt-0 max-md:text-[1.5rem]"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          What changed
        </h1>
        <p className="mt-2 text-sm text-[var(--color-ink-2)] max-md:hidden">
          Watchdog matches and changes to properties in your monitored
          collections — price moves, delistings, relistings, new sources — and
          collections shared with you.{' '}
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
        className="shrink-0 px-3 py-1.5 text-[0.78rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:text-[var(--color-ink)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors max-md:h-9 max-md:py-0 max-md:text-[13px]"
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
    <div className="inline-flex items-center gap-1 max-md:flex-wrap">
      {tabs.map((t) => {
        const active = t.key === value;
        return (
          <button
            key={t.key}
            type="button"
            onClick={() => onChange(t.key)}
            className={[
              'px-2.5 py-1 text-[0.78rem] rounded-[var(--radius-sm)] transition-colors max-md:h-9 max-md:px-3 max-md:py-0 max-md:text-[13px]',
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

/* One feed row. A share has no listing subject, so it renders as a text row;
 * watchdog + collection_monitor rows render the listing card. One component per
 * shape so each calls its hooks unconditionally (rules-of-hooks) — the
 * dispatcher itself uses none. */
function NotificationRow({ dispatch: d }: { dispatch: WatchdogDispatch }) {
  if (d.source_kind === 'share') return <ShareNotificationRow dispatch={d} />;
  return <StandardNotificationRow dispatch={d} />;
}

function useMarkSeen(d: WatchdogDispatch): () => void {
  const qc = useQueryClient();
  const markSeen = useMutation({
    mutationFn: () => markWatchdogDispatchSeen(d.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: notificationKeys.all });
    },
  });
  return () => {
    if (d.seen_at == null) markSeen.mutate();
  };
}

/* The unread dot is decorative; the state rides on the control's description
 * (`id`), so marking a row seen never renames it. */
function UnreadDot({ unread, id, color }: { unread: boolean; id: string; color: string }) {
  return (
    <span className="mt-1.5 w-1.5 shrink-0 max-md:mt-2.5">
      {unread && (
        <>
          <span className="block w-1.5 h-1.5 rounded-full" style={{ background: color }} aria-hidden />
          <span id={id} hidden>
            unread
          </span>
        </>
      )}
    </span>
  );
}

/* A wrapped line ends on " ·", never starts on one. */
const NBSP = '\u00a0';
/* A row line on the phone: text and figures as items that wrap whole. */
const PHONE_FIGURES = 'max-md:flex max-md:flex-wrap max-md:items-baseline max-md:gap-x-2 max-md:whitespace-normal';

function StandardNotificationRow({ dispatch: d }: { dispatch: WatchdogDispatch }) {
  const markSeen = useMarkSeen(d);
  const unread = d.seen_at == null;
  const unreadId = useId();
  const meta = CHANGE_META[d.change_kind] ?? {
    label: d.change_kind,
    dot: 'var(--color-ink-3)',
  };
  const place = d.display_label ?? '—';
  const sourceLabel =
    d.source_kind === 'collection_monitor'
      ? (d.collection_name ?? 'Monitoring')
      : (d.subscription_name ?? 'Watchdog');
  const to = dispatchTarget(d);
  const control = {
    onClick: markSeen,
    'aria-describedby': unread ? unreadId : undefined,
    className:
      'flex items-start gap-3 py-3.5 group w-full text-left max-md:flex-wrap max-md:gap-x-2.5 max-md:gap-y-1.5 max-md:px-4',
  };

  const body = (
    <>
      <UnreadDot unread={unread} id={unreadId} color="var(--color-copper)" />

      {/* change-kind chip */}
      <span className="mt-0.5 inline-flex items-center gap-1.5 shrink-0 px-2 py-0.5 text-[0.7rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-2)]">
        <span
          aria-hidden
          className="w-1.5 h-1.5 rounded-full"
          style={{ background: meta.dot }}
        />
        {meta.label}
      </span>

      {/* On the phone this is the row's second line, under the chip. Its
        * figures are flex items there: one that does not fit starts the next
        * line whole, without the " · " (a dot opening a line reads as noise). */}
      <div className="min-w-0 flex-1 max-md:order-last max-md:basis-full max-md:pl-4">
        <p className={`text-sm text-[var(--color-ink)] group-hover:text-[var(--color-copper)] transition-colors truncate ${PHONE_FIGURES}`}>
          <span>
            <span className="font-medium">{place}</span>
            {listingKindLabel(d) && (
              <span className="text-[var(--color-ink-3)]">{NBSP}· {listingKindLabel(d)}</span>
            )}
          </span>
          {d.price_czk != null && (
            <span className="text-[var(--color-ink-2)] font-mono tabular-nums whitespace-nowrap">
              <span className="max-md:hidden">{' '}· </span>
              {fmtCzk(d.price_czk)}
            </span>
          )}
        </p>
        <p className={`mt-0.5 text-[0.78rem] text-[var(--color-ink-3)] truncate ${PHONE_FIGURES}`}>
          {sourceLabel}
          {d.prev_price_czk != null && d.trigger_price_czk != null && (
            <span className="font-mono tabular-nums text-[var(--color-ink-4)] whitespace-nowrap">
              <span className="max-md:hidden">{' '}· </span>
              {fmtCzk(d.prev_price_czk)} → {fmtCzk(d.trigger_price_czk)}
            </span>
          )}
        </p>
      </div>

      <RowTime iso={d.dispatched_at} />
    </>
  );
  return to === FEED ? (
    <button type="button" {...control}>{body}</button>
  ) : (
    <Link to={to} {...control}>{body}</Link>
  );
}

/* When the row was dispatched; the exact time on hover. On the phone it closes
 * the first line, in the roll's mono. */
function RowTime({ iso }: { iso: string }) {
  return (
    <span
      className="shrink-0 text-[0.7rem] tracking-wide text-[var(--color-ink-4)] cursor-help mt-0.5 max-md:ml-auto max-md:mt-[5px] max-md:font-mono max-md:text-[11px] max-md:tracking-normal max-md:tabular-nums"
      title={fmtAbsolute(iso)}
    >
      {fmtRelative(iso)}
    </span>
  );
}

/* What each share kind's link says. */
const SHARE_META = {
  collection: { open: 'Open collection' },
  pipeline: { open: 'Open pipeline' },
  dashboard: { open: 'Open dashboard' },
} as const;

/* A share's sentence, from the sharer and the live name (migration 656: no text
 * is stored). Each part falls back on its own: the email once no share joins the
 * two accounts, the name once the thing is no longer readable. */
function shareSentence(d: WatchdogDispatch): string {
  const who = d.actor_email ?? 'Someone';
  switch (d.share_kind) {
    case 'collection':
      return d.share_name
        ? `${who} shared the collection "${d.share_name}" with you`
        : `${who} shared a collection with you`;
    case 'dashboard':
      return d.share_name
        ? `${who} shared the dashboard "${d.share_name}" with you`
        : `${who} shared a dashboard with you`;
    case 'pipeline':
      return `${who} shared their pipeline with you${
        d.share_mode ? ` (${d.share_mode === 'edit' ? 'edit' : 'view only'})` : ''
      }`;
    default:
      return 'Something was shared with you.';
  }
}

/* share: someone shared a collection, their pipeline or a Stats dashboard with
 * me (migrations 610, 641, 647: one kind, one renderer). A text row (shareSentence
 * names the owner and what was shared); pressing the text marks it read, the link
 * marks it read and opens what dispatchTarget names. Two sibling controls, never a
 * link inside a button; no link when there is nothing to open. */
function ShareNotificationRow({ dispatch: d }: { dispatch: WatchdogDispatch }) {
  const markSeen = useMarkSeen(d);
  const unread = d.seen_at == null;
  const unreadId = useId();
  const meta = CHANGE_META[d.change_kind];
  const kind = d.share_kind ? SHARE_META[d.share_kind] : null;
  const to = dispatchTarget(d);

  return (
    <div className="flex items-start gap-3 py-3.5 max-md:flex-wrap max-md:gap-x-2.5 max-md:gap-y-1 max-md:px-4">
      <UnreadDot unread={unread} id={unreadId} color="var(--color-copper)" />
      <button
        type="button"
        onClick={markSeen}
        aria-describedby={unread ? unreadId : undefined}
        className="min-w-0 flex-1 text-left flex items-start gap-3 max-md:flex-col max-md:gap-1.5"
      >
        <span className="mt-0.5 inline-flex items-center gap-1.5 shrink-0 px-2 py-0.5 text-[0.7rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-2)]">
          <span aria-hidden className="w-1.5 h-1.5 rounded-full" style={{ background: meta.dot }} />
          {meta.label}
        </span>
        <span className="min-w-0 flex-1 text-sm text-[var(--color-ink)] break-words max-md:flex-none">
          {shareSentence(d)}
        </span>
      </button>
      {kind && to !== FEED && (
        <Link
          to={to}
          onClick={markSeen}
          className="shrink-0 mt-0.5 text-[0.78rem] text-[var(--color-ink-2)] underline decoration-[var(--color-rule-strong)] underline-offset-2 hover:text-[var(--color-ink)] hover:decoration-[var(--color-ink-3)] transition-colors max-md:order-last max-md:-my-1.5 max-md:inline-flex max-md:min-h-11 max-md:basis-full max-md:items-center max-md:pl-4 max-md:text-[13px]"
        >
          {kind.open}
        </Link>
      )}
      <RowTime iso={d.dispatched_at} />
    </div>
  );
}

function EmptyState({ seen }: { seen: WatchdogSeenFilter }) {
  return (
    <div className="px-6 py-12 text-center border border-dashed border-[var(--color-rule)] rounded-[var(--radius-md)] max-md:px-4 max-md:py-8">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        {seen === 'unseen' ? 'Nothing unread' : 'No notifications yet'}
      </p>
      <p className="mt-2 text-sm text-[var(--color-ink-2)]">
        Turn on monitoring for a collection, or save a watchdog, and changes show
        up here. A collection, a pipeline or a dashboard someone shares with you
        is announced here too.
      </p>
    </div>
  );
}
